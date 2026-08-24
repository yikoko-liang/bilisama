"""The preview UI server: one FastAPI app riding inside the dev-talk process.

Three design points, each guarding a promise made elsewhere:

- The random token is a path prefix and the only auth (qwen-audio-agent's
  renderer-server pattern). Everything binds 127.0.0.1, the token rotates per
  run, and the WebSocket upgrade checks Origin — enough fence for a preview;
  the real two-door scheme stays in stage 7 (plan section 6.5).
- obs/health.py's create_app docstring says "mounted by the UI server later";
  later is now. It mounts last so it cannot swallow /ws or /config.
- uvicorn's serve() installs its own SIGINT/SIGTERM handlers, which would
  silently replace dev-talk's graceful shutdown (distill, store close). The
  _QuietServer override keeps uvicorn's hands off the controls; a test pins
  the bypass so a uvicorn upgrade that moves the hook fails loudly.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import socket
from collections.abc import Awaitable, Callable, Generator, Mapping
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from bilisama.config._ui import Reload
from bilisama.config.schema import Settings
from bilisama.config.ui_meta import UI_META
from bilisama.obs.health import HealthRegistry
from bilisama.obs.health import create_app as create_health_app
from bilisama.obs.logging import get_logger
from bilisama.ui.audio import AudioBroker, AudioOwner
from bilisama.ui.config_edit import field_control
from bilisama.ui.events import ClientEvent, ServerEvent, frame
from bilisama.ui.hub import UiHub

__all__ = [
    "Handler",
    "UiServer",
    "bind_ui_socket",
    "config_snapshot",
    "create_ui_app",
    "default_endpoint_path",
    "write_endpoint_file",
]

log = get_logger("bilisama.ui.server")

Handler = Callable[[dict[str, Any]], Awaitable[None]]

_WEB_ROOT = Path(__file__).parent / "web"

# Downlink chunks the socket may hold. Roughly two seconds of 24 kHz mono at
# the sizes providers actually send — enough to ride out a scheduling hiccup,
# far short of the minutes of drift this project measured when nothing bounded
# the queue at all.
_AUDIO_QUEUE = 64

# What a barge-in sends down the audio socket, behind everything already
# queued. Same word the control socket uses, so there is one name for one idea.
_CLEAR_MARKER = json.dumps({"event": "playback.clear", "data": {}})

# Blocks between two log lines about an uplink that keeps failing. They arrive
# every 20 ms, so this is one line per outage plus one every five seconds while
# it lasts — visible in the log without drowning it.
_UPLINK_LOG_EVERY = 250


# ------------------------------------------------------------ socket & files


def bind_ui_socket(port: int) -> socket.socket:
    """Bind the UI port on loopback, resolving port 0 to a real one right here.

    Binding before the app exists means the origin string is known up front —
    no chicken-and-egg with uvicorn's startup.

    Args:
        port: Desired port; 0 lets the OS pick.

    Returns:
        A listening socket for uvicorn's `serve(sockets=...)`.

    Raises:
        OSError: when the port is taken. The caller decides whether that kills
            the process (it should not — the UI is a passenger).
    """
    return socket.create_server(("127.0.0.1", port))


def default_endpoint_path() -> Path:
    """`<data home>/bilisama/ui/endpoint.json` — the same roof as personas and
    the s2s engine install, so the desktop shell attaches with zero arguments."""
    base = os.environ.get("XDG_DATA_HOME", "")
    root = Path(base).expanduser() if base else Path.home() / ".local" / "share"
    return root / "bilisama" / "ui" / "endpoint.json"


def write_endpoint_file(path: Path, *, url: str, pid: int) -> None:
    """Publish where the UI lives, atomically and owner-only.

    The URL embeds the auth token, hence 0600 and the tmp+rename: a reader
    never sees a half-written file, and other local users never read it at
    all. The file is BORN 0600 — a write-then-chmod would leave a umask-wide
    window with the token readable.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps({"url": url, "pid": pid}, ensure_ascii=False))
    os.replace(tmp, path)


# ------------------------------------------------------------ config snapshot


def config_snapshot(settings: Settings) -> list[dict[str, Any]]:
    """Everything the panel's config tab renders — values plus editor facts.

    ui_meta's first consumer. Secret-marked fields collapse to a presence
    label before the value ever leaves the process — the panel has no business
    holding even a credential *reference* string. `editable` marks the rows
    whose reload class is LIVE (the honest set: consumers read them at call
    time); kind/choices/min/max come from the pydantic field so the page can
    render the right control without guessing.
    """
    rows: list[dict[str, Any]] = []
    for path, meta in UI_META.items():
        parent: Any = None
        value: Any = settings
        for part in path.split("."):
            parent = value
            value = getattr(value, part)
        control: dict[str, Any] = {"kind": None, "choices": None, "min": None, "max": None}
        editable = False
        if isinstance(value, BaseModel):
            # Section-header entries ("speech.dashscope" etc) resolve to a whole
            # sub-model. Stringifying it would inline every child value —
            # including the secret refs the branch below exists to mask.
            value = None
        else:
            if isinstance(parent, BaseModel):
                info = type(parent).model_fields.get(path.rsplit(".", 1)[-1])
                if info is not None:
                    control = field_control(info)
            editable = meta.reload is Reload.LIVE and not meta.secret
            if meta.secret:
                value = "已配置" if value else "未配置"
            elif isinstance(value, float) and not math.isfinite(value):
                # max_speech_ms defaults to inf; JSONResponse runs allow_nan=False.
                value = str(value)
            elif not isinstance(value, str | int | float | bool | None):
                value = str(value)
        rows.append(
            {
                "path": path,
                "label": meta.label,
                "hint": meta.hint,
                "group": meta.group,
                "order": meta.order,
                "unit": meta.unit,
                "audience": str(meta.audience),
                "reload": str(meta.reload),
                "value": value,
                "editable": editable,
                **control,
            }
        )
    rows.sort(key=lambda row: (str(row["group"]), int(row["order"]), str(row["path"])))
    return rows


# ------------------------------------------------------------ app


def create_ui_app(
    *,
    hub: UiHub,
    registry: HealthRegistry,
    settings: Settings,
    token: str,
    origin: str,
    handlers: Mapping[ClientEvent, Handler],
    hello: Callable[[], Mapping[str, Any]],
    web_root: Path | None = None,
    user_skins_root: Path | None = None,
    broker: AudioBroker | None = None,
    on_audio: Callable[[bytes], Awaitable[None]] | None = None,
) -> FastAPI:
    """Assemble the app: page, WebSocket, config, static assets, health.

    Args:
        hub: Broadcast hub; each WebSocket client becomes one subscriber.
        registry: Health probes, served under /{token}/health.
        settings: For the read-only config snapshot.
        token: Random per-run path prefix; anything outside it is 404.
        origin: The exact Origin a browser page of ours presents. A mismatch
            is refused before the upgrade; a missing Origin is allowed — that
            is a non-browser client on loopback, not a web page.
        handlers: One coroutine per ClientEvent; unknown events only log.
        hello: Builds the hello payload at connect time (fresh panel state).
        web_root: Override for tests; defaults to the packaged ui/web tree.
        user_skins_root: `<data home>/bilisama/skins`, mounted only when it
            exists. User-imported packs there shadow the packaged ones because
            the page tries this mount first.
        broker: Decides which client holds the devices. None leaves the audio
            socket unmounted, which is the shape every run had before it
            existed — audio stays in sounddevice.
        on_audio: Where uplink microphone frames go, normally the link's
            push_audio.
    """
    root = web_root or _WEB_ROOT
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    prefix = f"/{token}"
    # The same loopback page arrives as 127.0.0.1 or localhost depending on
    # what the streamer typed; both spellings are our own origin.
    allowed_origins = {origin, origin.replace("://127.0.0.1:", "://localhost:")}

    @app.middleware("http")
    async def _security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' data:"
        # The token lives in the path; never leak it through a Referer header.
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get(prefix + "/")
    async def index() -> FileResponse:
        return FileResponse(root / "index.html")

    @app.get(prefix + "/config")
    async def config() -> JSONResponse:
        return JSONResponse(config_snapshot(settings))

    @app.websocket(prefix + "/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        client_origin = ws.headers.get("origin")
        if client_origin is not None and client_origin not in allowed_origins:
            # A browser page from somewhere else. Refuse before accepting.
            log.warning("ui.ws_origin_refused", origin=client_origin)
            await ws.close(code=4403)
            return
        await ws.accept()
        replay, queue = hub.attach()
        sender: asyncio.Task[None] | None = None
        try:
            await ws.send_text(frame(ServerEvent.HELLO, dict(hello())))
            for line in replay:
                await ws.send_text(line)
            sender = asyncio.create_task(_pump(ws, queue), name="ui:ws-pump")
            while True:
                raw = await ws.receive_text()
                await _dispatch(raw, handlers)
        except WebSocketDisconnect:
            pass
        finally:
            # Detach FIRST: the sender dies with WebSocketDisconnect when the
            # client vanished mid-send, and awaiting it re-raises that inside
            # this finally — a detach placed after would be skipped, leaking
            # one dead 256-frame queue per hard-closed tab.
            hub.detach(queue)
            if sender is not None:
                sender.cancel()
                # CancelledError is a BaseException and needs naming; the
                # Exception arm swallows the send-failure the pump died of.
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await sender

    # Static mounts come after the routes so /, /ws and /config win; the
    if broker is not None:
        # Wired here rather than by each caller, because both halves are pure
        # plumbing with no product decision in them — and a caller that forgets
        # gets a panel stuck on 「读取中…」 with nothing to explain it.
        broker.announce_through(
            lambda owner: hub.broadcast(ServerEvent.AUDIO_OWNER, {"owner": owner})
        )
        relay: dict[ClientEvent, Handler] = {}

        async def _ask(data: dict[str, Any]) -> None:
            # The panel and the devices are two different shell windows, so a
            # request crosses the server to reach the holder. The server has no
            # opinion about either; it repeats what it heard.
            hub.broadcast(ServerEvent.AUDIO_COMMAND, data)

        async def _report(data: dict[str, Any]) -> None:
            kind = str(data.get("kind") or "")
            if kind == "devices":
                hub.broadcast(ServerEvent.AUDIO_DEVICES, data)
            elif kind == "level":
                hub.broadcast(ServerEvent.AUDIO_LEVEL, data)

        relay[ClientEvent.AUDIO_ASK] = _ask
        relay[ClientEvent.AUDIO_REPORT] = _report

        def _chain(first: Handler, second: Handler) -> Handler:
            async def both(data: dict[str, Any]) -> None:
                await first(data)
                await second(data)

            return both

        # Chained, not overridden either way. The relay is plumbing the caller
        # should not have to know about, and a caller that does register these
        # — the browser tests record every client event — still deserves to see
        # them. Whichever one wins outright, something quietly stops working.
        handlers = {
            **handlers,
            **{
                event: (_chain(action, handlers[event]) if event in handlers else action)
                for event, action in relay.items()
            },
        }

        @app.websocket(prefix + "/audio")
        async def audio_endpoint(ws: WebSocket) -> None:
            """The microphone and speaker, for whichever client wins them.

            Separate from /ws on purpose: that one broadcasts and drops the
            oldest frame under pressure, which would starve the panel behind
            48 KB/s of PCM and reorder samples besides. This one is point to
            point and binary.
            """
            client_origin = ws.headers.get("origin")
            if client_origin is not None and client_origin not in allowed_origins:
                log.warning("ui.audio_origin_refused", origin=client_origin)
                await ws.close(code=4403)
                return
            await ws.accept()
            # The shell's preload declares itself; a plain tab cannot, which is
            # exactly the distinction we want to rank on.
            who: AudioOwner = "shell" if ws.query_params.get("role") == "shell" else "browser"
            outbound: asyncio.Queue[bytes | str | None] = asyncio.Queue(maxsize=_AUDIO_QUEUE)

            def send(pcm: bytes) -> None:
                # Drop the NEWEST under pressure, the opposite of the control
                # hub. A late sample is worse than a missing one: keeping it
                # would push everything behind it further out of time, which
                # is the backlog this project already measured once.
                if not outbound.full():
                    outbound.put_nowait(pcm)

            # None through the queue is the pump's hang-up sentinel; reused
            # here so a displaced client is told to let go of its microphone
            # rather than left capturing into a socket nobody reads.
            def close() -> None:
                # The queue is bounded, and a page that stopped reading fills
                # it — audio arrives faster than it plays, which is how this
                # project once measured 106 seconds of unplayed sound inside
                # 48 seconds of wall clock. Putting the sentinel without
                # looking would raise QueueFull out of claim() at exactly the
                # moment the devices change hands, leaving the broker pointing
                # at a socket that is about to be closed and the local pair
                # parked forever. Everything queued goes first: it belongs to a
                # client that just lost the devices, and the new owner is
                # already being fed the same reply.
                while not outbound.empty():
                    outbound.get_nowait()
                outbound.put_nowait(None)

            def flush() -> None:
                # Everything still queued belongs to the utterance that was
                # just interrupted. The marker goes on afterwards, so by the
                # time the page reads it, it has seen every stale sample and
                # can drop the lot in one go.
                while not outbound.empty():
                    outbound.get_nowait()
                outbound.put_nowait(_CLEAR_MARKER)

            # None until claim() answers. A claim that fails partway has
            # already pointed the broker at this socket (AudioBroker.claim
            # swaps the owner over before anything that can raise), so "no
            # answer" has to be treated like "granted" and the devices handed
            # back. A plain False is the opposite case: someone else holds
            # them, and releasing would take them out from under that client.
            granted: bool | None = None
            pump: asyncio.Task[None] | None = None
            dropped = 0
            try:
                granted = await broker.claim(who, send=send, close=close, flush=flush)
                if not granted:
                    # Someone stronger has the devices. Say so and hang up
                    # rather than sit on a socket nobody feeds.
                    await ws.close(code=4409)
                    return
                pump = asyncio.create_task(_pump_audio(ws, outbound), name="ui:audio-out")
                while True:
                    # Both kinds on one socket: PCM up, and the playback
                    # receipts that describe the PCM coming down. They used to
                    # go out on the control socket, which is a different
                    # connection opening on its own schedule — when the audio
                    # arrived first, every playback.started was dropped and the
                    # floor gate never learned she was speaking. Receipts ride
                    # with the audio they are about.
                    message = await ws.receive()
                    if message.get("type") == "websocket.disconnect":
                        break
                    payload = message.get("bytes")
                    if payload is not None:
                        if on_audio is not None:
                            dropped = await _forward_uplink(on_audio, payload, dropped)
                        continue
                    text = message.get("text")
                    if text is not None:
                        await _dispatch(text, handlers)
            except WebSocketDisconnect:
                pass
            finally:
                if granted is not False:
                    await broker.release(who)
                if pump is not None:
                    pump.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await pump

    # health app mounts last because its prefix is the bare token and a mount
    # swallows everything under itself.
    app.mount(prefix + "/assets", StaticFiles(directory=root), name="assets")
    if user_skins_root is not None and user_skins_root.is_dir():
        app.mount(prefix + "/skins", StaticFiles(directory=user_skins_root), name="skins")
    app.mount(prefix, create_health_app(registry), name="health")
    return app


async def _forward_uplink(
    on_audio: Callable[[bytes], Awaitable[None]], pcm: bytes, dropped: int
) -> int:
    """Hand one microphone block upstream. Never raises.

    An exception escaping this call would reach the endpoint's finally, which
    hands the devices back to sounddevice — PortAudio streams reopen with no
    echo cancellation behind them, the page reconnects half a second later and
    parks them again, and since the link's backoff is the slower of the two,
    the next block hits the same failure. One link hiccup becomes a run of real
    device churn. push_audio raises for as long as the link is between sockets
    (RealtimeClient._send_raw), so this is ordinary weather, and dev-talk's
    own microphone path already answered it the same way: a send that fails
    while the link is down costs one block, not the session (dev-talk's
    _uplink).

    The net is deliberately wide. What push_audio raises belongs to the
    transport — ConnectionError, a websockets error — and naming those here
    would reach across a layer boundary to buy nothing: the rule at this end is
    that NO failure of a caller-supplied handler may cost the devices. It is
    not silent, and it does not go quiet on a fault that never clears: the
    first failure of a run logs, so does every _UPLINK_LOG_EVERY after it, and
    so does the recovery.

    Args:
        on_audio: Normally the link's push_audio.
        pcm: One uplink block from the page.
        dropped: How many blocks in a row have failed so far.

    Returns:
        The new run length: 0 once a block gets through.
    """
    try:
        await on_audio(pcm)
    except Exception as exc:
        if dropped % _UPLINK_LOG_EVERY == 0:
            log.warning(
                "ui.uplink_dropped",
                error=type(exc).__name__,
                error_text=str(exc),
                dropped=dropped + 1,
            )
        return dropped + 1
    if dropped:
        log.info("ui.uplink_resumed", dropped=dropped)
    return 0


async def _pump_audio(ws: WebSocket, queue: asyncio.Queue[bytes | str | None]) -> None:
    """Move downlink PCM onto the wire until the socket goes away.

    Text rides the same queue as the samples so that a stop is ordered behind
    every sample it is meant to stop — the whole reason barge-in cannot be left
    to the control socket.
    """
    while True:
        chunk = await queue.get()
        if chunk is None:
            await ws.close(code=1001)
            return
        if isinstance(chunk, str):
            await ws.send_text(chunk)
            continue
        await ws.send_bytes(chunk)


async def _pump(ws: WebSocket, queue: asyncio.Queue[str | None]) -> None:
    """Move hub frames onto the wire until the hub says hang up."""
    while True:
        line = await queue.get()
        if line is None:
            # 1001 going away: the process is shutting down, not an error.
            await ws.close(code=1001)
            return
        await ws.send_text(line)


async def _dispatch(raw: str, handlers: Mapping[ClientEvent, Handler]) -> None:
    """Route one client frame; bad input logs and is dropped, never raises."""
    try:
        message = json.loads(raw)
        # Legal JSON is not necessarily an object: [1,2], null and "str" all
        # parse fine and then have no .get — treat them as invalid frames, not
        # as a reason to kill the connection.
        if not isinstance(message, dict):
            raise TypeError("frame is not an object")
        event = ClientEvent(str(message.get("event")))
    except (ValueError, TypeError):
        log.warning("ui.client_frame_invalid", size=len(raw))
        return
    handler = handlers.get(event)
    if handler is None:
        # Vocabulary is append-only: a newer page against an older process
        # sends events we know the shape of but do not serve yet.
        log.warning("ui.client_event_unhandled", client_event=str(event))
        return
    data = message.get("data")
    await handler(data if isinstance(data, dict) else {})


# ------------------------------------------------------------ server


def _report_server_death(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error("ui.server_died", error_text=str(exc))


class _QuietServer(uvicorn.Server):
    """uvicorn that keeps its hands off the process signal handlers.

    serve() wraps itself in capture_signals(), which calls signal.signal for
    SIGINT/SIGTERM (uvicorn 0.52 server.py:322-347) and would silently replace
    dev-talk's graceful-exit handler. The UI is a passenger; shutdown authority
    stays with the host process.
    """

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None, None, None]:
        yield


class UiServer:
    """Runs uvicorn on an already-bound socket as one task among dev-talk's.

    Call `hub.aclose()` before `stop()`: graceful shutdown waits for open
    connections, and a browser holds its WebSocket forever unless our side
    hangs up first.
    """

    def __init__(self, app: FastAPI, sock: socket.socket) -> None:
        config = uvicorn.Config(app, log_config=None, access_log=False, lifespan="off")
        self._server = _QuietServer(config)
        self._sock = sock
        self._task: asyncio.Task[None] | None = None

    @property
    def port(self) -> int:
        return int(self._sock.getsockname()[1])

    @property
    def started(self) -> bool:
        return self._server.started

    def start(self) -> None:
        self._task = asyncio.create_task(self._server.serve(sockets=[self._sock]), name="ui:server")
        # A dead server otherwise fails silently: browsers just retry forever
        # and the voice loop never notices. One warning names the corpse.
        self._task.add_done_callback(_report_server_death)

    async def stop(self) -> None:
        """Graceful first, three seconds of patience, then the axe."""
        if self._task is None:
            self._sock.close()
            return
        self._server.should_exit = True
        try:
            await asyncio.wait_for(self._task, timeout=3.0)
        except TimeoutError:
            # wait_for already cancelled the task; nothing further to await.
            log.warning("ui.server_stop_timeout")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("ui.server_stop_failed")
        finally:
            self._task = None
            with contextlib.suppress(OSError):
                self._sock.close()
