"""The log vocabulary, frozen.

Event names are the grouping key for everything downstream: the panel's log
pane, whatever reads the rolling file afterwards, and section 2.8's latency
probes, which reuse these same names. That makes them an append-only
vocabulary — the same discipline `SkipReason` (obs/outcome.py) and the UI's
`ServerEvent` (ui/events.py) already carry, and for the same reason: a rename
is silent everywhere except in the one place someone is grepping a week later.

Adding a name is one line here. Renaming one is a red test, on purpose. If a
name really has to go, delete the line and say why in the commit — that at
least leaves a record that the old name meant something once.

The scan is deliberately structural rather than a grep: `log.info(f"...")`
should never appear, and an AST walk is what makes the difference between a
constant and a formatted string visible.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src" / "bilisama"
_METHODS = frozenset({"debug", "info", "warning", "error", "exception"})
_LOGGERS = frozenset({"log", "logger", "_log"})

KNOWN_EVENTS: frozenset[str] = frozenset(
    {
        "assembly.context_push_failed",
        "assembly.context_pushed",
        "assembly.anchor_context_write_failed",
        "assembly.event_observer_failed",
        "assembly.started",
        "audio.claim_refused",
        "audio.claimed",
        "audio.displaced",
        "audio.released",
        "bilibili.client_stopped",
        "bilibili.close_failed",
        "bilibili.connected",
        "bilibili.connecting",
        "bilibili.credential_stale",
        "bilibili.map_failed",
        "bilibili.sc_withdrawn_before_emit",
        "bilibili.vendor_drift",
        "bootstrap.s2s_config_written",
        "bootstrap.s2s_keys_unknown",
        "bootstrap.s2s_reconcile_unavailable",
        "config.migrated: %s",
        "dev_talk.assistants_unreadable",
        "dev_talk.skins_unreadable",
        "dev_talk.console_injected",
        "dev_talk.distill_done",
        "dev_talk.distill_failed",
        "dev_talk.link_error",
        "dev_talk.memory_close_failed",
        "dev_talk.memory_finalize_failed",
        "dev_talk.pet_not_installed",
        "dev_talk.pet_spawn_failed",
        "dev_talk.pet_started",
        "dev_talk.playback_cleared",
        "dev_talk.session_started",
        "dev_talk.side_close_failed",
        "dev_talk.speaker_close_failed",
        "dev_talk.speaker_probe_failed",
        "dev_talk.speaker_unavailable",
        "dev_talk.speech_close_failed",
        "dev_talk.task_died",
        "dev_talk.turn_done",
        "dev_talk.ui_close_failed",
        "dev_talk.ui_endpoint_file_unwritable",
        "dev_talk.ui_port_unavailable",
        "dev_talk.ui_started",
        "dev_talk.uplink_resumed",
        "distill.batch_applied",
        "distill.batch_failed",
        "distill.batch_started",
        "distill.batch_unparseable",
        "distill.entry_dropped",
        "distill.growth_trimmed",
        "distill.rolling_crashed",
        "distill.rolling_done",
        "distill.rolling_failed",
        "distill.rolling_started",
        "distill.summary_blocked",
        "distill.summary_clipped",
        "floor.cooldown_started",
        "floor.link_lost",
        "floor.playback_edge",
        "floor.speech_edge_expected",
        "floor.speech_started",
        "floor.speech_stopped",
        "guard.hit_allowed",
        "guard.loaded",
        "guard.word_hit",
        "hosted.bootstrap_sent",
        "hosted.protection_unsupported",
        "hosted.session_replayed",
        "hosted.suspended",
        "hosted.resumed",
        "intents.built",
        "intents.no_speaking_path",
        "link.fatal",
        "link.reconnect_already_running",
        "link.reconnect_died_on_arrival",
        "link.reconnect_failed",
        "link.reconnect_gave_up",
        "link.reconnected",
        "link.reply_created",
        "link.reply_done",
        "link.reply_first_frame",
        "link.reply_requested",
        "link.rotating",
        "link.slot_freed",
        "link.slot_taken",
        "link.slot_waited",
        "loop.lag",
        "memory.facts_written",
        "memory.opened",
        "memory.segments_built",
        "memory.stream_begun",
        "memory.stream_ended",
        "persona.anchor_loaded",
        "persona.growth_lock_contended",
        "persona.hand_edited_unreadable",
        "persona.proactive_prompt_loaded",
        "persona.prompt_assembled",
        "proactive.budget_exhausted",
        "proactive.side_model_missing_fallback",
        "proactive.refresh_failed",
        "proactive.topic_ready",
        "proactive.topic_submitted",
        "s2s.protection_armed",
        "s2s.protection_ended",
        "s2s.rearm_deferred",
        "s2s.session_replayed",
        "s2s.suspended",
        "s2s.resumed",
        "safety.breaker_opened",
        "safety.breaker_reset",
        "safety.combo_settled",
        "safety.combo_suppressed",
        "safety.dedup_hit",
        "scheduler.barge_survived",
        "scheduler.barged_in",
        "scheduler.dedup_dropped",
        "scheduler.dispatch_failed",
        "scheduler.dispatch_retry",
        "scheduler.dispatched",
        "scheduler.end_protection_failed",
        "scheduler.event_failed",
        "scheduler.gate_blocked",
        "scheduler.history_write_failed",
        "scheduler.panic_muted",
        "scheduler.panic_released",
        "scheduler.preempted",
        "scheduler.protection_ended",
        "scheduler.settled",
        "scheduler.submitted",
        "scheduler.task_failed",
        "scheduler.verdict",
        "scoring.danmaku_scored",
        "selector.advance_failed",
        "selector.breaker_open",
        "selector.deferred_released",
        "selector.skip_sink_failed",
        "selector.skipped",
        "selector.window_opened",
        "selector.window_won",
        "side.call_failed",
        "side.call_finished",
        "side.call_started",
        "source.exited",
        "source.gave_up",
        "source.restarted",
        "source.restarting",
        "ui.audio_origin_refused",
        "ui.client_attached",
        "ui.client_detached",
        "ui.client_event_unhandled",
        "ui.client_frame_invalid",
        "ui.config_edit_applied",
        "ui.config_edit_refused",
        "ui.frame_flow_restored",
        "ui.frames_dropping",
        "ui.poke_cooling_down",
        "ui.poke_filed",
        "ui.server_died",
        "ui.server_stop_failed",
        "ui.server_stop_timeout",
        "ui.uplink_dropped",
        "ui.uplink_page_resumed",
        "ui.uplink_resumed",
        "ui.uplink_silence_filled",
        "ui_test.failed",
        "ui.ws_origin_refused",
        "volcano.interrupt_unsent",
        "volcano.interrupted",
        "volcano.chat_ended",
        "volcano.context_pushed",
        "volcano.error_frame",
        "volcano.event_ignored",
        "volcano.frame_unreadable",
        "volcano.goodbye_skipped",
        "volcano.handshake_skipped",
        "volcano.reply_implicit",
        "volcano.reply_requested",
        "volcano.reply_timed_out",
        "volcano.link_fatal",
        "volcano.orphan_tombstoned",
        "volcano.settled_unnamed",
        "volcano.session_swapped",
        "volcano.swap_timed_out",
        "volcano.swap_deferred",
        "volcano.session_started",
        "volcano.suspended",
        "volcano.resumed",
        "volcano.base_instructions_ignored",
        "volcano.reconnect_failed",
        "volcano.reconnect_gave_up",
        "volcano.reconnected",
    }
)


def _log_calls() -> list[tuple[Path, ast.Call]]:
    """Every `log.<level>(...)` in production code, vendored trees excluded."""
    found: list[tuple[Path, ast.Call]] = []
    for path in sorted(_SRC.rglob("*.py")):
        if "_vendor" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr not in _METHODS:
                continue
            if not isinstance(func.value, ast.Name) or func.value.id not in _LOGGERS:
                continue
            found.append((path, node))
    return found


def _event_names() -> set[str]:
    names: set[str] = set()
    for _, call in _log_calls():
        if call.args and isinstance(call.args[0], ast.Constant):
            value = call.args[0].value
            if isinstance(value, str):
                names.add(value)
    return names


def test_the_event_vocabulary_is_append_only() -> None:
    """A rename reads as a new event to everything downstream and as silence to
    everything that was grouping by the old one."""
    live = _event_names()
    vanished = KNOWN_EVENTS - live
    assert not vanished, (
        f"这些事件名不见了：{sorted(vanished)}。改名对下游等于「旧的没了、新的凭空出现」——"
        "真要改就把这里的行也删掉，并在 commit 里说清楚为什么。"
    )
    added = live - KNOWN_EVENTS
    assert not added, (
        f"新事件名没登记：{sorted(added)}。加一行到 KNOWN_EVENTS 就行——"
        "登记是为了让下一次改名被这条测试拦住。"
    )


def test_every_event_name_is_a_constant_not_a_formatted_string() -> None:
    """`log.info(f"speech stopped at {ms}")` reads fine once and can never be
    grouped, filtered or counted (obs/logging.py's first rule)."""
    offenders: list[str] = []
    for path, call in _log_calls():
        first = call.args[0] if call.args else None
        if first is None:
            offenders.append(f"{path.name}:{call.lineno} 没有事件名")
        elif not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
            offenders.append(f"{path.name}:{call.lineno} 事件名不是字符串常量")
    assert not offenders, "事件名必须是常量：\n  " + "\n  ".join(offenders)


def test_every_event_name_is_namespaced() -> None:
    """`子系统.动词短语`. Without the prefix the panel's log pane is one flat
    list and 「调度器这十分钟干了什么」 has no cheap answer."""
    bad = sorted(n for n in _event_names() if "." not in n or n != n.lower())
    assert not bad, f"事件名要写成小写的「子系统.动词短语」：{bad}"
