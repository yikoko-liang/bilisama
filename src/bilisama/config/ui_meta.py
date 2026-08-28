"""UI metadata, keyed by field path.

This used to hang off every field as `json_schema_extra`, which accounted for most
of the schema's bulk while having no consumer at all — the settings page is not
built yet. As plain data the schema is back to types and defaults, and the Electron
side can read this directly instead of digging it out of a JSON Schema.

The cost is a second place to keep in sync, so `tests/unit/test_ui_meta.py`
reconciles the two: field paths must match exactly, numeric fields must be bounded.
Labels and hints stay in Chinese — they are shown to the streamer.

Who actually reads this today: `ui/server.py:149` (`config_snapshot`) renders the
config tab from it, and `ui/config_edit.py` refuses a write to a path that is not
in here. Between them they pass through label, hint, group, order, unit, audience,
reload, secret — and `provider_scoped`, which `ui/server.py:174` uses to hide the
sections belonging to backends this session is not using. `widget`, `wizard_step`
and `aliases` are still written and not read: the panel infers its own controls,
and there is no wizard. `check_ui_meta` below and the tests keep those three
honest until the page catches up (plan §7.5, backlog: panel side).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace

from bilisama.config._ui import Audience, Reload
from bilisama.config.enums import ProviderName


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
        reload=Reload.RESTART,
        group="总览",
        order=1,
    ),
    # The whole [audio] section is written and not read (schema.py:150-180).
    # Every hint here says so out loud rather than describing the behaviour the
    # field was named for: a switch that promises a number it does not deliver
    # sends people tuning it when something else is wrong.
    "audio.echo_guard": FieldMeta(
        label="抢跑静音",
        hint="这个开关现在没人读。打断走的是「她一听见你说话就把播放队列清空」那条路，"
        "不是压音量",
        audience=Audience.OPERATOR,
        reload=Reload.RESTART,
        group="音频",
        order=4,
    ),
    "audio.input_device": FieldMeta(
        label="麦克风",
        hint="这里选了不算数：真正用哪个麦克风由面板「现场」页上的下拉框决定",
        widget="device",
        audience=Audience.STREAMER,
        reload=Reload.RESTART,
        group="音频",
        order=1,
        wizard_step=4,
    ),
    "audio.output_device": FieldMeta(
        label="AI 声音输出到",
        hint="同上，这里选了不算数：扬声器也在面板「现场」页上选",
        widget="device",
        audience=Audience.STREAMER,
        reload=Reload.RESTART,
        group="音频",
        order=2,
        wizard_step=4,
    ),
    "audio.input_enabled": FieldMeta(
        label="语音输入",
        hint="关掉后麦克风还在采、电平还能看，只是不再送进语音后端；每次启动恢复开",
        audience=Audience.STREAMER,
        reload=Reload.LIVE,
        group="音频",
        order=5,
    ),
    "audio.output_enabled": FieldMeta(
        label="语音播报",
        hint="关掉后她照常想和写，只是不出声；每次启动恢复开",
        audience=Audience.STREAMER,
        reload=Reload.LIVE,
        group="音频",
        order=6,
    ),
    "audio.noise_sensitivity": FieldMeta(
        label="环境噪音灵敏度",
        hint="0 更抗噪，100 更容易听见小声说话；这是本地门限，不是服务端判停参数",
        widget="slider",
        audience=Audience.STREAMER,
        reload=Reload.LIVE,
        group="音频",
        order=7,
    ),
    "audio.output_route": FieldMeta(
        label="输出方式",
        hint="这两个值现在都不生效，声音去哪儿由「声音」那一节的扬声器决定；"
        "别让 OBS 把她的声音再放出来，那条路回声消除够不着",
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
        label="形象 / 皮肤包",
        hint="跟着形象类型解释：tofu 不用填；sprite 填皮肤包目录名；live2d 填模型目录名",
        audience=Audience.STREAMER,
        reload=Reload.RESTART,
        group="形象",
        order=2,
        wizard_step=3,
    ),
    "avatar.renderer": FieldMeta(
        label="形象类型",
        hint="tofu 内置像素机器人（豆腐），零素材；sprite 精灵图皮肤包；live2d 待接入（阶段 5）",
        audience=Audience.STREAMER,
        reload=Reload.RESTART,
        group="形象",
        order=1,
        wizard_step=3,
    ),
    "interaction.burst_uniques": FieldMeta(
        label="旧版批量欢迎人数",
        hint="动态进房合并已接管，这个键没有读者了；留着只为兼容旧配置",
        audience=Audience.DEVELOPER,
        reload=Reload.RESTART,
        group="互动",
        order=6,
    ),
    "interaction.burst_window_s": FieldMeta(
        label="旧版批量欢迎窗口",
        hint="动态进房合并已接管，这个键没有读者了；留着只为兼容旧配置",
        unit="s",
        audience=Audience.DEVELOPER,
        reload=Reload.RESTART,
        group="互动",
        order=7,
    ),
    "interaction.burst_cooldown_s": FieldMeta(
        label="旧版批量欢迎冷却",
        hint="动态进房合并已接管，这个键没有读者了；留着只为兼容旧配置",
        unit="s",
        audience=Audience.DEVELOPER,
        reload=Reload.RESTART,
        group="互动",
        order=15,
    ),
    "interaction.chattiness": FieldMeta(
        label="话痨程度",
        hint="在当前直播间活跃度上相对调节普通弹幕、进房欢迎和主动话题的频率；"
        "回复长度由下面的独立档位管",
        widget="segmented",
        audience=Audience.STREAMER,
        reload=Reload.LIVE,
        group="互动",
        order=1,
        wizard_step=5,
        aliases=("话多", "频率"),
    ),
    "interaction.reply_length": FieldMeta(
        label="回复长度",
        hint="每句说多长：短档一句短话，中档一到两句，长档两到四句",
        widget="segmented",
        audience=Audience.STREAMER,
        reload=Reload.LIVE,
        group="互动",
        order=2,
    ),
    "interaction.gift_battery_high": FieldMeta(
        label="高额礼物门槛",
        hint="按观众礼物面板上的电池数分档（1 电池 = 0.1 元）；到这个数按高额答谢",
        unit="电池",
        audience=Audience.STREAMER,
        reload=Reload.LIVE,
        group="互动",
        order=4,
    ),
    "interaction.gift_battery_medium": FieldMeta(
        label="中额礼物门槛",
        hint="到这个数按中额答谢；不得高于高额门槛",
        unit="电池",
        audience=Audience.STREAMER,
        reload=Reload.LIVE,
        group="互动",
        order=5,
    ),
    "interaction.sc_protect_ms": FieldMeta(
        label="旧版付费消息保护时长",
        hint="已不再生效：主播现在任何时候都能打断，付费答谢靠重新排队防丢",
        unit="ms",
        audience=Audience.DEVELOPER,
        reload=Reload.LIVE,
        group="互动",
        order=3,
    ),
    "interaction.entry_welcome": FieldMeta(label="进房欢迎", group="互动", order=8),
    "interaction.entry_welcome.ordinary": FieldMeta(
        label="欢迎普通观众",
        hint="普通进房按房间活跃度合并成一句欢迎；繁忙时自动闭嘴",
        audience=Audience.STREAMER,
        reload=Reload.LIVE,
        group="互动",
        order=9,
    ),
    "interaction.entry_welcome.naval": FieldMeta(
        label="欢迎舰队用户",
        hint="现役舰长/提督/总督进房按名欢迎",
        audience=Audience.STREAMER,
        reload=Reload.LIVE,
        group="互动",
        order=10,
    ),
    "interaction.entry_welcome.ranking": FieldMeta(
        label="欢迎高级粉丝牌",
        hint="佩戴本房 5 级以上粉丝牌的观众进房按名欢迎",
        audience=Audience.STREAMER,
        reload=Reload.LIVE,
        group="互动",
        order=11,
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
        reload=Reload.RESTART,
        group="记忆",
        order=2,
    ),
    "memory.retain_event_days": FieldMeta(
        label="原始事件保留",
        unit="天",
        audience=Audience.OPERATOR,
        reload=Reload.RESTART,
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
        hint="对应 config/personas/<id>/；直播中切换会整套换掉锚点、生长文件和提示词",
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
    "persona.streamer_name": FieldMeta(
        label="AI 怎么称呼你",
        hint="填你的名字或昵称，它说话时就会带上；留空按「主播」称呼。只管本场，重启清空",
        audience=Audience.STREAMER,
        reload=Reload.LIVE,
        group="人设",
        order=2,
        wizard_step=5,
    ),
    "persona.display_name": FieldMeta(
        label="AI 叫什么",
        hint="它自称什么。出厂填着「豆腐」，而且不跟着人设走——换成 hanako 这类人设时"
        "要连这一行一起改，否则她还是自称豆腐。留空才回落到人设的目录名",
        audience=Audience.STREAMER,
        reload=Reload.LIVE,
        group="人设",
        order=3,
        wizard_step=5,
    ),
    "persona.growth": FieldMeta(label="生长层", group="人设", order=4),
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
        hint="直播中可换房（进程内重连）；面板里改的是本场，不写回配置文件",
        audience=Audience.STREAMER,
        reload=Reload.LIVE,
        group="直播间",
        order=1,
        wizard_step=1,
        aliases=("房间", "roomid"),
    ),
    "room.stream_intro": FieldMeta(
        label="直播主题",
        hint="本场在做什么，一两句话；会进提示词，欢迎新观众时用得上，也参与弹幕相关性判断",
        widget="textarea",
        audience=Audience.STREAMER,
        reload=Reload.LIVE,
        group="直播间",
        order=4,
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
        reload=Reload.RESTART,
        group="安全",
        order=2,
    ),
    "safety.on_hit": FieldMeta(
        label="命中后怎么办",
        audience=Audience.OPERATOR,
        reload=Reload.RESTART,
        group="安全",
        order=3,
    ),
    "safety.wordlist_path": FieldMeta(
        label="敏感词表",
        widget="file",
        audience=Audience.OPERATOR,
        reload=Reload.RESTART,
        group="安全",
        order=1,
    ),
    "speech.volcano": FieldMeta(label="火山引擎", provider_scoped="volcano", group="语音"),
    "speech.volcano.endpoint": FieldMeta(
        label="服务地址",
        hint="留空即用内置的公网地址，一般不用改",
        provider_scoped="volcano",
        audience=Audience.OPERATOR,
        reload=Reload.RECONNECT,
        group="火山语音",
        order=1,
        wizard_step=2,
    ),
    "speech.volcano.api_key_ref": FieldMeta(
        label="API Key",
        hint="控制台 > API Key 管理里拿一个，单独一个就够，不用再填 App ID",
        provider_scoped="volcano",
        audience=Audience.STREAMER,
        reload=Reload.RECONNECT,
        group="火山语音",
        order=2,
        wizard_step=2,
        secret=True,
    ),
    "speech.volcano.app_id_ref": FieldMeta(
        label="App ID（老账号）",
        hint="只有拿不到 API Key 时才填。跟下面的 Access Token 成对，缺一个都连不上",
        provider_scoped="volcano",
        audience=Audience.OPERATOR,
        reload=Reload.RECONNECT,
        group="火山语音",
        order=3,
        secret=True,
    ),
    "speech.volcano.access_key_ref": FieldMeta(
        label="Access Token（老账号）",
        hint="跟上面的 App ID 成对。别把 API Key 填这儿——放错位置服务端只回一句认不出这个凭据",
        provider_scoped="volcano",
        audience=Audience.OPERATOR,
        reload=Reload.RECONNECT,
        group="火山语音",
        order=4,
        secret=True,
    ),
    "speech.volcano.model": FieldMeta(
        label="模型版本",
        hint="1.2.1.1 用文字描述人设、配官方音色；2.2.0.0 用角色档案、配克隆音色",
        provider_scoped="volcano",
        audience=Audience.OPERATOR,
        reload=Reload.RECONNECT,
        group="火山语音",
        order=5,
        wizard_step=2,
    ),
    "speech.volcano.speaker": FieldMeta(
        label="音色",
        hint="留空用服务端默认。两个版本的音色清单不通用，换版本要跟着换",
        provider_scoped="volcano",
        audience=Audience.STREAMER,
        reload=Reload.RECONNECT,
        group="火山语音",
        order=6,
        wizard_step=2,
    ),
    "speech.volcano.end_smooth_window_ms": FieldMeta(
        label="判停静音（毫秒）",
        hint="停多久算一句说完了。调小抢话，调大接话慢——这条路上唯一的判停旋钮",
        provider_scoped="volcano",
        audience=Audience.OPERATOR,
        reload=Reload.RECONNECT,
        group="火山语音",
        order=7,
        unit="ms",
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
    "speech.dashscope.voice": FieldMeta(
        label="音色",
        hint="留空用服务端默认，那个默认偏尖（实测 343Hz）。名字写错时报错会列出可用音色",
        provider_scoped="dashscope",
        audience=Audience.STREAMER,
        reload=Reload.RECONNECT,
        group="托管语音服务",
        order=4,
        wizard_step=2,
    ),
    "speech.dashscope.session_cap_min": FieldMeta(
        label="会话轮换（分钟）",
        hint="留空跟随服务商自己的上限；0 = 不轮换。到点前主动换一条连接，免得说到一半被服务端掐断",
        provider_scoped="dashscope",
        audience=Audience.OPERATOR,
        reload=Reload.RECONNECT,
        group="托管语音服务",
        order=5,
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
        reload=Reload.RECONNECT,
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
        reload=Reload.RECONNECT,
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
    "speech.openai_ga.voice": FieldMeta(
        label="音色",
        hint="留空用服务端默认",
        provider_scoped="openai_ga",
        audience=Audience.OPERATOR,
        reload=Reload.RECONNECT,
        group="托管语音服务",
        order=4,
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
    "speech.openai_ga.session_cap_min": FieldMeta(
        label="会话轮换（分钟）",
        hint="留空跟随服务商自己的上限；0 = 不轮换。到点前主动换一条连接，免得说到一半被服务端掐断",
        provider_scoped="openai_ga",
        audience=Audience.OPERATOR,
        reload=Reload.RECONNECT,
        group="托管语音服务",
        order=5,
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
        reload=Reload.RECONNECT,
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
        reload=Reload.RECONNECT,
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
        reload=Reload.ENGINE,
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
        reload=Reload.ENGINE,
        group="判停",
        order=1,
    ),
    "speech.s2s.turn.unanswered_reopen_ms": FieldMeta(
        label="未答复重开上限", unit="ms", reload=Reload.ENGINE, group="判停", order=10
    ),
    "speech.side": FieldMeta(label="后台模型", group="后台模型"),
    "speech.side.api_key_ref": FieldMeta(
        label="侧路模型 Key", secret=True, reload=Reload.RESTART, group="后台模型", order=3
    ),
    "speech.side.base_url": FieldMeta(
        label="侧路模型地址",
        audience=Audience.OPERATOR,
        reload=Reload.RESTART,
        group="后台模型",
        order=1,
    ),
    "speech.side.model": FieldMeta(
        label="侧路模型 id",
        audience=Audience.OPERATOR,
        reload=Reload.RESTART,
        group="后台模型",
        order=2,
    ),
    "speech.side.thinking": FieldMeta(
        label="思考模式",
        hint="固定关。侧路调用不需要思考，且会拖慢后台任务",
        reload=Reload.RESTART,
        group="后台模型",
        order=4,
    ),
    "speech.side.tool_choice": FieldMeta(
        label="工具调用",
        hint="固定关。侧路调用不该有副作用",
        reload=Reload.RESTART,
        group="后台模型",
        order=5,
    ),
    "custom_tts.api_key_ref": FieldMeta(
        label="语音引擎 Key", secret=True, reload=Reload.RESTART, group="声音", order=4
    ),
    "custom_tts.engine": FieldMeta(
        label="语音引擎",
        hint="可插拔；主力规划是 IndexTTS（授权和 GPU 到位即切），qwen3_cloud 是当前默认",
        # 主播挑的是「音色」和「语速」，不是 qwen3_local 还是 gpt_sovits——
        # 那取决于这台机器部署了什么，是运营的事。加「AI 怎么称呼你」那两项时
        # 撞上了主播视图 20 项的上限，回头一查，标错受众的是这一条。
        audience=Audience.OPERATOR,
        reload=Reload.RESTART,
        group="声音",
        order=1,
        wizard_step=3,
    ),
    # Operator, not streamer: the streamer tier is capped at twenty controls,
    # and between "她听起来是谁" and "快一点慢一点" the first one wins the slot.
    # Speed also has no consumer yet — the TTS chain it belongs to is stage 4,
    # while speech.dashscope.voice governs the voice shipping today.
    "custom_tts.speed": FieldMeta(
        label="语速", audience=Audience.OPERATOR, reload=Reload.RESTART, group="声音", order=3
    ),
    "custom_tts.voice": FieldMeta(
        label="音色",
        audience=Audience.STREAMER,
        reload=Reload.RESTART,
        group="声音",
        order=2,
        wizard_step=3,
    ),
}


_PROVIDERS = frozenset(p.value for p in ProviderName)


def _scope_by_path(table: dict[str, FieldMeta]) -> None:
    """Anything under `speech.<provider>.` belongs to that provider. Say so.

    Thirty entries take their scope from here today; the twenty-four under
    `speech.s2s.` are invisible in practice because s2s is the shipped default
    and `ui/server.py` only hides the sections that are NOT running. The six
    that changed behaviour are dashscope's and openai_ga's endpoint, model and
    key status, which the panel used to list while another backend was live.

    Thirty entries had left `provider_scoped` blank — every endpoint, model and
    key under dashscope, openai_ga and s2s, plus every knob under
    `speech.s2s.turn` — so the panel listed all the backends' addresses at once
    and only one of them did anything. That is the exact complaint
    `ui/server.py:176` was written against; the metadata that would have
    prevented it was simply not filled in.

    Derived rather than typed out thirty more times, because the next
    provider would forget too. The path already says which backend owns a
    field, and check_ui_meta refuses a declaration that disagrees with it, so
    the explicit value could never have said anything different — it could only
    be missing. An explicit one still wins: a field that lives under one
    provider's section while belonging to another is not a shape we have, but
    inventing a rule that forbids it is not this function's business.
    """
    for path, entry in table.items():
        if entry.provider_scoped:
            continue
        parts = path.split(".")
        if len(parts) > 2 and parts[0] == "speech" and parts[1] in _PROVIDERS:
            table[path] = replace(entry, provider_scoped=parts[1])


_scope_by_path(UI_META)


# Read-only rows: chattiness computes these five and the TOML has no field for
# any of them (derive.py is the single writer). They are a separate dict rather
# than UI_META entries because `ui/server.py:149` resolves every UI_META path
# with getattr against Settings — a `_derived.*` key in there would raise on the
# config tab instead of rendering. `_derived` is the name `bilisama config show`
# already prints them under.
DERIVED_META: dict[str, FieldMeta] = {
    "_derived.idle_threshold_s": FieldMeta(
        label="冷场多久起话题",
        derived_from="interaction.chattiness",
        audience=Audience.OPERATOR,
        reload=Reload.LIVE,
        group="互动",
        order=20,
        unit="秒",
    ),
    "_derived.danmaku_window_s": FieldMeta(
        label="弹幕挑选窗口（基准值，运行时随房间活跃度浮动）",
        derived_from="interaction.chattiness",
        audience=Audience.DEVELOPER,
        reload=Reload.LIVE,
        group="互动",
        order=21,
        unit="秒",
    ),
    "_derived.score_threshold": FieldMeta(
        label="弹幕入选分数线",
        derived_from="interaction.chattiness",
        audience=Audience.DEVELOPER,
        reload=Reload.LIVE,
        group="互动",
        order=22,
    ),
    "_derived.cooldown_s": FieldMeta(
        label="两次发言最短间隔",
        derived_from="interaction.chattiness",
        audience=Audience.OPERATOR,
        reload=Reload.LIVE,
        group="互动",
        order=23,
        unit="秒",
    ),
    "_derived.max_output_tokens": FieldMeta(
        label="单次回复长度上限",
        derived_from="interaction.reply_length",
        audience=Audience.DEVELOPER,
        reload=Reload.LIVE,
        group="互动",
        order=24,
        unit="token",
    ),
}


def check_ui_meta(meta: Mapping[str, FieldMeta] | None = None) -> list[str]:
    """Plan §7.7 gate 1: metadata complete enough to render a control.

    `group` is in here because it was the one key the gate named and never
    looked at. All 108 entries carry one today, so nothing would have noticed
    until a settings page put an unlabelled row in a group called "".

    The bounds half of that gate needs the schema rather than the metadata and
    stays a schema walk (`test_numeric_fields_declare_bounds`).

    Args:
        meta: The table to check. Defaults to the shipped one; the tests pass a
            planted entry so every rule here is known to bite.

    Returns:
        One Chinese complaint per problem, empty when the table is fit to render.
    """
    table = UI_META if meta is None else meta
    complaints: list[str] = []
    for path, entry in table.items():
        if not entry.label:
            complaints.append(f"{path}：缺 label，控件会渲染成空白")
        if not entry.group:
            complaints.append(f"{path}：缺 group，设置页不知道把它放进哪一栏")
        scoped = entry.provider_scoped
        if scoped:
            if scoped not in _PROVIDERS:
                complaints.append(f"{path}：provider_scoped={scoped!r} 不是一个 provider 名字")
            elif not path.startswith(f"speech.{scoped}"):
                complaints.append(f"{path}：provider_scoped={scoped!r} 跟它自己的路径对不上")
        seen: set[str] = set()
        for alias in entry.aliases:
            # The path itself is fair game as an alias — `speech.provider` is
            # English and 「后端」 is what the streamer would type. Only a repeat
            # of the label, or of another alias, adds a row and finds nothing.
            if not alias or alias == entry.label or alias in seen:
                complaints.append(f"{path}：aliases 里的 {alias!r} 搜不出任何新东西")
            seen.add(alias)
    return complaints
