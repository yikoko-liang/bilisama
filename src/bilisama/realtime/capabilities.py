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

from dataclasses import dataclass, field, replace


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
    # Per-MODEL, not per-provider, it turns out (probed live 2026-08-10):
    # qwen3.5-omni-flash-realtime accepts and echoes all three;
    # qwen-audio-3.0-realtime-flash/-plus refuse semantic_vad outright
    # ("Supported values: server_vad, smart_turn. Use turn_detection: null for
    # push-to-talk mode."). This constant keeps the omni set and `for_model`
    # below narrows the ones we measured — a provider-level intersection would
    # wrongly refuse semantic_vad on omni.
    turn_detection_types=frozenset({"smart_turn", "server_vad", "semantic_vad"}),
)

OPENAI_GA = Capabilities(
    owns_tts=True,
    single_response_slot=True,  # only one response may write the default conversation
    out_of_band_exempt_from_slot=True,  # out-of-band responses do run in parallel
    # Required over WebSocket, and supported here. Nothing in src/ reads this
    # flag yet, and the honest reason is not laziness: truncating the model's
    # own history takes the played_ms the browser reports on playback.cancelled
    # (ui/events.py:71) carried from L4 through L3 down to a client method that
    # does not exist — and the only provider declaring True is the one dev-talk
    # refuses to dial (dev_talk.py:1066). Until both land, a barge-in trims our
    # memory and leaves the model still holding what the audience never heard.
    item_truncate=True,
    acknowledges_session_update=True,
    turn_detection_types=frozenset({"server_vad", "semantic_vad"}),
)


# Turn detection is the one capability that splits BELOW the provider: the same
# DashScope account serves models with different answers (see DASHSCOPE above).
# Only models we actually probed are listed — an unlisted one inherits its
# provider, because blocking a working config on our own ignorance is worse
# than the endpoint refusal this check exists to pre-empt.
_MODEL_TURN_TYPES: dict[str, frozenset[str]] = {
    "qwen-audio-3.0-realtime-flash": frozenset({"server_vad", "smart_turn"}),
    "qwen-audio-3.0-realtime-plus": frozenset({"server_vad", "smart_turn"}),
}


def for_model(base: Capabilities, model: str) -> Capabilities:
    """Narrow a provider's declaration down to one model's measured reality.

    Args:
        base: The provider-level capabilities.
        model: The model that will actually be dialed. Empty or unprobed
            names leave `base` untouched.

    Returns:
        `base` itself when nothing is known about the model, so identity
        comparisons in the registry keep working.
    """
    narrowed = _MODEL_TURN_TYPES.get(model.strip())
    if narrowed is None or narrowed == base.turn_detection_types:
        return base
    return replace(base, turn_detection_types=narrowed)
