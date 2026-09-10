"""The voice gate: hold her own microphone turn until its head says who the
streamer was talking to.

The provider answers every VAD turn by itself and nothing above the link
could stop it — see the scheduler's kill path (skip_implicit) for the half
that cancels. This is the half that decides. It sits between the fan-out and
the two playback views: the scheduler keeps its ungated view (it closes the
floor on ReplyStarted and must not be delayed), the speakers read what comes
out of here. For each implicit turn the gate buffers frames until the head
decodes (turn_protocol.MarkerHead), then either releases the buffer in order
or drops it and reports a Skip to whoever kills the turn.

Three facts shape the edges. Every shipped provider sends text before audio,
so the common path holds nothing for long; the timer exists for a shape
where audio leads, and a marker read after the timer released the frames is
a late marker — the one case the speakers must be flushed. A held turn that
ends inside an unfinished marker (``[AU``) is dropped without a cancel: there
is nothing left to cancel. And the scheduler's view being ungated is what
makes the ordering safe: the gate reports a Skip synchronously, inside the
pump, before the scheduler's own task has seen the frame it acted on.

Constants live here, not in config (event_pacing.py's rule): they are
measured facts about providers, not knobs a streamer turns.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from bilisama.clock import Clock
from bilisama.config.enums import VoiceReplyMode
from bilisama.director.turn_protocol import (
    NOTE_MAX_CHARS,
    HeadState,
    MarkerHead,
    Ruling,
    TurnAction,
    TurnPolicy,
)
from bilisama.obs.logging import get_logger
from bilisama.realtime import link
from bilisama.scene_markers import lookup

__all__ = ["Skip", "VoiceTurnGate"]

log = get_logger(__name__)

# How long a turn may be held waiting for text when audio leads. The three
# shipped shapes send text first (plan §三), so this only ever fires on a
# shape nobody has measured; 0.6 s is the most the first syllable may lag.
_HOLD_MAX_S = 0.6
# The buffer is outside the fan-out queue (256 deep); a release is one burst,
# and this keeps the burst well under the queue.
_HOLD_CAP_FRAMES = 200
# How many skipped handle ids the gate remembers for ui_feed's benefit.
_SKIPPED_REMEMBERED = 64
# How long the gate keeps DECODING a turn it has already muted, to catch the
# note the provider streams after the tag. Muting is not delayed by this —
# the frames stop at the closing bracket — only the cancel is, and only until
# the note is full or this expires. Without the window, a provider that
# streams character by character never sends the note in time: production
# logged 298 empty notes out of 302 skips on 2026-09-09.
_NOTE_WINDOW_S = 0.3

Emit = Callable[[link.LinkEvent], None]


@dataclass(frozen=True, slots=True)
class Skip:
    """One turn the gate decided against, for whoever kills it."""

    handle: link.ReplyHandle
    # None: the reply ended inside an unfinished marker; nothing to cancel.
    ruling: Ruling | None
    # Frames of this turn already reached the speakers (late marker): flush.
    clear_playback: bool


@dataclass(slots=True)
class _Turn:
    handle: link.ReplyHandle
    head: MarkerHead
    opened_at: float
    holding: bool
    frames: list[link.LinkEvent] = field(default_factory=list)
    released: bool = False
    # Muted: the tag was read, the frames stop here, nothing more will play.
    # Skipped: the verdict went out and the scheduler killed the turn. Between
    # the two the gate is still decoding, collecting the note.
    muted: bool = False
    skipped: bool = False
    timer: asyncio.Task[None] | None = None
    note_timer: asyncio.Task[None] | None = None
    midway_logged: bool = False


class VoiceTurnGate:
    """Decide, per implicit turn, whether its frames reach the speakers."""

    def __init__(
        self,
        clock: Clock,
        *,
        policy: TurnPolicy,
        mode: VoiceReplyMode,
        on_skip: Callable[[Skip], None],
        decoder_factory: Callable[[], MarkerHead] = MarkerHead,
        hold_max_s: float = _HOLD_MAX_S,
        hold_cap_frames: int = _HOLD_CAP_FRAMES,
    ) -> None:
        self._clock = clock
        self._policy = policy
        self._mode = mode
        self._on_skip = on_skip
        self._decoder_factory = decoder_factory
        self._hold_max_s = hold_max_s
        self._hold_cap_frames = hold_cap_frames
        self._emit: Emit | None = None
        self._turns: dict[int, _Turn] = {}
        self._skipped_ids: deque[int] = deque(maxlen=_SKIPPED_REMEMBERED)
        self._tasks: set[asyncio.Task[None]] = set()
        self._passed = 0
        self._skipped = 0
        self._timeouts = 0
        self._late_markers = 0
        self._longest_hold_ms = 0

    # ------------------------------------------------------------ wiring

    def attach(self, emit: Emit) -> None:
        """Where frames released by the timer (or a mode change) go.

        feed() returns its releases to the caller; the timer runs in its own
        task and has no caller, so it needs this.
        """
        self._emit = emit

    @property
    def mode(self) -> VoiceReplyMode:
        return self._mode

    def set_mode(self, mode: VoiceReplyMode) -> None:
        """Switch live. Going to ALWAYS releases whatever is held; the gate
        keeps decoding afterwards so an exact marker still skips (the safety
        net for a prompt that was pushed before the switch)."""
        if mode is self._mode:
            return
        log.info("voice_gate.mode_changed", mode=mode.value, holding=self._holding_count())
        self._mode = mode
        if mode is VoiceReplyMode.ALWAYS:
            for turn in list(self._turns.values()):
                if turn.holding:
                    self._emit_all(self._release(turn, why="mode"))

    def skipped(self, handle_id: int) -> bool:
        """Whether this reply was dropped here (the health card and tests)."""
        return handle_id in self._skipped_ids

    def status(self) -> dict[str, Any]:
        return {
            "mode": self._mode.value,
            "holding": self._holding_count(),
            "passed": self._passed,
            "skipped": self._skipped,
            "timeouts": self._timeouts,
            "late_markers": self._late_markers,
            "longest_hold_ms": self._longest_hold_ms,
        }

    def close(self) -> None:
        for turn in self._turns.values():
            self._disarm(turn)
        self._turns.clear()

    # ------------------------------------------------------------ the gate

    def feed(self, event: link.LinkEvent) -> tuple[link.LinkEvent, ...]:
        """What the speakers should see for this frame — now.

        Synchronous: called from the fan-out's pump for every frame, in
        order. Returns the frame itself, nothing (held or dropped), or a
        held burst followed by the frame.
        """
        if isinstance(event, link.LinkDown):
            self._drop_all()
            return (event,)
        if isinstance(event, link.ReplyStarted):
            return self._on_started(event)
        if isinstance(event, link.ReplyTextDelta):
            return self._on_text(event)
        if isinstance(event, link.ReplyAudioDelta):
            return self._on_audio(event)
        if isinstance(event, link.ReplyDone):
            return self._on_done(event)
        return (event,)

    def _on_started(self, event: link.ReplyStarted) -> tuple[link.LinkEvent, ...]:
        handle = event.handle
        if not handle.implicit:
            return (event,)
        holding = self._mode is VoiceReplyMode.WHEN_ADDRESSED
        self._turns[handle.handle_id] = _Turn(
            handle=handle,
            head=self._decoder_factory(),
            opened_at=self._clock.monotonic(),
            holding=holding,
            # Under ALWAYS the frames flow straight to the views, which is
            # what "released" means to the done below.
            released=not holding,
        )
        if holding:
            log.debug("voice_gate.held", handle_id=handle.handle_id)
        # The start itself passes: it carries no content, and the views
        # downstream key their "she is speaking" state on it.
        return (event,)

    def _on_text(self, event: link.ReplyTextDelta) -> tuple[link.LinkEvent, ...]:
        turn = self._turns.get(event.handle.handle_id)
        if turn is None:
            return (event,)
        if turn.skipped:
            return ()
        if turn.muted:
            # Decided and silenced, still reading: these are the words of the
            # note, and they arrive after the bracket that ended the turn.
            turn.head.feed(event.text)
            if self._note_is_full(turn):
                self._settle(turn, clear_playback=False)
            return ()
        state = turn.head.feed(event.text)
        if turn.holding:
            turn.frames.append(event)
            self._arm(turn)
            return self._judge(turn)
        return self._watch(turn, event, state)

    def _on_audio(self, event: link.ReplyAudioDelta) -> tuple[link.LinkEvent, ...]:
        turn = self._turns.get(event.handle.handle_id)
        if turn is None:
            return (event,)
        if turn.skipped or turn.muted:
            return ()
        if not turn.holding:
            return (event,)
        turn.frames.append(event)
        self._arm(turn)
        if len(turn.frames) >= self._hold_cap_frames:
            return self._release(turn, why="cap")
        return ()

    def _on_done(self, event: link.ReplyDone) -> tuple[link.LinkEvent, ...]:
        turn = self._turns.pop(event.handle.handle_id, None)
        if turn is None:
            return (event,)
        self._disarm(turn)
        if turn.skipped:
            # Skipped while held: nothing of this turn reached the views, so
            # its end is nobody's business downstream either (the scheduler
            # has the ungated view). Skipped after a release — the late
            # marker — the views did see frames, and get the end too.
            return (event,) if turn.released else ()
        if turn.muted:
            # The reply ended inside the note window. Better than the timer:
            # the note is whole, and the cancel is moot on a turn that is
            # already over — which is what 3% of production cancels raced.
            turn.head.finish()
            self._settle(turn, clear_playback=False)
            return (event,) if turn.released else ()
        if not turn.holding:
            return (event,)
        if event.status is not link.ReplyStatus.COMPLETED:
            # Cut, failed or timed out while held: the head never plays.
            turn.frames.clear()
            log.debug(
                "voice_gate.passed",
                handle_id=turn.handle.handle_id,
                why="ended_" + event.status.value,
                frames=0,
            )
            return (event,)
        state = turn.head.finish()
        if state is HeadState.DANGLING:
            self._skip(turn, ruling=None, clear_playback=False)
            return ()
        if state is HeadState.MARKED:
            ruling = turn.head.ruling
            assert ruling is not None  # MARKED says so
            if self._policy.action(ruling) is TurnAction.SKIP:
                self._skip(turn, ruling=ruling, clear_playback=False)
                return ()
        # Plain, undecidable, or a scene the policy answers: she must not go
        # mute over a head we could not read.
        return (*self._release(turn, why="end"), event)

    # ------------------------------------------------------------ decisions

    def _judge(self, turn: _Turn) -> tuple[link.LinkEvent, ...]:
        state = turn.head.state
        if state is HeadState.PLAIN:
            return self._release(turn, why="plain")
        if state is HeadState.MARKED:
            ruling = turn.head.ruling
            assert ruling is not None
            if self._policy.action(ruling) is TurnAction.SPEAK:
                return self._release(turn, why="speak")
            self._mute(turn, clear_playback=False)
            return ()
        if len(turn.frames) >= self._hold_cap_frames:
            return self._release(turn, why="cap")
        return ()

    def _watch(
        self, turn: _Turn, event: link.ReplyTextDelta, state: HeadState
    ) -> tuple[link.LinkEvent, ...]:
        """A turn already flowing to the speakers, still being read.

        Two reasons a released turn is still decoded: the timer let it go
        before the head arrived (the late-marker case, which skips and asks
        for a flush), and ALWAYS mode, where an exact marker still skips.
        """
        if state is HeadState.MARKED:
            ruling = turn.head.ruling
            assert ruling is not None
            if self._policy.action(ruling) is TurnAction.SKIP:
                self._late_markers += 1
                log.info(
                    "voice_gate.late_marker",
                    handle_id=turn.handle.handle_id,
                    category=ruling.category.value,
                    mode=self._mode.value,
                )
                self._mute(turn, clear_playback=True)
                return ()
        elif not turn.midway_logged and _looks_like_marker(event.text):
            # A marker that is not at the head is prose to the gate — half of
            # the sentence may already have played — but worth one line: a
            # model doing this often is one the prompt needs to reach.
            turn.midway_logged = True
            log.info("voice_gate.marker_midway", handle_id=turn.handle.handle_id)
        return (event,)

    def _release(self, turn: _Turn, *, why: str) -> tuple[link.LinkEvent, ...]:
        self._disarm(turn)
        frames = tuple(turn.frames)
        turn.frames.clear()
        turn.holding = False
        turn.released = True
        held_ms = int((self._clock.monotonic() - turn.opened_at) * 1000)
        self._longest_hold_ms = max(self._longest_hold_ms, held_ms)
        self._passed += 1
        log.debug(
            "voice_gate.passed",
            handle_id=turn.handle.handle_id,
            why=why,
            frames=len(frames),
            held_ms=held_ms,
        )
        return frames

    def _mute(self, turn: _Turn, *, clear_playback: bool) -> None:
        """Stop the turn reaching the speakers, then wait for its note.

        Everything the room could hear ends here. What is still owed is the
        verdict — and the note that rides on it, which the provider has not
        finished streaming. A turn whose audio already played (a late marker)
        cannot wait: it settles at once so the flush is immediate.
        """
        self._disarm(turn)
        turn.frames.clear()
        turn.holding = False
        turn.muted = True
        if clear_playback or self._note_is_full(turn):
            # Nothing to wait for: the audio is already out and needs the
            # flush now, or the whole note arrived in the same chunk as the
            # tag — which is what a provider that batches its text does.
            self._settle(turn, clear_playback=clear_playback)
            return
        task = asyncio.ensure_future(self._note_window(turn))
        task.set_name(f"voice-gate:note:{turn.handle.handle_id}")
        turn.note_timer = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _note_window(self, turn: _Turn) -> None:
        await self._clock.sleep(_NOTE_WINDOW_S)
        if not turn.skipped:
            self._settle(turn, clear_playback=False)

    def _note_is_full(self, turn: _Turn) -> bool:
        ruling = turn.head.ruling
        return ruling is not None and len(ruling.note) >= NOTE_MAX_CHARS

    def _settle(self, turn: _Turn, *, clear_playback: bool) -> None:
        """Emit the verdict and let the scheduler kill the turn."""
        if turn.skipped:
            return
        if turn.note_timer is not None:
            turn.note_timer.cancel()
            turn.note_timer = None
        self._skip(turn, ruling=turn.head.ruling, clear_playback=clear_playback)

    def _skip(self, turn: _Turn, *, ruling: Ruling | None, clear_playback: bool) -> None:
        self._disarm(turn)
        turn.frames.clear()
        turn.holding = False
        turn.muted = True
        turn.skipped = True
        self._skipped += 1
        self._skipped_ids.append(turn.handle.handle_id)
        if ruling is not None and ruling.unbracketed:
            log.info("voice_gate.marker_unbracketed", handle_id=turn.handle.handle_id)
        log.info(
            "voice_gate.skipped",
            handle_id=turn.handle.handle_id,
            category=ruling.category.value if ruling is not None else "dangling",
            # `note_text` is folded to a length by the log formatter, the
            # way every audience-shaped field is; the panel shows the words.
            note_text=ruling.note if ruling is not None else turn.head.text,
            held_ms=int((self._clock.monotonic() - turn.opened_at) * 1000),
            clear_playback=clear_playback,
        )
        try:
            self._on_skip(Skip(turn.handle, ruling, clear_playback))
        except Exception as exc:
            # The kill is somebody else's; failing to report it must not take
            # the pump down — the frames are dropped here regardless.
            log.warning(
                "voice_gate.cancel_failed",
                handle_id=turn.handle.handle_id,
                error_text=str(exc)[:200],
            )

    def _drop_all(self) -> None:
        for turn in self._turns.values():
            self._disarm(turn)
            if turn.note_timer is not None:
                turn.note_timer.cancel()
                turn.note_timer = None
        self._turns.clear()

    # ------------------------------------------------------------ the timer

    def _arm(self, turn: _Turn) -> None:
        if turn.timer is not None:
            return
        task = asyncio.ensure_future(self._expire(turn))
        task.set_name(f"voice-gate:hold:{turn.handle.handle_id}")
        turn.timer = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _disarm(self, turn: _Turn) -> None:
        if turn.timer is not None:
            turn.timer.cancel()
            turn.timer = None

    async def _expire(self, turn: _Turn) -> None:
        await self._clock.sleep(self._hold_max_s)
        if not turn.holding:
            return
        self._timeouts += 1
        self._emit_all(self._release(turn, why="timeout"))

    def _emit_all(self, frames: tuple[link.LinkEvent, ...]) -> None:
        if not frames:
            return
        if self._emit is None:
            # attach() is part of wiring; without it a timed-out hold has
            # nowhere to go, and silently dropping it would read as her going
            # mute for no reason.
            log.warning("voice_gate.unattached", frames=len(frames))
            return
        for frame in frames:
            self._emit(frame)

    def _holding_count(self) -> int:
        return sum(1 for turn in self._turns.values() if turn.holding)


def _looks_like_marker(text: str) -> bool:
    """A closed bracket tag anywhere in a delta, for the midway log only."""
    start = text.find("[")
    while start != -1:
        end = text.find("]", start + 1)
        if end == -1:
            return False
        inner = text[start + 1 : end]
        if inner and _is_tag_word(inner):
            return True
        start = text.find("[", end + 1)
    return False


def _is_tag_word(word: str) -> bool:
    return lookup(word) is not None
