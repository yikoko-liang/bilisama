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

from typing import TYPE_CHECKING, Literal, Protocol

from bilisama.obs.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = ["AudioBroker", "AudioOwner", "LocalAudio"]

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

    __slots__ = ("_announce", "_local", "_owner", "_send")

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

    @property
    def owner(self) -> AudioOwner | None:
        return self._owner

    @property
    def local_is_live(self) -> bool:
        """True when the sounddevice pair is the one being heard."""
        return self._owner is None

    async def claim(self, who: AudioOwner, *, send: Callable[[bytes], None]) -> bool:
        """Hand the devices to a page, if it outranks whoever has them.

        Args:
            who: shell or browser; a shell displaces a tab, never the reverse.
            send: How to push downlink PCM to this client.

        Returns:
            True if the caller now owns the devices. False means someone
            stronger has them and this client should watch, not listen.
        """
        if self._owner is not None and _RANK[who] <= _RANK[self._owner]:
            log.info("audio.claim_refused", who=who, holder=self._owner)
            return False
        first = self._owner is None
        self._owner = who
        self._send = send
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
        if self._local is not None:
            await self._local.resume()
        log.info("audio.released", who=who)
        self._notify()

    def play(self, pcm: bytes) -> None:
        """Send one downlink chunk to the owner, or drop it.

        Dropping is right during the gap between a tab closing and the local
        pair coming back: those samples have nowhere to go, and holding them
        would only play them late.
        """
        if self._send is not None:
            self._send(pcm)

    def _notify(self) -> None:
        if self._announce is not None:
            self._announce(self._owner)
