"""PokeResponder: the intent shape and the cooldown boundary.

The poke rides the existing scheduling machinery, so what matters here is that
the intent it files is exactly the proactive-topic shape (trusted, lowest
priority, short TTL) and that clicking enthusiastically cannot flood the heap.
"""

from __future__ import annotations

from bilisama.clock import FakeClock
from bilisama.director.intent import Intent, Priority
from bilisama.ui.poke import PokeResponder


def _build(clock: FakeClock, *, max_tokens: int = 120) -> tuple[PokeResponder, list[Intent]]:
    submitted: list[Intent] = []
    responder = PokeResponder(clock, submit=submitted.append, max_tokens=max_tokens)
    return responder, submitted


def test_poke_files_a_trusted_lowest_priority_quip() -> None:
    clock = FakeClock(start=100.0)
    responder, submitted = _build(clock)
    assert responder.poke() is True
    (intent,) = submitted
    assert intent.source == "ui.poke"
    assert intent.priority is Priority.PROACTIVE
    assert intent.trusted is True
    assert intent.injection.item_text is None  # nothing enters model history
    assert intent.injection.reply.max_tokens == 40  # quip cap beats the panel budget
    assert intent.created_at == 100.0
    assert intent.expires_at == 108.0  # a poke answered late is worse than none
    assert intent.dedup_key == "ui.poke:100.0"


def test_small_panel_budget_wins_over_the_quip_cap() -> None:
    responder, submitted = _build(FakeClock(), max_tokens=24)
    responder.poke()
    assert submitted[0].injection.reply.max_tokens == 24


def test_cooldown_boundary_is_exact() -> None:
    clock = FakeClock()
    responder, submitted = _build(clock)
    assert responder.poke() is True
    clock._now += 14.999  # direct nudge; advance() needs a running loop
    assert responder.poke() is False
    assert len(submitted) == 1
    clock._now += 0.002  # past 15s since the first poke
    assert responder.poke() is True
    assert len(submitted) == 2


def test_rapid_double_click_submits_once() -> None:
    responder, submitted = _build(FakeClock())
    assert responder.poke() is True
    assert responder.poke() is False
    assert len(submitted) == 1
    # dedup_key must differ across windows, or the scheduler's ring would
    # swallow the second legitimate poke.
    responder2, submitted2 = _build(FakeClock(start=500.0))
    responder2.poke()
    assert submitted[0].dedup_key != submitted2[0].dedup_key
