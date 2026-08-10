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

from collections.abc import AsyncIterator

from bilisama.clock import Clock
from bilisama.config.enums import ProviderName
from bilisama.realtime import dialect as dia
from bilisama.realtime import link
from bilisama.realtime.client import RealtimeClient
from bilisama.realtime.providers import profile_for

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
    ) -> None:
        profile = profile_for(provider)
        self._client = RealtimeClient(
            url, caps=profile.caps, codec=profile.codec, clock=clock, watchdog_s=watchdog_s
        )
        self._codec = profile.codec
        self._caps = profile.caps

    async def connect(self) -> None:
        await self._client.connect()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def set_context(self, instructions: str) -> None:
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
            instructions=spec.instructions,
            max_output_tokens=spec.max_tokens,
        )
        return await self._client.request_reply(frame)

    async def cancel(self, handle: link.ReplyHandle) -> None:
        await self._client.cancel(handle)

    def events(self) -> AsyncIterator[link.LinkEvent]:
        return self._client.events()
