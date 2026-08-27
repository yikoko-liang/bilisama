"""What happens when the streamer clicks the pet.

The visual reaction belongs to the page and is always instant. This module only
decides whether she also says something: it files a lowest-priority intent and
lets the floor and scheduler rule on airing it, exactly like a proactive topic.
The poke never looks at the gates itself — that would be a second copy of the
floor's judgement, drifting from the first.
"""

from __future__ import annotations

from collections.abc import Callable

from bilisama.clock import Clock
from bilisama.director.intent import Injection, Intent, Priority
from bilisama.obs.logging import bind, get_logger
from bilisama.realtime.link import ReplySpec

__all__ = ["PokeResponder"]

log = get_logger(__name__)

# A poked reply is a throwaway quip: short, cheap, and worthless once late.
_COOLDOWN_S = 15.0
_EXPIRES_S = 8.0
_MAX_TOKENS_CAP = 40

_INSTRUCTIONS = "主播戳了戳你，简短俏皮地回应一下，一句话。"

# What lands in the conversation. It has to be there at all: DashScope refuses
# a response.create on a conversation holding no user message, out-of-band
# included (probed live 2026-08-24), so a poke that injected nothing died at
# the first click of a fresh session — backlog item 56. Plan section 4.5 said
# so from the start: every proactive opening enters as a synthesized user item
# plus response.create.
#
# No <bilisama_live_events> wrapper, deliberately. That tag means "audience
# data, not the streamer" (persona/prompt.py:28), and this IS the streamer.
# Written the way a person pokes rather than as a stage direction, because
# parenthetical narration is the shape that gets read aloud.
_ITEM = "戳了戳你"


class PokeResponder:
    """Turns pet clicks into at most one intent per cooldown window."""

    __slots__ = ("_clock", "_cooldown_s", "_last", "_max_tokens", "_submit")

    def __init__(
        self,
        clock: Clock,
        *,
        submit: Callable[[Intent], None],
        max_tokens: int,
        cooldown_s: float = _COOLDOWN_S,
    ) -> None:
        """Args:
        clock: Injected clock; the cooldown is time-driven and must be testable.
        submit: Scheduler.submit.
        max_tokens: The panel's reply-length budget; capped further here
            because a poke response should be a quip, not a paragraph.
        cooldown_s: Minimum spacing between poked replies.
        """
        self._clock = clock
        self._submit = submit
        self._max_tokens = min(_MAX_TOKENS_CAP, max_tokens)
        self._cooldown_s = cooldown_s
        self._last: float | None = None

    def poke(self) -> bool:
        """File the intent unless the cooldown is still running.

        Returns:
            True if an intent was submitted. False means the click stays a
            purely visual event — the page animates either way.
        """
        now = self._clock.monotonic()
        if self._last is not None and now - self._last < self._cooldown_s:
            # The click still animates, so from the streamer's side a swallowed
            # poke and a poke the floor refused look identical. This line is
            # what tells the two apart — and it is the only record of the
            # cooldown, since a poke that never becomes an Intent never reaches
            # the scheduler and so never earns a verdict.
            log.info(
                "ui.poke_cooling_down",
                since_last_ms=round((now - self._last) * 1000),
                cooldown_ms=round(self._cooldown_s * 1000),
            )
            return False
        self._last = now
        intent = Intent(
            source="ui.poke",
            priority=Priority.PROACTIVE,
            injection=Injection(
                reply=ReplySpec(instructions=_INSTRUCTIONS, max_tokens=self._max_tokens),
                item_text=_ITEM,
            ),
            trusted=True,
            dedup_key=f"ui.poke:{now}",
            created_at=now,
            expires_at=now + _EXPIRES_S,
        )
        self._submit(intent)
        # Logged after the submit, so the line means "the scheduler has it",
        # not "we were about to hand it over". Bound to the id the verdict will
        # carry: the poke files at the lowest rung and expires in 8s, so 「戳了
        # 没反应」 is usually answered by the verdict line, and this one is how
        # you find it.
        with bind(intent_id=intent.dedup_key):
            log.info("ui.poke_filed", max_tokens=self._max_tokens, ttl_ms=round(_EXPIRES_S * 1000))
        return True
