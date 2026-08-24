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

from array import array
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

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

    __slots__ = ("_announce", "_close", "_flush", "_local", "_owner", "_send")

    def __init__(
        self,
        *,
        local: LocalAudio | None = None,
        announce: Callable[[AudioOwner | None], None] | None = None,
    ) -> None:
        """Args:
        local: The sounddevice pair to park and restore. None in tests, and
            in any run that never had one.
        announce: Told who holds the devices, on every change. The panel shows
            it; a tab that is only watching needs to know why it has no sound.
        """
        self._local = local
        self._announce = announce
        self._owner: AudioOwner | None = None
        self._send: Callable[[bytes], None] | None = None
        self._close: Callable[[], None] | None = None
        self._flush: Callable[[], None] | None = None

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
        if self._owner is not None and _RANK[who] <= _RANK[self._owner]:
            log.info("audio.claim_refused", who=who, holder=self._owner)
            return False
        first = self._owner is None
        displaced = self._close if not first else None
        self._owner = who
        self._send = send
        self._close = close
        self._flush = flush
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
        if self._owner != who:
            return
        self._owner = None
        self._send = None
        self._close = None
        self._flush = None
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
_BUCKET_MS = 20
# Six seconds of history, and a lag search covering two. The page schedules
# playback ahead of itself, so what we sent is heard some unknown amount later
# — anywhere inside its buffer. Searching wide costs 300x100 multiply-adds once
# a second, which is nothing.
_WINDOW = 6000 // _BUCKET_MS
_MAX_LAG = 2000 // _BUCKET_MS
# Below this the window is mostly silence and a correlation off it means
# nothing. Peak amplitude out of 32767.
_QUIET = 300


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

    __slots__ = ("_captured", "_played")

    def __init__(self) -> None:
        self._played = _Envelope(24000)
        self._captured = _Envelope(16000)

    def note_played(self, pcm: bytes) -> None:
        """One downlink chunk, on its way to whoever holds the speaker."""
        self._played.feed(pcm)

    def note_captured(self, pcm: bytes) -> None:
        """One uplink chunk, as the microphone heard it."""
        self._captured.feed(pcm)

    def reading(self) -> EchoReading | None:
        """The current estimate, or None when there is nothing to judge.

        None means she has not been speaking, so the microphone has no echo of
        hers to contain — silence is not evidence of anything.
        """
        played = self._played.window(_WINDOW)
        heard = self._captured.window(_WINDOW)
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
    """Peak amplitude per 20 ms bucket, built as chunks arrive."""

    __slots__ = ("_buckets", "_carry", "_per_bucket", "_step")

    def __init__(self, rate: int) -> None:
        self._per_bucket = rate * _BUCKET_MS // 1000
        # Every eighth sample. A peak envelope survives decimation fine, and
        # this runs on the audio path — 2000 abs() a second beats 20000.
        self._step = 8
        self._carry = b""
        self._buckets: deque[int] = deque(maxlen=_WINDOW)

    def feed(self, pcm: bytes) -> None:
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

    def window(self, size: int) -> list[int]:
        return list(self._buckets)[-size:]


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
