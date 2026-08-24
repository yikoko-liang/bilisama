"""Who holds the microphone and the speaker, and the socket they travel on.

The point of moving audio into the page is echo cancellation. Chromium has it
(`getUserMedia({echoCancellation: true})`), the shell IS Chromium, and without
it the streamer has to wear headphones or give up barge-in during playback —
the trade dev_talk has been apologising for on every start.

There is a hard precondition, and it is what shapes this module: the canceller
can only subtract audio the browser itself rendered. Capturing in the page
while playback stays in sounddevice would leave it with no reference signal
and no effect at all. So both ends move, together, or neither does.

Two decisions worth stating because their alternatives look reasonable:

Audio gets its own socket. The control hub broadcasts to every client and
drops the oldest frame when a queue fills — right for sticky state, wrong for
samples, and 48 KB/s of PCM through it would starve the panel. This one is
point to point with the owner and carries binary frames.

Exactly one owner at a time, and the shell outranks a browser tab. Two
capture streams would fight over the device; two playback queues would talk
over each other. The shell is the product path and a tab is a developer's
convenience, so when both are open the shell wins and the tab is told plainly
that it is watching rather than left looking broken.
"""

from __future__ import annotations

import asyncio
from array import array
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from bilisama.clock import Clock, SystemClock
from bilisama.obs.logging import get_logger

__all__ = [
    "AudioBroker",
    "AudioOwner",
    "EchoProbe",
    "EchoReading",
    "LocalAudio",
    "PlaybackTally",
]

log = get_logger(__name__)

# Ranked, best first. A claim only displaces a strictly weaker holder.
AudioOwner = Literal["shell", "browser"]
_RANK: dict[AudioOwner, int] = {"shell": 2, "browser": 1}


class LocalAudio(Protocol):
    """The sounddevice pair the broker parks while a page holds the devices.

    dev-talk implements this over its own microphone pump and speaker. Both
    calls run off the loop: releasing a PortAudio stream blocks, and doing it
    here would freeze the very loop that has to keep the socket alive.
    """

    async def suspend(self) -> None: ...

    async def resume(self) -> None: ...


class AudioBroker:
    """At most one holder of the microphone and speaker.

    Nobody holding is the normal state, not a failure: `--no-ui`, a shell that
    was never installed, a page nobody opened. Local audio simply keeps
    running, which is what every session did before this existed.
    """

    __slots__ = (
        "_announce",
        "_close",
        "_flush",
        "_local",
        "_lock",
        "_on_handoff",
        "_owner",
        "_send",
    )

    def __init__(
        self,
        *,
        local: LocalAudio | None = None,
        announce: Callable[[AudioOwner | None], None] | None = None,
        on_handoff: Callable[[], None] | None = None,
    ) -> None:
        """Args:
        local: The sounddevice pair to park and restore. None in tests, and
            in any run that never had one.
        announce: Told who holds the devices, on every change. The panel shows
            it; a tab that is only watching needs to know why it has no sound.
        on_handoff: Told that the devices changed hands — a claim that
            displaces the holder, and every release. It means "nothing still
            queued is ever coming back", and PlaybackTally is who needs to hear
            it: the receipts it counts are sent BY the page, so a page that
            walks away with buffers already on the Web Audio timeline never
            reports their end. The count never returns to zero and the floor
            gate stays shut for the rest of the session. The link layer carries
            the same fix for the same shape — floor.on_link_lost (floor.py:70-83)
            exists because a socket dying mid-sentence never sends the event
            that would reopen the floor.
        """
        self._local = local
        self._announce = announce
        self._on_handoff = on_handoff
        self._owner: AudioOwner | None = None
        self._send: Callable[[bytes], None] | None = None
        self._close: Callable[[], None] | None = None
        self._flush: Callable[[], None] | None = None
        # Held across the whole of claim() and release(). Both await in the
        # middle: dev-talk's local pair wraps blocking PortAudio calls in
        # asyncio.to_thread (dev_talk's _LocalPair), which hands the loop away
        # whatever the device costs. A page reload closes one socket a few
        # milliseconds before the next one opens, so without this the release
        # tail restarts the microphone the new owner's claim just parked, and
        # the session ends with two capture streams on one device — the thing
        # this module exists to prevent.
        self._lock = asyncio.Lock()

    @property
    def owner(self) -> AudioOwner | None:
        return self._owner

    @property
    def local_is_live(self) -> bool:
        """True when the sounddevice pair is the one being heard."""
        return self._owner is None

    async def claim(
        self,
        who: AudioOwner,
        *,
        send: Callable[[bytes], None],
        close: Callable[[], None] | None = None,
        flush: Callable[[], None] | None = None,
    ) -> bool:
        """Hand the devices to a page, if it outranks whoever has them.

        Args:
            who: shell or browser; a shell displaces a tab, never the reverse.
            send: How to push downlink PCM to this client.
            close: How to hang up on this client when something stronger takes
                the devices. Without it a displaced tab keeps a live capture
                running on a socket that will never be fed again, and its panel
                goes on claiming to hold devices it lost.
            flush: How to drop everything queued for this client and tell it to
                stop playing. See flush().

        Returns:
            True if the caller now owns the devices. False means someone
            stronger has them and this client should watch, not listen.
        """
        async with self._lock:
            if self._owner is not None and _RANK[who] <= _RANK[self._owner]:
                log.info("audio.claim_refused", who=who, holder=self._owner)
                return False
            first = self._owner is None
            displaced = self._close if not first else None
            self._owner = who
            self._send = send
            self._close = close
            self._flush = flush
            if not first:
                # A handover, whether or not the loser left a close callback.
                self._handoff()
            if displaced is not None:
                log.info("audio.displaced", by=who)
                displaced()
            if first and self._local is not None:
                # Only on the way in from nobody: a shell taking over from a tab
                # must not restart devices that are already parked.
                await self._local.suspend()
            log.info("audio.claimed", who=who)
            self._notify()
            return True

    async def release(self, who: AudioOwner) -> None:
        """Give the devices back. Ignored from a client that never had them."""
        async with self._lock:
            if self._owner != who:
                return
            self._owner = None
            self._send = None
            self._close = None
            self._flush = None
            self._handoff()
            if self._local is not None:
                await self._local.resume()
            log.info("audio.released", who=who)
            self._notify()

    def flush(self) -> None:
        """Barge-in: drop what is queued and tell the page to stop.

        The control socket already carries playback.clear, and on its own that
        is not enough — it is a DIFFERENT connection, so audio the server had
        already queued arrives after the page has cleared and gets scheduled
        all over again. What the streamer hears is the text stopping while the
        voice carries on for a beat, which is exactly what a barge-in must not
        feel like.

        So the stop travels with the audio: everything queued goes, and the
        marker behind it is ordered after every sample already in flight.
        """
        if self._flush is not None:
            self._flush()

    def play(self, pcm: bytes) -> None:
        """Send one downlink chunk to the owner, or drop it.

        Dropping is right during the gap between a tab closing and the local
        pair coming back: those samples have nowhere to go, and holding them
        would only play them late.
        """
        if self._send is not None:
            self._send(pcm)

    def announce_through(self, announce: Callable[[AudioOwner | None], None]) -> None:
        """Say who holds the devices this way from now on.

        Set by create_ui_app rather than by whoever built the broker: the hub
        it broadcasts on is the app's, and a caller that wires one but not the
        other leaves every panel waiting on an answer that never comes.
        """
        self._announce = announce

    def _notify(self) -> None:
        if self._announce is not None:
            self._announce(self._owner)

    def _handoff(self) -> None:
        if self._on_handoff is not None:
            self._on_handoff()


class PlaybackTally:
    """How many segments the page still has to play, and the gate that reads it.

    Backlog item 41 exists for this class. The receipts are per SEGMENT: one
    reply is scheduled as many buffers, and between any two of them there is an
    instant where the previous has ended and the next has not started. Treating
    that instant as "finished speaking" opens the floor mid-sentence and lets
    the scheduler dispatch over her — which is precisely how the 106 seconds of
    unplayed audio accumulated the first time, just arriving from a different
    direction.

    So the gate follows a COUNT, not the last event. It closes on the first
    segment and opens only when the last one is done.
    """

    __slots__ = ("_notify", "_on_playback", "_outstanding")

    def __init__(
        self,
        *,
        on_playback: Callable[[bool], None],
        notify: Callable[[], None],
    ) -> None:
        """Args:
        on_playback: SpeakingFloor.on_playback — the gate itself, unchanged
            product code that only wanted a producer.
        notify: Scheduler.notify. State gates release on events, and playback
            finishing is not one the link ever sends.
        """
        self._on_playback = on_playback
        self._notify = notify
        self._outstanding = 0

    @property
    def outstanding(self) -> int:
        return self._outstanding

    def started(self) -> None:
        self._outstanding += 1
        if self._outstanding == 1:
            self._on_playback(True)

    def ended(self) -> None:
        # Clamped: a receipt for a segment that was already cancelled would
        # otherwise drive the count negative and wedge the gate shut.
        self._outstanding = max(0, self._outstanding - 1)
        if self._outstanding == 0:
            self._on_playback(False)
            self._notify()

    def cancelled(self) -> None:
        """A barge-in took everything scheduled. Nothing is outstanding now."""
        if self._outstanding == 0:
            return
        self._outstanding = 0
        self._on_playback(False)
        self._notify()


# One envelope point per 20 ms. The trick that makes this cheap: buckets are a
# TIME grid, so a 24 kHz downlink and a 16 kHz uplink both land on the same
# 50 Hz axis and no resampling is needed to compare them.
#
# A time grid it has to actually BE, which is the part that bites: the uplink
# feeds every 20 ms forever (dev-talk's uplink) while the downlink only feeds
# while she is speaking (dev-talk calls note_played on a reply delta and on
# nothing else). Counting arrivals instead of seconds would splice her replies
# end to end, sliding the two streams apart by every pause between them —
# without bound, and far past what the lag search below can recover.
_BUCKET_MS = 20
_BUCKET_S = _BUCKET_MS / 1000
# Six seconds of history, and a lag search covering two. The page schedules
# playback ahead of itself, so what we sent is heard some unknown amount later
# — anywhere inside its buffer. Searching wide costs 300x100 multiply-adds once
# a second, which is nothing.
_WINDOW = 6000 // _BUCKET_MS
_MAX_LAG = 2000 // _BUCKET_MS
# Below this the window is mostly silence and a correlation off it means
# nothing. Peak amplitude out of 32767.
_QUIET = 300
# Buckets kept behind the window. Audio reaches us faster than it plays — this
# project once measured 106 seconds of it inside 48 seconds of wall clock — so
# the newest downlink buckets routinely describe sound nobody has heard yet.
# Holding six extra seconds is what lets a read slide back to the part that is
# actually in the air; a page further ahead than that gets no reading at all,
# which is the honest answer rather than a correlation against the future.
_DEPTH = _WINDOW + 6000 // _BUCKET_MS


@dataclass(frozen=True, slots=True)
class EchoReading:
    """How much of her own voice came back through the microphone."""

    leak: float
    """0..1. Correlation between what we played and what the mic heard."""
    lag_ms: int
    """Roughly where the match sat. A hint, not a measurement: speech segments
    run long against a 20 ms grid, so a shifted envelope still overlaps itself
    heavily and neighbouring lags score almost the same. Useful for telling a
    direct acoustic path from one routed through another application, and not
    for anything finer."""
    verdict: Literal["ok", "suspect", "leaking"]


class EchoProbe:
    """Is she hearing herself? The one question Chromium cannot answer.

    Echo cancellation only subtracts audio the browser itself rendered
    (media_switches.cc:506-509). When her voice reaches the microphone by any
    other route — OBS monitoring it back out to the speakers is the one this
    product actively recommends — the canceller is working perfectly and the
    echo is there anyway. Nothing errors. The panel says cancellation is on.
    The only symptom is turn detection firing on her own voice, which reads as
    a broken endpointer and sends people tuning a VAD threshold that was never
    the problem (plan section 11).

    So this watches both streams and asks whether the microphone's loudness
    envelope contains a delayed copy of the reply's. It never cancels
    anything — it only tells you whether something else should have.

    An indicator, not proof. The streamer answering her produces some genuine
    correlation, so the number is reported rather than turned into a verdict
    the caller cannot see behind.

    Amplitude-blind by construction: the correlation is normalised, so a faint
    echo and a loud one both read near 1. That is the right choice here —
    "any of her voice is coming back" is the question, and a quiet leak
    false-triggers turn detection just as well as a loud one.

    One honest limitation, found staging a leak against the real endpoint: the
    fault this looks for eats its own evidence. A real echo trips the server's
    turn detection, the reply is cancelled, and her speech stops — so the
    window fills with silence and the correlation drops. It reads clearly over
    a stream that keeps being interrupted, and reads weakly over the single
    interruption that started it.
    """

    def status(self) -> dict[str, object]:
        """A health-registry card. Shows up in the panel alongside the others."""
        reading = self.reading()
        if reading is None:
            if not self._captured.window(_WINDOW, self._clock.monotonic()):
                # Nobody is feeding the microphone: no page holds the devices,
                # so the sound is back on the local sounddevice pair, where
                # there is no echo cancellation at all. Saying 「没听见她自己」
                # here would be the most misleading moment to say it.
                return {"ok": True, "state": "麦克风这一路没数据，判断不了"}
            return {"ok": True, "state": "她还没开口，没什么可判断的"}
        wording = {
            "ok": "没听见她自己",
            "suspect": "疑似听见了她自己",
            "leaking": "她的声音正漏进麦克风",
        }[reading.verdict]
        return {
            # suspect stays healthy: the streamer answering her is genuine
            # correlation, and a card that cries wolf gets ignored.
            "ok": reading.verdict != "leaking",
            "state": wording,
            "leak": reading.leak,
            "lag_ms": reading.lag_ms,
        }

    __slots__ = ("_captured", "_clock", "_played")

    def __init__(self, *, clock: Clock | None = None) -> None:
        """Args:
        clock: Both envelopes are laid out on it, which is what keeps the two
            streams comparable when one of them goes quiet for a while. A test
            passes a fake so a whole conversation can happen in milliseconds.
        """
        self._clock: Clock = clock or SystemClock()
        self._played = _Envelope(24000)
        self._captured = _Envelope(16000)

    def note_played(self, pcm: bytes) -> None:
        """One downlink chunk, on its way to whoever holds the speaker."""
        self._played.feed(pcm, self._clock.monotonic())

    def note_captured(self, pcm: bytes) -> None:
        """One uplink chunk, as the microphone heard it."""
        self._captured.feed(pcm, self._clock.monotonic())

    def reading(self) -> EchoReading | None:
        """The current estimate, or None when there is nothing to judge.

        None means one of the two sides has nothing recent to offer: she has
        not been speaking, so the microphone has no echo of hers to contain, or
        nobody is feeding the microphone at all. Silence is not evidence of
        anything, and neither is a stream that stopped.
        """
        now = self._clock.monotonic()
        played = self._played.window(_WINDOW, now)
        heard = self._captured.window(_WINDOW, now)
        span = min(len(played), len(heard))
        if span < _MAX_LAG + _WINDOW // 4:
            return None
        played, heard = played[-span:], heard[-span:]
        if max(played) < _QUIET:
            return None

        best, best_lag = 0.0, 0
        for lag in range(_MAX_LAG):
            # The reply leads: what we sent at t is heard at t + lag.
            score = _correlate(heard[lag:], played[: span - lag])
            if score > best:
                best, best_lag = score, lag
        verdict: Literal["ok", "suspect", "leaking"] = (
            "leaking" if best >= 0.5 else "suspect" if best >= 0.25 else "ok"
        )
        return EchoReading(leak=round(best, 3), lag_ms=best_lag * _BUCKET_MS, verdict=verdict)


class _Envelope:
    """Peak amplitude per 20 ms bucket, laid out on the clock.

    Every bucket stands for a real 20 ms of the session, and a stream that
    sends nothing for a while leaves that time behind as silence rather than
    closing the gap. Both envelopes are read right-aligned to the same instant,
    so a gap that quietly collapsed would slide one against the other by its
    own length — and the downlink is nothing but gaps between replies.
    """

    __slots__ = ("_buckets", "_carry", "_covered_to", "_per_bucket", "_step")

    def __init__(self, rate: int) -> None:
        self._per_bucket = rate * _BUCKET_MS // 1000
        # Every eighth sample. A peak envelope survives decimation fine, and
        # this runs on the audio path — 2000 abs() a second beats 20000.
        self._step = 8
        self._carry = b""
        self._buckets: deque[int] = deque(maxlen=_DEPTH)
        # When the newest bucket ends, on the caller's clock. None until the
        # first chunk: an envelope nobody has fed covers no time at all.
        self._covered_to: float | None = None

    def feed(self, pcm: bytes, now: float) -> None:
        """One chunk, and the moment it was handed over.

        Args:
            pcm: 16-bit mono samples at this envelope's rate.
            now: Monotonic seconds. For the downlink this is when the chunk was
                handed to the page rather than when it will come out of the
                speaker; the two differ by whatever the page has buffered, and
                the lag search in EchoProbe.reading is what absorbs that.
        """
        if self._covered_to is None:
            self._covered_to = now
        idle = self._idle_buckets(now)
        if idle:
            # Nothing was sent for this long, so the device made no sound for
            # this long. The carry goes with it: those bytes are the tail of
            # the audio BEFORE the gap and must not share a bucket with what
            # comes after it.
            self._buckets.extend([0] * min(idle, _DEPTH))
            self._covered_to += idle * _BUCKET_S
            self._carry = b""
        data = self._carry + pcm
        width = self._per_bucket * 2
        whole = len(data) // width
        for i in range(whole):
            block = data[i * width : (i + 1) * width]
            samples = array("h")
            samples.frombytes(block)
            peak = 0
            for j in range(0, len(samples), self._step):
                value = samples[j]
                # -32768 has no positive counterpart; clamp rather than let
                # abs() hand back a number that does not fit the range.
                peak = max(peak, 32767 if value == -32768 else abs(value))
            self._buckets.append(peak)
        self._carry = data[whole * width :]
        self._covered_to += whole * _BUCKET_S

    def window(self, size: int, now: float) -> list[int]:
        """The `size` buckets ending at `now`, oldest first.

        Time nobody fed comes back as zeros, and buckets describing sound that
        has not been heard yet are held back until it has. An empty list means
        no part of this window was ever fed — the honest answer when a page
        hands the devices back and stops sending, because the samples it sent
        before that must not go on being judged as if the microphone were
        still there.
        """
        if self._covered_to is None:
            return []
        buckets = list(self._buckets)
        drift = round((now - self._covered_to) / _BUCKET_S)
        if drift >= 0:  # the stream ran dry: pad the silence back on
            if drift >= size:
                return []
            return buckets[-(size - drift) :] + [0] * drift
        end = len(buckets) + drift  # buffered ahead: leave the unplayed tail out
        if end <= 0:
            return []
        return buckets[max(0, end - size) : end]

    def _idle_buckets(self, now: float) -> int:
        """Whole buckets of silence between the newest one and `now`."""
        if self._covered_to is None:
            return 0
        return max(0, int((now - self._covered_to) / _BUCKET_S))


def _correlate(left: list[int], right: list[int]) -> float:
    """Normalised correlation of two envelopes, 0 when either is flat."""
    span = min(len(left), len(right))
    if span < 8:
        return 0.0
    left, right = left[:span], right[:span]
    mean_l = sum(left) / span
    mean_r = sum(right) / span
    cov = var_l = var_r = 0.0
    for a, b in zip(left, right, strict=True):
        da, db = a - mean_l, b - mean_r
        cov += da * db
        var_l += da * da
        var_r += db * db
    if var_l <= 0 or var_r <= 0:
        return 0.0
    return max(0.0, float(cov / (var_l * var_r) ** 0.5))
