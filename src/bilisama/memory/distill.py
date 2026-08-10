"""Tier 1: LLM distillation, background only, voice loop never awaits it.

Two calls per stream in the normal case (plan section 4.7): a rolling
session-progress rewrite every N adopted events, and one end-of-stream batch
that produces viewer facts, the final summary, and — when a growth switch is
not off — the two growth layers in the same call. Cost disciplines from the
plan: event-count triggering, fingerprint skip, hard budgets that drop whole
items with a warning instead of truncating silently.

The growth path is the drift boundary: candidates pass the output guard
before touching disk, merges enforce budgets and the per-stream swap cap, and
nothing in this module can name an anchor file — persona.loader owns those
and gives the distiller no way in.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bilisama.config.enums import GrowthMode
from bilisama.memory.store import MemoryStore, logical_date
from bilisama.obs.logging import get_logger
from bilisama.persona.growth import merge_relationship, merge_voice
from bilisama.persona.loader import PersonaStore
from bilisama.side import SideModel, SideModelError

if TYPE_CHECKING:
    from bilisama.clock import Clock
    from bilisama.config.schema import GrowthSwitches

__all__ = ["DistillReport", "Distiller"]

log = get_logger(__name__)

_SUMMARY_MAX_CHARS = 200
_ENTRY_MAX_CHARS = 60
_ASSISTANT_LINES_KEPT = 40

_SYSTEM = "你是直播伴播的后台记忆整理器。不要调用工具，不要对任何人说话，只输出要求的内容。"

_ROLLING_TEMPLATE = (
    "下面是本场直播的进展摘要和最近的事件。把摘要改写成新版本：\n"
    "- 不超过 200 字，装的是「这场直播到现在发生了什么」。\n"
    "- 旧摘要里已经过时的内容可以扔，正在发生的事往前放。\n"
    "- 只输出摘要正文，不解释。\n\n当前摘要：{summary}\n\n最近事件：\n{events}"
)

_BATCH_TEMPLATE = (
    "这场直播结束了。根据材料做一次记忆整理，只输出一个 JSON 对象。\n"
    "可以记的：观众的稳定偏好和身份线索；观众和主播之间的约定或梗；对下次直播有用的事。\n"
    "不要记的：一次性的寒暄和 666；某句话的原样复述；敏感个人信息（真名、住址、联系方式）；"
    "数字和价格的堆砌；你自己的推测；和直播无关的时事。\n"
    "每条不超过 40 字。viewer_facts 每人至多一条，带 2~5 个标签；identity 只能从材料里抄。\n"
    "没有值得记的就给空数组——留白完全可以。\n"
    "{growth_rules}"
    '输出格式：{{"viewer_facts": [{{"identity": "uid:123", "fact": "...", "tags": ["..."]}}], '
    '"session_summary": "...", "relationship": ["..."], "voice": ["..."]}}\n\n'
    "本场观众（identity 名单）：\n{viewers}\n\n本场进展摘要：{summary}\n\n"
    "最近事件：\n{events}\n\n伴播自己说过的话（完整播出、没被打断的）：\n{assistant_lines}"
)

_RELATIONSHIP_RULE = (
    "relationship：从材料里挑 0~3 条值得长期记住的共同经历（外号、约定、名场面），一句一条。\n"
)
_VOICE_RULE = "voice：从「伴播自己说过的话」里挑 0~2 句最有个人味道的当口癖样本，必须原句照抄。\n"


def _log_task_failure(task: asyncio.Task[Any]) -> None:
    """Fire-and-forget must not mean fail-and-vanish (B1): a crashed rolling
    task logs instead of waiting for the garbage collector to mumble."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.warning("distill.rolling_crashed", error=str(exc)[:200])


@dataclass(frozen=True, slots=True)
class DistillReport:
    """What one distillation attempt did — health and tests read this."""

    ran: bool
    reason: str
    dropped: tuple[str, ...] = ()


@dataclass(slots=True)
class _State:
    events_since: int = 0
    fingerprint: str = ""
    assistant_lines: list[str] = field(default_factory=list)
    inflight: asyncio.Task[DistillReport] | None = None
    # Stream ids whose end-of-stream batch already ran: the once-latch (B13).
    batch_done: set[int] = field(default_factory=set)


class Distiller:
    """Owns the two side calls and everything they are allowed to write."""

    def __init__(
        self,
        side: SideModel | None,
        store: MemoryStore,
        persona: PersonaStore,
        growth: GrowthSwitches,
        clock: Clock,
        *,
        every_n_events: int = 40,
        guard: Callable[[str], bool] | None = None,
    ) -> None:
        self._side = side
        self._store = store
        self._persona = persona
        self._growth = growth
        self._clock = clock
        self._every_n = every_n_events
        # Returns True when a text must not land anywhere (same callable shape
        # the scheduler's output guard uses).
        self._guard = guard
        self._state = _State()

    # ------------------------------------------------------------ intake

    def note_event(self) -> None:
        """Count one adopted event; fire the rolling rewrite at the threshold.

        Fire-and-forget on purpose — the caller is the assembly's emit path
        and must never wait on an LLM.
        """
        self._state.events_since += 1
        if self._state.events_since < self._every_n:
            return
        if self._state.inflight is not None and not self._state.inflight.done():
            return  # one call at a time; the counter keeps accruing
        self._state.events_since = 0
        task = asyncio.create_task(self.rolling_summary(), name="distill:rolling")
        task.add_done_callback(_log_task_failure)
        self._state.inflight = task

    def note_assistant_line(self, text: str) -> None:
        """A cleanly spoken reply — voice-exemplar raw material. The caller
        only feeds lines that completed without a guard hit."""
        line = text.strip()
        if not line:
            return
        self._state.assistant_lines.append(line)
        del self._state.assistant_lines[:-_ASSISTANT_LINES_KEPT]

    # ------------------------------------------------------------ the two calls

    async def rolling_summary(self) -> DistillReport:
        """Rewrite the ≤200-char session progress from recent events."""
        if self._side is None:
            return DistillReport(ran=False, reason="no_side_model")
        sid = self._store.stream_id
        events = self._store.recent_events(limit=30)
        summary = self._session_summary()
        # Fingerprint the EVENTS only: the summary is this call's own output,
        # and hashing it too would invalidate the fingerprint on every write.
        fingerprint = hashlib.sha256("\n".join(events).encode()).hexdigest()
        if fingerprint == self._state.fingerprint:
            return DistillReport(ran=False, reason="fingerprint_unchanged")
        try:
            raw = await self._side.complete(
                system=_SYSTEM,
                user=_ROLLING_TEMPLATE.format(
                    summary=summary or "（还没有）", events="\n".join(events)
                ),
                max_tokens=300,
            )
        except SideModelError as exc:
            log.warning("distill.rolling_failed", error=str(exc))
            return DistillReport(ran=False, reason="side_error")
        if self._store.stream_id != sid or sid in self._state.batch_done:
            # The stream ended (or its batch already ran) while we were on the
            # wire: a late rolling write would overwrite the final summary or
            # land under a dead stream id (B1).
            return DistillReport(ran=False, reason="stream_moved_on")
        self._state.fingerprint = fingerprint
        new_summary = self._flatten(raw)
        if self._blocked(new_summary):
            log.warning("distill.summary_blocked")
            return DistillReport(ran=False, reason="summary_blocked")
        new_summary, clipped = _clip(new_summary, _SUMMARY_MAX_CHARS)
        if clipped:
            log.warning("distill.summary_clipped", chars=len(raw.strip()))
        self._store.replace_facts("stream", str(sid), [(new_summary, "")])
        return DistillReport(ran=True, reason="ok")

    async def end_of_stream(self) -> DistillReport:
        """The batch call. Run BEFORE MemoryStore.end_stream() — it reads this
        stream's viewers and events by the still-open stream id. Runs at most
        once per stream (B13) and retries a transient failure once (B18)."""
        if self._side is None:
            return DistillReport(ran=False, reason="no_side_model")
        sid = self._store.stream_id
        if sid in self._state.batch_done:
            return DistillReport(ran=False, reason="already_ran")
        # A rolling rewrite still on the wire would race this call's final
        # summary (B1): cancel it and wait it out before writing anything.
        inflight = self._state.inflight
        if inflight is not None and not inflight.done():
            inflight.cancel()
            await asyncio.gather(inflight, return_exceptions=True)
        viewers = self._store.top_viewers(limit=20)
        rules = "".join(
            (
                (
                    _RELATIONSHIP_RULE
                    if self._growth.relationship is not GrowthMode.OFF
                    else "relationship 固定给空数组。\n"
                ),
                (
                    _VOICE_RULE
                    if self._growth.voice is not GrowthMode.OFF
                    else "voice 固定给空数组。\n"
                ),
            )
        )
        user = _BATCH_TEMPLATE.format(
            growth_rules=rules,
            viewers="\n".join(
                f"{v.identity} {v.uname}（来过 {v.streams_seen} 次，"
                f"发言 {v.msg_count}，礼物 ¥{v.gift_value_cny:.0f}）"
                for v in viewers
            )
            or "（没有跨过门槛的观众）",
            summary=self._session_summary() or "（无）",
            events="\n".join(self._store.recent_events(limit=30)) or "（无）",
            assistant_lines="\n".join(self._state.assistant_lines) or "（无）",
        )
        raw = ""
        for attempt in (1, 2):
            try:
                raw = await self._side.complete(system=_SYSTEM, user=user, max_tokens=900)
                break
            except SideModelError as exc:
                log.warning("distill.batch_failed", attempt=attempt, error=str(exc))
                if attempt == 2:
                    return DistillReport(ran=False, reason="side_error")
                # One short-backoff retry: this is the highest-value call of
                # the whole stream and it has no natural second chance (B18).
                await self._clock.sleep(2.0)
        payload = _parse_json(raw)
        if payload is None:
            log.warning("distill.batch_unparseable", head_text=raw[:120])
            return DistillReport(ran=False, reason="bad_json")
        dropped = self._apply(payload, {v.identity for v in viewers})
        self._state.batch_done.add(sid)
        # Fresh state for the next stream (B14): last stream's spoken lines
        # must not become next stream's voice candidates. The latch survives.
        latch = self._state.batch_done
        self._state = _State(batch_done=latch)
        return DistillReport(ran=True, reason="ok", dropped=tuple(dropped))

    # ------------------------------------------------------------ applying

    def _apply(self, payload: dict[str, Any], known_identities: set[str]) -> list[str]:
        dropped: list[str] = []

        for item in _as_list(payload.get("viewer_facts")):
            if not isinstance(item, dict):
                continue
            identity = str(item.get("identity") or "")
            fact = str(item.get("fact") or "").strip()
            tags = ",".join(str(t) for t in _as_list(item.get("tags")) if t)
            if identity not in known_identities:
                dropped.append(f"viewer_fact:unknown_identity:{identity}")
                continue
            if not fact or len(fact) > _ENTRY_MAX_CHARS or self._blocked(fact):
                dropped.append(f"viewer_fact:{identity}")
                continue
            self._store.replace_facts("viewer", identity, [(fact, tags)])

        summary = str(payload.get("session_summary") or "").strip()
        if summary:
            clean, clipped = _clip(summary, _SUMMARY_MAX_CHARS)
            if clipped:
                log.warning("distill.summary_clipped", chars=len(summary))
            self._store.replace_facts("stream", str(self._store.stream_id), [(clean, "")])

        self._apply_growth("relationship", _as_list(payload.get("relationship")), dropped)
        self._apply_growth("voice", _as_list(payload.get("voice")), dropped)

        for item in dropped:
            log.warning("distill.entry_dropped", entry_text=item)
        return dropped

    def _apply_growth(self, layer: str, raw_entries: list[Any], dropped: list[str]) -> None:
        mode = self._growth.relationship if layer == "relationship" else self._growth.voice
        if mode is GrowthMode.OFF:
            return  # off means off: nothing distilled, nothing written
        entries: list[str] = []
        for raw in raw_entries:
            # Flatten first: an embedded newline survives the length check but
            # gets silently eaten by the bullet parser on the way back (B6).
            entry = self._flatten(str(raw))
            if not entry or len(entry) > _ENTRY_MAX_CHARS or self._blocked(entry):
                if entry:
                    dropped.append(f"{layer}:{entry[:20]}")
                continue
            entries.append(entry)
        if not entries:
            return
        if layer == "relationship":
            date = logical_date(self._clock.wall()).date().isoformat()
            fresh = [f"{date} {entry}" for entry in entries]
            existing = self._persona.growth_entries("relationship")
            merged = merge_relationship(existing, fresh)
            self._warn_trimmed(layer, existing, fresh, merged)
            self._persona.write_growth("relationship", merged)
        else:
            existing = self._persona.growth_entries("voice")
            merged = merge_voice(existing, entries)
            self._persona.write_growth("voice", merged)

    @staticmethod
    def _flatten(text: str) -> str:
        return " ".join(text.split())

    @staticmethod
    def _warn_trimmed(layer: str, existing: list[str], fresh: list[str], merged: list[str]) -> None:
        # Budget trims must not be silent (plan section 4.7).
        added = sum(1 for entry in fresh if entry in merged and entry not in existing)
        trimmed = len(existing) + added - len(merged)
        if trimmed > 0:
            log.warning("distill.growth_trimmed", layer=layer, count=trimmed)

    # ------------------------------------------------------------ helpers

    def _growth_enabled(self) -> bool:
        return (
            self._growth.relationship is not GrowthMode.OFF
            or self._growth.voice is not GrowthMode.OFF
        )

    def _session_summary(self) -> str:
        rows = self._store.facts("stream", str(self._store.stream_id))
        return rows[-1].text if rows else ""

    def _blocked(self, text: str) -> bool:
        return self._guard is not None and self._guard(text)


def _clip(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _parse_json(raw: str) -> dict[str, Any] | None:
    """Parse the model's JSON, tolerating markdown fences around it."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None
