"""Who gets the microphone, and what happens to the local pair meanwhile.

Two capture streams would fight over the device and two playback queues would
talk over each other, so exactly one client holds them. Nobody holding is the
normal state — `--no-ui`, no shell installed, no page open — and then the
sounddevice pair keeps running exactly as it did before any of this existed.
"""

from __future__ import annotations

import asyncio
import math
import random
import struct
import wave

from bilisama.clock import FakeClock
from bilisama.ui.audio import AudioBroker, EchoProbe, PlaybackTally


class _Local:
    """Stands in for dev-talk's microphone pump and speaker."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def suspend(self) -> None:
        self.calls.append("suspend")

    async def resume(self) -> None:
        self.calls.append("resume")


class _SlowLocal:
    """A local pair whose resume only lands after the loop has been given away.

    dev-talk's is exactly this shape: both halves wrap a blocking PortAudio
    call in `asyncio.to_thread`, and the microphone task is started in the tail
    AFTER that await returns (_LocalPair.resume). The tail is what a claim
    arriving in the meantime can overtake.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.inside_resume = asyncio.Event()
        self.let_resume_finish = asyncio.Event()

    async def suspend(self) -> None:
        self.calls.append("suspend")

    async def resume(self) -> None:
        self.inside_resume.set()
        await self.let_resume_finish.wait()
        self.calls.append("resume")


async def test_nobody_holding_leaves_the_local_pair_alone() -> None:
    local = _Local()
    broker = AudioBroker(local=local)
    assert broker.owner is None
    assert broker.local_is_live
    assert local.calls == [], "没人来的时候不该动本地设备"


async def test_a_claim_parks_the_local_pair_and_a_release_brings_it_back() -> None:
    local = _Local()
    broker = AudioBroker(local=local)
    assert await broker.claim("shell", send=lambda _pcm: None)
    assert local.calls == ["suspend"]
    assert not broker.local_is_live

    await broker.release("shell")
    assert local.calls == ["suspend", "resume"]
    assert broker.local_is_live


async def test_the_shell_outranks_a_browser_tab() -> None:
    """Both open is the ordinary developer setup: shell for real, tab to look."""
    local = _Local()
    broker = AudioBroker(local=local)
    assert await broker.claim("browser", send=lambda _pcm: None)
    assert await broker.claim("shell", send=lambda _pcm: None), "壳该顶掉标签页"
    assert broker.owner == "shell"
    # The devices never went back to the local pair in between — restarting
    # PortAudio streams for a handover nobody asked for is pure churn.
    assert local.calls == ["suspend"]


async def test_a_tab_cannot_take_the_devices_from_the_shell() -> None:
    broker = AudioBroker()
    assert await broker.claim("shell", send=lambda _pcm: None)
    assert not await broker.claim("browser", send=lambda _pcm: None)
    assert broker.owner == "shell"


async def test_a_displaced_client_releasing_does_not_park_the_new_one() -> None:
    """The tab hangs up after losing the handover; the shell keeps the devices.

    Without the owner check this is where the sound dies: a stale release from
    the loser would hand everything back to sounddevice while the shell is
    still playing into a socket nobody feeds.
    """
    local = _Local()
    broker = AudioBroker(local=local)
    await broker.claim("browser", send=lambda _pcm: None)
    await broker.claim("shell", send=lambda _pcm: None)

    await broker.release("browser")
    assert broker.owner == "shell"
    assert local.calls == ["suspend"], "本地设备不该在壳还拿着的时候被叫醒"


async def test_downlink_goes_to_the_owner_and_is_dropped_with_nobody_there() -> None:
    heard: list[bytes] = []
    broker = AudioBroker()
    broker.play(b"\x01\x02")  # nobody holds it yet
    assert heard == []

    await broker.claim("shell", send=heard.append)
    broker.play(b"\x03\x04")
    await broker.release("shell")
    broker.play(b"\x05\x06")  # gone again
    assert heard == [b"\x03\x04"], "只有当前持有者该听见，其余丢掉"


async def test_every_change_is_announced() -> None:
    """A tab with no sound has to be able to say why."""
    seen: list[str | None] = []
    broker = AudioBroker(announce=seen.append)
    await broker.claim("browser", send=lambda _pcm: None)
    await broker.claim("shell", send=lambda _pcm: None)
    await broker.release("shell")
    assert seen == ["browser", "shell", None]


# ------------------------------------------------------------ the segment count


def _tally() -> tuple[PlaybackTally, list[bool], list[int]]:
    gate: list[bool] = []
    woke: list[int] = []
    return (
        PlaybackTally(on_playback=gate.append, notify=lambda: woke.append(1)),
        gate,
        woke,
    )


def test_the_gate_closes_on_the_first_segment_and_opens_on_the_last() -> None:
    tally, gate, woke = _tally()
    tally.started()
    tally.started()
    assert gate == [True], "第二段不该再关一次门"
    tally.ended()
    assert gate == [True], "还有一段没播完，门不能开"
    tally.ended()
    assert gate == [True, False]
    assert woke == [1], "播完要叫醒派发循环，链路不会替它发这个事件"


def test_a_segment_ending_while_others_are_queued_keeps_the_floor_shut() -> None:
    """Ledger #41's trap, in one test.

    The page schedules ahead: by the time a segment reports ended, the next
    one is already queued and started. A gate that watched the last event
    would read that ending as "finished speaking", open mid-sentence, and let
    the scheduler dispatch over her — the same backlog as before, arriving
    from the other direction. Only the count knows better.
    """
    tally, gate, _woke = _tally()
    tally.started()  # segment one
    tally.started()  # two, queued behind it
    tally.ended()  # one finishes — a last-event gate opens HERE
    tally.started()  # three
    tally.ended()  # two
    assert gate == [True], f"她还在说话，闸门却动了：{gate}"
    tally.ended()  # three, the last
    assert gate == [True, False]


def test_a_barge_in_clears_everything_outstanding() -> None:
    tally, gate, woke = _tally()
    tally.started()
    tally.started()
    tally.cancelled()
    assert tally.outstanding == 0
    assert gate == [True, False]
    assert woke == [1]


def test_a_late_receipt_after_a_barge_in_cannot_wedge_the_gate() -> None:
    """The page stops sources it already reported as started; an ended for one
    of them can still be in flight. Unclamped, the count goes negative and the
    floor never opens again."""
    tally, gate, _woke = _tally()
    tally.started()
    tally.cancelled()
    tally.ended()  # the straggler
    assert tally.outstanding == 0
    tally.started()
    assert gate[-1] is True, "下一句还得能正常关门"


def test_the_count_is_also_the_witness_that_she_is_making_sound() -> None:
    """页面拿着设备的时候，「她在出声」只有这一个证人。

    本机扬声器这时没有流，play() 直接返回，`speaker.busy` 整段都是 False——桌宠
    于是生成期显示「思考中」，ReplyDone 一到就掉回「空闲」，而页面这时候才刚开始
    播（清单第 26 条）。同一个计数既是闸门也是这个证据，不该再造第二个。
    """
    tally, _gate, _woke = _tally()
    seen = [tally.busy]  # 还没开播
    tally.started()
    seen.append(tally.busy)
    tally.started()
    tally.ended()  # 第一段播完了，第二段还在
    seen.append(tally.busy)
    tally.ended()
    seen.append(tally.busy)
    assert seen == [False, True, True, False], f"「说话中」跟着声音走的：{seen}"


def test_a_barge_in_stops_the_speaking_state_with_the_sound() -> None:
    """打断之后页面不再出声，桌宠也不能继续摆「说话中」。"""
    tally, _gate, _woke = _tally()
    tally.started()
    tally.cancelled()
    seen = [tally.busy]
    tally.ended()  # 迟到的收条：计数被夹在 0，状态也不能跟着翻过去
    seen.append(tally.busy)
    assert seen == [False, False], f"打断之后桌宠还在摆说话中：{seen}"


async def test_a_displaced_client_is_hung_up_on() -> None:
    """Losing the devices has to reach the client that lost them.

    Left connected, a displaced tab keeps a live capture running on a socket
    nobody will ever read again — the microphone light stays on, the panel goes
    on claiming to hold devices it does not, and the OS sees two captures where
    the point of the handover was to have one.
    """
    hung_up: list[str] = []
    broker = AudioBroker()
    await broker.claim("browser", send=lambda _pcm: None, close=lambda: hung_up.append("browser"))
    assert hung_up == []
    await broker.claim("shell", send=lambda _pcm: None, close=lambda: hung_up.append("shell"))
    assert hung_up == ["browser"], "壳拿走设备时没有通知被顶掉的那个"


async def test_the_first_claimant_is_not_hung_up_on() -> None:
    """Nobody held them before, so there is nobody to tell."""
    hung_up: list[str] = []
    broker = AudioBroker()
    await broker.claim("shell", send=lambda _pcm: None, close=lambda: hung_up.append("shell"))
    assert hung_up == []


async def test_a_reload_cannot_wake_the_local_pair_behind_the_new_owner() -> None:
    """页面重载：旧 socket 还给设备的同时，新 socket 已经在敲门。

    Cmd-R 或者壳重启，两件事差几毫秒，而归还要等一次 to_thread 落地。没有互斥
    的话新 claim 会在 resume 的尾巴之前跑完，尾巴再把本机麦克风拉起来——页面
    拿着设备，sounddevice 那对同时活着，同一个物理设备上两条采集流，她的声音
    还会从两个地方出来，而 sounddevice 那份不在 Chromium 的回声消除参考里。
    """
    local = _SlowLocal()
    broker = AudioBroker(local=local)
    assert await broker.claim("browser", send=lambda _pcm: None)

    giving_back = asyncio.create_task(broker.release("browser"))
    await local.inside_resume.wait()
    taking_over = asyncio.create_task(broker.claim("shell", send=lambda _pcm: None))
    for _ in range(8):
        # Nothing but the mutex should be able to hold the claim here.
        await asyncio.sleep(0)
    local.let_resume_finish.set()
    await giving_back
    assert await taking_over

    assert broker.owner == "shell"
    assert not broker.local_is_live
    assert local.calls == [
        "suspend",
        "resume",
        "suspend",
    ], f"归还和顶替交错了，本机那对最后是活的：{local.calls}"


async def test_a_handover_says_the_queued_audio_no_longer_counts() -> None:
    """The page that was going to play it is gone; the receipts never arrive.

    PlaybackTally counts on the page reporting every segment back. A page that
    loses the devices mid-reply reports nothing for what it had already queued,
    the count never returns to zero, and the floor gate stays shut for the rest
    of the session. The link layer solved the same shape once and left the note
    (floor.on_link_lost).
    """
    cancelled: list[int] = []
    broker = AudioBroker(on_handoff=lambda: cancelled.append(1))

    await broker.claim("browser", send=lambda _pcm: None)
    assert cancelled == [], "第一个来的没有前任，没什么要作废"
    await broker.claim("shell", send=lambda _pcm: None)
    assert cancelled == [1], "顶替时排队的段落没作废"
    await broker.release("shell")
    assert cancelled == [1, 1], "还回设备时排队的段落没作废"


async def test_nothing_is_written_off_while_the_devices_stay_put() -> None:
    """A refused claim and a loser's late release both change nothing."""
    cancelled: list[int] = []
    broker = AudioBroker(on_handoff=lambda: cancelled.append(1))
    await broker.claim("shell", send=lambda _pcm: None)

    assert not await broker.claim("browser", send=lambda _pcm: None)
    await broker.release("browser")  # the tab that lost, hanging up afterwards
    assert broker.owner == "shell"
    assert cancelled == [], "设备没易主，正在播的段落不该被作废"


# ------------------------------------------------------------ hearing herself


def _tone(seconds: float, rate: int, amp: int = 9000, hz: int = 220) -> bytes:
    n = max(1, int(rate * seconds))
    if amp == 0:
        return b"\x00\x00" * n
    return struct.pack(
        f"<{n}h", *(int(amp * math.sin(2 * math.pi * hz * i / rate)) for i in range(n))
    )


_TICK_S = 0.02


def _syllables(rnd: random.Random, ticks: int, level: int) -> list[int]:
    """An irregular speech envelope: one amplitude held for a syllable at a time.

    Irregular on purpose. A regular pattern correlates with itself at every
    multiple of its period, which lets a probe that looks at the wrong lag —
    or at no lag at all — come out right by accident.
    """
    out: list[int] = []
    while len(out) < ticks:
        out += [rnd.choice([level // 3, level // 2, level * 3 // 4, level])] * rnd.randint(3, 8)
    return out[:ticks]


async def _converse(
    probe: EchoProbe,
    clock: FakeClock,
    *,
    leak: float,
    room: float = 0.12,
    gap_s: float = 1.0,
    lag_s: float = 0.3,
    turns: int = 6,
    seed: int = 7,
) -> None:
    """Drive both streams the way a live session does — on the clock.

    The production shape is not the obvious one, and getting it wrong is what
    hid two envelopes drifting apart:

    - The microphone feeds every 20 ms for as long as the page holds the
      devices, silence included (dev-talk's uplink).
    - The downlink exists only while she is actually speaking: dev-talk calls
      note_played on a ReplyAudioDelta and on nothing else, so the pause
      between two replies reaches the probe as no calls at all.

    Args:
        probe: The probe under test.
        clock: Moved forward 20 ms per block, so both sides land on real time.
        leak: How much of her own voice comes back through the mic, 0..1.
        room: The streamer and the room, as a share of her level. Never zero
            in a real room — and a microphone that IS zero short-circuits the
            correlation, which is how a probe that never correlated anything
            passed six tests.
        gap_s: Silence between two replies. Nothing is sent during it.
        lag_s: The trip through the air, back into the microphone.
        turns: How many replies.
        seed: Fixes the speech pattern.
    """
    rnd = random.Random(seed)
    her: list[int] = []
    for _ in range(turns):
        her += _syllables(rnd, int(rnd.choice([0.6, 0.9, 1.3, 1.8]) / _TICK_S), 9000)
        her += [0] * int(gap_s / _TICK_S)
    room_noise = _syllables(rnd, len(her), max(1, int(9000 * room))) if room else [0] * len(her)

    lag_ticks = int(lag_s / _TICK_S)
    for k, level in enumerate(her):
        if level:
            # No delta, no call — the pauses reach the probe as nothing.
            probe.note_played(_tone(_TICK_S, 24000, amp=level))
        echo = int(her[k - lag_ticks] * leak) if k >= lag_ticks else 0
        probe.note_captured(_tone(_TICK_S, 16000, amp=min(32767, echo + room_noise[k])))
        await clock.advance(_TICK_S)


async def test_a_clean_session_is_not_reported_as_a_leak() -> None:
    """Cancellation working, or headphones on: the mic never hears her.

    The microphone is far from silent — the streamer is talking, the room is
    there. That is the point: a probe that answered 「麦克风不静音就是漏了」
    would pass every other test in this file and fail this one.

    Not「ok」on the nose, and that is honest rather than sloppy: a search for
    the best of a hundred lags has a floor, and two unrelated speech envelopes
    over one window land anywhere up to about 0.45 (measured over 30 seeds of
    this same setup). Which is exactly why suspect counts as healthy — see the
    card test below.
    """
    clock = FakeClock()
    probe = EchoProbe(clock=clock)
    await _converse(probe, clock, leak=0.0, room=0.8)
    reading = probe.reading()
    assert reading is not None
    assert reading.verdict != "leaking", reading
    assert probe.status()["ok"] is True


async def test_her_voice_coming_back_is_caught() -> None:
    """The case Chromium cannot report: her reply reaches the microphone by a
    route its canceller never saw — OBS monitoring it back out, most likely.
    Nothing errors, the panel says cancellation is on, and turn detection
    starts firing on her own voice."""
    clock = FakeClock()
    probe = EchoProbe(clock=clock)
    await _converse(probe, clock, leak=1.0)
    reading = probe.reading()
    assert reading is not None
    assert reading.verdict == "leaking", reading


async def test_the_pauses_between_her_replies_do_not_lose_the_echo() -> None:
    """她回一句、停几秒、再回一句——这是每一场的样子。

    下行只在她说话时才有数据，上行一刻不停。两条包络要是各按「来了多少块」
    走，每一段停顿都会把它们错开停顿那么长，误差按轮累积、没有上界，两秒的
    延迟搜索补不回来。满强度的回声于是被判成健康——这个模块存在的唯一理由
    就此失效，而症状看起来像判停器坏了。
    """
    clock = FakeClock()
    probe = EchoProbe(clock=clock)
    await _converse(probe, clock, leak=1.0, gap_s=3.0, turns=5)
    reading = probe.reading()
    assert reading is not None
    assert reading.verdict == "leaking", reading


async def test_the_trip_through_the_air_shows_up_as_a_delay() -> None:
    """A hint, not a measurement — but it has to move with the real delay.

    It is how a direct acoustic path is told from one routed through another
    application, and a search that always returned lag 0 would still call the
    leak a leak.
    """
    clock = FakeClock()
    near = EchoProbe(clock=clock)
    await _converse(near, clock, leak=1.0, lag_s=0.06)
    close_by = near.reading()

    clock = FakeClock()
    far = EchoProbe(clock=clock)
    await _converse(far, clock, leak=1.0, lag_s=0.5)
    across_the_room = far.reading()

    assert close_by is not None and across_the_room is not None
    assert close_by.lag_ms < 200, close_by
    assert across_the_room.lag_ms > 400, across_the_room


async def test_a_faint_echo_counts_the_same_as_a_loud_one() -> None:
    """Amplitude-blind on purpose. A quiet leak false-triggers turn detection
    just as well, so 「any of her voice is coming back」 is the question.

    Her voice comes back at a tenth of the level she went out at, no louder
    than the room around the microphone.
    """
    clock = FakeClock()
    probe = EchoProbe(clock=clock)
    await _converse(probe, clock, leak=0.12, room=0.12)
    reading = probe.reading()
    assert reading is not None
    assert reading.verdict == "leaking", reading


async def test_silence_is_not_evidence() -> None:
    """她那一路在送，送的全是静音（供应商补的空档就是这个样子）。

    窗口里没有她的声音，麦克风再吵也没有可比对的东西。这时候报「ok」是一张
    没人挣来的健康证明。
    """
    clock = FakeClock()
    probe = EchoProbe(clock=clock)
    for _ in range(400):
        probe.note_played(_tone(_TICK_S, 24000, amp=0))
        probe.note_captured(_tone(_TICK_S, 16000, amp=3000))
        await clock.advance(_TICK_S)
    assert probe.reading() is None
    assert "开口" in str(probe.status()["state"]), probe.status()


async def test_a_reply_from_ten_minutes_ago_is_not_evidence_about_now() -> None:
    """她十分钟没说话了，卡还拿那时候的回复跟现在的麦克风做相关。

    下行收不到静音，包络就永远停在她最后一句上。「她还没开口」这道门于是只
    在第一次回复之前拦得住一次，之后再也不触发。
    """
    clock = FakeClock()
    probe = EchoProbe(clock=clock)
    await _converse(probe, clock, leak=1.0)
    assert probe.reading() is not None

    await clock.advance(600)
    for _ in range(50):  # the mic is still running; she simply has not spoken
        probe.note_captured(_tone(_TICK_S, 16000, amp=4000))
        await clock.advance(_TICK_S)
    assert probe.reading() is None, "拿十分钟前那段回复当现在的证据"
    # The microphone is fine — she just has not spoken. Two different reasons
    # for「判断不了」, and the card has to say which.
    assert "开口" in str(probe.status()["state"]), probe.status()


async def test_a_page_that_handed_the_devices_back_stops_being_judged() -> None:
    """页面把设备还回去，麦克风那条就断了，下行还在。

    _captured 停在页面走的那一刻，_played 继续换新，卡片于是拿当前的回复跟
    早就没了的麦克风做相关，读数是随机的。而且这一刻恰恰是最不能装懂的时候：
    声音回到了本机 sounddevice，那条路压根没有回声消除（docs/runbook.md:262）。
    """
    clock = FakeClock()
    probe = EchoProbe(clock=clock)
    await _converse(probe, clock, leak=1.0)
    assert probe.reading() is not None, "页面还拿着设备的时候本来就该判"

    for _ in range(400):  # 8 秒：她还在说，麦克风那一路没人喂了
        probe.note_played(_tone(_TICK_S, 24000, amp=9000))
        await clock.advance(_TICK_S)

    assert probe.reading() is None, "拿冻住的麦克风包络继续下结论"
    card = probe.status()
    assert card["ok"] is True
    assert "麦克风" in str(card["state"]), card


async def test_too_little_history_says_nothing() -> None:
    clock = FakeClock()
    fresh = EchoProbe(clock=clock)
    for _ in range(5):
        fresh.note_played(_tone(_TICK_S, 24000))
        fresh.note_captured(_tone(_TICK_S, 16000))
        await clock.advance(_TICK_S)
    assert fresh.reading() is None, "刚开始就下结论，等于拿噪声当证据"


async def test_the_health_card_stays_calm_about_a_suspicion() -> None:
    """Only an outright leak marks the card unhealthy. The streamer answering
    her produces genuine correlation, and a card that cries wolf gets ignored."""
    clock = FakeClock()
    quiet = EchoProbe(clock=clock)
    await _converse(quiet, clock, leak=0.0, room=0.8)
    assert quiet.status()["ok"] is True

    clock = FakeClock()
    loud = EchoProbe(clock=clock)
    await _converse(loud, clock, leak=1.0)
    card = loud.status()
    assert card["ok"] is False
    assert "漏" in str(card["state"]), card


# ------------------------------------------------------------ AudioInputSwitch


def _loud_frame(amplitude: int = 12000, samples: int = 320) -> bytes:
    import struct as _struct

    return b"".join(_struct.pack("<h", amplitude) for _ in range(samples))


async def test_noise_gate_substitutes_silence_and_never_drops_frames() -> None:
    from bilisama.ui.audio import AudioInputSwitch

    sent: list[bytes] = []

    async def sink(pcm: bytes) -> None:
        sent.append(pcm)

    switch = AudioInputSwitch(sink, noise_sensitivity=0)
    quiet = _loud_frame(amplitude=10)  # ~-70 dBFS, under any threshold
    for _ in range(12):
        await switch.push_audio(quiet)
    assert len(sent) == 12, "the provider's audio clock never misses a frame"
    assert all(frame == bytes(len(quiet)) for frame in sent[9:]), "gated frames are silence"

    sent.clear()
    await switch.push_audio(_loud_frame())
    assert sent[-1] != bytes(len(sent[-1])), "speech passes"
    for _ in range(8):
        await switch.push_audio(quiet)
    assert all(frame != bytes(len(frame)) for frame in sent), "the 8-frame hold keeps word endings"


async def test_disabled_input_sends_silence_but_keeps_metering() -> None:
    from bilisama.ui.audio import AudioInputSwitch

    sent: list[bytes] = []

    async def sink(pcm: bytes) -> None:
        sent.append(pcm)

    switch = AudioInputSwitch(sink)
    switch.set_enabled(False)
    await switch.push_audio(_loud_frame())
    assert sent == [bytes(640)], "off means silence on the wire, not a hole in the clock"
    assert switch.signal_level > 0, "the meter still shows the microphone is alive"


async def test_pause_drops_everything_and_browser_election_is_exclusive() -> None:
    from bilisama.ui.audio import AudioInputSwitch

    sent: list[bytes] = []

    async def sink(pcm: bytes) -> None:
        sent.append(pcm)

    switch = AudioInputSwitch(sink)
    switch.set_paused(True)
    await switch.push_audio(_loud_frame())
    assert sent == [], "a paused transport must see nothing — the socket is closed"
    switch.set_paused(False)

    switch.use_browser(True)
    await switch.push_audio(_loud_frame())
    assert sent == [] and switch.blocked_microphone_frames == 1
    await switch.push_browser_audio(_loud_frame())
    assert len(sent) == 1 and switch.browser_frames == 1
    switch.use_browser(False)
    await switch.push_browser_audio(_loud_frame())
    assert switch.blocked_browser_frames == 1


# ------------------------------------------------------------- UplinkRecorder


async def test_recorder_captures_exactly_what_the_backend_received(
    tmp_path: object,
) -> None:
    """The tap is downstream of both gates, which is the point of the file.

    A recording of the microphone would show speech the model never heard;
    replaying it would then prove nothing about a session that disagreed.
    """
    from pathlib import Path as _Path

    from bilisama.ui.audio import AudioInputSwitch, UplinkRecorder

    assert isinstance(tmp_path, _Path)
    out = tmp_path / "uplink.wav"
    sent: list[bytes] = []

    async def sink(pcm: bytes) -> None:
        sent.append(pcm)

    recorder = UplinkRecorder(out, flush_seconds=0.02)
    switch = AudioInputSwitch(sink, recorder=recorder)
    await switch.push_audio(_loud_frame())
    switch.set_enabled(False)
    await switch.push_audio(_loud_frame())  # substituted silence, and recorded as such
    recorder.close()

    with wave.open(str(out)) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2)
        written = w.readframes(w.getnframes())
    assert written == b"".join(sent), "the file is the wire, byte for byte"
    assert written[640:] == bytes(640), "the muted frame was recorded as the silence it became"


async def test_recorder_survives_an_unwritable_path_without_taking_the_uplink_down(
    tmp_path: object,
) -> None:
    from pathlib import Path as _Path

    from bilisama.ui.audio import AudioInputSwitch, UplinkRecorder

    assert isinstance(tmp_path, _Path)
    blocked = tmp_path / "wall"
    blocked.write_text("not a directory", encoding="utf-8")
    sent: list[bytes] = []

    async def sink(pcm: bytes) -> None:
        sent.append(pcm)

    recorder = UplinkRecorder(blocked / "uplink.wav", flush_seconds=0.02)
    switch = AudioInputSwitch(sink, recorder=recorder)
    await switch.push_audio(_loud_frame())
    await switch.push_audio(_loud_frame())

    assert recorder.stopped, "a failed write stops recording rather than retrying every 20 ms"
    assert len(sent) == 2, "and the uplink keeps its frames — losing the file is not losing audio"


async def test_recorder_caps_a_runaway_session_and_close_is_idempotent(
    tmp_path: object,
) -> None:
    from pathlib import Path as _Path

    from bilisama.ui.audio import UplinkRecorder

    assert isinstance(tmp_path, _Path)
    out = tmp_path / "capped.wav"
    recorder = UplinkRecorder(out, flush_seconds=0.02, max_seconds=0.04)
    for _ in range(10):
        recorder.feed(_loud_frame())
    assert recorder.stopped, "the cap holds; an all-day session must not fill the disk"
    capped = recorder.seconds
    recorder.feed(_loud_frame())
    assert recorder.seconds == capped, "frames after the cap are dropped, not counted"
    recorder.close()
    recorder.close()  # twice is a no-op, not a crash on a closed file
    with wave.open(str(out)) as w:
        assert w.getnframes() > 0, "what was captured before the cap is still playable"


async def test_recorder_is_optional_and_absent_by_default() -> None:
    from bilisama.ui.audio import AudioInputSwitch

    async def sink(pcm: bytes) -> None:
        return None

    switch = AudioInputSwitch(sink)
    await switch.push_audio(_loud_frame())
    assert "recorded_s" not in switch.status(), "no recorder, no row in the health card"
