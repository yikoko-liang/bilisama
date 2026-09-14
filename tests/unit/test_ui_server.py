"""UI server: token fence, Origin check, config scrubbing, health mount,
endpoint file, and the uvicorn signal bypass.

The signal test is the load-bearing one: uvicorn 0.52 installs its own
SIGINT/SIGTERM handlers inside serve() (server.py:322-347), and dev-talk's
graceful shutdown (distill, store close) dies silently if that ever leaks
through. If a uvicorn upgrade moves the hook, that test goes red first.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import signal
import socket
import stat
import time
from collections.abc import Awaitable, Callable, Iterator, MutableMapping
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from bilisama.clock import FakeClock
from bilisama.config.schema import Settings
from bilisama.obs.health import HealthRegistry
from bilisama.ui.audio import AudioBroker, AudioOwner
from bilisama.ui.events import ClientEvent, ServerEvent
from bilisama.ui.hub import UiHub
from bilisama.ui.server import (
    _AUDIO_QUEUE,
    _UPLINK_SILENCE,
    UiServer,
    _fill_uplink_silence,
    bind_ui_socket,
    config_snapshot,
    create_ui_app,
    write_endpoint_file,
)

_TOKEN = "cafe0123deadbeef"
_ORIGIN = "http://127.0.0.1:7777"


def _settings(**overrides: Any) -> Settings:
    return Settings.model_validate(overrides)


def _build(
    settings: Settings | None = None,
    *,
    broker: AudioBroker | None = None,
) -> tuple[TestClient, UiHub, list[tuple[ClientEvent, dict[str, Any]]]]:
    hub = UiHub(FakeClock())
    registry = HealthRegistry()
    registry.register("assembly", lambda: {"events_seen": 3})
    calls: list[tuple[ClientEvent, dict[str, Any]]] = []

    def _recorder(event: ClientEvent) -> Any:
        async def handle(data: dict[str, Any]) -> None:
            calls.append((event, data))

        return handle

    app = create_ui_app(
        hub=hub,
        registry=registry,
        settings=settings or _settings(),
        token=_TOKEN,
        origin=_ORIGIN,
        handlers={event: _recorder(event) for event in ClientEvent},
        hello=lambda: {"persona": {"id": "tofu", "name": "豆腐"}},
        broker=broker,
    )
    return TestClient(app), hub, calls


# ------------------------------------------------------------ token fence


@pytest.mark.parametrize("path", ["/", "/config", "/health", "/wrongtoken/", "/wrongtoken/config"])
def test_everything_outside_the_token_prefix_is_404(path: str) -> None:
    client, _, _ = _build()
    assert client.get(path).status_code == 404


def test_index_and_assets_are_served_under_the_token() -> None:
    client, _, _ = _build()
    page = client.get(f"/{_TOKEN}/")
    assert page.status_code == 200
    assert "<html" in page.text
    asset = client.get(f"/{_TOKEN}/assets/js/main.js")
    assert asset.status_code == 200


def test_security_headers_ride_on_every_response() -> None:
    client, _, _ = _build()
    headers = client.get(f"/{_TOKEN}/").headers
    assert headers["content-security-policy"].startswith("default-src 'self'")
    assert headers["referrer-policy"] == "no-referrer"
    assert headers["x-content-type-options"] == "nosniff"


# ------------------------------------------------------------ health mount


def test_health_is_mounted_without_swallowing_the_routes() -> None:
    client, _, _ = _build()
    health = client.get(f"/{_TOKEN}/health")
    assert health.status_code == 200
    payload = health.json()
    assert payload["status"] == "ok"
    assert payload["components"]["assembly"] == {"events_seen": 3}
    # The mount sits at the bare token prefix; the explicit routes must win.
    assert client.get(f"/{_TOKEN}/config").status_code == 200
    assert client.get(f"/{_TOKEN}/").status_code == 200


# ------------------------------------------------------------ config snapshot


def test_config_snapshot_masks_every_secret_reference() -> None:
    settings = _settings(
        room={"credential_ref": "env:BILI_SESSDATA"},
        speech={
            "provider": "dashscope",
            "dashscope": {"api_key_ref": "env:DASHSCOPE_API_KEY"},
        },
    )
    client, _, _ = _build(settings)
    response = client.get(f"/{_TOKEN}/config")
    assert response.status_code == 200
    body = response.text
    assert "BILI_SESSDATA" not in body
    assert "DASHSCOPE_API_KEY" not in body
    rows = {row["path"]: row for row in response.json()}
    assert rows["room.credential_ref"]["value"] == "已配置"
    # The key belongs to the provider this session actually dials — with any
    # other one selected the row is not on the page at all, and asserting it
    # was masked would pass for the wrong reason.
    assert rows["speech.dashscope.api_key_ref"]["value"] == "已配置"


def test_config_snapshot_reports_unset_secrets_as_missing() -> None:
    rows = {row["path"]: row for row in config_snapshot(_settings())}
    assert rows["room.credential_ref"]["value"] == "未配置"


def test_config_snapshot_covers_all_meta_and_serializes() -> None:
    rows = config_snapshot(_settings())
    from bilisama.config.ui_meta import UI_META

    # Everything except the fields belonging to a provider this session is not
    # using — showing both sets is how the panel came to list two endpoints and
    # two voices, only one of each doing anything.
    active = _settings().speech.provider.value
    expected = {
        path
        for path, meta in UI_META.items()
        if not meta.provider_scoped or meta.provider_scoped == active
    }
    assert {row["path"] for row in rows} == expected
    assert expected != set(UI_META), "没有任何字段被 provider 过滤掉，这条断言就没在测东西"
    # JSONResponse runs allow_nan=False; the inf default in the s2s turn
    # section must have been stringified by the snapshot.
    json.dumps(rows, allow_nan=False)
    paths = {row["path"] for row in rows}
    assert "speech.s2s.endpoint" in paths, "当前 provider 的字段被过滤掉了"
    assert "speech.dashscope.voice" not in paths, "别的 provider 的字段还在铺"
    sample = {row["path"]: row for row in rows}["avatar.renderer"]
    assert sample["label"] == "形象类型"
    assert sample["value"] == "sprite"
    assert sample["audience"] == "streamer"


def test_config_snapshot_editable_set_is_the_honest_live_set() -> None:
    rows = {row["path"]: row for row in config_snapshot(_settings())}
    editable = {path for path, row in rows.items() if row["editable"]}
    # Grown with the control-centre rework, each entry the same way the
    # 2026-08-14 audit demanded: a consumer wired first (dev-talk's
    # run_reload_hook / refresh_* chain), then the ui_meta flip to LIVE.
    speak = {
        f"interaction.speak.{name}" for name in _settings().interaction.speak.__class__.model_fields
    }
    hooked = {
        "interaction.chattiness",
        "interaction.reply_length",
        "interaction.gift_battery_high",
        "interaction.gift_battery_medium",
        "interaction.entry_welcome.ordinary",
        "interaction.entry_welcome.naval",
        "interaction.entry_welcome.ranking",
        "interaction.sc_protect_ms",
        # LIVE: run_reload_hook re-renders the voice rules (the marker
        # contract rides this switch) and, once wired, flips the voice gate.
        "interaction.voice_reply",
        # LIVE with the switch it governs (ledger #91): both reach the Assembly
        # through refresh_interaction_settings and are read per intent.
        "interaction.protect_paid_replies",
        "interaction.proactive.max_per_hour",
        "interaction.proactive.wake_interval_s",
        "interaction.proactive.collection_window_s",
        "room.stream_intro",
        # LIVE since the yiko-merge audit: run_reload_hook connects/disconnects
        # in process, and the panel edit is session-only (never persisted).
        "room.room_id",
        # LIVE since the skin picker: the consumer is the pet page, re-poked
        # by the PANEL_STATE broadcast after every panel edit.
        "avatar.model_id",
        "audio.input_enabled",
        "audio.output_enabled",
        "audio.noise_sensitivity",
        "persona.id",
        "persona.streamer_name",
        "persona.display_name",
        "persona.growth.relationship",
        "persona.growth.voice",
    }
    assert editable == speak | hooked | {"runtime.log_level", "runtime.log_viewer_content"}
    # Section headers stay read-only even when marked LIVE for grouping.
    assert rows["interaction.speak"]["editable"] is False
    # Editor facts ride along: the page renders controls without guessing.
    assert rows["interaction.speak.danmaku"]["kind"] == "bool"
    level = rows["runtime.log_level"]
    assert level["kind"] == "select"
    assert level["choices"] == ["debug", "info", "warning", "error"]
    # Secrets never become editable, whatever their reload class says.
    assert rows["room.credential_ref"]["editable"] is False


# ------------------------------------------------------------ websocket


def test_ws_with_our_origin_gets_hello_then_replay() -> None:
    client, hub, _ = _build()
    hub.broadcast(ServerEvent.VOICE_STATE, {"state": "idle"})
    with client.websocket_connect(f"/{_TOKEN}/ws", headers={"origin": _ORIGIN}) as ws:
        hello = json.loads(ws.receive_text())
        assert hello["event"] == "hello"
        assert hello["data"]["persona"]["name"] == "豆腐"
        replayed = json.loads(ws.receive_text())
        assert replayed["event"] == "voice.state"
        assert replayed["data"]["state"] == "idle"


def test_ws_with_a_foreign_origin_is_refused_before_accept() -> None:
    client, _, _ = _build()
    with pytest.raises(WebSocketDisconnect):  # noqa: SIM117 - the connect itself must raise
        with client.websocket_connect(f"/{_TOKEN}/ws", headers={"origin": "http://evil.example"}):
            pass


def test_ws_without_an_origin_is_a_loopback_tool_and_allowed() -> None:
    client, _, _ = _build()
    with client.websocket_connect(f"/{_TOKEN}/ws") as ws:
        assert json.loads(ws.receive_text())["event"] == "hello"


def test_ws_accepts_the_localhost_spelling_of_our_own_origin() -> None:
    """The streamer may type localhost instead of 127.0.0.1; same page."""
    client, _, _ = _build()
    spelled = _ORIGIN.replace("://127.0.0.1:", "://localhost:")
    with client.websocket_connect(f"/{_TOKEN}/ws", headers={"origin": spelled}) as ws:
        assert json.loads(ws.receive_text())["event"] == "hello"


def test_client_frames_reach_their_handlers() -> None:
    client, _, calls = _build()
    with client.websocket_connect(f"/{_TOKEN}/ws", headers={"origin": _ORIGIN}) as ws:
        ws.receive_text()  # hello
        ws.send_text(json.dumps({"event": "pet.poke", "data": {}}))
        ws.send_text(json.dumps({"event": "panel.set", "data": {"panic_mute": True}}))
    assert calls == [
        (ClientEvent.PET_POKE, {}),
        (ClientEvent.PANEL_SET, {"panic_mute": True}),
    ]


def test_malformed_and_unknown_client_frames_are_dropped_quietly() -> None:
    client, _, calls = _build()
    with client.websocket_connect(f"/{_TOKEN}/ws", headers={"origin": _ORIGIN}) as ws:
        ws.receive_text()  # hello
        ws.send_text("not json at all")
        ws.send_text(json.dumps({"event": "no.such.event", "data": {}}))
        # Legal JSON that is not an object parses fine and then has no .get —
        # it must be dropped, not kill the connection.
        ws.send_text("[1, 2]")
        ws.send_text("null")
        ws.send_text('"just a string"')
        ws.send_text(json.dumps({"event": "pet.poke", "data": "not a dict"}))
    # Every bad frame vanished; the poke with a bad payload got {}.
    assert calls == [(ClientEvent.PET_POKE, {})]


# ------------------------------------------------------------ endpoint file


def test_endpoint_file_is_owner_only_json_and_atomic(tmp_path: Path) -> None:
    target = tmp_path / "ui" / "endpoint.json"
    write_endpoint_file(target, url=f"http://127.0.0.1:7777/{_TOKEN}/", pid=4242)
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload == {"url": f"http://127.0.0.1:7777/{_TOKEN}/", "pid": 4242}
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert list(target.parent.iterdir()) == [target]  # no .tmp left behind


def test_endpoint_file_overwrite_wins_cleanly(tmp_path: Path) -> None:
    target = tmp_path / "endpoint.json"
    write_endpoint_file(target, url="http://127.0.0.1:1/one/", pid=1)
    write_endpoint_file(target, url="http://127.0.0.1:2/two/", pid=2)
    assert json.loads(target.read_text(encoding="utf-8"))["pid"] == 2


# ------------------------------------------------------------ real server


def _sigint_probe() -> Iterator[None]:  # pragma: no cover - helper shape only
    yield


async def test_quiet_server_leaves_sigint_alone_and_stops_gracefully() -> None:
    """Run real uvicorn on port 0; dev-talk's handler must survive serve()."""
    marker = signal.getsignal(signal.SIGINT)
    hub = UiHub(FakeClock())
    registry = HealthRegistry()
    sock = bind_ui_socket(0)
    port = sock.getsockname()[1]
    app = create_ui_app(
        hub=hub,
        registry=registry,
        settings=_settings(),
        token=_TOKEN,
        origin=f"http://127.0.0.1:{port}",
        handlers={},
        hello=lambda: {},
    )
    server = UiServer(app, sock)
    assert server.port == port
    server.start()
    try:
        for _ in range(200):
            if server.started:
                break
            await asyncio.sleep(0.01)
        assert server.started, "uvicorn never reported startup"
        assert signal.getsignal(signal.SIGINT) is marker, "uvicorn stole SIGINT"
    finally:
        await hub.aclose()
        await server.stop()
    # The port is free again: graceful shutdown closed the socket.
    probe = socket.create_server(("127.0.0.1", port))
    probe.close()


async def test_bind_ui_socket_reports_a_taken_port() -> None:
    holder = socket.create_server(("127.0.0.1", 0))
    port = holder.getsockname()[1]
    try:
        with pytest.raises(OSError):
            bind_ui_socket(port)
    finally:
        holder.close()


# ------------------------------------------------------------ the audio socket


def _audio_ui(
    broker: AudioBroker, *, uplink_raises: Exception | None = None
) -> tuple[FastAPI, list[bytes]]:
    """An app with the audio socket mounted, plus what the mic sent upstream.

    Args:
        broker: Who gets the devices.
        uplink_raises: Raised by the FIRST uplink chunk and only that one —
            the shape of push_audio during a link reconnect (it raises
            ConnectionError while the socket is gone, RealtimeClient._send_raw).
    """
    heard: list[bytes] = []
    pending = [uplink_raises]

    async def on_audio(chunk: bytes) -> None:
        boom, pending[0] = pending[0], None
        if boom is not None:
            raise boom
        heard.append(chunk)

    app = create_ui_app(
        hub=UiHub(FakeClock()),
        registry=HealthRegistry(),
        settings=_settings(),
        token=_TOKEN,
        origin=_ORIGIN,
        handlers={},
        hello=lambda: {},
        broker=broker,
        on_audio=on_audio,
    )
    return app, heard


def _audio_app(
    broker: AudioBroker, *, uplink_raises: Exception | None = None
) -> tuple[TestClient, list[bytes]]:
    app, heard = _audio_ui(broker, uplink_raises=uplink_raises)
    return TestClient(app), heard


def test_no_broker_means_no_audio_socket() -> None:
    """Every run before this one had no audio socket; that shape still works."""
    client, _hub, _calls = _build()
    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect(f"/{_TOKEN}/audio"),
    ):
        pass


def test_the_microphone_reaches_the_link_and_the_reply_comes_back() -> None:
    broker = AudioBroker()
    client, heard = _audio_app(broker)
    with client.websocket_connect(f"/{_TOKEN}/audio?role=shell") as ws:
        ws.send_bytes(b"\x01\x02\x03\x04")
        broker.play(b"\xaa\xbb")
        assert ws.receive_bytes() == b"\xaa\xbb"
    assert heard == [b"\x01\x02\x03\x04"]


def test_hanging_up_gives_the_devices_back() -> None:
    """A closed tab must not leave the local pair parked forever."""
    broker = AudioBroker()
    client, _heard = _audio_app(broker)
    with client.websocket_connect(f"/{_TOKEN}/audio?role=shell"):
        assert broker.owner == "shell"
    assert broker.owner is None


def test_a_second_claimant_is_turned_away_rather_than_left_hanging() -> None:
    """Two capture streams would fight over the device, so the weaker one is
    closed outright — a socket nobody feeds looks like a bug to the page."""
    broker = AudioBroker()
    client, _heard = _audio_app(broker)
    with client.websocket_connect(f"/{_TOKEN}/audio?role=shell"):
        with (
            pytest.raises(WebSocketDisconnect) as refused,
            client.websocket_connect(f"/{_TOKEN}/audio") as loser,
        ):
            # Accepted and then closed with a code, rather than refused at the
            # handshake: the page has to tell "someone else has the devices"
            # apart from "your origin is wrong", and only a close code says
            # which.
            loser.receive_bytes()
        assert refused.value.code == 4409


def test_the_audio_socket_refuses_a_foreign_origin() -> None:
    """Same fence as the control socket: this one carries a live microphone."""
    broker = AudioBroker()
    client, _heard = _audio_app(broker)
    with (
        pytest.raises(WebSocketDisconnect) as refused,
        client.websocket_connect(f"/{_TOKEN}/audio", headers={"origin": "http://evil.example"}),
    ):
        pass
    assert refused.value.code == 4403


def test_a_barge_in_drops_queued_audio_and_stops_the_page_in_order() -> None:
    """The half the control socket cannot do.

    playback.clear travels on the control socket, which is a different
    connection: audio the server had already queued arrives after the page has
    cleared and starts playing again. What the streamer hears is the text
    stopping while the voice carries on for a beat — reported from a live
    session. The stop has to ride the same wire as the samples it stops, behind
    every one of them.
    """
    broker = AudioBroker()
    client, _heard = _audio_app(broker)
    with client.websocket_connect(f"/{_TOKEN}/audio?role=shell") as ws:
        broker.play(b"\x11" * 8)
        broker.play(b"\x22" * 8)
        broker.flush()
        broker.play(b"\x33" * 8)  # the next utterance, after the interruption

        # Whatever the queue still held is gone; what survives is the marker
        # and everything after it.
        seen: list[object] = []
        for _ in range(4):
            message = ws.receive()
            seen.append(message.get("text") or message.get("bytes"))
            if len(seen) >= 2:
                break
        assert seen[0] == json.dumps({"event": "playback.clear", "data": {}}), seen
        assert seen[1] == b"\x33" * 8, seen


def test_a_failing_uplink_costs_one_chunk_not_the_devices() -> None:
    """链路重连的那几百毫秒里，push_audio 必抛（RealtimeClient._send_raw）。

    裸着调它，异常会穿过 endpoint 把音频 socket 掀掉，finally 顺手把设备还给
    sounddevice——本机麦克风和扬声器重新开流（没有回声消除），页面退避半秒又
    连回来再把它们停掉。链路退避比页面慢，所以下一块还打在同一个错上，一抖就
    是一串真实的设备开关。本机麦克风那条路早就为同一件事加过护栏
    （dev_talk.py 的 _uplink：一次发送失败的代价是一个块，不是一整场）。
    """
    broker = AudioBroker()
    client, heard = _audio_app(broker, uplink_raises=ConnectionError("还没连接"))
    with client.websocket_connect(f"/{_TOKEN}/audio?role=shell") as ws:
        ws.send_bytes(b"\x01\x01")  # lands inside the reconnect window
        ws.send_bytes(b"\x02\x02")  # the link is back
        broker.play(b"\xaa\xbb")
        assert ws.receive_bytes() == b"\xaa\xbb", "一次发送失败把整条 socket 掀了"
        assert broker.owner == "shell", "设备被一次上行失败还了回去"
    assert heard == [b"\x02\x02"], "丢的应该只有出事的那一块"


# ------------------------------------------------------- the uplink never stops


async def _run_filler(
    idle: Callable[[], float],
    on_audio: Callable[[bytes], Awaitable[None]],
    *,
    ticks: float = 0.05,
) -> None:
    """Let the filler tick for a while, then stop it the way the endpoint does."""
    task = asyncio.create_task(
        _fill_uplink_silence(on_audio, idle, gap_s=0.02, frame_s=0.001),
        name="test:fill",
    )
    await asyncio.sleep(ticks)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_a_page_that_is_still_capturing_is_never_padded() -> None:
    """页面在正常送采集块的时候，服务端一个字都不该插。

    多插进去的每一块都是凭空长出来的时间，供应商的判停窗口按音频时钟走
    （realtime/client.py:218-219），插多了等于把主播的停顿拉长。
    """
    sent: list[bytes] = []

    async def on_audio(pcm: bytes) -> None:
        sent.append(pcm)

    await _run_filler(lambda: 0.0, on_audio)
    assert sent == [], f"页面还在送，服务端却插了 {len(sent)} 块"


async def test_a_page_that_stopped_capturing_gets_silence_put_under_it() -> None:
    """§3.3 规则 7：断流不是暂停，是把供应商的音频时钟冻住。

    麦克风被拒、或者换设备时 getUserMedia 慢了几秒，socket 照样开着、设备照样
    算页面拿着、本机麦克风还 park 着——上行整段归零。本机那条路一直老实照做
    （静音块顶着，dev_talk.py:342-347），页面这条继承了设备没继承规矩。
    """
    sent: list[bytes] = []

    async def on_audio(pcm: bytes) -> None:
        sent.append(pcm)

    await _run_filler(lambda: 5.0, on_audio)
    assert sent, "页面不采集了，上行整个停住"
    assert set(sent) == {_UPLINK_SILENCE}, "补进去的不是一块 20ms 的静音"


async def test_a_failing_link_does_not_kill_the_filler() -> None:
    """链路重连的那几百毫秒里 push_audio 必抛，而这正是最不能停的时候。"""
    sent: list[bytes] = []
    failures = [3]

    async def on_audio(pcm: bytes) -> None:
        if failures[0]:
            failures[0] -= 1
            raise ConnectionError("还没连接")
        sent.append(pcm)

    await _run_filler(lambda: 5.0, on_audio)
    assert failures[0] == 0
    assert sent, "一次发送失败就把补静音的活儿停了"


def test_the_audio_socket_keeps_the_clock_running_when_the_page_goes_quiet() -> None:
    """接线测试：这条规矩得真的挂在音频 socket 上，不是只有一个函数会做。"""
    broker = AudioBroker()
    client, heard = _audio_app(broker)
    block = b"\x11\x22" * 320
    with client.websocket_connect(f"/{_TOKEN}/audio?role=shell") as ws:
        ws.send_bytes(block)  # 采集正常的最后一块
        deadline = time.monotonic() + 3.0
        while len(heard) < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
    assert block in heard, "真的采集块没送上去"
    assert len(heard) >= 3, f"页面不出声之后上行也跟着停了：{len(heard)} 块"
    assert set(heard) - {block} == {_UPLINK_SILENCE}, "补的不是静音块"


def test_a_client_that_never_got_the_devices_does_not_speak_for_the_holder() -> None:
    """被 4409 挡回去的标签页不能往链路里插静音——持有者正在采集，插进去的是噪声。"""
    broker = AudioBroker()
    client, heard = _audio_app(broker)
    # The holder is the broker itself rather than a second socket: a real one
    # would be running its own filler, and then nothing in `heard` says which
    # client put it there.
    asyncio.run(broker.claim("shell", send=lambda _pcm: None))
    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect(f"/{_TOKEN}/audio") as loser,
    ):
        loser.receive_bytes()
    time.sleep(0.25)  # 好几个补静音周期
    assert heard == [], f"只是来看看的那个标签页往上行插了 {len(heard)} 块"


class _StuckPage:
    """A page that never reads a frame, driven straight at the ASGI app.

    TestClient cannot express this: the frames the app sends land in an
    unbounded queue, so the pump never blocks and the downlink queue never
    fills. Backpressure is the entire precondition here, so this one speaks
    ASGI itself.
    """

    def __init__(self) -> None:
        self.accepted = asyncio.Event()
        self._forever = asyncio.Event()
        self._connected = False

    async def receive(self) -> MutableMapping[str, Any]:
        if not self._connected:
            self._connected = True
            return {"type": "websocket.connect"}
        await self._forever.wait()  # holds the socket open, says nothing
        return {"type": "websocket.disconnect", "code": 1000}

    async def send(self, message: MutableMapping[str, Any]) -> None:
        if message["type"] == "websocket.accept":
            self.accepted.set()
            return
        await self._forever.wait()  # nothing is ever read off the wire


def _audio_scope(role: str) -> dict[str, Any]:
    path = f"/{_TOKEN}/audio"
    return {
        "type": "websocket",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "scheme": "ws",
        "server": ("127.0.0.1", 7777),
        "client": ("127.0.0.1", 54321),
        "root_path": "",
        "path": path,
        "raw_path": path.encode(),
        "query_string": f"role={role}".encode(),
        "headers": [],
        "subprotocols": [],
        "state": {},
    }


async def test_displacing_a_page_whose_queue_is_full_still_hands_the_devices_over() -> None:
    """壳来顶替一个已经压满的标签页。

    页面卡住或者音频来得比实时快（这个项目量过 48 秒墙钟里堆出 106 秒没放的
    音频），下行队列就满了。哨兵要是不管满不满硬塞，顶替会在 claim 里炸出
    QueueFull：新连接没建泵、finally 的 release 也没跑，broker 从此认定一个
    死 socket 拿着设备，本机那对再也不 resume——整场既听不见也说不出。
    """
    broker = AudioBroker()
    app, _heard = _audio_ui(broker)
    page = _StuckPage()
    holder = asyncio.create_task(app(_audio_scope("browser"), page.receive, page.send))
    try:
        await asyncio.wait_for(page.accepted.wait(), timeout=2.0)
        held_by = broker.owner
        assert held_by == "browser"
        for _ in range(_AUDIO_QUEUE + 2):
            broker.play(b"\x00" * 960)

        assert await broker.claim("shell", send=lambda _pcm: None), "壳该顶掉标签页"
        assert broker.owner == "shell"
    finally:
        holder.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await holder
    assert broker.owner == "shell", "被顶掉的页面收摊时把设备从壳手里抢走了"


class _HalfDeadBroker(AudioBroker):
    """A claim that fails AFTER it has already pointed the broker at the caller.

    Real shape: claim swaps owner, _send and _close over first and only then
    touches anything that can fail — the displaced client's close callback and
    the local pair's suspend, which is a blocking PortAudio call in a thread.
    """

    __slots__ = ()

    async def claim(
        self,
        who: AudioOwner,
        *,
        send: Callable[[bytes], None],
        close: Callable[[], None] | None = None,
        flush: Callable[[], None] | None = None,
        monitor: Callable[[bytes], None] | None = None,
        clear_monitor: Callable[[int], None] | None = None,
    ) -> bool:
        await super().claim(
            who,
            send=send,
            close=close,
            flush=flush,
            monitor=monitor,
            clear_monitor=clear_monitor,
        )
        raise RuntimeError("PortAudio 没关掉")


def test_a_claim_that_blows_up_hands_the_devices_back() -> None:
    """否则 broker 认定一个不存在的持有者，本机那对再也不会回来。"""
    broker = _HalfDeadBroker()
    client, _heard = _audio_app(broker)
    with (
        pytest.raises(RuntimeError),
        client.websocket_connect(f"/{_TOKEN}/audio?role=shell"),
    ):
        pass
    assert broker.owner is None, "claim 半路炸了，设备不能算在它头上"


# ------------------------------------------------------------ the panel relay


def _drain(queue: asyncio.Queue[str | None]) -> list[dict[str, Any]]:
    """Everything the hub has for one client right now, parsed."""
    frames: list[dict[str, Any]] = []
    while True:
        try:
            line = queue.get_nowait()
        except asyncio.QueueEmpty:
            return frames
        if line is not None:
            frames.append(json.loads(line))


def test_the_panel_reaches_whoever_holds_the_devices() -> None:
    """面板和设备持有者是壳的两个窗口，请求得过一趟服务端。"""
    client, hub, calls = _build(broker=AudioBroker())
    # The panel is just another client of the same hub. Watching from here
    # instead of through a second socket keeps a missing relay a fast failure
    # rather than a test that blocks forever on a frame nobody will send.
    _replay, panel = hub.attach()
    with client.websocket_connect(f"/{_TOKEN}/ws", headers={"origin": _ORIGIN}) as ws:
        ws.receive_text()  # hello
        ws.send_text(json.dumps({"event": "audio.ask", "data": {"kind": "devices"}}))
    seen = _drain(panel)
    assert [item["event"] for item in seen] == [ServerEvent.AUDIO_COMMAND.value], seen
    # The hub stamps every frame with a ts; the ask itself must arrive intact.
    assert seen[0]["data"]["kind"] == "devices"
    # Chained, not overridden: the caller's own handler still sees the event.
    assert calls == [(ClientEvent.AUDIO_ASK, {"kind": "devices"})]


def test_the_holders_answer_comes_back_to_the_panel() -> None:
    """设备列表和电平都是持有者报上来、服务端播回去的。"""
    client, hub, calls = _build(broker=AudioBroker())
    _replay, panel = hub.attach()
    devices = {"kind": "devices", "inputs": [{"id": "a", "label": "内建麦克风"}]}
    with client.websocket_connect(f"/{_TOKEN}/ws", headers={"origin": _ORIGIN}) as ws:
        ws.receive_text()  # hello
        ws.send_text(json.dumps({"event": "audio.report", "data": devices}))
        ws.send_text(json.dumps({"event": "audio.report", "data": {"kind": "level", "rms": 0.4}}))
        # A kind nobody serves is dropped rather than broadcast under some
        # other name.
        ws.send_text(json.dumps({"event": "audio.report", "data": {"kind": "no.such.thing"}}))
    seen = _drain(panel)
    assert [item["event"] for item in seen] == [
        ServerEvent.AUDIO_DEVICES.value,
        ServerEvent.AUDIO_LEVEL.value,
    ], seen
    assert seen[0]["data"]["inputs"] == devices["inputs"]
    assert seen[1]["data"]["rms"] == 0.4
    assert [event for event, _data in calls] == [ClientEvent.AUDIO_REPORT] * 3


def test_the_panic_control_never_calls_itself_a_microphone_switch() -> None:
    """It stops HER; the microphone is not touched.

    The label read 「紧急闭麦」 until 2026-08-25. 闭麦 means muting your own
    microphone, which is the opposite of what the button does — and the word was
    already taken by `--mute-while-speaking`, which really does mute the
    microphone during playback. One word, two opposite meanings, in one program,
    on the one control a streamer reaches for when something has gone wrong.

    Asserted against the file rather than the rendered page: the browser caches
    static assets between tests, so a page-level check does not go red when this
    line changes.
    """
    page = (Path(__file__).resolve().parents[2] / "src/bilisama/ui/web/index.html").read_text(
        encoding="utf-8"
    )
    button = next(line for line in page.splitlines() if 'id="p-panic"' in line)
    assert "紧急叫停" in button, f"按钮文案变了：{button.strip()}"
    assert "闭麦" not in button, f"又叫回「闭麦」了，那是关麦克风的意思：{button.strip()}"
    assert "不碰麦克风" in button, "title 里没写清它不关麦克风"
