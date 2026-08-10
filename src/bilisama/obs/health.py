"""Runtime health: one place that answers "what state is everything in".

Components register a probe; the snapshot pulls them all. A probe that raises
reports as an error entry instead of taking the endpoint down — the whole
point is being readable during an incident. The FastAPI app mounts into the
UI server when that exists (stage 5); `bilisama doctor` and the panel read
the same snapshot (plan section 4.12).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from fastapi import FastAPI

__all__ = ["HealthRegistry", "create_app"]

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
            except Exception as exc:  # noqa: BLE001 — a broken probe IS the finding
                healthy = False
                components[name] = {"error": str(exc)}
        return {"status": "ok" if healthy else "degraded", "components": components}


def create_app(registry: HealthRegistry) -> FastAPI:
    """A minimal app exposing GET /health. Mounted by the UI server later."""
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)

    @app.get("/health")
    def health() -> dict[str, Any]:
        return registry.snapshot()

    return app
