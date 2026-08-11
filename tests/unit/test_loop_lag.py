"""The loop-lag monitor: the runtime half of the blocking-call defence (#25).

The accounting is tested without real time via _note; the one integration
test commits the actual crime — a synchronous sleep on the loop — and asserts
the monitor saw it.
"""

from __future__ import annotations

import asyncio
import contextlib
import time

from bilisama.obs.loop_lag import LoopLagMonitor


def test_note_records_max_and_warns_only_over_threshold() -> None:
    monitor = LoopLagMonitor(warn_over_s=0.05)
    monitor._note(0.010, now=1.0)
    assert monitor.warn_count == 0, "10ms is under the 50ms threshold"
    assert monitor.max_lag_ms == 10.0
    monitor._note(0.120, now=2.0)
    assert monitor.warn_count == 1
    assert monitor.max_lag_ms == 120.0
    monitor._note(0.080, now=3.0)
    assert monitor.max_lag_ms == 120.0, "max keeps the worst, not the latest"


def test_every_incident_counts_but_the_log_is_throttled() -> None:
    monitor = LoopLagMonitor(warn_over_s=0.05, log_cooldown_s=5.0)
    monitor._note(0.1, now=0.0)
    monitor._note(0.1, now=1.0)  # inside the cooldown: counted, not logged
    monitor._note(0.1, now=6.0)
    assert monitor.warn_count == 3
    assert monitor._last_logged == 6.0, "the middle incident must not reset the throttle"


def test_negative_lag_clamps_to_zero() -> None:
    """A loop that wakes early (clock jitter) must not underflow the stats."""
    monitor = LoopLagMonitor()
    monitor._note(-0.001, now=1.0)
    assert monitor.max_lag_ms == 0.0
    assert monitor.status() == {"max_lag_ms": 0.0, "warn_count": 0}


async def test_a_blocked_loop_shows_up_as_lag() -> None:
    monitor = LoopLagMonitor(interval_s=0.01, warn_over_s=0.05)
    task = asyncio.create_task(monitor.run())
    await asyncio.sleep(0.03)  # give it a few clean samples first
    time.sleep(0.12)  # noqa: ASYNC251  -- the crime under test: block the loop
    await asyncio.sleep(0.05)  # let the monitor wake and take the measurement
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert monitor.max_lag_ms >= 80, f"loop blocked ~120ms, measured {monitor.max_lag_ms}ms"
    assert monitor.warn_count >= 1
