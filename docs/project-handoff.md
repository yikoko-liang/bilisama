# BiliSama 项目交接文档

更新时间：2026-09-15  
当前分支：yiko/planner_update  
当前提交：1dcdb54（Update planner interaction workflow）

这份文档给下一位继续开发、联调或验收的人使用。它记录当前代码真正做了什么、已经确认的产品约定、仍需真实模型验证的地方。完整 Prompt 和历史验收记录放在文末链接中。

## 1. 产品定位和业务背景

BiliSama 是 B 站直播间的 AI 伴播。它同时接收主播语音和直播间事件，在不抢主播主持位置的前提下，回答观众问题、感谢互动、欢迎观众、整理观点，并在合适的冷场时机主动开口。

生产角色名称是“豆腐”。历史代码和素材中出现过 Mia、Miya、Miku、Hanako 等名称；当前生产模板和测试集应使用“豆腐”，变量名仍可能沿用 agentName。

已知的目标主播类型：

- AIGC 教学类：AI 代码侠土豆、Ai_随风。主要内容是 AI 动态漫、AI 真人短剧、Prompt 编写、Skill 测试和生成效果品鉴。
- AI coding 类：小小小名不是小明。主要内容是用 DeepSeek harness 开发工具或产品、debug、看视频和品鉴效果。

一场直播会在讲解、操作、自言自语、回答弹幕、收集观点、感谢礼物、欢迎进房和观看效果之间切换。意图判断不能把整段直播固定成一种类型，也不能只看一个关键词。

## 2. 当前分支、仓库和工作区

### 2.1 Git 状态

当前分支是 yiko/planner_update，最新提交已推到内部 GitLab：

- 内部仓库： https://git.bilibili.co/yiangjiang/bilisama.git
- 远端分支：origin/yiko/planner_update
- 最新提交：1dcdb54051ecadd3d5e61394214abd1bf873190f
- GitHub 远端名：github，指向 https://github.com/yikoko-liang/bilisama.git

该分支以远端 yiang/feat/planner 的 85431db 为 Planner 基线，之后加入语音门、测试集、默认 Qwen 启动入口、主播事件上下文和交互状态报告。最近一次提交包含代码、配置、测试和文档；生成的报告、音频、图片、Excel 原始附件和 AGENTS.md 没有入库。

接手先执行：

~~~bash
git status --short --branch
git log --oneline --decorate -8
git remote -v
~~~

工作区可能仍有 outputs/、预制音频、测试 Excel、架构图片和本机规则文件。除非明确需要，不要执行 git add .。

### 2.2 凭据规则

path.sh 被 .gitignore 忽略，只放本机凭据。代码读取的环境变量包括：

- BILI_SESSDATA：B 站登录态；没有它时大部分 UID 和昵称会被匿名化。
- ali_api_key、dashscope_url：DashScope/Qwen Realtime。
- volcano_api_key：火山豆包和 Seed TTS 2.0 测试音频。
- side_model_name 或 speech.side 配置：主动话题、观点总结、记忆蒸馏使用的侧路模型。

不要把真实 key 写进配置、Prompt、测试素材、提交或交接消息。若 key 曾在聊天、日志或截图中暴露，继续开发前应考虑轮换。

## 3. 从启动到播放的整体结构

~~~text
主播麦克风/直播 Mock 音轨
        │
        ▼
Realtime provider（DashScope / OpenAI GA / 火山 / 本地 s2s）
        │ LinkEvent：语音、回复文字/音频、工具调用、连接状态
        ├── VoiceTurnGate：解析隐式语音回复开头的 [SKIP]
        ├── 主播语音播放链
        └── InteractionReports：接收独立后台状态报告

B 站弹幕/礼物/SC/上舰/进房
        │
        ▼
ingest source → safety/dedup → selector/entry/gift 聚合 → Assembly
        │                                  │
        │                                  ├── MemoryStore / Distiller
        │                                  ├── ProactiveOpportunities
        │                                  └── Intent → Scheduler → Realtime 回复
        │                                                        │
        └────────────── 事件流水、UI 状态、可观测日志 ◄──────────┘
~~~

生产入口只有一条：start_bilisama.sh 最终执行 bilisama dev-talk --director。装配和任务启动在 src/bilisama/dev_talk.py 的 run_director；核心装配在 src/bilisama/app.py:82 的 Assembly。

四个功能域：

| 功能域 | 目录 | 作用 |
|---|---|---|
| 界面 | src/bilisama/ui/、desktop/preview/ | FastAPI 面板、桌宠壳、测试页、直播 Mock |
| Realtime | src/bilisama/realtime/ | 统一 SpeechLink、协议方言、provider 适配 |
| 编排 | src/bilisama/director/、app.py、proactive.py、memory/ | 意图、状态、说话权、队列、上下文 |
| 直播接入 | src/bilisama/ingest/ | blivedm 事件转换、身份、过滤、窗口和节奏 |

L3 编排层不能依赖具体 realtime.providers.*。依赖方向由 tests/unit/test_dependency_direction.py 守门。

## 4. Realtime 和 provider 差异

### 4.1 对外抽象

L3 只依赖 SpeechLink 和 ReplySpec（src/bilisama/realtime/link.py）。它可以连接、暂停、恢复、推送音频、写上下文、请求一轮回复、取消和读取 LinkEvent，不直接拼 provider 协议帧。

ReplySpec 的两个重要部分：

- item_text：写入共享历史的事实或事件材料。
- instructions / base_instructions：本轮要遵守的回复规则。

共享人设和历史不会按语音/事件拆成两份；拆开的是本轮 Prompt 的规则入口。支持逐轮 base instruction 的 provider 会在事件回复时单独带事件规则；火山没有逐轮指令通道，事件规则需要进入会话上下文。

### 4.2 provider

| provider | 默认/入口 | 当前特点 |
|---|---|---|
| DashScope | qwen-audio-3.0-realtime-flash | 当前默认启动路径；支持 report_interaction 工具报告 |
| OpenAI GA | 由环境和命令行指定 | 与 HostedLink 共用传输层；旁路能力取决于 capabilities |
| Volcano | 2.2.0.0 + 配套 speaker | 火山二进制协议，自己维护槽位和重连；没有逐轮指令和当前工具报告通路 |
| 本地 s2s | scripts/smoke_provider_b.sh serve 后连接 | 独立 venv，协议补丁通过 shim 注入 |

quiet_window_s 由 provider 提供，编排层不重复计算。当前出厂值约为：托管 0.6 秒、火山 1.8 秒、本地 s2s 1.9 秒。

重要差异：

- HostedLink 和 s2s 是文本/音频事件流；火山把事件材料和本轮要求合成 ChatTextQuery。
- 火山 SC2.0 会通过换会话更新部分配置，换会话期间可能丢少量上行音频；O2.0 用 UpdateConfig，适合验收语音连续性。
- 火山不能可靠承载每轮隐藏 Prompt 标签，事件回复如果生成 [SKIP] 等标签，存在被念出来的风险，须单独听验。
- report_interaction 目前只在 DashScope 的指定 Qwen Realtime 模型装配；换到 Volcano 时，普通语音仍可用，但依赖工具报告的后台状态联动不会完整生效。

## 5. 共享上下文和输入边界

### 5.1 共享原则

语音、弹幕、礼物、进房、上舰、SC、主播本人打字和豆腐已播内容进入同一条会话历史。取消播报、[SKIP]、主播事件不走回复流程，都不等于删除历史或记忆。

代码入口：

- Assembly.on_event：src/bilisama/app.py:192，先写 MemoryStore、Distiller 和主动性观察，再决定是否进入回复。
- 事件观察约每 0.5 秒合并写入共享上下文：app.py:431-454。
- 主播本人弹幕写入 anchor_danmaku_context_item：app.py:223-229、466-476。
- 每个困难测试用例会新建会话，只加载本例背景，不加载上一例历史；同一用例内语音和事件仍共享上下文。

### 5.2 输入对象

语音 Prompt 先判断麦克风里的话是：

- 主播在对豆腐说话；
- 主播面向观众讲解或征集观点；
- 主播回答某条弹幕；
- 主播感谢礼物、SC、上舰或欢迎进房；
- 主播自言自语、操作旁白、观看效果或对旁人说话；
- 主播要求豆腐静默。

事件 Prompt 再判断弹幕目标：

- @ 的目标 UID 与主播一致：按正常面向主播的弹幕判断。
- @ 明确是其他观众：默认过滤，除非模型判断同时邀请豆腐、主播或全场参与。
- 无可靠 UID：不能凭昵称相同就断定目标。
- 观众之间问答、接梗、打招呼、互相评价：应过滤，不替被 @ 的观众回答。

主播自己的弹幕：

- 进入事件流水、MemoryStore、共享上下文和互动状态观察。
- 如果带可靠 @ 观众 UID 且正文是实际回答，InteractionState.mark_anchor_reply 只把最近对应的一条观众弹幕标为 handled。
- 不进入普通弹幕回复、观众活跃度或主动话题的未回应计数。
- 已在生成或播放的旧回复不被主播后来的打字反向打断；只影响后续候选。

## 6. Prompt 分层和输出协议

Prompt 的公开快照在 docs/current-interaction-prompts.md，生成脚本是 tools/export_interaction_prompts.py。

运行时顺序大致是：

~~~text
豆腐公共人设
  + 共享历史/直播事件数据
  + 语音回合规则 或 事件回合规则
  + 对应事件的本次事件要求
  + （若 provider 支持）独立的 base_instructions
  + （仅状态变化时）REPORT_RULES 和 function schema
~~~

### 6.1 公共人设必须保持的边界

- 豆腐是主播的 AI 伴播，不是直播主角。
- 她可以直接表达自己的偏好、观点、态度和能力边界，不盲目附和，也不代主播承诺。
- 她看不到视频画面，不能执行播放、暂停、点击按钮、改代码等后台动作；视觉问题要说明看不到，并请主播描述。
- 共享记忆只在相关话题被提及时无声参与，不主动暴露“我记得你上次……”。
- 输出是要念出来的话，不加 Markdown、动作描写、JSON 或内部分类。

### 6.2 语音回合

config/personas/live/voice_responses.md 加 voice_addressing.md：

- 明确对豆腐说：直接回答或执行能力范围内的委托。
- 自言自语、长讲解、对观众说、念弹幕、听不清对象：输出 [SKIP] 加不超过十字的观察说明，或只输出 [SKIP]。
- 语音回复必须保持现有协议：[SKIP] 是回复第一个字符，整条 message 一行；不能在正文里加入意图标签、事件编号或工具说明。
- “帮我总结弹幕”是独立委托：语音回合以 [SUMMARY] 记号开头并保持静默（2026-09-15 起，不再用函数报告 start），不把这句话当成向观众征集观点。
- 主播已经在回答某条弹幕、感谢礼物或欢迎进房时，允许豆腐知情附和，但不能完整重答、重新感谢或重新欢迎。

### 6.3 直播事件回合

config/personas/live/event_responses.md 和 src/bilisama/director/intents.py:_instruction_for 共同决定语气和边界：

- 弹幕：先自然交代“谁问了什么”，再给实际回答；能回答的先答，确实需要主播确认时自然把问题抛给主播，不能把需要确认当成不回复。观众互聊（@ 了别的观众、或刚被 @ 过就写回去）由程序按事实判定并拦下，模型不再自判互聊；观众回应主播提问的弹幕按正常弹幕整理给主播（2026-09-17）。
- 单条/批量：同题合并、保留分歧，不逐条复读；相似话题不等于同一条事件已处理。
- 礼物：按电池档位分普通、中额、高额；礼物名可接梗；不称普通礼物观众为老板；不口播金额、电池数或价格。
- SC：支持感谢和正文问题分开判断；只感谢支持用 support_thanked，正文也答了才 handled。
- 上舰：按舰长、提督、总督区分仪式感，允许附和，不重复完整感谢。
- VIP 进房：先欢迎，再视近期历史和本场背景简单同步热聊内容；刚说过就只欢迎，不固定说“咱们正在聊……”。
- 普通进房：根据活跃度短暂合并；可以只做轻量欢迎，不猜人数、不逐个报名单。
- 播放时机不归模型（2026-09-16）：主播在不在说话由 SpeakingFloor 和调度器处理，事件 prompt 只讲分工、不让模型判断，判断要不要回只看这条事件和它的处理记录；被插话后重入队的付费 / VIP 意图重放走 intents.replay_injection：写 [重放] 条目、只留形状规则加 REPLAY_RULES（只判主播有没有亲口处理）、不邀请报告工具，最多两次（2026-09-17）。

## 7. 意图、说话权和队列

### 7.1 意图类型

产品设计中的七类意图标签仍用于测试覆盖：TO_ME、AUDIENCE、SELF_TALK、READING、GUEST、UNSURE、DECLINED。当前生产 Realtime 并不会返回七类分类事件；实际语音链路是“接话 / [SKIP]”二分类。不要把测试页的预期标签说成后端已经分类成功。

### 7.2 优先级

src/bilisama/director/intent.py:24-47，数字越大越优先。

| 优先级 | 意图 | 说明 |
|---:|---|---|
| 100 | STREAMER | 主播自身语音，隐式回合，不是普通 Intent |
| 90 | DANMAKU_SUMMARY | 主播明确委托的弹幕总结；语音式交付，仅次于主播语音 |
| 80 | SUPERCHAT | SC 答谢/正文处理 |
| 70 | BIG_GIFT | 高额礼物 |
| 65 | GUARD_BUY | 上舰 |
| 50 | VIP_ENTER | VIP 进房、本房高等级粉丝牌进房和中额礼物 |
| 40 | BACKGROUND_RESULT | 预留，当前没有生产者 |
| 30 | DANMAKU | 普通弹幕、小礼物和普通进房 |
| 10 | PROACTIVE | 冷场主动话题 |

### 7.3 SpeakingFloor 和 Scheduler

SpeakingFloor（director/floor.py:32）按顺序阻挡：主播正在说、已有 Realtime 回合、音频仍在播、语音边沿静默窗、话痨冷却。主播语音永远拿回话权；普通事件不会盖过主播。

Scheduler（director/scheduler.py:180）负责：

- 先写事件块，再请求 Realtime 回复。
- 只有回复首字前允许普通意图抢占；开始播出的句子不被普通新事件切碎。
- 主播插话会取消正在生成/播放的事件；带 requeue_on_interrupt 的付费事件回队列，等待主播回合结束后再说。
- VIP 进房和上舰未被主播明确处理时，不直接 SKIP，而是生成后等待当前语音和高优先级任务完成；只有模型报告主播已经欢迎/处理，才将事件标为完成。
- 直播事件的 cancelled、skipped、expired 必须区分：取消可能是被主播抢话，不等于模型判断无需回复。
- 语音回合被 [SKIP] 拦截时，VoiceTurnGate 和调度器分别记账；被拦下的音频不计作豆腐已说出口。

### 7.4 Function call / 状态报告

报告函数是 report_interaction，声明和规则在 src/bilisama/director/interaction_state.py:29-150。

当前约定不是每轮强制调用：

- 普通知识问答、自言自语先听、连续讲解保持安静，且没有后台状态变化：只输出正文或 [SKIP]，不调用。
- 确认某个精确事件已处理、进入/解除持续静默、开始/取消观点征集、取消弹幕总结（开始改走语音回合开头的 [SUMMARY] 记号，见 §9.2）：同一 Realtime 响应里保留原 message，再独立调用报告。
- 报告中的 events 只能引用已提供的精确事件编号；同话题、同用户或早期背景不能批量关联。
- handled 表示主播确实回答了问题或完成感谢/欢迎；仅念题、叫昵称、准备回答用 processing。
- 主播本人带可靠 @ 目标 UID 的弹幕回答可以直接标对应最新事件为 handled。
- 报告不是口播内容，不改变 [SKIP] 单行格式，不等待工具回执再重新生成回答。

当前已知限制：只有 DashScope 指定 Qwen Realtime 装配这个工具；Volcano 路径需要后续补齐等价的状态通道，不能假设只改 Prompt 就能联动。

## 8. 直播事件接入、过滤和去重

### 8.1 事件流水

所有事件先进入 Assembly.on_event、MemoryStore 和 UI 流水，然后才决定是否进入说话队列。事件的“看见”“记住”“作为候选”“实际播出”是四个不同状态，排查时不能只看最后一条聊天气泡。

### 8.2 弹幕

（2026-09-16 订正：此前这一节写的“低信息过滤、打分、单窗口只选一条”是 9 月 14 日之前的形态。提交 0d103a9 把本地文本漏斗从选择器里删掉了，`scoring.py` 里的 `danmaku_text_signal`/`danmaku_score` 已无生产调用方，只有单测在测。）

当前普通弹幕的路：去重环和熔断（`safety.py`）→ 按房间活跃度开 1/2/4/8 秒窗口 → 窗口关闭时取最新的至多 8 条打成一个批次（`selector.py`，“Freshness/capacity only”）→ `deliver_danmaku_batch` 扣一个普通预算令牌 → 一次模型请求。批次里每条都进模型，该不该回由事件 Prompt 判，模型输出 [SKIP] 记为 model_declined。

程序侧在模型之前只做两件事（`director/viewer_threads.py`，2026-09-16 加）：

- 对其他观众说的弹幕不进回复车道：平台 reply 目标是非房主（硬事实），或者正文以“@昵称 ”开头且昵称既不是主播也不是豆腐、正文没有再转向主播/豆腐/大家（弱事实；没有分隔符的“@白团你那个…”判不出昵称边界，交给模型）。被拦的仍进记忆、面板和共享上下文，只是不排队；日志 `assembly.viewer_chat_skipped`，健康卡 `viewer_chat_skipped`。
- 被 @ 过的观众 90 秒内写回来的弹幕，事件行上标注“N 秒前被观众 X @过”，事件规则告诉模型这默认是互聊；是否 [SKIP] 仍由模型判。

同一观众没有固定冷却；同题合并、互聊识别都在模型侧。

### 8.3 礼物、SC、上舰和进房

- 礼物连击先以组合聚合，空闲约 1 秒结算；同一组合 10 分钟内不重复感谢。
- 礼物按前端可见电池分档，金额不进入模型事件行；默认高额保留 BIG_GIFT，中额降到 VIP_ENTER，普通礼物进入 DANMAKU 档。
- SC、上舰和高额礼物默认支持主播插话后重排队；付费保护开关 protect_paid_replies 默认关闭，打开后只对 SC/高额礼物在支持的 provider 上保护短窗口。
- 进房欢迎按活跃度合并。VIP 事件有“待欢迎”和“已欢迎”两套记录，只有播放完成才记为已欢迎；被主播语音打断时应保留待处理事件，后续排队。
- 关注、点赞、分享目前主要进入流水/记忆，不产生说话 Intent。

### 8.4 事件与主播语音去重

模型报告和主播本人弹幕只关闭“确定已经处理”的具体事件。主播说“我看到了”“准备回答”不能直接关闭；主播已经回答且有明确目标时才 handled。豆腐可以在主播感谢后附和一句，但不能把附和当成又来了一次新礼物或新欢迎。

## 9. 主动话题和弹幕总结

### 9.1 普通主动话题

ProactiveTopicLoop（src/bilisama/proactive.py）有后台候选刷新和前台冷场触发两部分，2026-09-16 按七层来源重做（细节见 architecture-status.md §6）：

- 七层来源（src/bilisama/proactive_sources.py 的 Layer）：欠着的回复（生成被打断和播放被打断都算，素材标明哪种）→ 没人答的弹幕 → 弹幕观点整理 → 接主播的话 → 本场进展/直播简介 → 在场常客记忆 → 通用趣味池（config/prompts/topics/，`interaction.proactive.topic_pool` 选文件，一场每条一次）。房间档位决定放行到哪一层：繁忙不起题，活跃 1–4，稀疏加 5，冷清全开。
- 侧路模型存在时先按本轮选题层生成不超过 80 字的候选；不存在时仍可让 Realtime 直接按层素材选题。
- 防重复三道：普通车道判过的弹幕在 Assembly.note_verdict 里 mark_answered，永久不再当「没人答」；主动话题用过的素材按半衰期回流（10 分钟硬排除，60 分钟忘记），没播出去的话题还回素材；本场话题账本（TopicLedger）出口查重 0.6，侧路候选重复就丢、她说出口的重复就冷却 5 分钟。
- 节奏：两次主动开口至少 90 秒，主播说话和她自己说完都重置冷场计时；侧路候选只在同层 60 秒内有效。
- 纯观众互聊、已处理、低信息事件和静默停止的内容不进候选。
- 未重排的中断事件是第 1 层，Prompt 要求先自然回顾背景，不能伪装成事件刚发生。
- unanswered_count 只用于换角度、降低参与门槛和观测连续未回应，不是唯一触发条件。

### 9.2 主播委托的弹幕总结

这是单独的 DANMAKU_SUMMARY=90，不要与普通冷场主动话题混淆：

1. 主播语音说“帮我看/整理/总结弹幕”。
2. Realtime 在这一语音回合开头写 [SUMMARY]（语音门解码，这轮静音；函数报告的 start 仍被接受但提示词不再要求，Volcano/s2s 也走得通）。
3. dev_talk.on_voice_skip 调 Assembly.request_danmaku_summary，用语音门记下的本轮 SpeechStarted 时刻作为边界。
4. ProactiveOpportunities.due_danmaku_summary 只取边界之前、尚未处理、最近的观众弹幕，最多 8 条；不把语音之后新到的弹幕混入。
5. 事件 Prompt 让 Realtime 在候选中分析近期热议 topic，优先多人反复提及、追问或有观点分歧的主题；孤立、低信息、互聊不选。
6. 生成一条给主播听的语音式总结，写回共享历史并标记候选已使用；没有形成热议时也回主播一句（各问各的、点一两条值得看的），不 [SKIP]。
7. 边界之前一条可总结的弹幕都没有时，提交一条「没有新弹幕」的空总结回复（SummaryOutcome.EMPTY），不沉默；半分钟内重复的委托才静默（SILENT）。主播点名交代的事原则上都要回，说不清就问，这条也写进了 voice_addressing.md。

这条链路是“同一 Realtime 会话中的语音报告 + 后续事件回复生成”，不是另起一个独立语音上下文，也不是等待未来弹幕再总结。受 SpeakingFloor 和普通事件队列影响，实际首声延迟仍受模型网络和当前播放状态影响，尚未建立稳定基线。

### 9.3 观点征集

主播面向观众征集观点时只启动 discussion 状态，不立即代观众回答。征集窗口默认 30（2026-09-17 从 120 改） 秒，可提前结束；窗口内只收取起点之后的观众弹幕，窗口结束后总结共同点和分歧。主播询问豆腐自己的观点不算观点征集。

## 10. 人设能力边界和用户体验约定

这些是后续改 Prompt 时不能丢的产品事实：

- 不能把主播当成第三个人。麦克风输入默认是当前主播本人；共享上下文里的主播弹幕也要标明“主播本人”。
- 喊错豆腐名字、谐音或口误不能直接拒答，要结合语义判断是否在叫她。
- 主播说“停一下/先别说/我看下效果”时，是否进入持续静默由模型判断；一旦进入，普通事件和主动话题不会自动解除。只有后续明确适合开口的意图才报告 release。
- “暂停视频”是主播对视频的动作，不自动等于让豆腐闭嘴。
- 豆腐无法看到视频，也不能真的播放/暂停视频或改后台；视觉和动作问题必须用自然语言说明边界。
- 弹幕需要主播确认时，豆腐不是直接 SKIP，而是先复述谁问了什么，再自然邀请主播回答。只有完全无法判断对象或无价值互聊时才跳过。
- 语音和事件冲突时，主播语音优先；事件回复已经生成但还没播，不应因主播新说话而丢掉，除非该具体事件已被主播明确处理或被策略撤销。

## 11. 测试集、直播 Mock 和验收

### 11.1 简单集和困难集

测试页面在 src/bilisama/ui/intent_test_runner.py 和 src/bilisama/ui/web/：

- 简单意图集：14 例，覆盖 7 类预期意图，每类 2 例；重点看“回给谁、是否开口、时机”。
- 困难多轮集：原 Excel 场景加新增场景，共 114 个执行步骤；包含主播讲解、回答弹幕、主播打字已答、礼物、VIP、观众互聊、等待和主动话题等。
- 每例开始会新建/重置模型会话，只加载当前例背景；同一例内语音和事件仍共享上下文。
- 每例卡片都有停止入口，停止时先停止预制台词和事件注入，再恢复正常链路。
- 预制主播语音用 Seed TTS 2.0 合成，16 kHz 单声道 PCM16；测试输入音频和豆腐回复音频是两条队列。

### 11.2 直播 Mock

1. 启动 dev-talk --director，打开测试页的“直播 Mock”。
2. Chrome 打开直播间，控制台选择共享标签页并勾选“共享标签页音频”。
3. 输入真实房间号，等待伴播后端、共享画面、共享音轨和真实直播流四项预检通过。
4. 点击开始，标签页音轨以 16 kHz/20 ms PCM 代替麦克风，真实直播事件仍走正式 ingest → Assembly → Scheduler 链路。
5. 停止共享后，输入恢复麦克风；控制台页面崩溃而不是正常结束共享时，后端可能持续收到静音，这条风险尚未完整实测。

### 11.3 门禁和真实效果

提交前：

~~~bash
scripts/gate.sh
~~~

最近一次门禁结果：

- Black、Ruff、全量 mypy：通过。
- 单元测试：2439 passed，4 skipped，136 deselected。
- 集成测试：20 passed，6 skipped。
- 浏览器测试：80 passed，2 failed，12 deselected；失败是音频设备模拟用例中 live tracks == 0，不是本次交互代码的语义回归。

测试结果必须分开记录：

- 程序证据：事件编号、状态变更、队列结论、播放回执、日志和健康卡。
- 模型效果：是否认对对象、是否该开口、是否在正确时机开口、是否重复。

没有真实模型或真直播间观测，不要把 Prompt 快照或单测通过写成“意图识别准确率”。

## 12. 启动和排查速查

### 12.1 默认 Qwen Realtime

~~~bash
source path.sh
./start_bilisama.sh --open
~~~

脚本默认是 dashscope / qwen-audio-3.0-realtime-flash。也可以显式写：

~~~bash
.venv/bin/bilisama dev-talk --director \
  --provider dashscope \
  --model qwen-audio-3.0-realtime-flash
~~~

### 12.2 火山豆包

~~~bash
source path.sh
./start_bilisama.sh --provider volcano --model 2.2.0.0 \
  --voice saturn_zh_female_keainvsheng_tob
~~~

config/bilisama.toml 的 provider 注释和启动脚本默认值有历史差异：TOML 保留火山配置，启动脚本默认 DashScope。排查时以命令行解析后的启动横幅为准。

### 12.3 真直播间

~~~bash
source path.sh
.venv/bin/bilisama dev-talk --director --room <房间号>
~~~

匿名连接会导致昵称和 UID 打码，主播弹幕关联、常客记忆和互聊判断都会受影响。

### 12.4 日志看什么

- voice_gate.passed/skipped/held/late_marker：语音门是否拦到 [SKIP]。
- scheduler.*：事件是排队、抢占、取消、重排还是播放完成。
- interaction.report_applied/report_invalid：后台状态报告是否被接受。
- proactive.topic_submitted（带 layer）、proactive.side_model_missing_fallback：主动话题候选与降级；proactive.candidate_duplicate / repeat_detected / no_material：出口查重丢弃、说出口后发现重复、本档位没素材。
- selector.*、event_pacing.*：弹幕被过滤、窗口落选或预算不足。
- 面板健康卡里的 recent_turns/recent_skipped 比累计计数更能反映当前语音门是否塌掉。

排查“为什么没回复”时按顺序检查：

~~~text
事件是否真的进入流水
→ 是否主播事件，或被 @ 其他观众的兜底拦下（assembly.viewer_chat_skipped）
→ 是否被窗口或预算淘汰
→ 是否已经被 InteractionState 标记 handled
→ 是否生成了 Intent
→ 是否被 SpeakingFloor/优先级挡住
→ 是否生成、播放，或被主播插话取消
~~~

## 13. 当前已知欠账和不要误判的地方

1. 生产模型仍是语音“接话 / [SKIP]”二分类，不会返回七类意图标签。
2. report_interaction 在 Volcano 上没有完整等价通道；依赖它的事件关联必须在 DashScope 上验。
3. Prompt 中的公共规则与 intents.py 逐类规则存在重复，当前靠测试和人工同步，没有自动一致性门禁。
4. BACKGROUND_RESULT 有优先级但暂无生产者。
5. capabilities.item_truncate 当前没有读者。
6. 延迟基线仍是测量设计，未形成稳定的 P50/P95；不要把 0.6/1.8/1.9 秒 quiet window 当作完整端到端延迟。
7. 浏览器音频设备接管的两个模拟用例仍失败；这不等于直播 Mock 或语音门逻辑已经失败。
8. 真实模型对弹幕“谁问了什么、是否被主播回答、观众是否互聊”的判断必须用同一时间窗交错 A/B 和多轮脚本验收，不能只看一条成片回复。
9. 生成完成不等于已经播完；排队、播放回执和主播插话要分别记录。
10. 测试台中的预制语音是输入素材，不是豆腐已经说过的话；不能把台词播放记进助手输出。

## 14. 继续开发的建议顺序

1. 先在 DashScope Qwen Realtime 上用困难集复现具体 badcase，并保存事件编号、报告、队列和播放日志。
2. 只改一个 Prompt 层或一个状态转移，先补复现测试，再跑目标单测。
3. 涉及语音/事件联动时，同时验证主播语音优先、事件是否保留、是否重排、是否重复。
4. 如果改了公共 Prompt、事件规则或 function schema，重新导出 docs/current-interaction-prompts.md，并同步 docs/runbook.md 和验收记录。
5. 跑 scripts/gate.sh，把浏览器层已知失败与新失败分开。
6. 提交时只加入明确范围的源码、配置、测试和文档；不要把 outputs/、path.sh、密钥、音频缓存和原始 Excel 一起提交。

## 15. 相关文档索引

- [操作手册](runbook.md)：启动、provider、桌宠、直播 Mock、真直播间和常见排错。
- [架构现状](architecture-status.md)：逐模块说明实际实现、边界和欠账。
- [当前互动 Prompt 快照](current-interaction-prompts.md)：公共人设、语音规则、事件规则、主动话题和报告 schema。
- [Planner 合并说明](planner-integration.md)：语音门、二分类输出和与本地测试集的合并关系。
- [语音/事件联动计划](voice-event-linkage-plan.md)：需求拆解和设计验收矩阵。
- [语音/事件联动验收](voice-event-linkage-acceptance.md)：程序回归与真实模型验证记录。
- [意图测试页说明](intent-test-console.md)：简单集、困难集、预制语音和逐例会话隔离。
- [互动场景设计](intent-scenarios.md)：主播状态 × 助手参与方式的原始场景矩阵。
- [MVP 验证方案](mvp-validation-plan.md)：四道门、业务评分、历史回归记录。
- [延迟基线设计](latency-baseline.md)：尚未完成的端到端延迟测量方法。

