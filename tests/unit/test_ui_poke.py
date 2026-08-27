"""PokeResponder: the intent shape and the cooldown boundary.

The poke rides the existing scheduling machinery, so what matters here is that
the intent it files is exactly the proactive-topic shape (trusted, lowest
priority, short TTL) and that clicking enthusiastically cannot flood the heap.
"""

from __future__ import annotations

import logging

import pytest

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
    # It used to be None — "nothing enters model history" — and that was
    # exactly the bug: DashScope will not answer a conversation holding no user
    # message. See the dedicated test below (ledger #56).
    assert intent.injection.item_text == "戳了戳你"
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


def test_a_poke_writes_something_into_the_conversation() -> None:
    """Ledger #56: an injection with no item cannot be answered on DashScope.

    Probed live 2026-08-24, all four combinations: that endpoint refuses
    `response.create` on a conversation holding no user message, and going
    out-of-band does not exempt it. Poke and the proactive topic were the only
    two intents that injected nothing, so on the shipping provider both died
    at the first click of a fresh session — "Cannot create response:
    conversation has no messages or no user message."

    Plan section 4.5 already says every proactive opening enters as a
    synthesized role:"user" item plus response.create. These two were the
    exception, not the rule.
    """
    clock = FakeClock()
    filed: list[Intent] = []
    PokeResponder(clock, submit=filed.append, max_tokens=120).poke()
    assert filed, "戳了一下却什么都没提交"
    assert filed[0].injection.item_text, "戳一戳没往会话里写任何东西，DashScope 上会被拒"


# ------------------------------------------------------------ what it records


def _lines(caplog: pytest.LogCaptureFixture, event: str) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.getMessage() == event]


def test_a_filed_poke_is_recorded_at_info(caplog: pytest.LogCaptureFixture) -> None:
    """The streamer's own click is a decision, not bookkeeping — a handful a
    stream, so it belongs at the level the panel shows by default."""
    caplog.set_level(logging.DEBUG)
    responder, _ = _build(FakeClock(start=100.0))

    assert responder.poke() is True

    (record,) = _lines(caplog, "ui.poke_filed")
    assert record.levelno == logging.INFO
    fields = getattr(record, "fields", {})
    assert fields["max_tokens"] == 40
    assert fields["ttl_ms"] == 8000, "戳一戳过期得快，这是它经常没下文的原因"


def test_a_swallowed_poke_says_the_cooldown_ate_it(caplog: pytest.LogCaptureFixture) -> None:
    """「我戳了怎么没反应」, the case with no other trace at all.

    A poke inside the cooldown never becomes an Intent, so the scheduler never
    sees it and no verdict is ever written — while the pet animates exactly as
    it does for a poke that went through. This line is the only record.
    """
    clock = FakeClock()
    responder, submitted = _build(clock)
    assert responder.poke() is True
    caplog.set_level(logging.DEBUG)
    # The first poke logged too, and whether caplog kept that record depends on
    # the root level some earlier test left behind. Drop it explicitly so this
    # test reads the same alone as it does in a full run.
    caplog.clear()
    clock._now += 3.0  # direct nudge; advance() needs a running loop

    assert responder.poke() is False

    assert len(submitted) == 1
    (record,) = _lines(caplog, "ui.poke_cooling_down")
    assert record.levelno == logging.INFO
    fields = getattr(record, "fields", {})
    assert fields["since_last_ms"] == 3000
    assert fields["cooldown_ms"] == 15000, "不写死等多久，主播只能猜"
    assert _lines(caplog, "ui.poke_filed") == [], "被冷却挡下的不该记成已提交"
