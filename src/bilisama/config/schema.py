"""Configuration types and defaults.

Types and defaults only. Cross-field rules are `validate.check`, and refusing to
start on a fatal one is `loader.load` — a model that cannot construct its own
defaults is unusable for fixtures, schema export and the settings UI.

UI metadata is not here either — see `ui_meta.UI_META`, keyed by field path.
"""

from __future__ import annotations

from typing import Final, Literal

from pydantic import BaseModel, Field

from bilisama.config.enums import Chattiness, GrowthMode, ProviderName, VoiceReplyMode

# The config shape this build reads. It lives here rather than in `migrate`
# because both of that module's neighbours need it and neither may import it:
# `validate` refuses a file from the future, `migrate` walks an old one forward.
CURRENT_VERSION: Final[int] = 4


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
    # "We start the server ourselves." Nothing reads it: today the server is
    # started by hand (scripts/smoke_provider_b.sh serve), and supervising it
    # from inside the app is a stage-7 job. Left in the schema so the intent is
    # visible, with this note so nobody assumes flipping it does anything.
    managed: bool = Field(True)
    llm_base_url: str = Field("http://127.0.0.1:9010/v1")
    llm_model: str = Field("")
    # Which shim patches the server starts with. This is the source, but not the
    # channel: the shim reads BILISAMA_S2S_PATCHES
    # (tools/s2s_shim/bilisama_s2s_shim/patches.py:275) and the launch JSON
    # cannot carry the list, so `bilisama config render-s2s` prints the export
    # line to go with the file (bootstrap/s2s_launch.py patch_env). Inside this
    # process the value also decides `owns_tts` in config/validate.py.
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
    # Which voice the provider speaks in. Empty means "whatever the server
    # picks", and that default is not a neutral choice: on DashScope it is
    # longanqian, measured at 343 Hz against the 180-260 Hz of an ordinary
    # adult female voice. It reads as shrill, and until this field existed
    # there was no way to say otherwise. Names are the provider's own; ask for
    # a wrong one and the server answers with the list it accepts.
    voice: str = Field("")
    # Rotate the session this many minutes in, or 0 to leave it alone. None —
    # the default — follows the provider's own published ceiling (DashScope 120
    # minutes, OpenAI 60), which the adapter already knows; this field only
    # exists for someone who wants a different number. Not `int = 0`: 0 already
    # means "never rotate", so defaulting to it would close the switch the
    # adapter just opened.
    session_cap_min: int | None = Field(None, ge=0)
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


class VolcanoConfig(BaseModel):
    """Volcengine's end-to-end dialogue model (Doubao S2S).

    Its own section rather than another HostedConfig: this endpoint takes two
    credentials instead of one, picks its persona key by model generation, and
    has no per-turn model query — so four of HostedConfig's fields would be
    dead here and three of these have nowhere to live there.
    """

    model_config = {"extra": "forbid"}

    endpoint: str = Field("")
    # The console's API Key, which authenticates on its own — the vendor's own
    # words are 「在任意接口中，填入 header 即可，不用填写 appid」. Probed live
    # 2026-08-27: it works on both the legacy and the duplex endpoints.
    api_key_ref: str = Field("")
    # The older pair, kept for an account that only has one. Alternatives to
    # api_key_ref, not complements: an API Key in the access-key position
    # draws a 401 whose text is about neither.
    app_id_ref: str = Field("")
    access_key_ref: str = Field("")
    # Which generation, and therefore which persona key: O2.0 takes a plain
    # instruction string under dialog.system_role, SC2.0 takes a character
    # manifest. Sending one under the other's key is accepted and ignored,
    # which is why this is a choice rather than something we sniff.
    model: Literal["1.2.1.1", "2.2.0.0"] = Field("1.2.1.1")
    # Voice. O2.0 takes the vendor's catalogue voices; SC2.0 takes cloned ones.
    # Empty leaves it to the server.
    speaker: str = Field("")
    # How long a pause counts as "done talking". The vendor's default; the
    # only turn-detection knob this endpoint exposes.
    end_smooth_window_ms: int = Field(1500, ge=0, le=10000)


class VolcanoDuplexConfig(BaseModel):
    """Doubao Seeduplex 3.0 — the full-duplex protocol, plain JSON frames.

    A separate section rather than a flag on VolcanoConfig: the two protocols
    share an account and nothing else. Voices come from a different catalogue
    (`uranus`, not `jupiter` or `saturn`), the model string is fixed, and the
    endpoint is a different path. Probed 2026-09-16 — see docs/runbook.md.
    """

    model_config = {"extra": "forbid"}

    endpoint: str = Field("")
    # The same console API Key as the legacy protocol; one header, no app id.
    api_key_ref: str = Field("")
    # The vendor publishes exactly one value for the duplex protocol. Kept as
    # a field so a bump is a config edit, and Literal so a typo is a startup
    # error rather than a session that opens and never answers.
    model: Literal["1.2.6.1"] = Field("1.2.6.1")
    # Voice. Named `voice` rather than the legacy protocol's `speaker`
    # because that is what this one calls it on the wire, and because
    # validate.py builds its 「改哪个字段」 advice from the name. Empty leaves
    # it to the server, which picks its own character — the mistake
    # VolcanoConfig.speaker documents for the legacy protocol.
    voice: str = Field("zh_female_vv_uranus_bigtts")
    speed: int = Field(0, ge=-50, le=100)
    loudness: int = Field(0, ge=-50, le=100)
    # Audio starts this local deadline; late transcription cannot extend it.
    # The provider still owns final transcription. Releasing the local speech
    # state does not commit its input or claim that its item has completed.
    speech_edge_s: float = Field(1.0, ge=0.3, le=10.0)
    # End remote input after a longer silence; zero disables the fallback.
    # A pause longer than this may be split into separate input turns.
    speech_commit_s: float = Field(3.0, ge=0.0, le=30.0)
    # How long a reply's frames wait in the adapter before they may play. On
    # this protocol 「主播开口了」 arrives ~850 ms after the streamer starts
    # (the transcriber has to recognise a word; DashScope's VAD is ~330 ms), and
    # she keeps playing over him for all of it. A hold makes the first N ms of
    # a reply unplayable, so a barge-in inside that window throws away sound
    # nobody heard yet — the 「她刚开口就被打断」 case. It does nothing for a
    # reply already audibly playing: what left the speaker is gone. Every reply
    # starts N ms later. 0 is today's behaviour. Live: the next turn uses it.
    reply_hold_ms: int = Field(0, ge=0, le=2000)
    # When the voice gate stops one of her own turns that nobody heard (it
    # held every frame), let the endpoint finish it instead of cancelling it:
    # the audio is dropped all the same. A cancel on this endpoint scrambles
    # the streamer's next transcription — items that never get a word, his
    # line cut after 0.2-0.3 s with words lost — and letting her short [SKIP]
    # line finish kept every line whole (real endpoint, 2026-10-05). The cost
    # is the slot staying hers until the endpoint's done, measured 0.25-2.85 s.
    # False is the behaviour before 2026-10-07: the rollback. Live: the next
    # kill uses it.
    skip_without_cancel: bool = True
    # Who answers a danmaku on this protocol, and it is a real fork rather
    # than a tuning knob, because this endpoint will not compose a reply on
    # demand (probed 2026-09-16; corrected 2026-09-21 — it WILL start a turn
    # on the greeting event, it just reads words we supply. See
    # providers/duplex.py's fact 5).
    #
    # "tool": the danmaku is filed, and she pulls it herself through a
    #   function call the next time the streamer's voice starts a turn. Her
    #   own words, no side model — but the wait is however long until the
    #   streamer next speaks, and a danmaku arriving during dead air keeps
    #   waiting. The only route that works here today.
    # "ghostwriter": the side model writes the line and the endpoint speaks it
    #   through speech_text_buffer.replacement. REFUSED AT STARTUP on this
    #   provider, because with a microphone running that frame is never spoken
    #   AND it kills the session: the transcriber stops closing turns and
    #   every later reply is swallowed, socket healthy, nothing said anywhere.
    #   Isolated 2026-09-17 against a control differing only in whether the
    #   frame was sent — control three clean turns, treatment dead after one
    #   (tests/integration/test_duplex_contract.py pins it). Kept as a value
    #   rather than deleted so the day the vendor fixes it there is something
    #   to turn back on.
    event_replies: Literal["ghostwriter", "tool"] = Field("ghostwriter")


class SpeechConfig(BaseModel):
    model_config = {"extra": "forbid"}

    provider: ProviderName = Field(ProviderName.S2S)
    s2s: S2SConfig = Field(default_factory=S2SConfig)
    dashscope: HostedConfig = Field(default_factory=HostedConfig)
    openai_ga: HostedConfig = Field(default_factory=HostedConfig)
    volcano: VolcanoConfig = Field(default_factory=VolcanoConfig)
    volcano_duplex: VolcanoDuplexConfig = Field(default_factory=VolcanoDuplexConfig)
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

    The pipeline DOES cancel acoustic echo since 2026-08-24 — the shell is
    Chromium and audio runs inside it, so getUserMedia's canceller has both the
    microphone and the reference signal. This docstring said the opposite until
    that landed.

    It only covers what the shell itself plays, though. OBS monitoring,
    background music and game audio reach the microphone untouched, and the
    symptom is turn detection false-triggering — which reads as a broken VAD
    (plan section 11) rather than as a routing problem.

    None of the four fields below has a runtime reader: the panel's device
    dropdowns are what actually decides where sound goes, and output_route is
    a plan for a virtual cable that nothing implements yet (backlog #35's
    family). config/validate.py is the only consumer, and only for advice.
    """

    model_config = {"extra": "forbid"}

    input_device: str = Field("auto")
    output_device: str = Field("auto")
    # "direct" since 2026-08-24. Routing her voice through a virtual cable
    # was right when nothing cancelled echo — it kept her out of the air
    # entirely. Now it is the one path that DEFEATS cancellation: whatever
    # plays the cable back out is another process, and the canceller only
    # subtracts what the shell itself played.
    output_route: Literal["virtual", "direct"] = Field("direct")
    echo_guard: Literal["duck", "off"] = Field("duck")
    # The pause-adjacent session switches: both reset to True on every
    # director start (session state, not a durable preference).
    input_enabled: bool = Field(True)
    output_enabled: bool = Field(True)
    # 0 favours noise rejection; 100 admits quieter speech. A local PCM gate
    # on the uplink, because smart_turn takes no server-VAD threshold and
    # the hosted endpoints' own knobs cannot see the streamer's room.
    noise_sensitivity: int = Field(50, ge=0, le=100)
    # Her voice's own volume, in percent of what the backend sends: 100 is
    # untouched, 0 silent, 200 twice the amplitude (+6 dB). Applied where she
    # is played (the page's GainNode, the local speaker's callback), so it
    # never reaches the wire or costs a reconnect, and the machine's volume
    # stays the streamer's. Above 100 a peak limiter sits behind it: nine real
    # duplex recordings at loudness 0 peaked at -9.9 to -3.5 dBFS, so 200
    # would push the hottest of them 2.5 dB past full scale without one.
    voice_volume: int = Field(100, ge=0, le=200)


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
    # Governs the entry lane's ONE voice, the batched welcome — individual
    # arrivals never speak at any setting. Off in the chat profile is what
    # makes observe mode genuinely silent.
    entry: bool = True
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


class EntryWelcomeConfig(BaseModel):
    """Which entry groups may receive a welcome.

    ordinary is the coalesced hello for plain arrivals; naval covers current
    fleet members (guard level off the wire); ranking covers the level-5+
    current-room medal wearers. All three sit under speak.entry / speak.
    vip_enter — those switches silence the lane, these choose within it.
    """

    model_config = {"extra": "forbid"}

    ordinary: bool = True
    naval: bool = True
    ranking: bool = True


class InteractionConfig(BaseModel):
    model_config = {"extra": "forbid"}

    chattiness: Chattiness = Field(Chattiness.MEDIUM)
    # Reply length rides its own slider instead of shadowing chattiness:
    # how OFTEN she speaks and how LONG she speaks are different worries.
    # Reuses the three-level enum; the token caps live in derive.py.
    reply_length: Chattiness = Field(Chattiness.LOW)
    # Whether her own microphone turn always plays (every VAD turn, as
    # before) or only when the streamer was talking to her, as she reports
    # at the head of the turn and the voice gate (director/voice_turn.py)
    # enforces. The prompt that teaches the markers rides this same switch
    # (persona/loader.py live_voice_rules), so the two halves cannot split:
    # taught without the gate she would read「SKIP」to the audience.
    voice_reply: VoiceReplyMode = Field(VoiceReplyMode.WHEN_ADDRESSED)
    speak: SpeakSwitches = Field(default_factory=SpeakSwitches)
    # Ledger #91. Off: the streamer's next word always lands, and a paid
    # thank-you that gets cut off is requeued. On: SC and high-tier gift
    # replies hold the floor for sc_protect_ms — fully on s2s, which disarms
    # the backend's own barge-in for that long; on hosted backends only the
    # scheduler half applies, and validate.py says so.
    protect_paid_replies: bool = Field(False)
    # The protection window's length. Honoured only while protect_paid_replies
    # is on; forwarded unconditionally so flipping the switch needs no rebuild.
    sc_protect_ms: int = Field(4000, ge=0, le=15000)
    # Tiers in the frontend battery unit — the number a viewer actually sees
    # on the gift panel (wire price 100 == 1 battery == CNY 0.1). validate.py
    # holds medium <= high.
    gift_battery_high: int = Field(1000, ge=1)
    gift_battery_medium: int = Field(100, ge=1)
    # Legacy PresenceWelcomer knobs. The dynamic entry coalescing (event
    # pacer) has taken over; these stay so older profiles keep loading.
    burst_uniques: int = Field(5, ge=1)
    burst_window_s: int = Field(45, ge=5)
    burst_cooldown_s: int = Field(90, ge=0)
    entry_welcome: EntryWelcomeConfig = Field(default_factory=EntryWelcomeConfig)
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
    # A short, streamer-authored description of the current show. It joins
    # the pushed context (its own 直播简介 section) and feeds the danmaku
    # relevance check, so welcomes and replies can reference what is
    # happening without guessing from a room title or screen content.
    stream_intro: str = Field("", max_length=500)


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

    id: str = Field("tofu")
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
    # Also travels as volcano's dialog.bot_name, which the vendor caps at 20
    # characters — a longer one is refused at StartSession, so the ceiling
    # belongs here rather than in one adapter.
    display_name: str = Field("", max_length=20)
    growth: GrowthSwitches = Field(default_factory=GrowthSwitches)


class AvatarConfig(BaseModel):
    model_config = {"extra": "forbid"}

    # Two axes, one job each (ledger #40, split in v4). `renderer` is the
    # MECHANISM: sprite = pet.json spritesheet pipeline (the shipping one),
    # live2d = stage 5, configured-but-unimplemented. `model_id` is the
    # ASSET the mechanism loads: empty means the built-in tofu robot (named
    # for the missing-glyph box — the tofu Noto set out to eliminate, here
    # alive with a face); a name means a skin-pack directory, user packs
    # under ~/.local/share/bilisama/skins shadowing packaged ones; the
    # explicit name "tofu" pins the PACKAGED copy so no user pack can wear
    # her face (renderer.js packagedOnly). For live2d it will name a model
    # directory. The old renderer value "tofu" mixed both axes and migrates
    # to sprite + empty model_id.
    renderer: Literal["sprite", "live2d"] = Field("sprite")
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

    config_version: int = CURRENT_VERSION
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
