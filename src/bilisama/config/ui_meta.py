"""UI metadata, keyed by field path.

This used to hang off every field as `json_schema_extra`, which accounted for most
of the schema's bulk while having no consumer at all — the settings page is not
built yet. As plain data the schema is back to types and defaults, and the Electron
side can read this directly instead of digging it out of a JSON Schema.

The cost is a second place to keep in sync, so `tests/unit/test_ui_meta.py`
reconciles the two: field paths must match exactly, numeric fields must be bounded.
Labels and hints stay in Chinese — they are shown to the streamer.
"""

from __future__ import annotations

from dataclasses import dataclass

from bilisama.config._ui import Audience, Reload


@dataclass(frozen=True, slots=True)
class FieldMeta:
    """How one config field appears in the settings UI.

    Widget type is inferred from the schema — bool to toggle, bounded number to
    slider, enum to select — so `widget` is only set when inference cannot do it.
    """

    label: str
    hint: str = ""
    audience: Audience = Audience.DEVELOPER
    reload: Reload = Reload.RESTART
    group: str = ""
    order: int = 0
    unit: str = ""
    widget: str = ""
    provider_scoped: str = ""
    derived_from: str = ""
    secret: bool = False
    wizard_step: int = 0
    aliases: tuple[str, ...] = ()


UI_META: dict[str, FieldMeta] = {
    # Which events to react to. Rendered as one switch matrix, so each entry
    # only needs a label.
    "interaction.speak.danmaku": FieldMeta(
        label="普通弹幕",
        audience=Audience.STREAMER,
        reload=Reload.LIVE,
        group="互动",
        order=1,
        widget="toggle",
    ),
    "interaction.speak.gift": FieldMeta(
        label="礼物",
        audience=Audience.STREAMER,
        reload=Reload.LIVE,
        group="互动",
        order=2,
        widget="toggle",
    ),
    "interaction.speak.super_chat": FieldMeta(
        label="Super Chat",
        audience=Audience.STREAMER,
        reload=Reload.LIVE,
        group="互动",
        order=3,
        widget="toggle",
    ),
    "interaction.speak.guard_buy": FieldMeta(
        label="上舰",
        audience=Audience.STREAMER,
        reload=Reload.LIVE,
        group="互动",
        order=4,
        widget="toggle",
    ),
    "interaction.speak.vip_enter": FieldMeta(
        label="VIP 进房",
        audience=Audience.STREAMER,
        reload=Reload.LIVE,
        group="互动",
        order=5,
        widget="toggle",
    ),
    "interaction.speak.entry": FieldMeta(
        label="普通观众进房",
        audience=Audience.STREAMER,
        reload=Reload.LIVE,
        group="互动",
        order=6,
        widget="toggle",
    ),
    "interaction.speak.follow": FieldMeta(
        label="关注",
        audience=Audience.STREAMER,
        reload=Reload.LIVE,
        group="互动",
        order=7,
        widget="toggle",
    ),
    "interaction.speak.like": FieldMeta(
        label="点赞",
        audience=Audience.STREAMER,
        reload=Reload.LIVE,
        group="互动",
        order=8,
        widget="toggle",
    ),
    "interaction.speak.share": FieldMeta(
        label="分享",
        audience=Audience.STREAMER,
        reload=Reload.LIVE,
        group="互动",
        order=9,
        widget="toggle",
    ),
    "interaction.speak.proactive": FieldMeta(
        label="主动起话题",
        audience=Audience.STREAMER,
        reload=Reload.LIVE,
        group="互动",
        order=10,
        widget="toggle",
    ),
    "interaction.speak.background_result": FieldMeta(
        label="后台任务结果",
        audience=Audience.STREAMER,
        reload=Reload.LIVE,
        group="互动",
        order=11,
        widget="toggle",
    ),
    "active_profile": FieldMeta(
        label="场景预设",
        widget="select",
        audience=Audience.STREAMER,
        reload=Reload.LIVE,
        group="总览",
        order=1,
    ),
    "audio.echo_guard": FieldMeta(
        label="抢跑静音",
        hint="主播一开口先把 AI 音量压下去，体感打断从 520ms 降到 90ms",
        audience=Audience.OPERATOR,
        reload=Reload.LIVE,
        group="音频",
        order=4,
    ),
    "audio.input_device": FieldMeta(
        label="麦克风",
        widget="device",
        audience=Audience.STREAMER,
        reload=Reload.RESTART,
        group="音频",
        order=1,
        wizard_step=4,
    ),
    "audio.output_device": FieldMeta(
        label="AI 声音输出到",
        widget="device",
        audience=Audience.STREAMER,
        reload=Reload.RESTART,
        group="音频",
        order=2,
        wizard_step=4,
    ),
    "audio.output_route": FieldMeta(
        label="输出方式",
        hint="virtual 走虚拟声卡给 OBS，AI 的声音不会进你的麦克风；direct 必须戴耳机",
        audience=Audience.STREAMER,
        reload=Reload.RESTART,
        group="音频",
        order=3,
        wizard_step=4,
    ),
    "avatar.expression_source": FieldMeta(
        label="表情驱动方式",
        hint="tag 只在我们自己做 TTS 时安全，否则标签会被念出来",
        audience=Audience.DEVELOPER,
        reload=Reload.RESTART,
        group="形象",
        order=3,
    ),
    "avatar.model_id": FieldMeta(
        label="形象模型",
        audience=Audience.STREAMER,
        reload=Reload.RESTART,
        group="形象",
        order=2,
        wizard_step=3,
    ),
    "avatar.renderer": FieldMeta(
        label="形象类型",
        audience=Audience.STREAMER,
        reload=Reload.RESTART,
        group="形象",
        order=1,
        wizard_step=3,
    ),
    "interaction.burst_uniques": FieldMeta(
        label="批量欢迎人数",
        audience=Audience.OPERATOR,
        reload=Reload.LIVE,
        group="互动",
        order=6,
    ),
    "interaction.burst_window_s": FieldMeta(
        label="批量欢迎窗口",
        unit="s",
        audience=Audience.OPERATOR,
        reload=Reload.LIVE,
        group="互动",
        order=7,
    ),
    "interaction.chattiness": FieldMeta(
        label="话痨程度",
        hint="它是冷场阈值、弹幕窗口、打分门槛、冷却、回复长度这五个数的唯一写者",
        widget="segmented",
        audience=Audience.STREAMER,
        reload=Reload.LIVE,
        group="互动",
        order=1,
        wizard_step=5,
        aliases=("话多", "频率"),
    ),
    "interaction.gift_gold_high": FieldMeta(
        label="大额礼物门槛",
        unit="金瓜子",
        audience=Audience.OPERATOR,
        reload=Reload.LIVE,
        group="互动",
        order=4,
    ),
    "interaction.gift_gold_medium": FieldMeta(
        label="中额礼物门槛",
        unit="金瓜子",
        audience=Audience.OPERATOR,
        reload=Reload.LIVE,
        group="互动",
        order=5,
    ),
    "interaction.sc_protect_ms": FieldMeta(
        label="付费消息保护时长",
        hint="这段时间内主播说话不打断 SC 答谢",
        unit="ms",
        audience=Audience.OPERATOR,
        reload=Reload.LIVE,
        group="互动",
        order=3,
    ),
    "interaction.speak": FieldMeta(
        label="回应哪些",
        widget="switch_matrix",
        audience=Audience.STREAMER,
        reload=Reload.LIVE,
        group="互动",
        order=2,
    ),
    "interaction.proactive": FieldMeta(label="主动话题", group="互动", order=12),
    "interaction.proactive.max_per_hour": FieldMeta(
        label="主动话题每小时上限",
        unit="次",
        audience=Audience.OPERATOR,
        reload=Reload.LIVE,
        group="互动",
        order=13,
    ),
    "interaction.proactive.wake_interval_s": FieldMeta(
        label="后台思考间隔",
        hint="话题候选多久刷新一次；开口时机由冷场阈值（话痨度派生）决定",
        unit="s",
        reload=Reload.LIVE,
        group="互动",
        order=14,
    ),
    "memory.db_path": FieldMeta(
        label="记忆库位置",
        hint="auto = 用户数据目录",
        widget="file",
        reload=Reload.RESTART,
        group="记忆",
        order=1,
    ),
    "memory.distill_every_n_events": FieldMeta(
        label="蒸馏触发间隔",
        hint="按事件计数，不按时间。冷场自动省钱",
        unit="条",
        audience=Audience.OPERATOR,
        reload=Reload.LIVE,
        group="记忆",
        order=2,
    ),
    "memory.retain_event_days": FieldMeta(
        label="原始事件保留",
        unit="天",
        audience=Audience.OPERATOR,
        reload=Reload.LIVE,
        group="记忆",
        order=3,
    ),
    "memory.clock_granularity_min": FieldMeta(
        label="时钟粒度",
        hint="推给模型的时间段按几分钟取整。取整越粗，安静时段的上下文推送越少；" "设 1 回到分钟级",
        unit="分钟",
        reload=Reload.RESTART,
        group="记忆",
        order=5,
    ),
    "memory.write_batch_ms": FieldMeta(
        label="写库攒批窗口",
        hint="0 = 来一条落一条。事件洪峰的大房间再开：事件先攒在内存，"
        "窗口到期或攒满 200 条打包落库；任何读取前先落盘，读写语义不变",
        unit="毫秒",
        group="记忆",
        order=4,
    ),
    "persona.id": FieldMeta(
        label="人设",
        hint="对应 config/personas/<id>/",
        audience=Audience.STREAMER,
        reload=Reload.LIVE,
        group="人设",
        order=1,
        wizard_step=5,
    ),
    "persona.data_dir": FieldMeta(
        label="人设数据目录",
        hint="auto = 用户数据目录。四个人设文件的活副本在这，随时能打开手改",
        widget="file",
        reload=Reload.RESTART,
        group="人设",
        order=2,
    ),
    "persona.growth": FieldMeta(label="生长层", group="人设", order=3),
    "persona.growth.relationship": FieldMeta(
        label="共同经历",
        hint="off 不长；collect 只攒进文件不进提示词；on 攒并注入",
        widget="segmented",
        audience=Audience.OPERATOR,
        reload=Reload.LIVE,
        group="人设",
        order=4,
    ),
    "persona.growth.voice": FieldMeta(
        label="口癖",
        hint="唯一影响说话风格的层。建议先 collect 观察几场，翻过文件放心了再开",
        widget="segmented",
        audience=Audience.OPERATOR,
        reload=Reload.LIVE,
        group="人设",
        order=5,
    ),
    "room.credential_ref": FieldMeta(
        label="登录凭据",
        hint="匿名也能连，但拿不到观众身份，per-viewer 记忆会全废",
        secret=True,
        audience=Audience.STREAMER,
        reload=Reload.RESTART,
        group="直播间",
        order=3,
        wizard_step=1,
    ),
    "room.platform": FieldMeta(label="平台", reload=Reload.RESTART, group="直播间", order=2),
    "room.room_id": FieldMeta(
        label="直播间号",
        audience=Audience.STREAMER,
        reload=Reload.RESTART,
        group="直播间",
        order=1,
        wizard_step=1,
        aliases=("房间", "roomid"),
    ),
    "runtime.log_level": FieldMeta(
        label="日志级别", audience=Audience.DEVELOPER, reload=Reload.LIVE, group="运行", order=2
    ),
    "runtime.log_viewer_content": FieldMeta(
        label="日志记录弹幕正文",
        hint="默认关。那是观众的话，排查问题时再开",
        audience=Audience.DEVELOPER,
        reload=Reload.LIVE,
        group="运行",
        order=3,
    ),
    "runtime.ui_port": FieldMeta(
        label="界面端口", hint="0 = 让系统分配", reload=Reload.RESTART, group="运行", order=1
    ),
    "safety.allowlist_path": FieldMeta(
        label="白名单",
        hint="防止误伤，比如角色名撞了敏感词",
        widget="file",
        audience=Audience.OPERATOR,
        reload=Reload.LIVE,
        group="安全",
        order=2,
    ),
    "safety.on_hit": FieldMeta(
        label="命中后怎么办",
        audience=Audience.OPERATOR,
        reload=Reload.LIVE,
        group="安全",
        order=3,
    ),
    "safety.wordlist_path": FieldMeta(
        label="敏感词表",
        widget="file",
        audience=Audience.OPERATOR,
        reload=Reload.LIVE,
        group="安全",
        order=1,
    ),
    "speech.dashscope": FieldMeta(label="DashScope", provider_scoped="dashscope", group="语音"),
    "speech.dashscope.api_key_ref": FieldMeta(
        label="API Key",
        hint="存在系统钥匙串里，这里只留一个引用",
        audience=Audience.STREAMER,
        reload=Reload.RECONNECT,
        group="托管语音服务",
        order=3,
        wizard_step=2,
        secret=True,
    ),
    "speech.dashscope.endpoint": FieldMeta(
        label="服务地址",
        audience=Audience.OPERATOR,
        reload=Reload.RECONNECT,
        group="托管语音服务",
        order=1,
        wizard_step=2,
    ),
    "speech.dashscope.model": FieldMeta(
        label="模型 id",
        audience=Audience.OPERATOR,
        reload=Reload.RECONNECT,
        group="托管语音服务",
        order=2,
        wizard_step=2,
    ),
    "speech.dashscope.turn": FieldMeta(
        label="DashScope 判停", provider_scoped="dashscope", group="判停"
    ),
    "speech.dashscope.turn.type": FieldMeta(
        label="判停方式",
        hint="server_vad 按静音时长判停，semantic_vad 按语义判停",
        provider_scoped="dashscope",
        audience=Audience.OPERATOR,
        reload=Reload.RECONNECT,
        group="判停",
        order=1,
    ),
    "speech.dashscope.turn.threshold": FieldMeta(
        label="判停灵敏度",
        hint="越高越不容易把噪音当成说话",
        provider_scoped="dashscope",
        audience=Audience.STREAMER,
        reload=Reload.LIVE,
        widget="slider",
        group="判停",
        order=2,
    ),
    "speech.dashscope.turn.silence_duration_ms": FieldMeta(
        label="静音多久算说完",
        hint="默认 300ms，上游默认 500——压低是 §2.8 的调优",
        unit="ms",
        provider_scoped="dashscope",
        audience=Audience.OPERATOR,
        reload=Reload.LIVE,
        group="判停",
        order=3,
    ),
    "speech.openai_ga": FieldMeta(
        label="OpenAI Realtime", provider_scoped="openai_ga", group="语音"
    ),
    "speech.openai_ga.api_key_ref": FieldMeta(
        label="API Key",
        hint="存在系统钥匙串里，这里只留一个引用",
        audience=Audience.STREAMER,
        reload=Reload.RECONNECT,
        group="托管语音服务",
        order=3,
        wizard_step=2,
        secret=True,
    ),
    "speech.openai_ga.endpoint": FieldMeta(
        label="服务地址",
        audience=Audience.OPERATOR,
        reload=Reload.RECONNECT,
        group="托管语音服务",
        order=1,
        wizard_step=2,
    ),
    "speech.openai_ga.model": FieldMeta(
        label="模型 id",
        audience=Audience.OPERATOR,
        reload=Reload.RECONNECT,
        group="托管语音服务",
        order=2,
        wizard_step=2,
    ),
    "speech.provider": FieldMeta(
        label="语音后端",
        hint="换这个会重连语音链路",
        audience=Audience.STREAMER,
        reload=Reload.RECONNECT,
        group="语音",
        order=1,
        wizard_step=2,
        aliases=("provider", "后端", "模型"),
    ),
    "speech.openai_ga.turn": FieldMeta(
        label="OpenAI 判停", provider_scoped="openai_ga", group="判停"
    ),
    "speech.openai_ga.turn.type": FieldMeta(
        label="判停方式",
        hint="server_vad 按静音时长判停，semantic_vad 按语义判停",
        provider_scoped="openai_ga",
        audience=Audience.OPERATOR,
        reload=Reload.RECONNECT,
        group="判停",
        order=1,
    ),
    "speech.openai_ga.turn.threshold": FieldMeta(
        label="判停灵敏度",
        hint="越高越不容易把噪音当成说话",
        provider_scoped="openai_ga",
        audience=Audience.STREAMER,
        reload=Reload.LIVE,
        widget="slider",
        group="判停",
        order=2,
    ),
    "speech.openai_ga.turn.silence_duration_ms": FieldMeta(
        label="静音多久算说完",
        hint="默认 300ms，上游默认 500——压低是 §2.8 的调优",
        unit="ms",
        provider_scoped="openai_ga",
        audience=Audience.OPERATOR,
        reload=Reload.LIVE,
        group="判停",
        order=3,
    ),
    "speech.s2s": FieldMeta(label="自建服务", provider_scoped="s2s", group="语音"),
    "speech.s2s.endpoint": FieldMeta(
        label="服务地址",
        audience=Audience.OPERATOR,
        reload=Reload.RECONNECT,
        group="自建语音服务",
        order=1,
    ),
    "speech.s2s.llm_base_url": FieldMeta(
        label="对话模型地址",
        hint="OpenAI 兼容的 chat-completions 端点",
        audience=Audience.OPERATOR,
        reload=Reload.ENGINE,
        group="自建语音服务",
        order=3,
        wizard_step=2,
    ),
    "speech.s2s.llm_model": FieldMeta(
        label="对话模型 id",
        audience=Audience.OPERATOR,
        reload=Reload.ENGINE,
        group="自建语音服务",
        order=4,
        wizard_step=2,
    ),
    "speech.s2s.managed": FieldMeta(
        label="由 BiliSama 拉起",
        hint="关掉则你自己在别处跑，这里只填地址",
        audience=Audience.OPERATOR,
        reload=Reload.RESTART,
        group="自建语音服务",
        order=2,
    ),
    "speech.s2s.patches": FieldMeta(
        label="运行时补丁",
        hint="全部关掉 = 零补丁模式，用它自带的 TTS 和提示词尾巴",
        widget="checkboxes",
        reload=Reload.ENGINE,
        group="自建语音服务",
        order=5,
    ),
    "speech.s2s.server_tts": FieldMeta(
        label="服务端 TTS 引擎",
        hint="s2s 服务器加载的引擎，跟我们自己的 [tts] 是两回事。产品路径纯文本时只是占位",
        reload=Reload.ENGINE,
        group="自建语音服务",
        order=6,
    ),
    "speech.s2s.server_tts_speaker": FieldMeta(
        label="服务端 TTS 音色",
        hint="只在零补丁模式（服务器自己出声）有效。不钉音色会每句换嗓子",
        audience=Audience.OPERATOR,
        reload=Reload.ENGINE,
        group="自建语音服务",
        order=7,
    ),
    "speech.s2s.turn": FieldMeta(label="判停参数", provider_scoped="s2s", group="判停"),
    "speech.s2s.turn.audio_enhancement": FieldMeta(
        label="离线降噪",
        hint="对已切好的段做降噪，不是 AEC",
        reload=Reload.ENGINE,
        group="判停",
        order=8,
    ),
    "speech.s2s.turn.max_speech_ms": FieldMeta(
        label="单段最长", unit="ms", reload=Reload.ENGINE, group="判停", order=6
    ),
    "speech.s2s.turn.min_silence_ms": FieldMeta(
        label="静音判定",
        hint="激进值靠投机重开兜底，不建议动",
        unit="ms",
        reload=Reload.LIVE,
        group="判停",
        order=3,
    ),
    "speech.s2s.turn.min_speech_continuation_ms": FieldMeta(
        label="续说门槛", unit="ms", reload=Reload.ENGINE, group="判停", order=5
    ),
    "speech.s2s.turn.min_speech_ms": FieldMeta(
        label="最短有效说话",
        hint="也是打断的门槛",
        unit="ms",
        reload=Reload.ENGINE,
        group="判停",
        order=4,
    ),
    "speech.s2s.turn.sample_rate": FieldMeta(
        label="采样率", reload=Reload.ENGINE, group="判停", order=2
    ),
    "speech.s2s.turn.short_segment_merge_ms": FieldMeta(
        label="碎片拼接窗口", unit="ms", reload=Reload.ENGINE, group="判停", order=11
    ),
    "speech.s2s.turn.smart_turn": FieldMeta(
        label="语义判停 SmartTurn",
        hint="关掉会退回纯静音判停，延迟方差变小但误切变多",
        audience=Audience.OPERATOR,
        reload=Reload.ENGINE,
        group="判停",
        order=12,
    ),
    "speech.s2s.turn.smart_turn_cpu_count": FieldMeta(
        label="SmartTurn 线程数", reload=Reload.ENGINE, group="判停", order=17
    ),
    "speech.s2s.turn.smart_turn_incomplete_delay_ms": FieldMeta(
        label="没说完时的延后开工", unit="ms", reload=Reload.ENGINE, group="判停", order=16
    ),
    "speech.s2s.turn.smart_turn_max_wait_ms": FieldMeta(
        label="重开宽限（判定没说完）",
        hint="这是延迟方差的唯一来源。上游默认 2000，我们压到 1200 换更稳的节奏",
        unit="ms",
        audience=Audience.OPERATOR,
        reload=Reload.ENGINE,
        group="判停",
        order=15,
    ),
    "speech.s2s.turn.smart_turn_model_path": FieldMeta(
        label="SmartTurn 模型路径",
        hint="留空则自动下载",
        widget="file",
        reload=Reload.ENGINE,
        group="判停",
        order=13,
    ),
    "speech.s2s.turn.smart_turn_threshold": FieldMeta(
        label="SmartTurn 阈值", reload=Reload.ENGINE, group="判停", order=14
    ),
    "speech.s2s.turn.speculative_reopen_ms": FieldMeta(
        label="重开宽限（判定说完）",
        hint="降低它能压 p50，代价是主播续说的窗口变窄",
        unit="ms",
        audience=Audience.OPERATOR,
        reload=Reload.ENGINE,
        group="判停",
        order=9,
    ),
    "speech.s2s.turn.speech_pad_ms": FieldMeta(
        label="前置缓冲",
        hint="开口前留多少音频，防止第一个字被切掉",
        unit="ms",
        reload=Reload.ENGINE,
        group="判停",
        order=7,
    ),
    "speech.s2s.turn.thresh": FieldMeta(
        label="VAD 灵敏度",
        hint="越高越不容易把噪音当成说话",
        unit="",
        audience=Audience.OPERATOR,
        reload=Reload.LIVE,
        group="判停",
        order=1,
    ),
    "speech.s2s.turn.unanswered_reopen_ms": FieldMeta(
        label="未答复重开上限", unit="ms", reload=Reload.ENGINE, group="判停", order=10
    ),
    "speech.side": FieldMeta(label="后台模型", group="后台模型"),
    "speech.side.api_key_ref": FieldMeta(
        label="侧路模型 Key", secret=True, reload=Reload.LIVE, group="后台模型", order=3
    ),
    "speech.side.base_url": FieldMeta(
        label="侧路模型地址",
        audience=Audience.OPERATOR,
        reload=Reload.LIVE,
        group="后台模型",
        order=1,
    ),
    "speech.side.model": FieldMeta(
        label="侧路模型 id",
        audience=Audience.OPERATOR,
        reload=Reload.LIVE,
        group="后台模型",
        order=2,
    ),
    "speech.side.thinking": FieldMeta(
        label="思考模式",
        hint="固定关。侧路调用不需要思考，且会拖慢后台任务",
        reload=Reload.LIVE,
        group="后台模型",
        order=4,
    ),
    "speech.side.tool_choice": FieldMeta(
        label="工具调用",
        hint="固定关。侧路调用不该有副作用",
        reload=Reload.LIVE,
        group="后台模型",
        order=5,
    ),
    "custom_tts.api_key_ref": FieldMeta(
        label="语音引擎 Key", secret=True, reload=Reload.LIVE, group="声音", order=4
    ),
    "custom_tts.engine": FieldMeta(
        label="语音引擎",
        hint="可插拔；主力规划是 IndexTTS（授权和 GPU 到位即切），qwen3_cloud 是当前默认",
        audience=Audience.STREAMER,
        reload=Reload.LIVE,
        group="声音",
        order=1,
        wizard_step=3,
    ),
    "custom_tts.speed": FieldMeta(
        label="语速", audience=Audience.STREAMER, reload=Reload.LIVE, group="声音", order=3
    ),
    "custom_tts.voice": FieldMeta(
        label="音色",
        audience=Audience.STREAMER,
        reload=Reload.LIVE,
        group="声音",
        order=2,
        wizard_step=3,
    ),
}
