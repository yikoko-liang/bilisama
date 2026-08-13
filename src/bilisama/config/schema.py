"""Configuration types and defaults.

Types and defaults only. Cross-field rules are `validate.check`, and refusing to
start on a fatal one is `loader.load` — a model that cannot construct its own
defaults is unusable for fixtures, schema export and the settings UI.

UI metadata is not here either — see `ui_meta.UI_META`, keyed by field path.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from bilisama.config.enums import Chattiness, GrowthMode, ProviderName


class TurnConfig(BaseModel):
    """Turn-detection knobs, passed through to speech-to-speech verbatim.

    Field names match upstream's `vad_arguments.py` exactly, so rendering is a
    direct mapping. All of them are startup-time: changing one means restarting the
    engine. A CI check reconciles these names against upstream in both directions.
    """

    model_config = {"extra": "forbid"}

    thresh: float = Field(0.6, ge=0.0, le=1.0)
    sample_rate: Literal[8000, 16000] = Field(16000)
    min_silence_ms: int = Field(64, ge=16, le=2000)
    min_speech_ms: int = Field(384, ge=100, le=2000)
    min_speech_continuation_ms: int = Field(192, ge=0, le=2000)
    max_speech_ms: float = Field(float("inf"), gt=0)
    speech_pad_ms: int = Field(500, ge=0, le=2000)
    audio_enhancement: bool = Field(False)
    speculative_reopen_ms: int = Field(800, ge=0, le=5000)
    unanswered_reopen_ms: int = Field(7000, ge=0, le=30000)
    short_segment_merge_ms: int = Field(0, ge=0, le=2000)
    smart_turn: bool = Field(True)
    smart_turn_model_path: str | None = Field(None)
    smart_turn_threshold: float = Field(0.5, ge=0.0, le=1.0)
    smart_turn_max_wait_ms: int = Field(1200, ge=0, le=5000)
    smart_turn_incomplete_delay_ms: int = Field(400, ge=0, le=3000)
    smart_turn_cpu_count: int = Field(2, ge=1, le=8)


class S2SConfig(BaseModel):
    model_config = {"extra": "forbid"}

    endpoint: str = Field("ws://127.0.0.1:8765/v1/realtime")
    managed: bool = Field(True)
    llm_base_url: str = Field("http://127.0.0.1:9010/v1")
    llm_model: str = Field("")
    patches: tuple[Literal["text_modality", "raw_instructions"], ...] = Field(
        ("text_modality", "raw_instructions")
    )
    # The TTS engine the s2s SERVER loads — distinct from [tts], which is our
    # own stage-4 chain. On the patched product path it never synthesises (a
    # structural placeholder, pick the lightest); in zero-patch mode it is the
    # voice the audience hears.
    server_tts: str = Field("kokoro")
    # Only audible when the server's own TTS speaks (zero-patch mode). The
    # CustomVoice model generates a random voice per reply when unset upstream
    # (measured live 2026-08-11), so the render always pins one.
    server_tts_speaker: str = Field("vivian")
    turn: TurnConfig = Field(default_factory=TurnConfig)


class HostedTurnConfig(BaseModel):
    """Turn detection for a hosted provider, pushed as session.update fields.

    These are the runtime-tunable OpenAI names, nothing like the s2s launch
    knobs in TurnConfig. silence_duration_ms defaults to 300 rather than the
    upstream 500 — the last line of the section 2.8 tuning list that had no
    field to land in until now.
    """

    model_config = {"extra": "forbid"}

    type: str = Field("server_vad")
    threshold: float = Field(0.5, ge=0.0, le=1.0)
    silence_duration_ms: int = Field(300, ge=100, le=2000)


class HostedConfig(BaseModel):
    model_config = {"extra": "forbid"}

    endpoint: str = Field("")
    model: str = Field("")
    api_key_ref: str = Field("")
    turn: HostedTurnConfig = Field(default_factory=HostedTurnConfig)


class SideModelConfig(BaseModel):
    """Runs the background jobs: proactive topics, memory distillation, fact extraction.

    Separate from the conversation model so it can be a cheaper one (plan §7.2).
    """

    model_config = {"extra": "forbid"}

    base_url: str = Field("")
    model: str = Field("")
    api_key_ref: str = Field("")
    # Pinned off. Present in the schema so the decision is visible, not adjustable.
    thinking: Literal["off"] = Field("off")
    tool_choice: Literal["none"] = Field("none")


class SpeechConfig(BaseModel):
    model_config = {"extra": "forbid"}

    provider: ProviderName = Field(ProviderName.S2S)
    s2s: S2SConfig = Field(default_factory=S2SConfig)
    dashscope: HostedConfig = Field(default_factory=HostedConfig)
    openai_ga: HostedConfig = Field(default_factory=HostedConfig)
    side: SideModelConfig = Field(default_factory=SideModelConfig)


# ---------------------------------------------------------------- everything else


class CustomTTSConfig(BaseModel):
    """Our own pluggable TTS chain (stage 4), engine-agnostic by design.

    qwen3_cloud is only today's runnable default; the strategic main engine is
    IndexTTS once its licence and a CUDA box land (plan §4.8), with volcengine
    and gpt_sovits registered behind the same ABC. Used only when the speech
    provider does not produce audio itself — when the provider speaks for us,
    nothing here is read. Plan §7.6 wants that combination reported rather
    than left looking configured; `validate.check` does not catch it yet.
    """

    model_config = {"extra": "forbid"}

    engine: Literal["qwen3_cloud", "qwen3_local", "volcengine", "gpt_sovits"] = Field("qwen3_cloud")
    voice: str = Field("")
    speed: float = Field(1.0, ge=0.5, le=2.0)
    api_key_ref: str = Field("")


class AudioConfig(BaseModel):
    """Mitigations for the biggest operational risk.

    Nothing in the pipeline cancels acoustic echo. If the assistant's own voice
    reaches the mic, turn detection false-triggers continuously.
    """

    model_config = {"extra": "forbid"}

    input_device: str = Field("auto")
    output_device: str = Field("auto")
    output_route: Literal["virtual", "direct"] = Field("virtual")
    echo_guard: Literal["duck", "off"] = Field("duck")


class SafetyConfig(BaseModel):
    model_config = {"extra": "forbid"}

    wordlist_path: str = Field("auto")
    allowlist_path: str = Field("auto")
    on_hit: Literal["drop_sentence", "mute_all"] = Field("drop_sentence")


class SpeakSwitches(BaseModel):
    """One switch per source. Events are always recorded; this only gates speech."""

    model_config = {"extra": "forbid"}

    danmaku: bool = True
    gift: bool = True
    super_chat: bool = True
    guard_buy: bool = True
    vip_enter: bool = True
    entry: bool = False
    follow: bool = False
    like: bool = False
    share: bool = False
    proactive: bool = True
    background_result: bool = False


class ProactiveConfig(BaseModel):
    """The proactive topic loop's own knobs.

    The idle threshold that actually triggers a topic is derived from
    chattiness (derive.py), not set here — single-writer rule.
    """

    model_config = {"extra": "forbid"}

    max_per_hour: int = Field(12, ge=0, le=60)
    wake_interval_s: int = Field(30, ge=5, le=300)


class DanmakuConfig(BaseModel):
    """Danmaku-lane knobs. Window length and score threshold are derived from
    chattiness (derive.py) — single-writer rule — so only the per-viewer
    cooldown lives here."""

    model_config = {"extra": "forbid"}

    # Seconds before the same viewer can win the danmaku window again. Armed
    # by the reply, not the attempt (safety.PerUidCooldown).
    per_uid_cooldown_s: int = Field(60, ge=0, le=600)


class InteractionConfig(BaseModel):
    model_config = {"extra": "forbid"}

    chattiness: Chattiness = Field(Chattiness.MEDIUM)
    speak: SpeakSwitches = Field(default_factory=SpeakSwitches)
    sc_protect_ms: int = Field(4000, ge=0, le=15000)
    gift_gold_high: int = Field(10000, ge=0)
    gift_gold_medium: int = Field(1000, ge=0)
    burst_uniques: int = Field(5, ge=1)
    burst_window_s: int = Field(45, ge=5)
    danmaku: DanmakuConfig = Field(default_factory=DanmakuConfig)
    proactive: ProactiveConfig = Field(default_factory=ProactiveConfig)


class MemoryConfig(BaseModel):
    model_config = {"extra": "forbid"}

    db_path: str = Field("auto")
    distill_every_n_events: int = Field(40, ge=5)
    retain_event_days: int = Field(7, ge=1)
    # 0 = write-through (each event lands in its own transaction, the target-
    # scale default). >0 = write-behind: events buffer in memory and land as
    # one transaction when the window ages out or 200 rows accumulate; every
    # read flushes first, so read-after-write semantics are unchanged. For
    # flood-rate rooms (plan section 16.8 item 26).
    write_batch_ms: int = Field(0, ge=0, le=5000)
    # The clock segment of the pushed context floors its numbers to this many
    # minutes. Every text change triggers a session.update, so this IS the
    # idle-time push cadence: 1 = one push per minute, the default 5 = one
    # per five.
    clock_granularity_min: int = Field(5, ge=1, le=30)


class RoomConfig(BaseModel):
    model_config = {"extra": "forbid"}

    room_id: int = Field(0, ge=0)
    platform: Literal["bilibili"] = Field("bilibili")
    credential_ref: str = Field("")


class GrowthSwitches(BaseModel):
    """Per-layer switches for the machine-grown persona files (plan section 4.6).

    Default off. The anchors cannot drift — the machine has no write path to
    them — but voice.md is few-shot with real style influence, so trust gets
    built by reading collect-mode output for a few streams before switching on.
    """

    model_config = {"extra": "forbid"}

    relationship: GrowthMode = Field(GrowthMode.OFF)
    voice: GrowthMode = Field(GrowthMode.OFF)


class PersonaConfig(BaseModel):
    model_config = {"extra": "forbid"}

    id: str = Field("mia")
    # auto = <data home>/personas/<id>. Live copies of all four persona files;
    # the shipped templates under config/personas/ carry only the two anchors.
    data_dir: str = Field("auto")
    # What the persona calls the streamer — every template's {{userName}}.
    # "主播" is the neutral default; a real name is what makes it sound like
    # someone sitting next to you rather than a service announcement.
    streamer_name: str = Field("主播")
    # What the persona calls itself in its templates ({{agentName}}). Empty
    # falls back to `id`, the filesystem-safe folder name. Set this only when
    # the spoken name should differ from the folder — a nickname, different
    # capitalisation, whatever the streamer wants. A ported persona keeping its
    # own name is the normal case, not something to translate away.
    display_name: str = Field("")
    growth: GrowthSwitches = Field(default_factory=GrowthSwitches)


class AvatarConfig(BaseModel):
    model_config = {"extra": "forbid"}

    renderer: Literal["live2d", "pngtuber"] = Field("live2d")
    model_id: str = Field("")
    expression_source: Literal["tag", "lexicon", "tool_call"] = Field("tag")


class RuntimeConfig(BaseModel):
    model_config = {"extra": "forbid"}

    ui_port: int = Field(0, ge=0, le=65535)
    log_level: Literal["debug", "info", "warning", "error"] = Field("info")
    log_viewer_content: bool = Field(False)


class Settings(BaseModel):
    """Root config. Every module reads from this object and nowhere else."""

    model_config = {"extra": "forbid"}

    config_version: int = 1
    active_profile: str = Field("normal")

    room: RoomConfig = Field(default_factory=RoomConfig)
    speech: SpeechConfig = Field(default_factory=SpeechConfig)
    custom_tts: CustomTTSConfig = Field(default_factory=CustomTTSConfig)
    audio: AudioConfig = Field(default_factory=AudioConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    interaction: InteractionConfig = Field(default_factory=InteractionConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    persona: PersonaConfig = Field(default_factory=PersonaConfig)
    avatar: AvatarConfig = Field(default_factory=AvatarConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
