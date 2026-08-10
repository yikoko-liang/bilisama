"""The health registry and its endpoint (backlog item 17, plan section 4.12)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from bilisama.obs.health import HealthRegistry, create_app


def test_snapshot_collects_every_probe() -> None:
    registry = HealthRegistry()
    registry.register("proactive", lambda: {"side_configured": False, "topics_produced": 0})
    registry.register("assembly", lambda: {"events_seen": 12})

    snap = registry.snapshot()
    assert snap["status"] == "ok"
    assert snap["components"]["proactive"]["side_configured"] is False
    assert snap["components"]["assembly"]["events_seen"] == 12


def test_a_broken_probe_degrades_instead_of_raising() -> None:
    """During an incident the endpoint must answer, not join the incident."""
    registry = HealthRegistry()
    registry.register("fine", lambda: {"ok": True})

    def broken() -> dict[str, bool]:
        raise RuntimeError("探针自己坏了")

    registry.register("broken", broken)

    snap = registry.snapshot()
    assert snap["status"] == "degraded"
    assert snap["components"]["fine"] == {"ok": True}
    assert "探针自己坏了" in snap["components"]["broken"]["error"]


def test_the_http_endpoint_serves_the_snapshot() -> None:
    registry = HealthRegistry()
    registry.register("assembly", lambda: {"events_seen": 3})
    client = TestClient(create_app(registry))

    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["components"]["assembly"]["events_seen"] == 3

    assert client.get("/docs").status_code == 404, "no accidental public surfaces"
