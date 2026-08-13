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
from bilisama.realtime.link import ReplySpec

__all__ = ["PokeResponder"]

# A poked reply is a throwaway quip: short, cheap, and worthless once late.
_COOLDOWN_S = 15.0
_EXPIRES_S = 8.0
_MAX_TOKENS_CAP = 40

_INSTRUCTIONS = "主播戳了戳你，简短俏皮地回应一下，一句话。"


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
            return False
        self._last = now
        self._submit(
            Intent(
                source="ui.poke",
                priority=Priority.PROACTIVE,
                injection=Injection(
                    reply=ReplySpec(instructions=_INSTRUCTIONS, max_tokens=self._max_tokens)
                ),
                trusted=True,
                dedup_key=f"ui.poke:{now}",
                created_at=now,
                expires_at=now + _EXPIRES_S,
            )
        )
        return True
