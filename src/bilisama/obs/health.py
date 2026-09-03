"""Runtime health: one place that answers "what state is everything in".

Components register a probe; the snapshot pulls them all. A probe that raises
reports as an error entry instead of taking the endpoint down — the whole
point is being readable during an incident.

The UI server mounts this app at /health and the panel's health card renders
the snapshot (ui/server.py). `bilisama doctor` is meant to read the same
snapshot (plan section 4.12) and does not exist yet; dev-talk also prints it
once on the way out.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from fastapi import FastAPI

from bilisama.clock import Clock

__all__ = ["HealthRegistry", "LinkHealth", "create_app"]

Probe = Callable[[], Mapping[str, Any]]


class HealthRegistry:
    """Name → probe. Probes are cheap, synchronous and side-effect free."""

    def __init__(self) -> None:
        self._probes: dict[str, Probe] = {}

    def register(self, name: str, probe: Probe) -> None:
        self._probes[name] = probe

    def snapshot(self) -> dict[str, Any]:
        components: dict[str, Any] = {}
        healthy = True
        for name, probe in self._probes.items():
            try:
                components[name] = dict(probe())
            except Exception as exc:
                healthy = False
                components[name] = {"error": str(exc)}
        return {"status": "ok" if healthy else "degraded", "components": components}


class LinkHealth:
    """Whether the speech provider is connected, and how long it has been that way.

    Plan §4.12 lists provider connection state as one of the things health has to
    answer; today the only place it shows is the startup banner and the log,
    neither of which the panel can read mid-stream.

    Fed by the caller rather than by watching the link itself: `realtime`
    imports `obs` for logging, so an import the other way would be a cycle. The
    caller already switches on these events (dev_talk `_consume_events`).
    """

    __slots__ = ("_clock", "_connected", "_drops", "_errors", "_last_error", "_provider")

    def __init__(self, clock: Clock, *, provider: str) -> None:
        self._clock = clock
        self._provider = provider
        # Not connected until something says so. Opening on True would answer
        # "why is she quiet" with the one word that sends people elsewhere.
        self._connected: dict[str, Any] = {
            "connected": False,
            "retrying": False,
            "last_reason": "",
            "reconnect_attempts": 0,
            "since": clock.monotonic(),
        }
        self._drops = 0
        self._errors = 0
        self._last_error = ""

    def mark_up(self, *, attempts: int) -> None:
        """The socket is live. `attempts` is which try got it (1 = first)."""
        self._connected.update(
            connected=True,
            retrying=False,
            reconnect_attempts=attempts,
            since=self._clock.monotonic(),
        )

    def mark_down(self, reason: str, *, retrying: bool) -> None:
        """The socket went away. The reason outlives the recovery on purpose:
        after the fact, "it dropped and came back" is the useful sentence."""
        self._drops += 1
        self._connected.update(
            connected=False,
            retrying=retrying,
            last_reason=reason,
            since=self._clock.monotonic(),
        )

    def mark_error(self, code: str, detail: str) -> None:
        """The provider refused something while the socket stayed up. Kept apart
        from the drop reason — folding them would report a live link as down."""
        self._errors += 1
        self._last_error = f"{code}: {detail}" if detail else code

    def status(self) -> dict[str, Any]:
        return {
            "provider": self._provider,
            **{k: v for k, v in self._connected.items() if k != "since"},
            "for_s": round(self._clock.monotonic() - float(self._connected["since"]), 1),
            "drops": self._drops,
            "errors": self._errors,
            "last_error": self._last_error,
        }


def create_app(registry: HealthRegistry) -> FastAPI:
    """A minimal app exposing GET /health.

    Mounted LAST by the UI server (ui/server.py:496): its prefix is the bare
    token, so anything mounted after it would be swallowed.
    """
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)

    @app.get("/health")
    def health() -> dict[str, Any]:
        return registry.snapshot()

    return app
