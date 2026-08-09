"""配置的类型与默认值。

每个字段挂一组 UI 元数据，见 _ui.ui()。
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator

from bilisama.config._ui import Audience, Reload, ui
from bilisama.config.enums import Chattiness, ProviderName
from bilisama.config.validate import check


class TurnConfig(BaseModel):
    """判停三件套。**字段名跟上游 vad_arguments.py 逐字对齐**，渲染就是直接映射。

    全部是启动期参数，改了要重启 P3'。CI 有一条门禁拿这里的字段名跟上游对账。
    """

    model_config = {"extra": "forbid"}

    thresh: float = Field(
        0.6,
        ge=0.0,
        le=1.0,
        json_schema_extra=ui(
            label="VAD 灵敏度",
            help="越高越不容易把噪音当成说话",
            unit="",
            audience=Audience.OPERATOR,
            reload=Reload.LIVE,
            group="判停",
            order=1,
        ),
    )
    sample_rate: Literal[8000, 16000] = Field(
        16000, json_schema_extra=ui(label="采样率", reload=Reload.ENGINE, group="判停", order=2)
    )
    min_silence_ms: int = Field(
        64,
        ge=16,
        le=2000,
        json_schema_extra=ui(
            label="静音判定",
            help="激进值靠投机重开兜底，不建议动",
            unit="ms",
            reload=Reload.LIVE,
            group="判停",
            order=3,
        ),
    )
    min_speech_ms: int = Field(
        384,
        ge=100,
        le=2000,
        json_schema_extra=ui(
            label="最短有效说话",
            help="也是打断的门槛",
            unit="ms",
            reload=Reload.ENGINE,
            group="判停",
            order=4,
        ),
    )
    min_speech_continuation_ms: int = Field(
        192,
        ge=0,
        le=2000,
        json_schema_extra=ui(
            label="续说门槛", unit="ms", reload=Reload.ENGINE, group="判停", order=5
        ),
    )
    max_speech_ms: float = Field(
        float("inf"),
        gt=0,
        json_schema_extra=ui(
            label="单段最长", unit="ms", reload=Reload.ENGINE, group="判停", order=6
        ),
    )
    speech_pad_ms: int = Field(
        500,
        ge=0,
        le=2000,
        json_schema_extra=ui(
            label="前置缓冲",
            help="开口前留多少音频，防止第一个字被切掉",
            unit="ms",
            reload=Reload.ENGINE,
            group="判停",
            order=7,
        ),
    )
    audio_enhancement: bool = Field(
        False,
        json_schema_extra=ui(
            label="离线降噪",
            help="对已切好的段做降噪，不是 AEC",
            reload=Reload.ENGINE,
            group="判停",
            order=8,
        ),
    )
    speculative_reopen_ms: int = Field(
        800,
        ge=0,
        le=5000,
        json_schema_extra=ui(
            label="重开宽限（判定说完）",
            help="降低它能压 p50，代价是主播续说的窗口变窄",
            unit="ms",
            audience=Audience.OPERATOR,
            reload=Reload.ENGINE,
            group="判停",
            order=9,
        ),
    )
    unanswered_reopen_ms: int = Field(
        7000,
        ge=0,
        le=30000,
        json_schema_extra=ui(
            label="未答复重开上限", unit="ms", reload=Reload.ENGINE, group="判停", order=10
        ),
    )
    short_segment_merge_ms: int = Field(
        0,
        ge=0,
        le=2000,
        json_schema_extra=ui(
            label="碎片拼接窗口", unit="ms", reload=Reload.ENGINE, group="判停", order=11
        ),
    )
    smart_turn: bool = Field(
        True,
        json_schema_extra=ui(
            label="语义判停 SmartTurn",
            help="关掉会退回纯静音判停，延迟方差变小但误切变多",
            audience=Audience.OPERATOR,
            reload=Reload.ENGINE,
            group="判停",
            order=12,
        ),
    )
    smart_turn_model_path: str | None = Field(
        None,
        json_schema_extra=ui(
            label="SmartTurn 模型路径",
            help="留空则自动下载",
            widget="file",
            reload=Reload.ENGINE,
            group="判停",
            order=13,
        ),
    )
    smart_turn_threshold: float = Field(
        0.5,
        ge=0.0,
        le=1.0,
        json_schema_extra=ui(label="SmartTurn 阈值", reload=Reload.ENGINE, group="判停", order=14),
    )
    smart_turn_max_wait_ms: int = Field(
        1200,
        ge=0,
        le=5000,
        json_schema_extra=ui(
            label="重开宽限（判定没说完）",
            help="这是延迟方差的唯一来源。上游默认 2000，我们压到 1200 换更稳的节奏",
            unit="ms",
            audience=Audience.OPERATOR,
            reload=Reload.ENGINE,
            group="判停",
            order=15,
        ),
    )
    smart_turn_incomplete_delay_ms: int = Field(
        400,
        ge=0,
        le=3000,
        json_schema_extra=ui(
            label="没说完时的延后开工", unit="ms", reload=Reload.ENGINE, group="判停", order=16
        ),
    )
    smart_turn_cpu_count: int = Field(
        2,
        ge=1,
        le=8,
        json_schema_extra=ui(
            label="SmartTurn 线程数", reload=Reload.ENGINE, group="判停", order=17
        ),
    )


class S2SConfig(BaseModel):
    model_config = {"extra": "forbid"}

    endpoint: str = Field(
        "ws://127.0.0.1:8765/v1/realtime",
        json_schema_extra=ui(
            label="服务地址",
            audience=Audience.OPERATOR,
            reload=Reload.RECONNECT,
            group="自建语音服务",
            order=1,
        ),
    )
    managed: bool = Field(
        True,
        json_schema_extra=ui(
            label="由 BiliSama 拉起",
            help="关掉则你自己在别处跑，这里只填地址",
            audience=Audience.OPERATOR,
            reload=Reload.RESTART,
            group="自建语音服务",
            order=2,
        ),
    )
    llm_base_url: str = Field(
        "http://127.0.0.1:9010/v1",
        json_schema_extra=ui(
            label="对话模型地址",
            help="OpenAI 兼容的 chat-completions 端点",
            audience=Audience.OPERATOR,
            reload=Reload.ENGINE,
            group="自建语音服务",
            order=3,
            wizard_step=2,
        ),
    )
    llm_model: str = Field(
        "",
        json_schema_extra=ui(
            label="对话模型 id",
            audience=Audience.OPERATOR,
            reload=Reload.ENGINE,
            group="自建语音服务",
            order=4,
            wizard_step=2,
        ),
    )
    patches: tuple[Literal["text_modality", "raw_instructions"], ...] = Field(
        ("text_modality", "raw_instructions"),
        json_schema_extra=ui(
            label="运行时补丁",
            help="全部关掉 = 零补丁模式，用它自带的 TTS 和提示词尾巴",
            widget="checkboxes",
            reload=Reload.ENGINE,
            group="自建语音服务",
            order=5,
        ),
    )
    tts_placeholder: str = Field(
        "kokoro",
        json_schema_extra=ui(
            label="占位 TTS 引擎",
            help="纯文本模式下它不合成音频，但结构上必须在，挑个最轻的",
            reload=Reload.ENGINE,
            group="自建语音服务",
            order=6,
        ),
    )
    turn: TurnConfig = Field(
        default_factory=TurnConfig,
        json_schema_extra=ui(label="判停参数", provider_scoped="s2s", group="判停"),
    )


class HostedConfig(BaseModel):
    model_config = {"extra": "forbid"}

    endpoint: str = Field(
        "",
        json_schema_extra=ui(
            label="服务地址",
            audience=Audience.OPERATOR,
            reload=Reload.RECONNECT,
            group="托管语音服务",
            order=1,
            wizard_step=2,
        ),
    )
    model: str = Field(
        "",
        json_schema_extra=ui(
            label="模型 id",
            audience=Audience.OPERATOR,
            reload=Reload.RECONNECT,
            group="托管语音服务",
            order=2,
            wizard_step=2,
        ),
    )
    api_key_ref: str = Field(
        "",
        json_schema_extra=ui(
            label="API Key",
            help="存在系统钥匙串里，这里只留一个引用",
            secret=True,
            audience=Audience.STREAMER,
            reload=Reload.RECONNECT,
            group="托管语音服务",
            order=3,
            wizard_step=2,
        ),
    )


class SideModelConfig(BaseModel):
    """后台侧路模型：主动话题、记忆蒸馏、事实抽取都用它。通常挑更便宜的。"""

    model_config = {"extra": "forbid"}

    base_url: str = Field(
        "",
        json_schema_extra=ui(
            label="侧路模型地址",
            audience=Audience.OPERATOR,
            reload=Reload.LIVE,
            group="后台模型",
            order=1,
        ),
    )
    model: str = Field(
        "",
        json_schema_extra=ui(
            label="侧路模型 id",
            audience=Audience.OPERATOR,
            reload=Reload.LIVE,
            group="后台模型",
            order=2,
        ),
    )
    api_key_ref: str = Field(
        "",
        json_schema_extra=ui(
            label="侧路模型 Key", secret=True, reload=Reload.LIVE, group="后台模型", order=3
        ),
    )
    # 固定关，见 §4.7。放在 schema 里是为了让人看见这个决定，不是为了让人改。
    thinking: Literal["off"] = Field(
        "off",
        json_schema_extra=ui(
            label="思考模式",
            help="固定关。侧路调用不需要思考，且会拖慢后台任务",
            reload=Reload.LIVE,
            group="后台模型",
            order=4,
        ),
    )
    tool_choice: Literal["none"] = Field(
        "none",
        json_schema_extra=ui(
            label="工具调用",
            help="固定关。侧路调用不该有副作用",
            reload=Reload.LIVE,
            group="后台模型",
            order=5,
        ),
    )


class SpeechConfig(BaseModel):
    model_config = {"extra": "forbid"}

    provider: ProviderName = Field(
        ProviderName.S2S,
        json_schema_extra=ui(
            label="语音后端",
            help="换这个会重连语音链路",
            audience=Audience.STREAMER,
            reload=Reload.RECONNECT,
            group="语音",
            order=1,
            wizard_step=2,
            aliases=("provider", "后端", "模型"),
        ),
    )
    s2s: S2SConfig = Field(
        default_factory=S2SConfig,
        json_schema_extra=ui(label="自建服务", provider_scoped="s2s", group="语音"),
    )
    dashscope: HostedConfig = Field(
        default_factory=HostedConfig,
        json_schema_extra=ui(label="DashScope", provider_scoped="dashscope", group="语音"),
    )
    openai_ga: HostedConfig = Field(
        default_factory=HostedConfig,
        json_schema_extra=ui(label="OpenAI Realtime", provider_scoped="openai_ga", group="语音"),
    )
    side: SideModelConfig = Field(
        default_factory=SideModelConfig, json_schema_extra=ui(label="后台模型", group="后台模型")
    )


# ---------------------------------------------------------------- 其余各段


class TTSConfig(BaseModel):
    """只在 provider 不自带音频时生效。"""

    model_config = {"extra": "forbid"}

    engine: Literal["qwen3_cloud", "qwen3_local", "volcengine", "gpt_sovits"] = Field(
        "qwen3_cloud",
        json_schema_extra=ui(
            label="语音引擎",
            help="云端首包约 97ms，本地约 400ms",
            audience=Audience.STREAMER,
            reload=Reload.LIVE,
            group="声音",
            order=1,
            wizard_step=3,
        ),
    )
    voice: str = Field(
        "",
        json_schema_extra=ui(
            label="音色",
            audience=Audience.STREAMER,
            reload=Reload.LIVE,
            group="声音",
            order=2,
            wizard_step=3,
        ),
    )
    speed: float = Field(
        1.0,
        ge=0.5,
        le=2.0,
        json_schema_extra=ui(
            label="语速", audience=Audience.STREAMER, reload=Reload.LIVE, group="声音", order=3
        ),
    )
    api_key_ref: str = Field(
        "",
        json_schema_extra=ui(
            label="语音引擎 Key", secret=True, reload=Reload.LIVE, group="声音", order=4
        ),
    )


class AudioConfig(BaseModel):
    """头号风险的对策。回声进麦克风会让判停一直误触发。"""

    model_config = {"extra": "forbid"}

    input_device: str = Field(
        "auto",
        json_schema_extra=ui(
            label="麦克风",
            widget="device",
            audience=Audience.STREAMER,
            reload=Reload.RESTART,
            group="音频",
            order=1,
            wizard_step=4,
        ),
    )
    output_device: str = Field(
        "auto",
        json_schema_extra=ui(
            label="AI 声音输出到",
            widget="device",
            audience=Audience.STREAMER,
            reload=Reload.RESTART,
            group="音频",
            order=2,
            wizard_step=4,
        ),
    )
    output_route: Literal["virtual", "direct"] = Field(
        "virtual",
        json_schema_extra=ui(
            label="输出方式",
            help="virtual 走虚拟声卡给 OBS，AI 的声音不会进你的麦克风；direct 必须戴耳机",
            audience=Audience.STREAMER,
            reload=Reload.RESTART,
            group="音频",
            order=3,
            wizard_step=4,
        ),
    )
    echo_guard: Literal["duck", "off"] = Field(
        "duck",
        json_schema_extra=ui(
            label="抢跑静音",
            help="主播一开口先把 AI 音量压下去，体感打断从 520ms 降到 90ms",
            audience=Audience.OPERATOR,
            reload=Reload.LIVE,
            group="音频",
            order=4,
        ),
    )


class SafetyConfig(BaseModel):
    model_config = {"extra": "forbid"}

    wordlist_path: str = Field(
        "auto",
        json_schema_extra=ui(
            label="敏感词表",
            widget="file",
            audience=Audience.OPERATOR,
            reload=Reload.LIVE,
            group="安全",
            order=1,
        ),
    )
    allowlist_path: str = Field(
        "auto",
        json_schema_extra=ui(
            label="白名单",
            help="防止误伤，比如角色名撞了敏感词",
            widget="file",
            audience=Audience.OPERATOR,
            reload=Reload.LIVE,
            group="安全",
            order=2,
        ),
    )
    on_hit: Literal["drop_sentence", "mute_all"] = Field(
        "drop_sentence",
        json_schema_extra=ui(
            label="命中后怎么办",
            audience=Audience.OPERATOR,
            reload=Reload.LIVE,
            group="安全",
            order=3,
        ),
    )


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

    chattiness: Chattiness = Field(
        Chattiness.MEDIUM,
        json_schema_extra=ui(
            label="话痨程度",
            help="它是冷场阈值、弹幕窗口、打分门槛、冷却、回复长度这五个数的唯一写者",
            widget="segmented",
            audience=Audience.STREAMER,
            reload=Reload.LIVE,
            group="互动",
            order=1,
            wizard_step=5,
            aliases=("话多", "频率"),
        ),
    )
    speak: SpeakSwitches = Field(
        default_factory=SpeakSwitches,
        json_schema_extra=ui(
            label="回应哪些",
            widget="switch_matrix",
            audience=Audience.STREAMER,
            reload=Reload.LIVE,
            group="互动",
            order=2,
        ),
    )
    sc_protect_ms: int = Field(
        4000,
        ge=0,
        le=15000,
        json_schema_extra=ui(
            label="付费消息保护时长",
            help="这段时间内主播说话不打断 SC 答谢",
            unit="ms",
            audience=Audience.OPERATOR,
            reload=Reload.LIVE,
            group="互动",
            order=3,
        ),
    )
    gift_gold_high: int = Field(
        10000,
        ge=0,
        json_schema_extra=ui(
            label="大额礼物门槛",
            unit="金瓜子",
            audience=Audience.OPERATOR,
            reload=Reload.LIVE,
            group="互动",
            order=4,
        ),
    )
    gift_gold_medium: int = Field(
        1000,
        ge=0,
        json_schema_extra=ui(
            label="中额礼物门槛",
            unit="金瓜子",
            audience=Audience.OPERATOR,
            reload=Reload.LIVE,
            group="互动",
            order=5,
        ),
    )
    burst_uniques: int = Field(
        5,
        ge=1,
        json_schema_extra=ui(
            label="批量欢迎人数",
            audience=Audience.OPERATOR,
            reload=Reload.LIVE,
            group="互动",
            order=6,
        ),
    )
    burst_window_s: int = Field(
        45,
        ge=5,
        json_schema_extra=ui(
            label="批量欢迎窗口",
            unit="s",
            audience=Audience.OPERATOR,
            reload=Reload.LIVE,
            group="互动",
            order=7,
        ),
    )


class MemoryConfig(BaseModel):
    model_config = {"extra": "forbid"}

    db_path: str = Field(
        "auto",
        json_schema_extra=ui(
            label="记忆库位置",
            help="auto = 用户数据目录",
            widget="file",
            reload=Reload.RESTART,
            group="记忆",
            order=1,
        ),
    )
    distill_every_n_events: int = Field(
        40,
        ge=5,
        json_schema_extra=ui(
            label="蒸馏触发间隔",
            help="按事件计数，不按时间。冷场自动省钱",
            unit="条",
            audience=Audience.OPERATOR,
            reload=Reload.LIVE,
            group="记忆",
            order=2,
        ),
    )
    retain_event_days: int = Field(
        7,
        ge=1,
        json_schema_extra=ui(
            label="原始事件保留",
            unit="天",
            audience=Audience.OPERATOR,
            reload=Reload.LIVE,
            group="记忆",
            order=3,
        ),
    )


class RoomConfig(BaseModel):
    model_config = {"extra": "forbid"}

    room_id: int = Field(
        0,
        ge=0,
        json_schema_extra=ui(
            label="直播间号",
            audience=Audience.STREAMER,
            reload=Reload.RESTART,
            group="直播间",
            order=1,
            wizard_step=1,
            aliases=("房间", "roomid"),
        ),
    )
    platform: Literal["bilibili"] = Field(
        "bilibili",
        json_schema_extra=ui(label="平台", reload=Reload.RESTART, group="直播间", order=2),
    )
    credential_ref: str = Field(
        "",
        json_schema_extra=ui(
            label="登录凭据",
            help="匿名也能连，但拿不到观众身份，per-viewer 记忆会全废",
            secret=True,
            audience=Audience.STREAMER,
            reload=Reload.RESTART,
            group="直播间",
            order=3,
            wizard_step=1,
        ),
    )


class PersonaConfig(BaseModel):
    model_config = {"extra": "forbid"}

    id: str = Field(
        "mia",
        json_schema_extra=ui(
            label="人设",
            help="对应 config/personas/<id>/",
            audience=Audience.STREAMER,
            reload=Reload.LIVE,
            group="人设",
            order=1,
            wizard_step=5,
        ),
    )


class AvatarConfig(BaseModel):
    model_config = {"extra": "forbid"}

    renderer: Literal["live2d", "pngtuber"] = Field(
        "live2d",
        json_schema_extra=ui(
            label="形象类型",
            audience=Audience.STREAMER,
            reload=Reload.RESTART,
            group="形象",
            order=1,
            wizard_step=3,
        ),
    )
    model_id: str = Field(
        "",
        json_schema_extra=ui(
            label="形象模型",
            audience=Audience.STREAMER,
            reload=Reload.RESTART,
            group="形象",
            order=2,
            wizard_step=3,
        ),
    )
    expression_source: Literal["tag", "lexicon", "tool_call"] = Field(
        "tag",
        json_schema_extra=ui(
            label="表情驱动方式",
            help="tag 只在我们自己做 TTS 时安全，否则标签会被念出来",
            audience=Audience.DEVELOPER,
            reload=Reload.RESTART,
            group="形象",
            order=3,
        ),
    )


class RuntimeConfig(BaseModel):
    model_config = {"extra": "forbid"}

    ui_port: int = Field(
        0,
        ge=0,
        le=65535,
        json_schema_extra=ui(
            label="界面端口", help="0 = 让系统分配", reload=Reload.RESTART, group="运行", order=1
        ),
    )
    log_level: Literal["debug", "info", "warning", "error"] = Field(
        "info",
        json_schema_extra=ui(
            label="日志级别", audience=Audience.DEVELOPER, reload=Reload.LIVE, group="运行", order=2
        ),
    )
    log_viewer_content: bool = Field(
        False,
        json_schema_extra=ui(
            label="日志记录弹幕正文",
            help="默认关。那是观众的话，排查问题时再开",
            audience=Audience.DEVELOPER,
            reload=Reload.LIVE,
            group="运行",
            order=3,
        ),
    )


class Settings(BaseModel):
    """根配置。程序里所有模块只从这个对象读。"""

    model_config = {"extra": "forbid"}

    config_version: int = 1
    active_profile: str = Field(
        "normal",
        json_schema_extra=ui(
            label="场景预设",
            widget="select",
            audience=Audience.STREAMER,
            reload=Reload.LIVE,
            group="总览",
            order=1,
        ),
    )

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
