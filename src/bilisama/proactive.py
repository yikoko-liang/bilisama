"""The proactive topic loop: openhanako's subconscious, turned inside out.

The original wrote a 9-to-12-line inner monologue before every reply — free
in a text UI, dead air before the first audible word in full-duplex voice.
Here the thinking runs in the background instead (plan section 4.6): a
periodic side call reads recent danmaku, the session progress and completed
dialogue, produces one topic candidate, and stores it. The mouth never waits
for the brain.

The foreground half watches the LIVE-EVENT lane for dead air. With an event
pacer wired, the idle target comes from room activity (30/60/120s, off when
busy) and the streamer's own voice does not reset the clock — a monologue is
material for the next topic, not a reason to never start one. A due topic
yields three ways before speaking: to any pending funnel work, to the shared
ordinary budget, and — as always — to everyone at dispatch, because
PROACTIVE is the lowest priority there is.

When no side model is configured the loop still opens cold rooms: the
Realtime model is asked to pick a topic straight from its shared context
instead of receiving a preselected candidate.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections import OrderedDict, deque
from typing import TYPE_CHECKING, Any, Literal

from bilisama.director.intent import Injection, Intent, Priority
from bilisama.director.intents import EVENT_DECISION_RULES, neutralize_tags
from bilisama.obs.logging import get_logger
from bilisama.proactive_opportunities import (
    DanmakuSummary,
    OpinionSummary,
    ProactiveOpportunities,
)
from bilisama.realtime.link import ReplySpec
from bilisama.side import SideModel, SideModelError

if TYPE_CHECKING:
    from collections.abc import Callable

    from bilisama.clock import Clock
    from bilisama.director.floor import SpeakingFloor
    from bilisama.event_pacing import EventPacer
    from bilisama.ingest.events import LiveEvent
    from bilisama.memory.store import MemoryStore
    from bilisama.obs.outcome import Verdict

__all__ = ["ProactiveTopicLoop"]

log = get_logger(__name__)

_TICK_S = 1.0
_TOPIC_TTL_S = 30.0  # a topic that waited half a minute is stale, drop it

# See the item_text comment below.
_DEAD_AIR_ITEM = "[本场] 这会儿没人说话"
_CANDIDATE_MAX_TOKENS = 80
_DIALOGUE_LINES_KEPT = 12
_DIALOGUE_LINE_MAX_CHARS = 200


class ProactiveTopicLoop:
    """Background candidate refresh plus foreground idle trigger."""

    def __init__(
        self,
        side: SideModel | None,
        store: MemoryStore,
        floor: SpeakingFloor,
        clock: Clock,
        *,
        submit: Callable[[Intent], bool | None],
        prompt: str,
        idle_threshold_s: float,
        wake_interval_s: float = 30.0,
        max_per_hour: int = 12,
        max_tokens: int = 120,
        assistant_label: str = "助手",
        event_pacer: EventPacer | None = None,
        ordinary_pending: Callable[[], bool] | None = None,
        reply_base_instructions: Callable[[], str] | None = None,
        collection_window_s: float = 120.0,
        revoke: Callable[[str], bool] | None = None,
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
        # How the side prompt labels her lines. Config-owned, not hardcoded:
        # the shipped personas answer to different names.
        self._assistant_label = assistant_label
        self._event_pacer = event_pacer
        self._ordinary_pending = ordinary_pending or (lambda: False)
        self._reply_base_instructions = reply_base_instructions or (lambda: "")
        self._opportunities = ProactiveOpportunities(clock, collection_window_s=collection_window_s)
        self._silenced = False
        self._revoke = revoke or (lambda _key: False)
        self._submitted_opinions: OpinionSummary | None = None
        self._submitted_opportunities: OrderedDict[str, frozenset[str]] = OrderedDict()

        self._candidate: str | None = None
        self._candidate_revision: int | None = None
        self._replay_context: str | None = None
        self._replay_events: deque[str] = deque(maxlen=20)
        self._fingerprint = ""
        self._last_activity = clock.monotonic()
        self._last_refresh = -wake_interval_s  # first refresh happens on tick one
        self._submitted: deque[float] = deque()
        self._dialogue: deque[tuple[Literal["streamer", "assistant"], str]] = deque(
            maxlen=_DIALOGUE_LINES_KEPT
        )
        self._refresh_task: asyncio.Task[None] | None = None
        self._topics_produced = 0
        self._fallback_topics = 0
        self._unanswered_count = 0
        self._awaiting_response = False
        # The hourly cap is re-checked once a second while a topic sits ready,
        # so the block is a STATE, not an event. Latched so the log records the
        # flip into it once instead of once per tick.
        self._budget_blocked = False

    # ------------------------------------------------------------ inputs

    async def reset_for_replay(self, context: str | None) -> None:
        """Discard stale candidates and isolate side-model input per case."""
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            await asyncio.gather(self._refresh_task, return_exceptions=True)
            self._refresh_task = None
        self._revoke_submitted_collection()
        for key in tuple(self._submitted_opportunities):
            self._revoke(key)
        self._submitted_opportunities.clear()
        self._replay_context = context
        self._opportunities.reset()
        self._silenced = False
        self._replay_events.clear()
        self._dialogue.clear()
        self._candidate = None
        self._fingerprint = ""
        self._submitted.clear()
        self._unanswered_count = 0
        self._awaiting_response = False
        self._budget_blocked = False
        self._last_activity = self._clock.monotonic()
        self._last_refresh = self._last_activity - self._wake_interval_s

    def note_replay_event(self, event: LiveEvent) -> None:
        """Keep only events actually received after this case began."""
        if self._replay_context is not None:
            self._replay_events.append(
                f"[{event.kind.value}] {event.viewer.name or '观众'}: {event.text}"
            )

    def note_event(self, event: LiveEvent) -> None:
        """Observe facts without turning host messages into audience activity."""
        self._opportunities.note_event(event)

    def set_silenced(self, silenced: bool) -> None:
        self._silenced = silenced
        if silenced:
            self._candidate = None
            self._fingerprint = ""
            self._opportunities.clear_interrupted()
            for key in tuple(self._submitted_opportunities):
                self._revoke(key)
            self._submitted_opportunities.clear()

    def collect_opinions(self, topic: str, *, started_at: float | None = None) -> None:
        self._opportunities.collect_opinions(topic, started_at=started_at)
        self._revoke_submitted_collection()

    def request_danmaku_summary(self, *, before_at: float | None = None) -> None:
        """Arm the separate voice-requested backlog-summary task."""
        self._opportunities.request_danmaku_summary(before_at=before_at)

    def cancel_danmaku_summary(self) -> None:
        self._opportunities.cancel_danmaku_summary()

    def danmaku_summary_intent(
        self, events: tuple[LiveEvent, ...] | None = None, *, now: float
    ) -> Intent | None:
        """Build one event-lane reply for the newest pre-voice danmaku."""
        summary = self._opportunities.due_danmaku_summary(events)
        if summary is None:
            return None
        return self._danmaku_summary_intent(summary, now=now)

    def complete_danmaku_summary(self, key: str) -> None:
        self._opportunities.complete_danmaku_summary(key)

    def mark_danmaku_summary_used(self, refs: set[str]) -> None:
        """Keep summarized backlog out of later proactive material."""
        self._opportunities.mark_used(refs)

    def _danmaku_summary_intent(self, summary: DanmakuSummary, *, now: float) -> Intent:
        return Intent(
            source="danmaku_summary",
            priority=Priority.DANMAKU_SUMMARY,
            injection=Injection(
                reply=ReplySpec(
                    base_instructions=self._reply_base_instructions() or None,
                    instructions=(
                        EVENT_DECISION_RULES
                        + (
                            "这是主播在这一轮语音中明确委托的弹幕总结，不是普通单条弹幕回复。"
                            "只处理输入中列出的、发生在该轮语音开始前且尚未处理的最新观众弹幕；"
                            "不要把共享历史、语音开始后的新弹幕或已经处理的记录混进来，也不要把观众互聊"
                            "当成主播问题。先在这些候选里判断哪个话题最近仍在升温：优先考虑近期反复提及、"
                            "多人围绕同一问题追问或出现明确观点分歧的主题；孤立的一条、纯互聊和低信息内容不要选。"
                            "只围绕选出的一个最热话题，告诉主播共同问题、关键分歧或值得知道的信息；"
                            "不逐条复读、不编造人数或共识。没有形成可判断的热议话题时只输出[SKIP]及简短原因。"
                            "这是交付主播委托，不重新征集、不机械追问主播；遵循当前回复长度档位。"
                        )
                    ),
                    max_tokens=self._max_tokens,
                    write_history=True,
                ),
                item_text=summary.item_text,
            ),
            trusted=False,
            dedup_key=summary.key,
            created_at=now,
            expires_at=now + _TOPIC_TTL_S,
            events=summary.events,
        )

    def cancel_collection(self) -> None:
        self._opportunities.cancel_collection()
        self._revoke_submitted_collection()

    def _revoke_submitted_collection(self) -> OpinionSummary | None:
        summary = self._submitted_opinions
        self._submitted_opinions = None
        if summary is not None and self._revoke(summary.key):
            return summary
        return None

    def finish_collection(self) -> None:
        """Request early delivery; the next tick still respects the speaking floor."""
        self._opportunities.finish_collection()

    def note_interrupted(
        self,
        key: str,
        source: str,
        background: str,
        partial_text: str,
        *,
        event_refs: tuple[str, ...] = (),
    ) -> None:
        if self._silenced:
            return
        self._opportunities.note_interrupted(
            key, source, background, partial_text, event_refs=event_refs
        )
        self._fingerprint = ""

    def discard_events(self, refs: set[str]) -> None:
        self._opportunities.discard_events(refs)
        self._candidate = None
        self._fingerprint = ""
        for key, dependencies in tuple(self._submitted_opportunities.items()):
            if refs.intersection(dependencies):
                self._submitted_opportunities.pop(key, None)
                self._revoke(key)
        summary = self._submitted_opinions
        if summary is not None and refs.intersection(summary.event_refs):
            withdrawn = self._revoke_submitted_collection()
            if withdrawn is not None:
                self._opportunities.restore_collection(withdrawn)

    def note_verdict(self, verdict: Verdict) -> None:
        """Do not resurrect completed speech when a later host answer arrives."""
        self._submitted_opportunities.pop(verdict.intent_id, None)
        summary = self._submitted_opinions
        if summary is not None and summary.key == verdict.intent_id:
            self._submitted_opinions = None

    def note_activity(self, *, responds_to_topic: bool = False) -> None:
        """Record one live-room event and, when applicable, a topic response."""
        self._last_activity = self._clock.monotonic()
        if responds_to_topic:
            self._note_response()

    def note_dialogue(self, role: Literal["streamer", "assistant"], text: str) -> None:
        """Keep bounded, completed dialogue as topic-candidate material.

        Voice is material, not activity: with a pacer wired, a streamer
        monologue enriches the next candidate without resetting the
        live-event dead-air clock. Without one (legacy wiring) the old
        behaviour stands.
        """
        line = " ".join(text.split())[:_DIALOGUE_LINE_MAX_CHARS]
        if not line:
            return
        self._dialogue.append((role, line))
        if role == "streamer":
            self._note_response()
        if self._event_pacer is None:
            self.note_activity()

    def _note_response(self) -> None:
        """Reset the consecutive miss count after human engagement."""
        if not self._awaiting_response:
            return
        self._awaiting_response = False
        self._unanswered_count = 0

    def configure(
        self,
        *,
        prompt: str | None = None,
        idle_threshold_s: float | None = None,
        wake_interval_s: float | None = None,
        max_per_hour: int | None = None,
        max_tokens: int | None = None,
        assistant_label: str | None = None,
        reply_base_instructions: Callable[[], str] | None = None,
        collection_window_s: float | None = None,
    ) -> None:
        """Apply control-centre settings to future topic work."""
        if prompt is not None:
            self._prompt = prompt
            self._fingerprint = ""
        if idle_threshold_s is not None:
            self._idle_threshold_s = idle_threshold_s
        if wake_interval_s is not None:
            self._wake_interval_s = wake_interval_s
        if max_per_hour is not None:
            self._max_per_hour = max_per_hour
        if max_tokens is not None:
            self._max_tokens = max_tokens
        if assistant_label is not None:
            self._assistant_label = assistant_label
        if reply_base_instructions is not None:
            self._reply_base_instructions = reply_base_instructions
        if collection_window_s is not None:
            self._opportunities.configure(collection_window_s)

    # ------------------------------------------------------------ the loop

    async def run(self) -> None:
        """Tick once a second on the injected clock. Cancel to stop."""
        if self._side is None:
            # Realtime can still generate directly from its shared context;
            # only the cheap background preselection is unavailable. Reported
            # once here and permanently in status() (plan section 7.6).
            log.warning("proactive.side_model_missing_fallback")
        while True:
            await self._clock.sleep(_TICK_S)
            self._tick()

    def _tick(self) -> None:
        now = self._clock.monotonic()
        self._opportunities.expire()
        if (
            self._candidate_revision is not None
            and self._candidate_revision != self._opportunities.revision
        ):
            self._candidate = None
            self._fingerprint = ""
        if self._silenced:
            return
        if self._floor.is_blocked():
            # Voice owns the floor but — with a pacer — does not rewrite the
            # live-event clock. Once the floor is free, event pacing decides
            # whether a topic is due.
            if self._event_pacer is None:
                self._last_activity = now
            return
        if self._submit_opinion_summary(now):
            return
        if self._side is not None and now - self._last_refresh >= self._wake_interval_s:
            self._last_refresh = now
            self._spawn_refresh()
        pacing = self._event_pacer.snapshot() if self._event_pacer is not None else None
        if pacing is not None and not pacing.proactive_enabled:
            return
        idle_threshold = pacing.proactive_idle_s if pacing is not None else self._idle_threshold_s
        if now - self._last_activity < idle_threshold:
            return
        if self._event_pacer is None and self._candidate is None:
            # Legacy wiring has no fallback path: no candidate, no topic.
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
        if self._ordinary_pending():
            # A danmaku window or a pending welcome is about to speak: real
            # interaction outranks an icebreaker, before either is queued.
            return
        if self._event_pacer is not None and not self._event_pacer.try_consume("proactive"):
            return
        self._speak(now)

    def _submit_opinion_summary(self, now: float) -> bool:
        summary = self._opportunities.due_collection()
        if summary is None:
            return False
        intent = Intent(
            source="proactive",
            priority=Priority.PROACTIVE,
            injection=Injection(
                reply=ReplySpec(
                    base_instructions=self._reply_base_instructions() or None,
                    instructions=(
                        "主播先前向观众征集观点，现在交付这次征集的总结。只归纳本次记录中"
                        "与征集话题有关的观众意见；主播文字、观众纯私聊、已由主播处理和"
                        "低信息弹幕不算有效观点。不要把更早的共享历史充当本次征集反馈。"
                        "简短告诉主播共同关注点和不同角度，不逐条复读，不虚构人数或共识。"
                        "没有足够依据就说明有限；无有效反馈时只输出[SKIP]及简短原因。"
                        "这是交付总结，不重新征集，不机械追问主播；遵循当前回复长度档位。"
                    ),
                    max_tokens=self._max_tokens,
                    write_history=True,
                ),
                item_text=summary.item_text,
            ),
            trusted=True,
            dedup_key=summary.key,
            created_at=now,
            expires_at=now + _TOPIC_TTL_S,
        )
        self._submitted_opinions = summary
        try:
            self._submit(intent)
        except (OSError, RuntimeError, ValueError):
            self._submitted_opinions = None
            raise
        self._opportunities.complete_collection(summary.key)
        self._last_activity = now
        return True

    def _speak(self, now: float) -> None:
        # The candidate came out of a side model that READ audience danmaku —
        # a second-order injection channel (A14). Flatten whitespace so it
        # cannot fake prompt structure, break wrapper tokens, cap the length;
        # the instructions text around it stays a fixed template.
        candidate = neutralize_tags(" ".join((self._candidate or "").split()))[:80]
        if candidate:
            topic_material = f"- 后台结合共享历史选出的候选话题是：{candidate}\n\n"
        else:
            self._fallback_topics += 1
            topic_material = (
                "- 后台候选暂不可用。请直接从共享历史中的最近对话、直播事件、"
                "直播简介、本场进展和记忆里选择话题。\n\n"
            )
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
        if self._awaiting_response:
            self._unanswered_count += 1
        current_time = self._clock.wall().astimezone().strftime("%Y-%m-%d %H:%M")
        unanswered = self._unanswered_count
        self._awaiting_response = True
        opportunity_refs = self._opportunities.material_event_refs()
        opportunities = self._opportunities.material(consume_interrupted=True)
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
            fallback=not candidate,
            idle_s=idle_s,
            unanswered=unanswered,
            topics_this_hour=len(self._submitted),
        )
        key = f"proactive:{int(now)}"
        if opportunity_refs:
            self._submitted_opportunities[key] = opportunity_refs
            while len(self._submitted_opportunities) > 64:
                self._submitted_opportunities.popitem(last=False)
        intent = Intent(
            source="proactive",
            priority=Priority.PROACTIVE,
            injection=Injection(
                reply=ReplySpec(
                    base_instructions=self._reply_base_instructions() or None,
                    instructions=(
                        "[系统任务：直播间主动破冰]\n"
                        "你被授权在直播间发起一次主动消息以活跃气氛。回复必须符合公共人设和"
                        "全部输出规则。\n\n"
                        "[情景分析]\n"
                        "- 直播间已经出现一段自然空档，可以主动接起一个话题。\n"
                        f"- 当前时间是：{current_time}。\n"
                        "- 前面主动开口后，没有收到主播语音或观众弹幕/SC 回应的连续"
                        f"次数是：连续 {unanswered} 次。\n"
                        f"{topic_material}"
                        "[行动指南]\n"
                        "1. 回顾共享历史中的最近对话、直播事件、直播简介和本场进展；"
                        "如果有没聊完的内容，优先自然延续。\n"
                        "2. 可以先表达自己的观察、判断、联想或轻吐槽，再向整个直播间抛出一个"
                        "低门槛的开放性问题，让主播和观众都能接。不要只点名追问主播。\n"
                        "3. 如果连续无人回应次数大于零，换一个角度并降低参与门槛，不重复上一次"
                        "的问题，也不责怪任何人没有回应。\n"
                        "4. 不要说冷场、暖场、大家还在吗或接下来聊什么；不报节目单，不重复最近"
                        "已经发起过的话题。\n"
                        "5. 有未解决的中断候选时，先简短回顾原问题或事件背景，再自然续接；"
                        "不要把旧事当作刚发生，也不恢复已处理、被拒绝或因静默停止的内容。\n"
                        "6. 可以从近期有效弹幕中找出共同话题或不同观点，面向其他观众深入讨论；"
                        "纯观众互聊、已处理和无实质信息的内容不作为话题。由你结合共享上下文"
                        "判断，不因话题接近就假定已回答。没有合适内容时只输出[SKIP]及简短原因。\n\n"
                        "[最终指令]\n"
                        "用最符合人设、最自然的方式，说一到两句能打破空档的开场白。只输出要说的"
                        "话，不解释任务和规则。"
                    ),
                    max_tokens=self._max_tokens,
                    write_history=True,
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
                item_text=_DEAD_AIR_ITEM + ("\n" + opportunities if opportunities else ""),
            ),
            trusted=True,
            dedup_key=key,
            created_at=now,
            expires_at=now + _TOPIC_TTL_S,
        )
        accepted = self._submit(intent)
        if accepted is False:
            self._submitted_opportunities.pop(key, None)
            return
        # A successful submission consumes the factual event material used to
        # build this opening. The rows stay in MemoryStore for distillation;
        # only the proactive candidate pool is advanced.
        self._opportunities.mark_used(opportunity_refs)

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
        if self._replay_context is None:
            events = self._store.recent_events(
                limit=20, exclude_lines=self._opportunities.used_event_lines()
            )
            rows = self._store.facts("stream", str(self._store.stream_id))
            summary = rows[-1].text if rows else ""
        else:
            events = list(self._replay_events)
            summary = self._replay_context
        dialogue = [
            f"{'主播' if role == 'streamer' else self._assistant_label}：{text}"
            for role, text in self._dialogue
        ]
        opportunities = self._opportunities.material()
        revision = self._opportunities.revision
        fingerprint = hashlib.sha256(
            "\n".join([summary, *events, *dialogue, opportunities]).encode()
        ).hexdigest()
        if fingerprint == self._fingerprint:
            return
        try:
            current_time = self._clock.wall().astimezone().strftime("%Y-%m-%d %H:%M")
            raw = await self._side.complete(
                system=self._prompt,
                user=(
                    "[情景分析]\n"
                    f"当前时间：{current_time}\n"
                    f"连续无人回应次数：{self._unanswered_count}\n"
                    f"本场进展：{summary or '（刚开播，还没有进展）'}\n"
                    f"最近主播与{self._assistant_label}的对话：\n"
                    f"{chr(10).join(dialogue) or '（还没有）'}\n"
                    f"最近弹幕和事件：\n{chr(10).join(events) or '（还没有）'}\n"
                    f"可选互动契机：\n{opportunities or '（还没有）'}\n"
                    "选题时结合话题、对象和处理状态，由你排除纯观众互聊、已处理和低信息内容；"
                    "可以选有意义的观众讨论，也可选未重排且未解决的中断内容，不恢复静默停止的内容。"
                ),
                max_tokens=_CANDIDATE_MAX_TOKENS,
            )
        except SideModelError as exc:
            log.warning("proactive.refresh_failed", error_text=str(exc))
            return
        if self._silenced or revision != self._opportunities.revision:
            return
        topic = raw.strip().splitlines()[0].strip() if raw.strip() else ""
        if topic:
            self._fingerprint = fingerprint
            self._candidate = topic
            self._candidate_revision = revision
            # A ready candidate and a spoken one are different states, and the
            # gap between them is where "she never says anything on her own"
            # gets diagnosed: no topic_ready means the side model came back
            # empty, topic_ready without topic_submitted means the floor was
            # never idle long enough.
            log.info("proactive.topic_ready", topic_text=topic, event_count=len(events))

    # ------------------------------------------------------------ health

    def status(self) -> dict[str, Any]:
        status: dict[str, Any] = {
            "side_configured": self._side is not None,
            "candidate_ready": self._candidate is not None,
            "topics_this_hour": len(self._submitted),
            "topics_produced": self._topics_produced,
            "fallback_topics": self._fallback_topics,
            "unanswered_count": self._unanswered_count,
        }
        status.update(self._opportunities.status())
        status["silenced"] = self._silenced
        if self._event_pacer is not None:
            pacing = self._event_pacer.snapshot()
            status["activity"] = pacing.activity.value
            status["effective_idle_s"] = round(pacing.proactive_idle_s, 2)
        return status
