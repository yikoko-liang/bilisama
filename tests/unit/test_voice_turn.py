"""The voice gate, driven frame by frame on a fake clock.

Each test is one row of the gate's table (plan §5.2): what the speakers see
for a frame, now. `on_skip` is a list; nothing here cancels anything — the
scheduler's kill path has its own tests.
"""

from __future__ import annotations

from bilisama.clock import FakeClock
from bilisama.config.enums import VoiceReplyMode
from bilisama.director.turn_protocol import Ruling, TurnPolicy
from bilisama.director.voice_turn import Skip, VoiceTurnGate
from bilisama.realtime import link
from bilisama.scene_markers import SceneCategory


class _Rig:
    def __init__(
        self,
        mode: VoiceReplyMode = VoiceReplyMode.WHEN_ADDRESSED,
        *,
        policy: TurnPolicy | None = None,
        hold_max_s: float = 0.6,
        hold_cap_frames: int = 200,
    ) -> None:
        self.clock = FakeClock()
        self.skips: list[Skip] = []
        self.emitted: list[link.LinkEvent] = []
        self.gate = VoiceTurnGate(
            self.clock,
            policy=policy or TurnPolicy(),
            mode=mode,
            on_skip=self.skips.append,
            hold_max_s=hold_max_s,
            hold_cap_frames=hold_cap_frames,
        )
        self.gate.attach(self.emitted.append)

    async def settle(self) -> None:
        """Run out the note window, so a muted turn reports its verdict."""
        await self.clock.advance(0.31)

    def start(self, *, implicit: bool = True) -> link.ReplyHandle:
        handle = link.ReplyHandle(implicit=implicit)
        assert self.gate.feed(link.ReplyStarted(handle)) == (link.ReplyStarted(handle),)
        return handle


def _text(handle: link.ReplyHandle, text: str) -> link.ReplyTextDelta:
    return link.ReplyTextDelta(handle, text)


def _audio(handle: link.ReplyHandle, n: int = 1) -> link.ReplyAudioDelta:
    return link.ReplyAudioDelta(handle, bytes([n % 256]) * 4)


def _done(
    handle: link.ReplyHandle, status: link.ReplyStatus = link.ReplyStatus.COMPLETED
) -> link.ReplyDone:
    return link.ReplyDone(handle, status)


async def test_frames_that_are_not_her_own_turn_pass_untouched() -> None:
    rig = _Rig()
    ours = rig.start(implicit=False)
    for event in (
        _text(ours, "[SKIP] 我们派发的回复不进门"),
        _audio(ours),
        link.SpeechStarted(),
        link.UserTranscriptDone("主播说的"),
        link.LinkUp(),
        _done(ours),
    ):
        assert rig.gate.feed(event) == (event,)
    assert rig.skips == []


async def test_a_marked_head_mutes_at_once_and_settles_with_the_whole_note() -> None:
    """Two moments, not one: the mute lands on the bracket, the verdict waits.

    Silencing her cannot wait — the bracket closes and no frame of this turn
    reaches a speaker again. The verdict can, because the note rides on it and
    the note is the words AFTER the bracket, which a provider streaming
    character by character has not sent yet.
    """
    rig = _Rig()
    hers = rig.start()
    assert rig.gate.feed(_text(hers, "[SKIP] 在聊")) == ()
    assert rig.gate.feed(_audio(hers)) == (), "muted before any verdict went out"
    assert rig.skips == [], "the note is still arriving"
    assert rig.gate.feed(_text(hers, "天气和明天的安排")) == ()
    await rig.settle()
    whole = Skip(hers, Ruling(SceneCategory.SKIP, "在聊天气和明天的安排"), False)
    assert rig.skips == [whole]
    # Everything after is dropped, the end included: nothing of this turn
    # reached the views, so they have nothing to close.
    assert rig.gate.feed(_done(hers)) == ()
    assert rig.skips == [whole], "reported once"
    assert rig.gate.skipped(hers.handle_id)
    assert rig.gate.status()["skipped"] == 1


async def test_a_reply_that_ends_inside_the_note_window_settles_at_once() -> None:
    """The end is better news than the timer: the note is whole and the turn
    is already over, so there is nothing left to race a cancel against."""
    rig = _Rig()
    hers = rig.start()
    assert rig.gate.feed(_text(hers, "[SKIP]")) == ()
    assert rig.gate.feed(_text(hers, " 主播在谢礼物")) == ()
    assert rig.skips == []
    assert rig.gate.feed(_done(hers)) == ()
    assert rig.skips == [Skip(hers, Ruling(SceneCategory.SKIP, "主播在谢礼物"), False)]


async def test_a_full_note_does_not_wait_out_the_window() -> None:
    rig = _Rig()
    hers = rig.start()
    assert rig.gate.feed(_text(hers, "[SKIP] " + "字" * 20)) == ()
    assert rig.skips == [Skip(hers, Ruling(SceneCategory.SKIP, "字" * 20), False)]
    assert rig.gate.feed(_text(hers, "还有更多但装不下了")) == ()
    assert len(rig.skips) == 1


async def test_a_plain_head_releases_on_its_first_character() -> None:
    rig = _Rig()
    hers = rig.start()
    first = _text(hers, "好")
    assert rig.gate.feed(first) == (first,)
    later = _text(hers, "的，我看到了")
    assert rig.gate.feed(later) == (later,), "released turns flow straight through"
    assert rig.skips == []
    assert rig.gate.status()["passed"] == 1


async def test_character_by_character_text_decides_on_the_closing_bracket() -> None:
    rig = _Rig()
    hers = rig.start()
    for ch in "[SKIP":
        assert rig.gate.feed(_text(hers, ch)) == ()
    assert rig.gate.feed(_text(hers, "]")) == (), "muted here"
    assert rig.skips == [], "but the note has not been sent yet"
    for ch in " 在念弹幕":
        assert rig.gate.feed(_text(hers, ch)) == ()
    await rig.settle()
    assert rig.skips == [Skip(hers, Ruling(SceneCategory.SKIP, "在念弹幕"), False)]


async def test_a_scene_the_policy_answers_releases_the_held_burst_in_order() -> None:
    rig = _Rig(policy=TurnPolicy(speak=frozenset({SceneCategory.TO_ME, SceneCategory.SKIP})))
    hers = rig.start()
    a1, a2 = _audio(hers, 1), _audio(hers, 2)
    assert rig.gate.feed(a1) == ()
    assert rig.gate.feed(a2) == ()
    marker = _text(hers, "[SKIP] 连麦的在问")
    assert rig.gate.feed(marker) == (a1, a2, marker)
    assert rig.skips == []


async def test_audio_leading_text_is_released_by_the_timer_and_a_late_marker_flushes() -> None:
    rig = _Rig(hold_max_s=0.6)
    hers = rig.start()
    a1, a2 = _audio(hers, 1), _audio(hers, 2)
    assert rig.gate.feed(a1) == ()
    assert rig.gate.feed(a2) == ()
    await rig.clock.advance(0.59)
    assert rig.emitted == [], "not yet"
    await rig.clock.advance(0.01)
    assert rig.emitted == [a1, a2], "the timer released the held frames, in order"
    assert rig.gate.status()["timeouts"] == 1
    # The text arrives after all — and says she should not have spoken.
    assert rig.gate.feed(_text(hers, "[SKIP] 主播在嘀咕")) == ()
    assert rig.skips == [
        Skip(hers, Ruling(SceneCategory.SKIP, "主播在嘀咕"), True)
    ], "a late marker cannot wait for its note: those frames are already playing"
    assert rig.gate.status()["late_markers"] == 1
    assert rig.gate.feed(_audio(hers, 3)) == (), "dropped from here on"
    # The views did hear the start of this one, so they get its end.
    assert rig.gate.feed(_done(hers, link.ReplyStatus.CANCELLED)) == (
        _done(hers, link.ReplyStatus.CANCELLED),
    )


async def test_a_full_buffer_is_released_rather_than_grown() -> None:
    rig = _Rig(hold_cap_frames=3)
    hers = rig.start()
    frames = [_audio(hers, n) for n in range(3)]
    assert rig.gate.feed(frames[0]) == ()
    assert rig.gate.feed(frames[1]) == ()
    assert rig.gate.feed(frames[2]) == tuple(frames)
    assert rig.gate.feed(_audio(hers, 9)) == (_audio(hers, 9),)


async def test_a_reply_that_ends_inside_a_marker_is_dropped_without_a_ruling() -> None:
    rig = _Rig()
    hers = rig.start()
    assert rig.gate.feed(_text(hers, "[AU")) == ()
    assert rig.gate.feed(_done(hers)) == ()
    assert rig.skips == [Skip(hers, None, False)]
    assert rig.gate.skipped(hers.handle_id)


async def test_a_reply_that_ends_with_an_unreadable_head_still_plays() -> None:
    rig = _Rig()
    hers = rig.start()
    a1 = _audio(hers, 1)
    assert rig.gate.feed(a1) == ()
    assert rig.gate.feed(_done(hers)) == (a1, _done(hers))
    assert rig.skips == []
    assert not rig.gate.skipped(hers.handle_id)


async def test_a_reply_that_ends_on_a_marker_is_still_skipped() -> None:
    rig = _Rig()
    hers = rig.start()
    assert rig.gate.feed(_text(hers, "SKIP")) == (), "bare tag, no separator yet"
    assert rig.gate.feed(_done(hers)) == ()
    assert rig.skips == [Skip(hers, Ruling(SceneCategory.SKIP, "", unbracketed=True), False)]


async def test_a_turn_cut_while_held_plays_nothing() -> None:
    rig = _Rig()
    hers = rig.start()
    assert rig.gate.feed(_audio(hers)) == ()
    cut = _done(hers, link.ReplyStatus.CANCELLED)
    assert rig.gate.feed(cut) == (cut,)
    assert rig.skips == []
    assert rig.emitted == []


async def test_a_dropped_link_empties_the_gate() -> None:
    rig = _Rig()
    hers = rig.start()
    assert rig.gate.feed(_audio(hers)) == ()
    down = link.LinkDown(reason="closed")
    assert rig.gate.feed(down) == (down,)
    assert rig.gate.status()["holding"] == 0
    late = _audio(hers, 5)
    assert rig.gate.feed(late) == (late,), "an unknown turn's frame is nobody's to hold"
    assert rig.emitted == []


async def test_switching_to_always_releases_the_hold_but_keeps_the_safety_net() -> None:
    rig = _Rig()
    hers = rig.start()
    a1 = _audio(hers, 1)
    assert rig.gate.feed(a1) == ()
    rig.gate.set_mode(VoiceReplyMode.ALWAYS)
    assert rig.emitted == [a1]
    assert rig.gate.mode is VoiceReplyMode.ALWAYS
    assert rig.gate.feed(_done(hers)) == (_done(hers),)
    # A new turn under ALWAYS flows straight through...
    again = rig.start()
    plain = _text(again, "好的")
    assert rig.gate.feed(plain) == (plain,)
    assert rig.gate.feed(_done(again)) == (_done(again),)
    # ...but an exact marker at the head still skips, and asks for a flush,
    # because those frames were never held.
    third = rig.start()
    assert rig.gate.feed(_text(third, "[SKIP] 连麦")) == ()
    assert rig.skips == [Skip(third, Ruling(SceneCategory.SKIP, "连麦"), True)]
    assert rig.gate.feed(_done(third)) == (_done(third),), "its frames were flowing: end passes"
    rig.gate.set_mode(VoiceReplyMode.WHEN_ADDRESSED)
    fourth = rig.start()
    assert rig.gate.feed(_audio(fourth)) == (), "holding again"


async def test_two_consecutive_turns_do_not_interfere() -> None:
    rig = _Rig()
    first = rig.start()
    assert rig.gate.feed(_text(first, "[SKIP] 讲故事")) == ()
    assert rig.gate.feed(_done(first)) == ()
    second = rig.start()
    plain = _text(second, "好的")
    assert rig.gate.feed(plain) == (plain,)
    assert rig.gate.feed(_done(second)) == (_done(second),)
    assert len(rig.skips) == 1
    assert rig.gate.skipped(first.handle_id) and not rig.gate.skipped(second.handle_id)


async def test_a_failing_skip_callback_still_drops_the_frames() -> None:
    rig = _Rig()

    def boom(skip: Skip) -> None:
        raise RuntimeError("调度器不在")

    rig.gate = VoiceTurnGate(
        rig.clock, policy=TurnPolicy(), mode=VoiceReplyMode.WHEN_ADDRESSED, on_skip=boom
    )
    hers = rig.start()
    assert rig.gate.feed(_text(hers, "[SKIP] 讲故事")) == ()
    assert rig.gate.feed(_audio(hers)) == (), "muted regardless of who reports it"
    await rig.settle()
    assert rig.gate.skipped(hers.handle_id)


async def test_a_marker_in_the_middle_is_prose_to_the_gate() -> None:
    rig = _Rig()
    hers = rig.start()
    first = _text(hers, "好的，")
    assert rig.gate.feed(first) == (first,)
    mid = _text(hers, "[SKIP] 这句已经在播了")
    assert rig.gate.feed(mid) == (mid,)
    assert rig.skips == []


async def test_close_disarms_every_timer() -> None:
    rig = _Rig()
    hers = rig.start()
    assert rig.gate.feed(_audio(hers)) == ()
    rig.gate.close()
    await rig.clock.advance(1.0)
    assert rig.emitted == [], "a closed gate releases nothing"


async def test_status_has_the_shape_the_health_card_reads() -> None:
    rig = _Rig()
    assert rig.gate.status() == {
        "mode": "when_addressed",
        "holding": 0,
        "passed": 0,
        "skipped": 0,
        "timeouts": 0,
        "late_markers": 0,
        "longest_hold_ms": 0,
    }
