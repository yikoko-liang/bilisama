"""Rate conversion for the one provider whose uplink is not 16 kHz.

Everything in this repo captures at 16 kHz (ui/web/js/capture-worklet.js) and
plays at 24 kHz (ui/web/js/audio.js), which matches s2s, DashScope and
Volcengine on both sides. OpenAI GA is 24 kHz in BOTH directions, so its
downlink already matches our playback and only the uplink needs converting.
Plan section 3.1 says "a resampler on each side"; that overstates it — there
is nothing for a downlink resampler to do.

Linear interpolation rather than a polyphase filter, deliberately. The input
is already band-limited to 8 kHz by the capture worklet, so upsampling cannot
add detail and the images linear interpolation leaves sit above the speech
band, where the far end's own front-end drops them. If an endpoint's
recognition ever measurably degrades on this path, a windowed-sinc kernel is
the fix and it drops in behind the same interface — but building one first
would be guessing at a problem nobody has reported.

No numpy: it was removed as a dependency that nothing imported (backlog item
51), and re-adding 21 MiB to interpolate 320 samples every 20 ms would be a
poor trade. `array` keeps the inner loop off Python objects.
"""

from __future__ import annotations

from array import array

__all__ = ["Resampler"]


class Resampler:
    """Stateful rate conversion for a stream of 16-bit little-endian mono PCM.

    Stateful because the frames arrive 20 ms at a time and the interpolation
    at a chunk boundary needs the previous chunk's last sample. Dropping that
    carry-over puts a discontinuity every 20 ms — 50 clicks a second, which
    reads as a bad microphone rather than as a bug here.
    """

    def __init__(self, *, source_rate: int, target_rate: int) -> None:
        """Args:
        source_rate: What the samples arriving here are.
        target_rate: What the far end expects.

        Raises:
            ValueError: Either rate is not positive.
        """
        if source_rate <= 0 or target_rate <= 0:
            raise ValueError(f"采样率必须是正数：{source_rate} → {target_rate}")
        self._source = source_rate
        self._target = target_rate
        # Where in the input stream the next output sample falls, carried
        # across chunks as a fraction so the phase never drifts.
        self._position = 0.0
        self._tail: int | None = None

    @property
    def passthrough(self) -> bool:
        """Whether this converts anything. A caller can skip the copy."""
        return self._source == self._target

    def reset(self) -> None:
        """Forget the carry-over. For a new connection, where the previous
        stream's last sample has nothing to do with this one's first."""
        self._position = 0.0
        self._tail = None

    def feed(self, pcm: bytes) -> bytes:
        """Convert one chunk.

        Args:
            pcm: 16-bit little-endian mono samples. An odd length is a caller
                bug — half a sample cannot be interpolated — and raises rather
                than being silently rounded, because a one-byte offset turns
                every subsequent sample into noise.

        Returns:
            The converted samples, same format. May be empty when the chunk is
            too short to produce an output sample at this ratio.

        Raises:
            ValueError: The byte count is odd.
        """
        if self.passthrough:
            return pcm
        if len(pcm) % 2:
            raise ValueError(f"PCM 字节数是奇数（{len(pcm)}），16 位样本切在了一半")
        if not pcm:
            return b""

        source = array("h")
        source.frombytes(pcm)
        # The previous chunk's last sample sits at index 0 of the working
        # buffer, which is what `_position` is measured against.
        if self._tail is not None:
            source.insert(0, self._tail)

        step = self._source / self._target
        out = array("h")
        position = self._position
        limit = len(source) - 1
        while position < limit:
            left = int(position)
            frac = position - left
            a = source[left]
            b = source[left + 1]
            out.append(int(a + (b - a) * frac))
            position += step

        self._tail = source[-1]
        # Re-base onto the next chunk, whose sample 0 is the tail we just kept.
        self._position = position - limit
        return out.tobytes()
