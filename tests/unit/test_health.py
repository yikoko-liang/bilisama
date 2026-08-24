"""The health registry and its endpoint (backlog item 17, plan section 4.12)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from bilisama.clock import FakeClock
from bilisama.obs.health import HealthRegistry, LinkHealth, create_app


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


# ------------------------------------------------------------ provider connection


def test_the_link_probe_starts_out_saying_it_is_not_connected() -> None:
    """Before the first connect there is nothing to be optimistic about, and a
    probe that opens on `connected: true` would answer "why is she silent" with
    the one word that sends people looking somewhere else."""
    clock = FakeClock()
    health = LinkHealth(clock, provider="s2s")

    status = health.status()
    assert status["connected"] is False
    assert status["provider"] == "s2s"
    assert status["drops"] == 0


def test_the_link_probe_follows_the_link_up_and_down() -> None:
    clock = FakeClock()
    health = LinkHealth(clock, provider="dashscope")
    health.mark_up(attempts=1)
    assert health.status()["connected"] is True

    health.mark_down("1006 abnormal closure", retrying=True)
    down = health.status()
    assert down["connected"] is False
    assert down["retrying"] is True
    assert "1006" in down["last_reason"]
    assert down["drops"] == 1

    health.mark_up(attempts=3)
    back = health.status()
    assert back["connected"] is True
    assert back["reconnect_attempts"] == 3
    # The reason survives the recovery: after the fact, "it dropped twice and
    # came back" is the interesting sentence, not "it is up".
    assert "1006" in back["last_reason"]
    assert back["drops"] == 1


def test_the_link_probe_keeps_the_last_error_apart_from_the_last_drop() -> None:
    """A LinkError is the provider refusing something while the socket stays up
    — folding it into the drop reason would report a live link as down."""
    clock = FakeClock()
    health = LinkHealth(clock, provider="s2s")
    health.mark_up(attempts=1)
    health.mark_error("invalid_request_error", "voice 名字不对")

    status = health.status()
    assert status["connected"] is True
    assert "voice 名字不对" in status["last_error"]
    assert status["errors"] == 1


async def test_the_link_probe_ages_its_last_change() -> None:
    """ "How long has it been like this" is most of the diagnosis."""
    clock = FakeClock()
    health = LinkHealth(clock, provider="s2s")
    health.mark_up(attempts=1)
    await clock.advance(42.0)
    assert health.status()["for_s"] == pytest.approx(42.0)
