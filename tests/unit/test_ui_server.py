"""UI server: token fence, Origin check, config scrubbing, health mount,
endpoint file, and the uvicorn signal bypass.

The signal test is the load-bearing one: uvicorn 0.52 installs its own
SIGINT/SIGTERM handlers inside serve() (server.py:322-347), and dev-talk's
graceful shutdown (distill, store close) dies silently if that ever leaks
through. If a uvicorn upgrade moves the hook, that test goes red first.
"""

from __future__ import annotations

import asyncio
import json
import signal
import socket
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from bilisama.clock import FakeClock
from bilisama.config.schema import Settings
from bilisama.obs.health import HealthRegistry
from bilisama.ui.events import ClientEvent, ServerEvent
from bilisama.ui.hub import UiHub
from bilisama.ui.server import (
    UiServer,
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
        hello=lambda: {"persona": {"id": "mia", "name": "米娅"}},
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
        speech={"dashscope": {"api_key_ref": "env:DASHSCOPE_API_KEY"}},
    )
    client, _, _ = _build(settings)
    response = client.get(f"/{_TOKEN}/config")
    assert response.status_code == 200
    body = response.text
    assert "BILI_SESSDATA" not in body
    assert "DASHSCOPE_API_KEY" not in body
    rows = {row["path"]: row for row in response.json()}
    assert rows["room.credential_ref"]["value"] == "已配置"
    assert rows["speech.dashscope.api_key_ref"]["value"] == "已配置"


def test_config_snapshot_reports_unset_secrets_as_missing() -> None:
    rows = {row["path"]: row for row in config_snapshot(_settings())}
    assert rows["room.credential_ref"]["value"] == "未配置"


def test_config_snapshot_covers_all_meta_and_serializes() -> None:
    rows = config_snapshot(_settings())
    from bilisama.config.ui_meta import UI_META

    assert {row["path"] for row in rows} == set(UI_META)
    # JSONResponse runs allow_nan=False; the inf default in the s2s turn
    # section must have been stringified by the snapshot.
    json.dumps(rows, allow_nan=False)
    sample = {row["path"]: row for row in rows}["avatar.renderer"]
    assert sample["label"] == "形象类型"
    assert sample["value"] == "tofu"
    assert sample["audience"] == "streamer"


# ------------------------------------------------------------ websocket


def test_ws_with_our_origin_gets_hello_then_replay() -> None:
    client, hub, _ = _build()
    hub.broadcast(ServerEvent.VOICE_STATE, {"state": "idle"})
    with client.websocket_connect(f"/{_TOKEN}/ws", headers={"origin": _ORIGIN}) as ws:
        hello = json.loads(ws.receive_text())
        assert hello["event"] == "hello"
        assert hello["data"]["persona"]["name"] == "米娅"
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
        ws.send_text(json.dumps({"event": "pet.poke", "data": "not a dict"}))
    # The two bad frames vanished; the poke with a bad payload got {}.
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
