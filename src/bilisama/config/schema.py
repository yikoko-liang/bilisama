"""配置的类型与默认值。

UI 元数据不在这里,在 ui_meta.UI_META，按字段路径索引。
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator

from bilisama.config.enums import Chattiness, ProviderName
from bilisama.config.validate import check


class TurnConfig(BaseModel):
    """判停三件套。**字段名跟上游 vad_arguments.py 逐字对齐**，渲染就是直接映射。

    全部是启动期参数，改了要重启 P3'。CI 有一条门禁拿这里的字段名跟上游对账。
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
    tts_placeholder: str = Field("kokoro")
    turn: TurnConfig = Field(default_factory=TurnConfig)


class HostedConfig(BaseModel):
    model_config = {"extra": "forbid"}

    endpoint: str = Field("")
    model: str = Field("")
    api_key_ref: str = Field("")


class SideModelConfig(BaseModel):
    """后台侧路模型：主动话题、记忆蒸馏、事实抽取都用它。通常挑更便宜的。"""

    model_config = {"extra": "forbid"}

    base_url: str = Field("")
    model: str = Field("")
    api_key_ref: str = Field("")
    # 固定关，见 §4.7。放在 schema 里是为了让人看见这个决定，不是为了让人改。
    thinking: Literal["off"] = Field("off")
    tool_choice: Literal["none"] = Field("none")


class SpeechConfig(BaseModel):
    model_config = {"extra": "forbid"}

    provider: ProviderName = Field(ProviderName.S2S)
    s2s: S2SConfig = Field(default_factory=S2SConfig)
    dashscope: HostedConfig = Field(default_factory=HostedConfig)
    openai_ga: HostedConfig = Field(default_factory=HostedConfig)
    side: SideModelConfig = Field(default_factory=SideModelConfig)


# ---------------------------------------------------------------- 其余各段


class TTSConfig(BaseModel):
    """只在 provider 不自带音频时生效。"""

    model_config = {"extra": "forbid"}

    engine: Literal["qwen3_cloud", "qwen3_local", "volcengine", "gpt_sovits"] = Field("qwen3_cloud")
    voice: str = Field("")
    speed: float = Field(1.0, ge=0.5, le=2.0)
    api_key_ref: str = Field("")


class AudioConfig(BaseModel):
    """头号风险的对策。回声进麦克风会让判停一直误触发。"""

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
    """每个来源一个开关。事件一律入库，这里只管说不说。"""

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


class InteractionConfig(BaseModel):
    model_config = {"extra": "forbid"}

    chattiness: Chattiness = Field(Chattiness.MEDIUM)
    speak: SpeakSwitches = Field(default_factory=SpeakSwitches)
    sc_protect_ms: int = Field(4000, ge=0, le=15000)
    gift_gold_high: int = Field(10000, ge=0)
    gift_gold_medium: int = Field(1000, ge=0)
    burst_uniques: int = Field(5, ge=1)
    burst_window_s: int = Field(45, ge=5)


class MemoryConfig(BaseModel):
    model_config = {"extra": "forbid"}

    db_path: str = Field("auto")
    distill_every_n_events: int = Field(40, ge=5)
    retain_event_days: int = Field(7, ge=1)


class RoomConfig(BaseModel):
    model_config = {"extra": "forbid"}

    room_id: int = Field(0, ge=0)
    platform: Literal["bilibili"] = Field("bilibili")
    credential_ref: str = Field("")


class PersonaConfig(BaseModel):
    model_config = {"extra": "forbid"}

    id: str = Field("mia")


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
    """根配置。程序里所有模块只从这个对象读。"""

    model_config = {"extra": "forbid"}

    config_version: int = 1
    active_profile: str = Field("normal")

    room: RoomConfig = Field(default_factory=RoomConfig)
    speech: SpeechConfig = Field(default_factory=SpeechConfig)
    tts: TTSConfig = Field(default_factory=TTSConfig)
    audio: AudioConfig = Field(default_factory=AudioConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    interaction: InteractionConfig = Field(default_factory=InteractionConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    persona: PersonaConfig = Field(default_factory=PersonaConfig)
    avatar: AvatarConfig = Field(default_factory=AvatarConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)

    @model_validator(mode="after")
    def _cross_field_checks(self) -> Self:
        problems = [p for p in check(self) if p.fatal]
        if problems:
            raise ValueError("\n".join(p.message for p in problems))
        return self
