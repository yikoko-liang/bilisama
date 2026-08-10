"""The speech-to-speech adapter: where its eight rules live and die.

Plan section 3.3 found eight ways a client can break this provider's turn
machinery. Every one is handled here or in the shared client, and none of them
is visible above SpeechLink:

1. Injections never go in-band. An in-band create inherits the streamer's open
   speculative turn (handlers/response.py:236-238) and loses its reply the
   moment they resume — verified live, tests/integration/test_real_server.py.
   request_reply therefore always sends conversation="none" with no input.
2. The watchdog lives in RealtimeClient.
3. No pre-emption of a pending implicit reply is attempted: cancel() exists for
   replies that have started; the scheduler queues rather than races (stage 2).
4. Slot bookkeeping in RealtimeClient never pairs created/done.
5. Command serialisation in RealtimeClient.
6. protected replies flip turn_detection.interrupt_response for their duration,
   always combined with out-of-band — the in-band-plus-no-interrupt combination
   is the one that dies silently.
7. push_audio passes through unserialised, and muting means sending silence —
   the adapter has no way to say "stop appending".
8. There is no commit, no clear: those methods simply do not exist here.

The session is pinned to text at set_context time (patch A's default covers the
implicit turn; rule-abiding explicit creates state text themselves via
Codec.response_create).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from bilisama.clock import Clock
from bilisama.config.enums import ProviderName
from bilisama.realtime import dialect as dia
from bilisama.realtime import link
from bilisama.realtime.client import RealtimeClient
from bilisama.realtime.providers import profile_for

__all__ = ["S2SLink"]


class S2SLink:
    """SpeechLink over a speech-to-speech server."""

    def __init__(
        self,
        url: str,
        *,
        clock: Clock | None = None,
        watchdog_s: float = 25.0,
        text_replies: bool = True,
    ) -> None:
        """Args:
        text_replies: True (the shipping path) pins the session and every
            explicit create to text — the patched server hands us prose and
            stage 4's TTS speaks it. False leaves the modality at the
            server's default (audio), which is what dev-talk's director mode
            wants against the zero-patch official pipeline: that server owns
            a real TTS, and a text-pinned session would mute the whole run.
        """
        profile = profile_for(ProviderName.S2S)
        self._client = RealtimeClient(
            url, caps=profile.caps, codec=profile.codec, clock=clock, watchdog_s=watchdog_s
        )
        self._codec = profile.codec
        self._text_replies = text_replies

    async def connect(self) -> None:
        await self._client.connect()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def set_context(self, instructions: str) -> None:
        # text_only pins the SESSION, which is what the implicit VAD turn obeys.
        await self._client.send_command(
            self._codec.session_patch(instructions=instructions, text_only=self._text_replies)
        )

    async def push_audio(self, pcm: bytes) -> None:
        await self._client.push_audio(pcm)

    async def add_context_item(self, text: str, *, role: str = "user") -> None:
        """Write one item into the history (the in-band half of the two-step).

        During a reply the server defers the ack (handlers/conversation.py:48-52)
        and flushes it later — so this neither waits for nor retries on a
        missing conversation.item.created. Retrying is how duplicates happen.
        """
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
        """The out-of-band half: ask for a reply without touching the history.

        input stays absent — chat.py:830-835 would replace the whole history
        with it. write_history is not the adapter's to honour beyond this:
        out-of-band replies never write back (base_openai_compatible_language_
        model.py:645), so L3 mirrors what was said into its own memory.
        """
        if spec.protected:
            await self._client.send_command(self._interrupt_patch(False))
        frame = self._codec.response_create(
            out_of_band=True,
            text_only=self._text_replies,
            instructions=spec.instructions,
            max_output_tokens=spec.max_tokens,
        )
        return await self._client.request_reply(frame)

    async def end_protection(self) -> None:
        """Re-arm barge-in after a protected reply. The scheduler calls this on
        ReplyDone, and a stage-2 hard cap makes sure it cannot be forgotten."""
        await self._client.send_command(self._interrupt_patch(True))

    async def cancel(self, handle: link.ReplyHandle) -> None:
        await self._client.cancel(handle)

    def events(self) -> AsyncIterator[link.LinkEvent]:
        return self._client.events()

    def _interrupt_patch(self, interruptible: bool) -> dict[str, Any]:
        # Runtime-tunable on this provider (runtime_config.py:58-76); only the
        # two turn_detection fields it actually reads travel with it.
        return {
            "type": dia.ClientEvent.SESSION_UPDATE.value,
            "session": {"turn_detection": {"interrupt_response": interruptible}},
        }
