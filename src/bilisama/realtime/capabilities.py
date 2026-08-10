"""Per-provider capability flags.

The test for belonging here is strict: **a field only the adapter reads is not a
capability, it is an adapter constant.** Sample rates, timeouts, session limits
and error patterns all failed that test and live in the adapters.

qwen-audio-agent's DEFAULT_CAPABILITIES is three booleans; dialect lives in a
separate codec object and connection details are plain functions on the provider.
Folding all three back into one dataclass would be inventing complexity rather
than inheriting their experience, so we keep the same split.

Every field below makes either the client or the scheduler grow a branch. That is
what earns it a place here.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Capabilities:
    # Does it produce audio, or hand us text to synthesize ourselves?
    owns_tts: bool = True
    # Only one response may be generating at a time.
    single_response_slot: bool = False
    # Whether out-of-band responses (conversation="none") consume that one slot.
    out_of_band_exempt_from_slot: bool = False
    # conversation.item.truncate support. Without it, truncation after a barge-in
    # can only happen in our own memory, never in the model's history.
    item_truncate: bool = False
    # Whether session.update is acknowledged. If not, the client must not wait.
    acknowledges_session_update: bool = True
    # Declared turn-detection types. Configuring an undeclared one is an error,
    # never a silent downgrade.
    turn_detection_types: frozenset[str] = field(default_factory=lambda: frozenset({"server_vad"}))

    @property
    def expr_tags_safe(self) -> bool:
        """Whether inline <expr/> tags survive the trip to the speaker.

        Derived rather than stored: it is exactly "we own the TTS", and storing a
        value you can compute gives you two places to keep in sync.
        """
        return not self.owns_tts


S2S = Capabilities(
    owns_tts=False,  # we ask for text and synthesize it ourselves
    single_response_slot=True,
    # Verified: handlers/response.py:202-206 checks in_response before
    # is_out_of_band on line 208, so out-of-band still consumes the slot.
    out_of_band_exempt_from_slot=False,
    item_truncate=False,  # not implemented upstream
    acknowledges_session_update=True,
    # Claiming semantic_vad would be lying: vad_handler.py accepts the field and
    # then ignores it, reading only threshold and silence_duration_ms.
    turn_detection_types=frozenset({"server_vad"}),
)

DASHSCOPE = Capabilities(
    owns_tts=True,
    # Verified on the real endpoint 2026-08-10 (qwen3.5-omni-flash-realtime on a
    # dedicated MaaS instance), closing plan section 13 item 5. The old guess of
    # False was wrong: a second in-band create is refused with "Conversation
    # already has an active response".
    single_response_slot=True,
    # conversation="none" during an active reply draws the same refusal — no
    # exemption, same as s2s. Only OpenAI GA runs out-of-band in parallel.
    out_of_band_exempt_from_slot=False,
    # conversation.item.truncate is swallowed in silence: no truncated ack, no
    # error, nothing. Worse than unsupported — undetectable at runtime.
    item_truncate=False,
    acknowledges_session_update=True,
    # All three accepted AND echoed back in session.updated, smart_turn included
    # — the value that looked like an s2s feature name leaking is real here.
    # Accepted-and-echoed is not proof the behaviour differs; that needs audio.
    turn_detection_types=frozenset({"smart_turn", "server_vad", "semantic_vad"}),
)

OPENAI_GA = Capabilities(
    owns_tts=True,
    single_response_slot=True,  # only one response may write the default conversation
    out_of_band_exempt_from_slot=True,  # out-of-band responses do run in parallel
    item_truncate=True,  # required over WebSocket, and supported here
    acknowledges_session_update=True,
    turn_detection_types=frozenset({"server_vad", "semantic_vad"}),
)
