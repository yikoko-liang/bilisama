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
from bilisama.obs.logging import get_logger
from bilisama.realtime import dialect as dia
from bilisama.realtime import link
from bilisama.realtime.client import RealtimeClient
from bilisama.realtime.providers import codec_for, compose_instructions, profile_for

__all__ = ["S2SLink"]

log = get_logger(__name__)


class S2SLink:
    """SpeechLink over a speech-to-speech server."""

    def __init__(
        self,
        url: str,
        *,
        clock: Clock | None = None,
        watchdog_s: float = 25.0,
        text_replies: bool = True,
        auto_reconnect: bool = True,
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
        codec = codec_for(ProviderName.S2S)
        self._client = RealtimeClient(
            url,
            caps=profile.caps,
            codec=codec,
            clock=clock,
            watchdog_s=watchdog_s,
            auto_reconnect=auto_reconnect,
        )
        self._codec = codec
        self._text_replies = text_replies
        self._context = ""
        # Barge-in is a session-level flag we turn OFF for protected replies.
        # Remembering that we did is the only way to put it back when the send
        # that should have done so never made it — see request_reply.
        self._barge_in_disarmed = False

    async def connect(self) -> None:
        """Open the socket and restore whatever this link already knew.

        Reconnecting is not a fresh start: the session we come back to has no
        instructions at all, and nothing upstream will notice. Assembly only
        pushes when the assembled text CHANGED (app.py refresh_context), so a
        reconnect between two identical pushes leaves the assistant running
        with no persona until the clock line ticks — up to a full granularity
        window. Replaying here keeps that invisible to L3.
        """
        self._client.on_resume = self._resume_session
        await self._client.connect()
        await self._resume_session()

    async def _resume_session(self) -> None:
        """Replay what a fresh socket does not know. Idempotent.

        Barge-in rides along with the instructions. If a protected reply
        disarmed it and the re-arm never reached the wire, this is the first
        moment it can: whether the server carries the old session's flag
        across a reconnect is unverified (our own mock does), so the new
        socket is told rather than trusted.
        """
        # Read before end_protection() clears it, so the line reports what this
        # replay actually had to pay back.
        owed_rearm = self._barge_in_disarmed
        if self._context:
            await self.set_context(self._context)
        if owed_rearm:
            await self.end_protection()
        # Logged after the sends, not before: a reconnect that dies mid-replay
        # must not leave a line claiming the persona is back. The failure has
        # its own line in the client (link.reconnect_failed).
        log.info(
            "s2s.session_replayed",
            context_len=len(self._context),
            rearmed_barge_in=owed_rearm,
            # NOT a `text_replies=` flag: the scrubber folds any field whose
            # name contains the word `text` down to `<bool>`, on the assumption
            # that it holds danmaku (obs/logging.py:51). The answer rides in the
            # VALUE, which is judged by nobody.
            modality="text" if self._text_replies else "server_default",
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def set_context(self, instructions: str) -> None:
        # text_only pins the SESSION, which is what the implicit VAD turn obeys.
        # Kept locally too: per-response instructions REPLACE the session's on
        # the wire, so request_reply must recompose (see compose_instructions).
        self._context = instructions
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

        Protection is disarmed and re-armed here rather than left to the
        caller, because a failure between the two sends is unrecoverable from
        outside: the scheduler only re-arms from ReplyDone or the protection
        cap, and both hang off an active reply that a failed send never
        created. The session would sit at interrupt_response=false for the
        rest of the stream — the streamer opens their mouth and nothing
        happens, for hours, over one lost frame (backlog item 46).
        """
        if spec.protected:
            await self._client.send_command(self._interrupt_patch(False))
            self._barge_in_disarmed = True
            log.info("s2s.protection_armed", protect_ms=spec.protect_ms)
        frame = self._codec.response_create(
            out_of_band=True,
            text_only=self._text_replies,
            instructions=compose_instructions(self._context, spec.instructions),
            max_output_tokens=spec.max_tokens,
        )
        try:
            return await self._client.request_reply(frame)
        except Exception:
            # Exception, not BaseException, and deliberately: CancelledError
            # sits outside it, and the only thing that cancels a dispatch is
            # shutdown cancelling the scheduler loop it runs inside
            # (scheduler.py:301). The socket closes on the way out, so a
            # session left disarmed there does not outlive anything. Re-arming
            # from a cancelled context would mean awaiting a send that is
            # already being cancelled.
            if spec.protected:
                # On the spot when the socket allows it. It usually will not:
                # request_reply raises from _send_raw, and this reaches the
                # wire through that same _send_raw, so the condition that
                # brings us here is mostly the condition that stops us
                # sending. The flag set above is what actually covers that
                # case — _resume_session re-arms on the socket that replaces
                # this one.
                try:
                    await self.end_protection()
                except Exception as rearm:
                    log.warning("s2s.rearm_deferred", error_text=str(rearm)[:200])
            raise

    async def end_protection(self) -> None:
        """Re-arm barge-in after a protected reply. The scheduler calls this on
        ReplyDone, and a stage-2 hard cap makes sure it cannot be forgotten."""
        was_armed = self._barge_in_disarmed
        await self._client.send_command(self._interrupt_patch(True))
        # Only after the frame is away: a raise here must leave the debt
        # standing so the next socket pays it.
        self._barge_in_disarmed = False
        # was_armed separates "the paid reply is over" from the scheduler's
        # belt-and-braces second call, which re-arms what is already armed.
        log.info("s2s.protection_ended", was_armed=was_armed)

    async def cancel(self, handle: link.ReplyHandle) -> None:
        await self._client.cancel(handle)

    def events(self) -> AsyncIterator[link.LinkEvent]:
        return self._client.events()

    def _interrupt_patch(self, interruptible: bool) -> dict[str, Any]:
        # Runtime-tunable on this provider (runtime_config.py:58-76); only the
        # two turn_detection fields it actually reads travel with it.
        session: dict[str, Any] = {"turn_detection": {"interrupt_response": interruptible}}
        if self._codec.needs_session_type:
            # The server rejects any session.update without this as
            # "Unknown or invalid event" (probed live, v0.2.12-40) — the same
            # quirk Codec.session_patch handles for set_context. Forgetting it
            # here made every paid reply's protection silently fail (found by
            # a live /gift test, 2026-08-11).
            session["type"] = "realtime"
        return {"type": dia.ClientEvent.SESSION_UPDATE.value, "session": session}
