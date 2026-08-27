"""The proactive topic loop: openhanako's subconscious, turned inside out.

The original wrote a 9-to-12-line inner monologue before every reply — free
in a text UI, dead air before the first audible word in full-duplex voice.
Here the thinking runs in the background instead (plan section 4.6): a
periodic side call reads recent danmaku and the session progress, produces
one topic candidate, and stores it. The mouth never waits for the brain.

The foreground half watches for dead air: when the floor has been open and
quiet past the chattiness-derived idle threshold, the stored candidate goes
in as a PROACTIVE intent — the lowest priority there is, pre-empted by
everyone, expiring fast. This is requirement 6 (直播策略) landing.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections import deque
from typing import TYPE_CHECKING, Any

from bilisama.director.intent import Injection, Intent, Priority
from bilisama.director.intents import neutralize_tags
from bilisama.obs.logging import get_logger
from bilisama.realtime.link import ReplySpec
from bilisama.side import SideModel, SideModelError

if TYPE_CHECKING:
    from collections.abc import Callable

    from bilisama.clock import Clock
    from bilisama.director.floor import SpeakingFloor
    from bilisama.memory.store import MemoryStore

__all__ = ["ProactiveTopicLoop"]

log = get_logger(__name__)

_TICK_S = 1.0
_TOPIC_TTL_S = 30.0  # a topic that waited half a minute is stale, drop it

# See the item_text comment below.
_DEAD_AIR_ITEM = "[本场] 这会儿没人说话"
_CANDIDATE_MAX_TOKENS = 80


class ProactiveTopicLoop:
    """Background candidate refresh plus foreground idle trigger."""

    def __init__(
        self,
        side: SideModel | None,
        store: MemoryStore,
        floor: SpeakingFloor,
        clock: Clock,
        *,
        submit: Callable[[Intent], None],
        prompt: str,
        idle_threshold_s: float,
        wake_interval_s: float = 30.0,
        max_per_hour: int = 12,
        max_tokens: int = 120,
    ) -> None:
        self._side = side
        self._store = store
        self._floor = floor
        self._clock = clock
        self._submit = submit
        self._prompt = prompt
        self._idle_threshold_s = idle_threshold_s
        self._wake_interval_s = wake_interval_s
        self._max_per_hour = max_per_hour
        self._max_tokens = max_tokens

        self._candidate: str | None = None
        self._fingerprint = ""
        self._last_activity = clock.monotonic()
        self._last_refresh = -wake_interval_s  # first refresh happens on tick one
        self._submitted: deque[float] = deque()
        self._refresh_task: asyncio.Task[None] | None = None
        self._topics_produced = 0
        # The hourly cap is re-checked once a second while a topic sits ready,
        # so the block is a STATE, not an event. Latched so the log records the
        # flip into it once instead of once per tick.
        self._budget_blocked = False

    # ------------------------------------------------------------ inputs

    def note_activity(self) -> None:
        """Anything happened — an event arrived, someone spoke. Resets idle."""
        self._last_activity = self._clock.monotonic()

    # ------------------------------------------------------------ the loop

    async def run(self) -> None:
        """Tick once a second on the injected clock. Cancel to stop."""
        if self._side is None:
            # Reported once here and permanently in status(): the switch says
            # on, the engine is missing — never silently (plan section 7.6).
            log.warning("proactive.no_side_model")
        while True:
            await self._clock.sleep(_TICK_S)
            self._tick()

    def _tick(self) -> None:
        now = self._clock.monotonic()
        if self._floor.is_blocked():
            # Someone is talking, audio is playing, or a window is open —
            # none of that counts as dead air.
            self._last_activity = now
            return
        if self._side is not None and now - self._last_refresh >= self._wake_interval_s:
            self._last_refresh = now
            self._spawn_refresh()
        if self._candidate is None:
            return
        if now - self._last_activity < self._idle_threshold_s:
            return
        if not self._budget_ok(now):
            if not self._budget_blocked:
                self._budget_blocked = True
                log.info(
                    "proactive.budget_exhausted",
                    topics_this_hour=len(self._submitted),
                    max_per_hour=self._max_per_hour,
                )
            return
        self._budget_blocked = False
        self._speak(now)

    def _speak(self, now: float) -> None:
        # The candidate came out of a side model that READ audience danmaku —
        # a second-order injection channel (A14). Flatten whitespace so it
        # cannot fake prompt structure, break wrapper tokens, cap the length;
        # the instructions text around it stays a fixed template.
        candidate = neutralize_tags(" ".join((self._candidate or "").split()))[:80]
        # Read before _last_activity is reset below — after that the number is
        # always zero, and "how long was the silence" is the whole reason a
        # proactive topic went in at all.
        idle_s = round(now - self._last_activity, 1)
        self._candidate = None
        # Force a regeneration next refresh even if no new events arrive: the
        # next dead-air stretch deserves a fresh angle, not this one reheated.
        self._fingerprint = ""
        self._last_activity = now
        self._submitted.append(now)
        self._topics_produced += 1
        # The submit side only. Whether the intent survives the scheduler is
        # scheduler.verdict's line to write, and this one must not pretend to
        # know: a PROACTIVE intent is the lowest priority there is and gets
        # pre-empted by anyone who speaks in the meantime.
        #
        # topic_text rather than topic: the candidate came out of a side model
        # that read audience danmaku, so the scrubber folding it to a length is
        # the correct treatment.
        log.info(
            "proactive.topic_submitted",
            topic_text=candidate,
            idle_s=idle_s,
            topics_this_hour=len(self._submitted),
        )
        self._submit(
            Intent(
                source="proactive",
                priority=Priority.PROACTIVE,
                injection=Injection(
                    reply=ReplySpec(
                        instructions=(
                            "冷场了，把这个话题自然地聊起来，一两句话，"
                            f"像随口起头，不要报节目单：{candidate}"
                        ),
                        max_tokens=self._max_tokens,
                    ),
                    # Something has to reach the conversation: DashScope
                    # refuses a response.create when it holds no user message,
                    # out-of-band included (probed live 2026-08-24), so a
                    # topic that injected nothing could never open a fresh
                    # session there — backlog item 56. Plan section 4.5 always
                    # said every proactive opening enters as a synthesized
                    # user item plus response.create; this was the exception.
                    #
                    # A bracket prefix rather than the <bilisama_live_events>
                    # wrapper: that tag means "audience data, not the
                    # streamer" (persona/prompt.py:28) and this is neither.
                    # The prefix matches the [弹幕] / [进房] lines she already
                    # reads as context, so it does not sound like something to
                    # say back.
                    item_text=_DEAD_AIR_ITEM,
                ),
                trusted=True,
                dedup_key=f"proactive:{int(now)}",
                created_at=now,
                expires_at=now + _TOPIC_TTL_S,
            )
        )

    def _budget_ok(self, now: float) -> bool:
        while self._submitted and now - self._submitted[0] > 3600.0:
            self._submitted.popleft()
        return len(self._submitted) < self._max_per_hour

    # ------------------------------------------------------------ refresh

    def _spawn_refresh(self) -> None:
        if self._refresh_task is not None and not self._refresh_task.done():
            return
        self._refresh_task = asyncio.create_task(self._refresh(), name="proactive:refresh")

    async def _refresh(self) -> None:
        assert self._side is not None
        events = self._store.recent_events(limit=20)
        rows = self._store.facts("stream", str(self._store.stream_id))
        summary = rows[-1].text if rows else ""
        fingerprint = hashlib.sha256("\n".join([summary, *events]).encode()).hexdigest()
        if fingerprint == self._fingerprint:
            return
        try:
            raw = await self._side.complete(
                system=self._prompt,
                user=(
                    f"本场进展：{summary or '（刚开播，还没有进展）'}\n"
                    f"最近弹幕和事件：\n{chr(10).join(events) or '（还没有）'}"
                ),
                max_tokens=_CANDIDATE_MAX_TOKENS,
            )
        except SideModelError as exc:
            log.warning("proactive.refresh_failed", error_text=str(exc))
            return
        topic = raw.strip().splitlines()[0].strip() if raw.strip() else ""
        if topic:
            self._fingerprint = fingerprint
            self._candidate = topic
            # A ready candidate and a spoken one are different states, and the
            # gap between them is where "she never says anything on her own"
            # gets diagnosed: no topic_ready means the side model came back
            # empty, topic_ready without topic_submitted means the floor was
            # never idle long enough.
            log.info("proactive.topic_ready", topic_text=topic, event_count=len(events))

    # ------------------------------------------------------------ health

    def status(self) -> dict[str, Any]:
        return {
            "side_configured": self._side is not None,
            "candidate_ready": self._candidate is not None,
            "topics_this_hour": len(self._submitted),
            "topics_produced": self._topics_produced,
        }
