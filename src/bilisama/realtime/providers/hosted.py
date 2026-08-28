"""The hosted-provider adapter, shaped by the mock until real endpoints answer.

One class serves both hosted profiles because their differences so far are
data (Capabilities, Codec) rather than behaviour. The day DashScope's session
rotation or its real capability bits (plan section 13 item 5) demand code of
their own, that code forks off into dashscope.py — not before; an empty
subclass would be an entity without a job.

Hosted providers own their TTS, so replies here are audio and set_context does
not pin the session to text.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from bilisama.clock import Clock, SystemClock
from bilisama.config.enums import ProviderName
from bilisama.obs.logging import get_logger
from bilisama.realtime import dialect as dia
from bilisama.realtime import link
from bilisama.realtime.client import RealtimeClient
from bilisama.realtime.providers import codec_for, compose_instructions, profile_for
from bilisama.realtime.resample import Resampler

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from bilisama.config.schema import HostedTurnConfig

__all__ = ["HostedLink"]

log = get_logger(__name__)

# What ui/web/js/capture-worklet.js produces, and what dev-talk's own mic
# pump sends. Every provider but OpenAI GA takes it unchanged.
_CAPTURE_RATE = 16000


class HostedLink:
    """SpeechLink over a hosted Realtime endpoint (DashScope or OpenAI)."""

    def __init__(
        self,
        url: str,
        provider: ProviderName,
        *,
        clock: Clock | None = None,
        watchdog_s: float = 25.0,
        headers: dict[str, str] | None = None,
        turn: HostedTurnConfig | None = None,
        voice: str = "",
        auto_reconnect: bool = True,
        reconnect_backoff_s: float = 1.0,
        session_cap_min: int | None = None,
        rotate_margin_min: float = 3.0,
        quiet_window_s: float = 0.6,
    ) -> None:
        """Args:
        quiet_window_s: How long the speaking floor holds after the streamer
            stops (plan section 3.3 rule 1). Handed in rather than computed
            here so that L3 can ask the LINK for it instead of doing its own
            arithmetic over provider config — that arithmetic was a two-armed
            branch whose `else` handed a fourth provider DashScope's timing.
        voice: Which voice the provider speaks in. Empty leaves the choice to
            the server, whose pick is not neutral — DashScope's is longanqian
            at 343 Hz, high enough to read as shrill. Names are the
            provider's; a wrong one draws a refusal listing the valid ones.
        session_cap_min: How long this endpoint lets one connection live.
            None takes the provider's published cap (ProviderProfile); 0
            disables rotation. It defaulted to 0, and every caller left it
            alone — so the rotation code shipped with no path that could
            reach it and the cap kept being met the passive way, by being cut
            off mid-sentence (backlog item 19). The cap is a property of the
            provider, so the adapter can know it without being told; a config
            field only has to exist to override it.
            We rotate `rotate_margin_min` early so the swap happens on our
            clock rather than mid-sentence on theirs.
        """
        profile = profile_for(provider)
        codec = codec_for(provider)
        self._provider = provider
        self._client = RealtimeClient(
            url,
            caps=profile.caps,
            codec=codec,
            clock=clock,
            watchdog_s=watchdog_s,
            headers=headers,
            auto_reconnect=auto_reconnect,
            reconnect_backoff_s=reconnect_backoff_s,
        )
        self._codec = codec
        self._caps = profile.caps
        self._turn = turn
        # Passthrough for everyone but OpenAI GA, which wants 24 kHz
        # uplink against the 16 kHz this chain captures at. Its downlink is
        # 24 kHz as well, which is already what we play, so nothing is
        # converted on the way in.
        self._uplink = Resampler(source_rate=_CAPTURE_RATE, target_rate=profile.uplink_rate)
        self._voice = voice
        self.quiet_window_s = quiet_window_s
        self._context = ""
        self._clock: Clock = clock or SystemClock()
        cap_min = profile.session_cap_min if session_cap_min is None else session_cap_min
        self._session_cap_s = max(0.0, (cap_min - rotate_margin_min) * 60.0)
        self._rotation: asyncio.Task[None] | None = None
        # Said once per link, not once per protected reply: see request_reply.
        self._warned_unprotected = False

    async def connect(self) -> None:
        """Open the socket, bootstrap the session, restore what we knew.

        The bootstrap frame carries modalities and turn detection; the context
        replay carries the persona. Both are per-connection state that a
        reconnect starts without, and no layer above this one re-sends them:
        Assembly pushes only on change (app.py refresh_context).
        """
        self._client.on_resume = self._resume_session
        await self._client.connect()
        await self._resume_session()
        self._arm_rotation()

    def _arm_rotation(self) -> None:
        """Restart the countdown. Every fresh socket gets a full lifetime."""
        if self._session_cap_s <= 0:
            return
        if self._rotation is not None:
            self._rotation.cancel()
        self._rotation = asyncio.create_task(self._rotate_when_due(), name="hosted:rotate")

    async def _rotate_when_due(self) -> None:
        await self._clock.sleep(self._session_cap_s)
        await self._client.rotate("session_cap")

    async def _resume_session(self) -> None:
        """Everything a fresh socket needs before it behaves like the old one.

        Called on the first connect and again after every reconnect, which is
        why it has to be idempotent: a session.update carrying the same values
        twice is free, and losing either half is not.
        """
        self._uplink.reset()
        frame = self._bootstrap_frame()
        if frame is not None:
            await self._client.send_command(frame)
            # Its absence is the failure that took a live probe to find: without
            # this frame DashScope never runs server VAD, and the stream just
            # sits there. Logged from the fields rather than the dict so the
            # line says what was asked for, not how the dialect spells it.
            log.info(
                "hosted.bootstrap_sent",
                provider=self._provider.value,
                turn_type=self._turn.type if self._turn is not None else "",
                voice=self._voice,
            )
        if self._context:
            await self.set_context(self._context)
        # A reconnect (ours or theirs) starts the clock over.
        self._arm_rotation()
        log.info(
            "hosted.session_replayed",
            provider=self._provider.value,
            bootstrapped=frame is not None,
            context_len=len(self._context),
            rotate_in_s=round(self._session_cap_s),
        )

    def _bootstrap_frame(self) -> dict[str, Any] | None:
        """The session bootstrap a hosted endpoint needs before audio flows.

        DashScope's beta endpoint leaves server VAD off until a session.update
        names it — dev-talk's wire mode carried this frame by hand until now
        (probed live 2026-08-10). Formats use the flat beta keys; the GA
        dialect nests them and runs server_vad by default, so a link built
        without turn config sends nothing at all.

        The voice rides along here rather than in set_context, because it is
        per-connection state like the rest of this frame: a rotated or
        reconnected socket goes back to the server's default without it.
        """
        session: dict[str, Any] = {}
        if self._turn is not None:
            # Field set follows the type: threshold/silence_duration_ms belong
            # to server_vad only — semantic_vad and smart_turn endpoints can
            # reject them outright (C9), killing the session on frame one.
            turn_detection: dict[str, Any] = {"type": self._turn.type}
            if self._turn.type == "server_vad":
                turn_detection["threshold"] = self._turn.threshold
                turn_detection["silence_duration_ms"] = self._turn.silence_duration_ms
            session[self._codec.modalities_key] = ["text", "audio"]
            session["turn_detection"] = turn_detection
            if not self._codec.nested_audio_format:
                session["input_audio_format"] = "pcm16"
                session["output_audio_format"] = "pcm16"
        if self._voice:
            session["voice"] = self._voice
        if not session:
            return None
        if self._codec.needs_session_type:
            session["type"] = "realtime"
        return {"type": dia.ClientEvent.SESSION_UPDATE.value, "session": session}

    async def aclose(self) -> None:
        if self._rotation is not None:
            self._rotation.cancel()
        await self._client.aclose()

    async def set_context(self, instructions: str) -> None:
        # Kept locally too: per-response instructions REPLACE the session's on
        # the wire (same protocol semantics as s2s), so request_reply
        # recomposes persona + per-turn ask.
        self._context = instructions
        await self._client.send_command(
            self._codec.session_patch(instructions=instructions, text_only=False)
        )

    async def push_audio(self, pcm: bytes) -> None:
        converted = self._uplink.feed(pcm)
        if not converted:
            # A chunk too short to produce one output sample at this ratio.
            # Forwarding it would base64 an empty buffer into an
            # input_audio_buffer.append with `"audio": ""`, which the GA
            # endpoint rejects — a rate conversion turning a valid frame into a
            # protocol error is the wrong way round.
            return
        await self._client.push_audio(converted)

    async def add_context_item(self, text: str, *, role: str = "user") -> None:
        # Content types are role-matched in the Realtime item schema: assistant
        # text is output_text, everyone else's is input_text.
        content_type = "output_text" if role == "assistant" else "input_text"
        await self._client.send_command(
            {
                "type": dia.ClientEvent.ITEM_CREATE.value,
                "item": {
                    "type": "message",
                    "role": role,
                    "content": [{"type": content_type, "text": text}],
                },
            }
        )

    async def request_reply(self, spec: link.ReplySpec) -> link.ReplyHandle:
        if spec.protected and not self._warned_unprotected:
            # Half of protection works here and half does not, which is why it
            # took months to notice: the scheduler still refuses to cancel a
            # protected reply itself (its _on_barge_in checks
            # _protection_active first), but the provider's own barge-in stays
            # armed, so the streamer's next word kills the paid answer
            # anyway. Closing it means disarming turn_detection over the
            # wire the way s2s does, and no live probe has confirmed the field
            # on either hosted endpoint — inventing one risks a rejected
            # session.update taking the reply with it. Until that probe lands
            # the gap is at least audible in the log instead of silent.
            self._warned_unprotected = True
            log.warning(
                "hosted.protection_unsupported",
                provider=self._provider.value,
                error_text="这个语音后端还不支持付费保护：SC 答谢仍会被主播开口打断。",
            )
        # Out-of-band only where it does not cost the slot: on GA it runs in
        # parallel; on the beta dialect the bit is a guess pending the real
        # endpoint test, so stay in-band there rather than assume.
        frame = self._codec.response_create(
            out_of_band=self._caps.out_of_band_exempt_from_slot,
            text_only=False,
            instructions=compose_instructions(self._context, spec.instructions),
            max_output_tokens=spec.max_tokens,
        )
        return await self._client.request_reply(frame)

    async def cancel(self, handle: link.ReplyHandle) -> None:
        await self._client.cancel(handle)

    async def end_protection(self) -> None:
        # Nothing to re-arm: request_reply never disarmed anything (backlog
        # item 45, and it warns when asked). A paired no-op keeps the
        # scheduler's lifecycle uniform across adapters, and it stays a no-op
        # rather than sending a session.update no endpoint was probed for.
        return

    def events(self) -> AsyncIterator[link.LinkEvent]:
        return self._client.events()
