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
from bilisama.realtime import dialect as dia
from bilisama.realtime import link
from bilisama.realtime.client import RealtimeClient
from bilisama.realtime.providers import compose_instructions, profile_for

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from bilisama.config.schema import HostedTurnConfig

__all__ = ["HostedLink"]


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
        auto_reconnect: bool = True,
        reconnect_backoff_s: float = 1.0,
        session_cap_min: int = 0,
        rotate_margin_min: float = 3.0,
    ) -> None:
        """Args:
        session_cap_min: How long this endpoint lets one connection live.
            DashScope 120, OpenAI 60 (plan section 3.1); 0 disables rotation.
            We rotate `rotate_margin_min` early so the swap happens on our
            clock rather than mid-sentence on theirs.
        """
        profile = profile_for(provider)
        self._client = RealtimeClient(
            url,
            caps=profile.caps,
            codec=profile.codec,
            clock=clock,
            watchdog_s=watchdog_s,
            headers=headers,
            auto_reconnect=auto_reconnect,
            reconnect_backoff_s=reconnect_backoff_s,
        )
        self._codec = profile.codec
        self._caps = profile.caps
        self._turn = turn
        self._context = ""
        self._clock: Clock = clock or SystemClock()
        self._session_cap_s = max(0.0, (session_cap_min - rotate_margin_min) * 60.0)
        self._rotation: asyncio.Task[None] | None = None

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
        frame = self._bootstrap_frame()
        if frame is not None:
            await self._client.send_command(frame)
        if self._context:
            await self.set_context(self._context)
        # A reconnect (ours or theirs) starts the clock over.
        self._arm_rotation()

    def _bootstrap_frame(self) -> dict[str, Any] | None:
        """The session bootstrap a hosted endpoint needs before audio flows.

        DashScope's beta endpoint leaves server VAD off until a session.update
        names it — dev-talk's wire mode carried this frame by hand until now
        (probed live 2026-08-10). Formats use the flat beta keys; the GA
        dialect nests them and runs server_vad by default, so a link built
        without turn config sends nothing at all.
        """
        if self._turn is None:
            return None
        # Field set follows the type: threshold/silence_duration_ms belong to
        # server_vad only — semantic_vad and smart_turn endpoints can reject
        # them outright (C9), which would kill the session on frame one.
        turn_detection: dict[str, Any] = {"type": self._turn.type}
        if self._turn.type == "server_vad":
            turn_detection["threshold"] = self._turn.threshold
            turn_detection["silence_duration_ms"] = self._turn.silence_duration_ms
        session: dict[str, Any] = {
            self._codec.modalities_key: ["text", "audio"],
            "turn_detection": turn_detection,
        }
        if self._codec.needs_session_type:
            session["type"] = "realtime"
        if not self._codec.nested_audio_format:
            session["input_audio_format"] = "pcm16"
            session["output_audio_format"] = "pcm16"
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
        await self._client.push_audio(pcm)

    async def add_context_item(self, text: str, *, role: str = "user") -> None:
        await self._client.send_command(
            {
                "type": dia.ClientEvent.ITEM_CREATE.value,
                "item": {
                    "type": "message",
                    "role": role,
                    "content": [{"type": "input_text", "text": text}],
                },
            }
        )

    async def request_reply(self, spec: link.ReplySpec) -> link.ReplyHandle:
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
        # Hosted protection is not implemented yet (request_reply ignores
        # spec.protected too — backlog item 19); a paired no-op keeps the
        # scheduler's lifecycle uniform across adapters.
        return

    def events(self) -> AsyncIterator[link.LinkEvent]:
        return self._client.events()
