# BiliSama 架构现状梳理（2026-09-03）

这份文档回答「今天代码长什么样」。它和实施计划分工：计划（`~/.claude/plans/` 下那份，路径见
CLAUDE.md 的会话上下文）管为什么这么设计、每一轮拍了什么板；这里管每个模块今天实际做到哪、
接缝在哪、哪些字段和方法有名无实。结论一律带 `file:line`，没验过的写「待验证」。

更新纪律：动了模块边界、加了 provider、把阶段 4 的某个空包填上，或者发现这里跟代码对不上，
当轮就改这份。行号漂了不必逐条追，但「有没有读者」「有没有生产者」这类事实必须准。

## 1. 整体形状

### 1.1 四个进程

| 进程 | 今天是什么 | 出处 |
|---|---|---|
| P1 桌面壳 | Electron 附着壳，只开窗口不拉起后端，自陈是阶段 7 要整个换掉的临时件；页面本体住在 P2 | `desktop/preview/main.mjs`；`src/bilisama/ui/server.py:493` 挂静态目录 |
| P2 bilisama | Python 3.12 单进程 asyncio，产品全在这里 | `src/bilisama/` |
| P3 托管语音 | DashScope、OpenAI GA、火山豆包三家 | `realtime/providers/hosted.py`、`volcano.py` |
| P3' 本地 s2s | speech-to-speech 服务，独立 venv，手工用 `scripts/smoke_provider_b.sh serve` 起；补丁靠 `PYTHONPATH` 注入，不改上游一个字节 | `bootstrap/s2s_launch.py`、`tools/s2s_shim/bilisama_s2s_shim/patches.py` |

拆进程的直接原因只有一个：s2s 把 torch / transformers / mlx 的版本焊死了，跟本体依赖合不来
（`docs/runbook.md:15-16`）。

### 1.2 四个功能域，不是四层栈

| 域 | 包 | 状态 |
|---|---|---|
| L1 界面 | `ui/`（FastAPI 服务 + `ui/web/` 页面 + 壳） | 六页面板、桌宠、音频进壳、直播 Mock、测试台都在；Live2D 没有 |
| L2 语音链路 | `realtime/` | 完成：`SpeechLink` 接口、共享客户端、四个 provider |
| L3 编排 | `director/`、`persona/`、`memory/`、`proactive.py`、`app.py` | 完成；`tts/`、`avatar/`、`tools/` 三个包的 `__init__.py` 是 0 字节 |
| L4 直播接入 | `ingest/` | 完成，真房间验过（计划 §15.1） |

调用方向是 L4 → L3 → L2，L1 跟 L3 双向。编号大不等于更底层：L4 是数据来源，L2 是出口。

### 1.3 依赖门禁

「L3 看不见协议」写成了测试：`director/`、`persona/`、`memory/`、`tools/`、`ui/` 五个包禁止
import `realtime.providers.*`，也禁止出现 `response.`、`conversation.item`、`input_audio_buffer`
三个字面量（`tests/unit/test_dependency_direction.py:40-49`）。它用 AST 而不是 grep，所以注释和
docstring 里提协议不算违规。

### 1.4 阶段状态对照

| 阶段 | 计划状态 | 代码实况 |
|---|---|---|
| 0 地基、1 L2、2 L3 骨架、3 人设记忆、6 弹幕 | 关闭 | 属实 |
| 4 TTS 与形象 | 后放 | `tts/`、`avatar/`、`tools/` 三个包空 |
| 5 L1 | 进行中，只剩 Live2D | `ui/web/js/renderer.js:17` 的 `RENDERABLE` 只有 sprite；配 live2d 会显示一句提示并退回内置形象 |
| 7 打包 | 未开始 | 没有 CI、没有 electron-builder，随包 config 装机后找不到（计划 §16.8 #43 / #55 / #63） |
| 8 联调 | 工具齐、验收没跑 | 测试台 63 张卡和直播 Mock 已合入（计划 §15.22）；G1 到 G3 一场没做 |

### 1.5 唯一入口：dev-talk --director

启动链只有一条：双击 `start_bilisama.command` → `start_bilisama.sh:234` exec `bilisama dev-talk --director`
→ `cli.py:453` 特判绕过 argparse → `dev_talk.py:3663` `asyncio.run(run_director)`。

`run_director`（`dev_talk.py:1549-3446`）就是生产装配层：造二十来个对象、起 14 个任务
（`director:mic / scheduler / proactive / assembly / play / controls / default-output / stdin /
loop-lag / playback / credential / ui-feed / ui-state / ui-level`，`dev_talk.py:3195-3331`）、一个
FastAPI 服务、一个 Electron 子进程，然后 `await stop.wait()`；收尾链在 3334-3446 行分七步各自 try。
`app.Assembly` 在 `src/` 里只有 `dev_talk.py:2068` 一个调用点。

CLI 只有三组子命令：`config {validate, show, diff, chattiness, render-s2s}`、`persona {list, review}`、
`dev-talk`（`cli.py:405-446`）。计划里的 `doctor`、`run`、`serve` 都不存在。

健康探针注册了十五个：assembly、proactive、distill、scheduler、link、outcomes、loop、selector、
pacing、audio_input、entries、bilibili、live_mock、audio_output、echo（`dev_talk.py:2288-2499`），
挂在 `GET /{token}/health`（`obs/health.py:115`，`ui/server.py:496`）。

## 2. L2 realtime：语音链路

### 2.1 SpeechLink 与事件词表

`SpeechLink`（`realtime/link.py:189-245`）是 L3 能看到的全部：connect / aclose / suspend / resume /
set_context / push_audio / add_context_item / request_reply / cancel / end_protection / events，
外加一个 `quiet_window_s` 属性——主播停口后闸门该关多久，由后端自己报数，L3 不算。

事件词表 `LinkEvent`（`link.py:82-175`）：SpeechStarted / SpeechStopped、ReplyStarted / TextDelta /
AudioDelta / Done(status, text)、ToolCall、UserTranscriptDelta / Done、LinkDown(retrying) /
LinkUp(attempts)、LinkError。`ReplyDone.status` 里的 `timed_out` 是我们的看门狗判的，不是 provider 说的。

`ReplySpec`（`link.py:45-61`）只描述意图：instructions、base_instructions（只替换这一条回复的会话级
上下文，不动会话本身）、max_tokens、write_history、protected、protect_ms。

### 2.2 Capabilities

`realtime/capabilities.py:21-51` 只装会让客户端或调度器长分支的位，七个：owns_tts、
single_response_slot、out_of_band_exempt_from_slot、item_truncate、acknowledges_session_update、
turn_detection_types、per_reply_base_instructions。四家的值在 54-147 行。实测结论：四家全是单槽，
只有 OpenAI GA 的旁路回复（协议里 `conversation="none"` 那种不写历史的回复）不占名额。

`for_model()`（155-169）按模型再收窄判停类型，因为 DashScope 同账号下 qwen-audio-3.0 不认
semantic_vad。`item_truncate` 这一位今天没有任何读者。

### 2.3 RealtimeClient

`realtime/client.py` 是三家 OpenAI 方言共用的传输层，里面没有一处 `if provider`。它扛的是计划
§3.3 那几条规则：

| 规则 | 落点 |
|---|---|
| 指令串行、发前等槽 | `_command_lock` + `_wait_for_slot`，:238-256、:306-321 |
| 音频直通不排队（静音也发） | `push_audio`，:228-236 |
| 槽位不靠配对 created/done：发出即忙、任意 done 即闲 | `_take_slot` / `_free_slot`，:323-342；`_on_done`，:657-673 |
| 25 秒看门狗 | `_WATCHDOG_S`，:52；`_watchdog`，:344-367 |
| 迟到帧作废：stale 句柄加 64 条墓碑环 | :139-140；`_bury`，:649-655 |
| 六次有界退避重连，适配器回调重放会话 | `_on_disconnect`，:395-428；`_reconnect_loop`，:453-503 |
| 主动换会话 | `rotate`，:430-451 |

`_dispatch`（:505-578）把方言归一化后的事件翻成 LinkEvent。

### 2.4 方言、错误分类、重采样

`dialect.py` 是 BETA（DashScope 用，`modalities` / `response.text.delta`）和 GA（s2s、OpenAI 用，
`output_modalities` / `response.output_text.delta`，session.update 必带 `type=realtime`）两套方言的
编解码，表在 62-108 行，出帧方法 session_patch / response_create / tool_spec 在 164-217 行。
`errors.py:81-110` 把传输错误分成 RETRYABLE / BACKOFF / FATAL 三类，致命错闩住不重试。
`resample.py` 只给 openai_ga 做 16k 到 24k 的上行插值。

### 2.5 注册表与工厂

`providers/__init__.py` 是注册表，也是「加 provider 改哪儿」的答案。`ProviderProfile`
（:252-330）一条记录装下 caps、codec、socket_path、default_model、session_cap_min、uplink_rate、
default_host、key_env；四条记录在 333-367 行。

- `resolve_endpoint`（:75-141）定连哪儿，顺序是命令行 > 配置 > 环境变量 > 内置默认。环境变量只有
  DashScope 认 `dashscope_url`；内置地址只有火山有。
- `quiet_window_s`（:182-217）按 provider 算静默窗：s2s 取 smart_turn_max_wait_ms 加
  smart_turn_incomplete_delay_ms，托管两家取 silence_duration_ms，火山取 end_smooth_window_ms，都再加
  0.3 秒。出厂值算出来是 s2s 1.9 秒、托管 0.6 秒、火山 1.8 秒。
- `compose_instructions`（:220-249）把「人设 + 本轮要求」拼成一条，因为协议里逐轮 instructions 会
  替换掉会话级人设；2026-08-14 真机探出所有注入回复裸奔的根因就是它。
- `factory.py` 的 `build_link`（:167-266）是全仓唯一一处按 provider 分支的 match，`LinkRequest`
  装所有适配器可能要的输入。

### 2.6 三个适配器

| 适配器 | 文件 | 关键行为 |
|---|---|---|
| HostedLink（DashScope 与 OpenAI GA 共用） | `providers/hosted.py` | 连上先发引导帧 modalities / turn_detection / voice / pcm16（:173-206）；会话到期前 3 分钟主动换 socket（:128-138）；改音色或判停参数走 `reconfigure_session` 换 socket（:235-255）；protected 只打一条警告，DashScope 实测不认 interrupt_response（:292-323，计划 #45 探明不可行）；end_protection 空实现（:328-333） |
| S2SLink | `providers/s2s.py` | 文件头 1-25 行逐条写了 §3.3 八条规则各住在哪；注入恒走 out-of-band（:182-233）；protected 时先发 interrupt_response=false 并记账，重连后补回（:105-133、:235-245） |
| VolcanoLink | `providers/volcano.py` + `volcano_wire.py` | 二进制帧、数字事件号，不共用 RealtimeClient，自己实现槽位、看门狗、按 question_id 的墓碑和六次重连（:988-1106、:1108-1167）；没有 response.create，`add_context_item` 只暂存，`request_reply` 把事件块和本轮要求合成一条 ChatTextQuery（:867-939）；人设走 StartSession，O2.0 事后用 UpdateConfig 改，SC2.0 改不了只能换会话（:679-782）；改名、换音色也走换会话（:551-605）；`dialog_id` 让重连接得上前 20 轮；ASR_INFO 翻成 SpeechStarted，TTS_ENDED 才算回复完成（:1208-1341） |

火山没有逐轮指令通道，所以多了一位 `per_reply_base_instructions=False`，装配层在它身上把事件规则
坍缩进会话上下文（`app.py:392-398`）。

## 3. L3 director：调度与说话权

### 3.1 Intent 与优先级

`Intent`（`director/intent.py:62-74`）是七个来源汇成的一种形状，带 trusted、dedup_key、expires_at、
requeue_on_interrupt 四个产品规则字段；`Injection` 是「写进历史的 item_text + 描述回复的 ReplySpec」
两半。优先级阶梯（:24-45）：

| 值 | 来源 | 备注 |
|---|---|---|
| 100 STREAMER | 主播语音 | 永远不是 Intent，只做比较上限 |
| 80 SUPERCHAT | SC | 被打断后重排队 |
| 70 BIG_GIFT | 高档礼物 | 同上 |
| 65 GUARD_BUY | 上舰 | 同上 |
| 50 VIP_ENTER | 舰队或本房 5 级粉丝牌进房、中档礼物 | |
| 40 BACKGROUND_RESULT | 后台任务结果 | 有定义无生产者 |
| 30 DANMAKU | 弹幕、小额礼物、进房欢迎 | 20 秒过期 |
| 10 PROACTIVE | 主动话题、戳一戳 | 30 秒过期 |

### 3.2 SpeakingFloor

`director/floor.py` 回答「现在能不能开口」。`blocking_reason()`（:139-163）按顺序查：主播在说 →
有轮次在飞（我们派的 turn_pending 或 provider 自己起的 implicit_active）→ 音频没播完 → 投机静默窗
或取消后承诺的说话边沿 → 话痨度冷却。返回的是挡人的原因而不是布尔，面板上才写得出「因为主播在说」。

静默窗由 `on_speech_stopped(quiet_s=…)` 起计时器（:60-73），闸门不认识任何 provider 字段名。
`on_link_lost`（:94-117）断线时强制放开所有状态位，否则重连后闸门永远关着。

### 3.3 Scheduler

`director/scheduler.py`（1156 行，文件头 1-40 行是承诺清单）是承重墙：一个按优先级和到达序排的
heapq、一个 `_active`、dedup 键从 submit 活到 settle、一个 `controls` 队列往 L1 发 PlaybackClear。

- 派发两步：先写事件块再要回复（`_dispatch`，:562-675）；派发失败时付费意图在 6 秒窗口内退避重试，
  其它直接 FAILED 判决。
- 抢占只在回复出第一个字之前发生（`_maybe_preempt`，:872-906）。一旦 `output_started`，高优先级也
  只能排队，只有主播插话和 panic 能掐正在播的句子。
- 主播插话（`_barge_in`，:758-798）：发 PlaybackClear、cancel，保护段内不取消；付费意图被打断后
  重排队（:958-1022）。s2s 的 done(cancelled) 先于 speech_started 到，所以 `_on_done` 里用
  `expect_speech_edge` 压住 0.3 秒，防止重排队的意图在两帧之间被再派再杀（:703-756，计划 #29）。
- 保护段生命周期：dispatch 时起 protect_ms 硬上限任务，settle 和硬上限两处都会 `_end_protection`
  且只执行一次（:827-870）。`protected` 的唯一生产者是 `intent_for`，由 `[interaction] protect_paid_replies` 开关决定（默认关）。
- 出口守卫：`_on_delta` 命中敏感词就 PlaybackClear + cancel + FAILED@SPEAKING，`on_hit=mute_all`
  时升级成 panic（:682-701）。
- 回复完成：文本喂给 spoken_sink 当口癖素材、按 write_history 把 assistant 回复写回历史（旁路回复
  不写回，所以要自己镜像）、起话痨度冷却（:703-718）。
- 每条 Intent 恰好一个 Verdict（`_emit`，:349-376），outcome 乘 phase；过期时记下当时挡它的闸门名
  （:491-531）；播放回执到了才写 spoken@played，否则 spoken@generating（:1041-1071）。
- `panic_mute`（:307-333）是唯一能杀保护段回复的东西；LinkDown 时清空队列并拒收新意图。
- 她自起的回复（provider 自己判停生成的那条，`ReplyHandle.implicit`）也记账（2026-09-09）：`_Implicit`（:144）
  从 ReplyStarted 活到 ReplyDone；杀它只有一条路 `skip_implicit`（:391），语音门、出口守卫、panic 三个调用方
  共用，幂等，一条取消一条判决（source「voice」，intent_id「voice:<句柄>」）。说完的文字进 `implicit_spoken_sink`，
  dev_talk 接的是和派发回复同一个 `spoken_line`。门在扇出的 pump 里同步判，可能比调度器自己的任务先看到
  这条回复，所以 `skip_implicit` 遇到还没登记的 implicit 句柄会先记成已杀，后到的 ReplyStarted 不覆盖。

### 3.4 intents.py：敌意输入边界

所有观众事件包进 `<bilisama_live_events>` 加一行免责声明（:100-103），标签本身被 `neutralize_tags`
打断（:69-76）；每种事件一行固定前缀（:125-150），金额、电池数、人数从不出现在行里——模型没看见就
说不出来。`_instruction_for`（:153-226）给每类事件写了逐轮说话规则：弹幕先复述问题、SC 先谢再答、
礼物按电池三档、上舰按舰长 / 提督 / 总督分仪式感、进房对照最近三次不重样。

`intent_for`（:229-313）做映射：礼物按 `total_battery` 分档，高档保 BIG_GIFT、中档降到 VIP_ENTER、
小额当弹幕；付费事件 `requeue_on_interrupt=True`；`protected` 由 `[interaction] protect_paid_replies` 决定（默认关，开了只保护 SC 和高额礼物，2026-09-03 加）——关着时主播开口是硬上限，付费保护的含义是「回来再说」；弹幕 TTL 20 秒从到达时刻起算。主播自己的弹幕走
`anchor_danmaku_context_item`（:106-122）只写上下文不回复。

这里的逐类规则和 `config/personas/live/event_responses.md` 有意重叠，代码注释明说要靠人手保持同步，
没有测试守着（计划 #94）。

### 3.5 output_guard

`director/output_guard.py`：跨分片子串匹配加白名单缓判（:56-116），`load_guard` 从 `[safety]` 装词表
（:136-168）。已知缺口写在文件头 14-17 行：回复结束时白名单判决还悬着的命中会放过去。出厂词表是
占位：`config/safety/wordlist.txt` 只有一条「这是一条测试敏感词」，白名单是空的（计划 #92）。

### 3.6 语音门：她自起的回复接不接（2026-09-09）

麦克风收的是全场声音，语音后端每次判停都会生成一条回复，四家都关不掉。做法是让模型报场景、程序决定接不接：

- `scene_markers.py`（118 行，无依赖，persona 和 director 都 import 它）：六个英文记号 `[AUDIENCE]`
  `[SELF_TALK]` `[READING]` `[GUEST]` `[UNSURE]` `[DECLINED]` 和中文标签；对她说的不写记号。英文是因为 s2s
  官方管线的剥字符规则只放行半角 `[]` 和 `\w`。`[DECLINED]` 留给阶段二的探针，麦克风合同不教。
- `director/turn_protocol.py`（196 行，纯函数）：`MarkerHead` 逐字解码回复开头——`[` 后面是某个记号的前缀就
  接着攒，不是就放行，闭合 `]` 才定分类，攒满 12 字当没有记号；括号内大小写和下划线随意，裸写要拼得一字不差；
  `TurnPolicy` 是唯一的策略表，默认只有 TO_ME 说，DECLINED 永远不说。
- `director/voice_turn.py`（426 行）：`VoiceTurnGate.feed` 在扇出的 pump 里同步跑，implicit 回复的帧攒到开头
  判定为止，放行按原序，不接就丢帧并回调 `Skip`；文字晚于声音超过 600 毫秒先放行，之后才见记号就切断并冲
  扬声器（`late_markers`）；攒够 200 帧放行；被攒住就拦下的回复连结束事件也不给播放侧。
- 接线（`dev_talk.py`）：`_Fanout` 分两族 view，`events()` 给调度器（不延迟，它靠 ReplyStarted 关地板），
  `gated_events()` 给本机播放和网页；`on_voice_skip`（:2134）把 `Skip` 变成 `scheduler.skip_implicit`，
  备注写进冷场话题的对话列表（「[主播念弹幕] 主播在念弹幕」）——出货的 s2s 产品路径上这是主播语音唯一的
  文字痕迹。健康卡多一张 `voice_gate`。
- 开关 `[interaction] voice_reply`（`config/schema.py:319`，出厂 `when_addressed`，`profiles/chat.toml` 写死
  `always`）同时管门和提示词：`live_voice_rules(addressing=True)`（`persona/loader.py:124`）把
  `voice_addressing.md` 拼在 `voice_responses.md` 后面，记号列表从 `scene_markers` 渲染；面板热改时先关门
  再教记号、先撤记号再开门（`dev_talk.py:2262`）。
- 假服务器上整条链路测过（`tests/unit/test_voice_gate_wiring.py`）：s2s 与 DashScope 两种帧形、超时放行、
  迟到切断、攒帧期间意图等待。真实服务探测（`tests/integration/test_voice_gate_probes.py`，2026-09-09）：三家都按合同写了记号；
  DashScope 文字领先音频 210 毫秒，火山文字音频同刻到，s2s 官方管线一个 `transcript.done` 先于全部音频，都在 600 毫秒
  暂存内；s2s 上首段处取消 1 毫秒内 `done(cancelled)`、零音频帧、只砍 TTS 不回滚历史；记号回复留在三家的历史里（#97）。
  s2s 产品配置（`stt: none`）没跑，判对率没量（计划 §15.28、#99）。

## 4. 人设：两层锚、两层生长、一份主动话题提示词

`PersonaStore`（`persona/loader.py:252`）读锚文件走「用户数据目录活副本 → 随包模板」两档回退
（:285-305）；机器没有写锚的路径，唯一例外是 `promote()`（:456-484），只从 `bilisama persona review`
由人触发。生长层 relationship.md、voice.md 只住数据目录，写入走文件锁（POSIX flock / Windows msvcrt，
:129-178，Windows 分支未实机验证）加原子替换（:180-203）；`growth_update`（:356-374）保证读改写在
同一把锁里，防止下播蒸馏和 `persona review` 互相复活对方删掉的行。

主动话题提示词三档回退：用户副本 → 人设随包 → `config/prompts/proactive.md`（:403-434）。
`pinned_text`（:436-454）读置顶记忆并把换行折成「；」防伪造段标题。`template_variables`（:82-104）
解析 {{userName}} / {{agentName}} / {{replyLength}}。直播输入规则
`config/personas/live/{event,voice}_responses.md` 缺了直接报错（:106-125）。

`persona/prompt.py` 定拼装顺序：`static_prefix` = identity → personality → LIVE_RULES → tool 块
（今天恒空，:70-76）；`dynamic_tail` 按变化频率排：口癖样本 → 共同经历 → 置顶 → 主播事实 → 直播简介
→ 本场进展 → 在场常客 → 时间（:82-111）。LIVE_RULES（:31-51）写死了说话人身份锁、三条记忆纪律、
不写舞台指示、不用 Markdown。`growth.py` 定预算：共同经历 30 条 / 800 字，口癖 12 行 / 400 字，
每场至多换入 2 句。

随包人设五个：tofu（出厂默认）、hanako、ming、butter（openhanako 移植，各带 proactive.md）、mia
（2026-08-31 从 yiko 元气版移植，无 proactive.md）。人设与名字是两层：切人设不动
`persona.display_name`。

## 5. 记忆与侧路模型

`memory/schema.sql` 四张表：stream、event、viewer、fact，fact 的 scope 取 streamer / viewer / stream。

`MemoryStore`（`memory/store.py`）Tier 0 同步写：`on_event`（:234-288）每个事件 INSERT event 加
UPSERT viewer，streams_seen 只在 last_stream_id 变化时加一；`write_batch_ms > 0` 时攒批写，所有读先
冲刷。读侧：`present_regulars` 取本场在场且来过两场以上的人（:297-310）、`top_viewers`（:312-325）、
`recent_events`（:340-359）、`facts` / `replace_facts` 删后插（:371-400）。逻辑日在中国时间 04:00
翻转（:82-88）。

`memory/context.py` 把行变成动态尾段：时钟行按 `clock_granularity_min` 取整写成「开播约 1 小时 45 分，
现在 23:10 左右，本周第 3 场」（:43-66）；`regulars_line`（:69-86）读在场常客加各自的 viewer 事实，
只查在场的人，这就是计划 §4.7 要的作用域隔离。

`Distiller`（`memory/distill.py`）两次侧路调用：每 N 个事件滚动改写 200 字本场进展，指纹没变就不发
（:187-243）；下播一次批量产观众事实、终稿摘要、共同经历、口癖，一次闩、重试一次（:245-330），
生长层写入前过出口守卫、走 merge 预算（:389-428）。蒸馏不再向模型索要标签（占预算，:346-350）。

三处读写不对称：`fact.tags` 只写不读；`streamer` scope 有读者（`context.py:89-90`）无写入方；
打断后既不截我们的记忆也不发 `item.truncate`，`played_ms` 在 Python 侧零读者（计划 #35、#73）。

侧路模型（跑在对话主链路旁边的便宜 chat-completions 调用）是 `side.py` 的 `OpenAICompatSideModel`：
一次非流式请求，tool_choice 钉 none，45 秒超时，http_status / timeout / transport / bad_shape 四类
失败分开记日志，永不把语音主循环带下去。

## 6. 主动性：ProactiveTopicLoop

`proactive.py` 后台每 `wake_interval_s` 用侧路模型读近 20 条事件、本场进展、最近 12 行对话产一个候选，
指纹跳过（:340-381）。前台每秒一 tick（:187-224）：闸门未阻塞、冷场超过阈值（接了节奏器时读房间
活跃度派生的 30 / 60 / 120 秒，热闹时关）、每小时预算未满、没有弹幕窗口或欢迎在等、从节奏器拿到
令牌，才 `_speak`。

`_speak`（:226-326）提交 PROACTIVE 意图：trusted=True、写一条「[本场] 这会儿没人说话」的 item
（DashScope 空会话拒绝 response.create，计划 #56 的修法）、指令模板带「连续无人回应次数」、TTL 30 秒、
write_history=True。没配侧路模型时不闭嘴，改让实时模型自己从共享上下文挑话题。

戳桌宠（`ui/poke.py`）也走 PROACTIVE 意图，但绕过所有 speak 开关（计划 #48，等产品拍板）。

## 7. 装配层：Assembly 与 dev_talk

### 7.1 Assembly.on_event 的顺序

`app.py:179-253` 的顺序就是分级机制本身：记忆恒记 → 蒸馏计数 → 主动话题活跃度（只有观众动作算陪伴）
→ 节奏器 → 面板事件流（在所有闸门之前）→ 主播自己的弹幕转上下文 → 暂停闸 → ENTRY 提升 VIP_ENTER
→ speak 开关 → 进房走 EntryCoalescer → VIP 一场只点名一次 → 弹幕和礼物进选择器漏斗 →
`_submit_event`（:310-337）。选择器赢家从 `deliver_selected`（:255-273）回流，普通预算在这里才扣。
控制台和测试台喂的事件 `room_id` 为 0，绕过漏斗直接说话——键盘不是人群。

### 7.2 上下文三种形态与推送

- `build_public_context`（:355）：人设前缀加记忆尾段，谁都读这份。
- `build_context`（:383）：再加主播语音规则，推给 session，管 provider 自己起的麦克风轮次。
- `build_event_context`（:392）：再加事件规则，作为逐轮 `base_instructions` 随每条事件回复走；火山
  没有逐轮通道，返回空串。

`refresh_context`（:400-429）每 10 秒重建、变了才推，保住 provider 的前缀缓存；时钟粒度 5 分钟决定
空闲时的推送节奏。`run`（:431-470）用 SupervisedSource 包每个源；`replace_persona`（:485-507）支持
直播中切人设。

### 7.3 dev_talk 里其它值得知道的

- 上行音频只有一条路，全经 `AudioInputSwitch`（`ui/audio.py:594-729`）：本机麦克风、页面 socket、
  直播 Mock 的标签页音轨三个来源进同一组闸——暂停门 → 噪声门（计量在前）→ 输入开关（替换成静音）
  → 送链路。被挡下时发等长静音，不是丢帧，provider 的音频时钟才不会冻住。
- 面板改配置走 `apply_runtime_edit`（`dev_talk.py:2217-2252`）：校验 → 内存生效 → 跑热更新钩子
  （`run_reload_hook`，:2154-2215）→ 落盘 → 任一失败回滚并重跑钩子；没有钩子的非 LIVE 字段直接拒。
  音频双开关、房间号、主播昵称四项是会话级、不落盘（:121-128）。
- 暂停序列（`on_panel_set`，:2645-2684）：关事件闸（记忆照记）→ 关麦 → panic → `speech.suspend()`，
  恢复严格反序，resume 失败就保持暂停并广播错误。
- 收尾链（:3334-3446）七步各自 try：先锁住本机设备不再复活，再 cancel 全部任务，关 hub 再关 uvicorn
  （顺序反了会等浏览器 socket 到天荒地老），只在 pid 是自己时删 `endpoint.json`，最后下播蒸馏、
  关库、关链路、关扬声器。
- 裸链路档（`run`，:3450-3531）只连语音链路不立 L3，dashscope 和 s2s 能驱动，从注册表判断而不是
  硬编码名单（:3631-3658）。
- `_Fanout`（:1264 起）把链路的一条事件流复制成两族 view：`events()` 原样、`gated_events()` 经语音门
  （3.6）。调度器读前者，两个播放消费者读后者；门抛异常时那一帧原样放行并记 `dev_talk.voice_gate_failed`。

## 8. L4 ingest：直播互动漏斗

### 8.1 端到端

B 站 WebSocket → vendored blivedm → `_Forwarder` 预算闸门 → 纯函数映射成 `LiveEvent` → 源内去重和
付费侧袋 → `Assembly.on_event` → 弹幕礼物进 `DanmakuSelector`、普通进房进 `EntryCoalescer`、SC /
上舰 / VIP 直通 → `intent_for` → `scheduler.submit`。SC 撤回反向走 `scheduler.revoke`
（`dev_talk.py:2269`，键由 `events.sc_dedup_key` 统一生成）。

### 8.2 事件模型

`ingest/events.py`：`EventKind` 十种（:26-39）；`Viewer.identity` 永不为空，uid 被打码时回退 uid_hash
（:117-124）；`LiveEvent.dedup_key` 有平台 id 就用，否则身份加正文加秒桶，礼物再拼 gift_id / num /
金额，防盲盒整批塌成一键（:199-220）；`raw` 永不进模型，喂之前必过 `redacted()`。
`Gift.is_paid` 和 `LiveEvent.is_paid` 不是一回事，路由付费车道的是后者。字段上的「NO CONSUMER YET」标记只剩 `face_url`、`session_generation`、`Gift.is_paid` 三处，都仍准确；四处过时的
标记 2026-09-03 已改成写明读者（#93）。

### 8.3 源与监管

`ingest/bilibili/source.py`：只用 SESSDATA 走 cookie jar，凭据过期时明确报「这一场连的其实是匿名」并
挂 `credential_stale`（:604-656）；解析预算弹幕 80 条/秒、进房 40 条/秒，付费永不丢（:77-93、
:700-711）；连麦镜像弹幕丢弃（:417-419）；`init_room()` 失败直接抛，不降级成短号（:578-582）；
`offer()`（:669-698）主播本人弹幕打 `is_anchor` 标、上舰 30 秒合并、付费进无界侧袋、普通进有界
队列，取出时先掏侧袋。

`ingest/sources.py` 的 `Source` 是 Protocol：name / start(emit) / stop（:31-47）。实现有真源、
`QueueSource`（控制台和测试台）、`SwitchableRoomSource`（换房）、`SupervisedSource`（退避重启，跑满
60 秒重置预算，用完预算干净放弃不连坐，:82-147）；回放源住在 `tests/fakes/replay.py`。切换靠装配时
注册哪几个，没有配置开关。

### 8.4 打分

`ingest/bilibili/scoring.py` 两阶段。`danmaku_text_signal`（:161-189）先判三态：空、纯数字、「666」
这类低信息、重复率过高直接拒；疑问、点名、纠错、故障、请求、与直播简介相关的硬通过；其余打分。
`danmaku_score`（:217-261）权重：文本实质 0.4、疑问 0.15、舰队 0.3 到 0.4、房管 0.15、本房粉丝牌最高
0.14、用户等级最高 0.1，没有减分项。`is_near_duplicate`（:192-214）做跨观众复读过滤。

### 8.5 选择器与延迟池

`ingest/bilibili/selector.py`：窗口只留当前最优一条，硬通过的分数抬到门槛线（:181-185）；主播说话时
赢家进容量 5 的延迟池，解阻只放最优一条，其余记 `lost_deferred`（:272-301）。hold 的是候选不是
Intent，所以不烧 TTL、不扣令牌。`EntryCoalescer`（:386-467）取代了攒 5 人批量欢迎：一人进房安静时
约 1 秒后点名，发了弹幕就撤销欢迎，BUSY 时不欢迎。旧的 `PresenceWelcomer` 还在文件里，生产不接线。

### 8.6 节奏器 event_pacing

`event_pacing.py` 用 60 秒滑窗把房间分成四档，升档立即、降档要熬 30 秒（:223-271）：

| 档 | 弹幕窗 | 令牌补给 | 进房合并等待 | 冷场阈值 | 门槛修正 | 欢迎与主动话题 |
|---|---|---|---|---|---|---|
| QUIET | 1 秒 | 8 秒 | 1 秒 | 30 秒 | −0.05 | 开 |
| SPARSE | 2 秒 | 12 秒 | 2 秒 | 60 秒 | 0 | 开 |
| ACTIVE | 4 秒 | 20 秒 | 4 秒 | 120 秒 | +0.05 | 开 |
| BUSY | 8 秒 | 60 秒 | 8 秒 | 300 秒 | +0.10 | 关 |

chattiness 只是乘在上面的系数 1.35 / 1.0 / 0.7，另定普通车道令牌桶容量 1 / 2 / 3；付费和 VIP 不排队
等令牌（:166-181）。这整张表刻意不做配置键。`derive.py:43-65` 那张按话痨度派生的基表仍在，运行期被
节奏器覆盖（`derive.py:81-102`）。

### 8.7 安全三件套与词表

`ingest/bilibili/safety.py` 只有三件，没有词表：去重环 0.35 秒 / 4096 条、熔断 60 秒 3 次（只数我们
自己抓到的失败）、礼物连击 1 秒静默结算加 600 秒压制（:58-63）。词表和白名单在输出侧的
`director/output_guard.py`。

### 8.8 换房与停车

`ui/room_control.py` 的 `SwitchableRoomSource`：进程内换房，源挂了就停在失败的房号上等面板给新选择，
不无限重试（:70-83）。直播 Mock 期间真房间会被暂挂，Mock 结束再恢复（`dev_talk.py:312-348`）。

## 9. 配置

### 9.1 加载顺序

`config/loader.py`：随包 `bilisama.toml` < `config/profiles/<p>.toml` < 数据目录里的用户层
`profiles/<p>.toml`（2026-08-31 新加）< 命令行和面板 overrides，深合并（:173）→ `migrate` 一版一步
升到 v4（:177）→ 无条件清退休键（:185）→ `Settings.model_validate`（:187）→ `check()` 有致命项就拒启
（:190）。`config show` 用 `origins()`（:114-129）标每个值来自哪一层。

### 9.2 字段总账

`config/schema.py` 共 22 个 section、114 个叶子字段：

| section | 字段 | 备注 |
|---|---|---|
| 根 | config_version=4, active_profile | |
| room | room_id, platform, credential_ref, stream_intro | |
| speech | provider（默认 s2s）+ s2s / dashscope / openai_ga / volcano / side 五节 | s2s.turn 17 个判停字段逐字对齐上游；hosted 是 endpoint / model / api_key_ref / voice / session_cap_min / turn{type, threshold, silence_duration_ms}；volcano 是 endpoint / api_key_ref / app_id_ref / access_key_ref / model / speaker / end_smooth_window_ms；side 是 base_url / model / api_key_ref，thinking 和 tool_choice 钉死关 |
| custom_tts | engine, voice, speed, api_key_ref | 整节无运行期读者，阶段 4 |
| audio | input_device, output_device, output_route, echo_guard, input_enabled, output_enabled, noise_sensitivity | 前四个无读者，`tests/unit/test_ui_meta.py:277` 守着这个事实 |
| safety | wordlist_path, allowlist_path, on_hit | |
| interaction | chattiness, reply_length, speak 十一开关, protect_paid_replies, sc_protect_ms, gift_battery_high / medium, burst_* 三个, entry_welcome 三分闸, proactive{max_per_hour, wake_interval_s} | burst_* 已无实际语义；sc_protect_ms 只在 protect_paid_replies 开着时生效 |
| memory | db_path, distill_every_n_events, retain_event_days, write_batch_ms, clock_granularity_min | db_path 无读者，实际用 room_dir/memory.db |
| persona | id, data_dir, streamer_name, display_name（≤20）, growth{relationship, voice} | |
| avatar | renderer sprite / live2d, model_id, expression_source | v4 拆成两轴 |
| runtime | ui_port, log_level, log_viewer_content | |

### 9.3 校验、派生、迁移

- `validate.py:131-394` 有 14 条跨字段规则，每条报中文加可点的修法；
  `tests/unit/test_config_validate.py:572` 用 AST 抠出源码里所有 `ConfigProblem` 对账防漏测。
  判停类型那条在 `realtime/providers/__init__.py:391-420`，拼错的 s2s 字段那条在
  `bootstrap/s2s_launch.py` 渲染前对账。
- `derive.py` 把冷场阈值、弹幕窗、分数线、冷却、回复 token 五个数从 chattiness 派生（:43-65），TOML
  写不进这些键；运行期再由节奏器和 `reply_length` 覆盖（:81-102、:70-74）。
- `migrate.py` 三步：v1→v2 人设 mia 改名 tofu；v2→v3 礼物档金瓜子换电池、删 `[interaction.danmaku]`；
  v3→v4 形象拆成 renderer + model_id（:43-102）。只向前，不回写文件。

### 9.4 面板改配置的通道与元数据

`ui_meta.py` 125 条元数据（113 个叶子字段加 12 个分组头），reload 分 live 35 / reconnect 25 /
engine 22 / restart 43；`widget`、`wizard_step`、`aliases` 三个键没有读者（:16-19 自己记着）。面板写
配置三道闸在 `ui/config_edit.py:128-135`：路径必须在 UI_META 里、secret 一律拒、reload 类必须在允许
集合。`persist.py` 保注释定点替换加 tmp + fsync + rename 原子写，落到数据目录的用户层 profile
（:126-137），`active_profile` 本身仍写基底文件。密钥只有环境变量一个后端（`secrets.py:22-40`，
先 `BILISAMA_KEY_<NAME>` 再 `NAME`）。

### 9.5 配了没人读的字段

`speech.s2s.managed`、`audio` 的前四项、整节 `custom_tts`、`memory.db_path`、`interaction.burst_*`、
`interaction.sc_protect_ms`、`avatar.renderer=live2d`、`room.platform`。这些字段在 ui_meta 的 hint 里
大多已如实写明「这里选了不算数」。

## 10. UI

### 10.1 服务与路由

`ui/server.py` 是 FastAPI + uvicorn，绑 127.0.0.1，随机 token 当路径前缀是唯一认证；路由是
`/{token}/`、`/live-mock`、`/config`、`/ws`（控制帧）、`/audio?role=shell|browser|mock`（二进制 PCM）、
`/assets`、`/skins`、health（:261-496）。每个响应带 CSP 和 nosniff（:247-259）。`endpoint.json` 出生
就是 0600（:118-131）。

### 10.2 WebSocket 词表

`ui/events.py`，append-only。服务端发 hello、voice.state、reply.delta / done、transcript.final、
event.feed、playback.clear、log.line、panel.state、audio.owner / command / devices / level、app.exiting、
live_mock.state / event；客户端发 pet.poke、panel.set、console.line、playback.started / ended /
cancelled、audio.ask / report、app.quit、test.run / stop、live_mock.check / start / stop / capture_stop。
`hub.py` 是唯一广播点：每客户端有界队列满则丢最旧，粘性状态加回放环，掉帧上报边沿触发。

### 10.3 音频进壳

`ui/audio.py` 是核心。`AudioBroker` 管设备归属，shell 优先于 browser，claim 全程持锁；上行页面
worklet 采 16k、20ms 一帧（`ui/web/js/capture-worklet.js:16-19`），服务端 100ms 没收到就替页面补静音
（`server.py:548-594`）；下行 24k，压力下丢最新；打断靠 `flush()` 清空队列再塞一个 clear 标记，
保证停在所有在飞样本之后（`audio.py:202-216`），页面收到就 `stopEverything()` 并回报
`playback.cancelled{played_ms}`。`PlaybackTally` 按未播完的段计数喂闸门的 queued_audio。
`EchoProbe`（:366-478）用互相关回答「她在听自己吗」，回答不了 OBS 监听回扬声器那种别的进程放出来的
回声。`?role=mock` 那条是直播 Mock 的标签页音轨，只替代麦克风，不持有设备、没有下行。

### 10.4 其它页面模块

| 模块 | 干什么 |
|---|---|
| `assistants.py` | 人设卡和锚编辑；卡面渲染模板变量，编辑器保持原始模板；只写活副本 |
| `skins.py` | 皮肤包发现，`pet.json` 是标志；tofu 恒首恒内置；kirby 有意不上架 |
| `voices.py` | 按 provider 给音色选择器：dashscope 15 个实测基频、火山 O2.0 四个官方，SC2.0 手填，s2s 不能改 |
| `poke.py` | 戳一戳走 PROACTIVE 意图，15 秒冷却，必须注入一条 user item |
| `room_control.py` | 进程内换房，失败停车 |
| `live_mock.py` | 用 Chrome 标签页音轨代替麦克风，只换输入不换产品行为 |
| `test_runner.py` | 63 张卡（功能 27 加业务 36）当普通 `QueueSource` 喂进同一个 Assembly，没有旁路；`tests/unit/test_testsets_backend.py:139` 参数化 63 次钉住 |
| `config_edit.py` | 面板写配置的三道闸、控件推断、共享的 speak / audio 路径表 |

## 11. 可观测与横切件

- `obs/outcome.py`：封闭词汇表，Outcome 六个、Phase 七个、SkipReason 22 个只增不改；
  `OutcomeWindow` 存最近 50 条。
- `obs/logging.py`：结构化 JSON 加脱敏，字段名含 text 的折成长度；`tests/unit/test_log_vocabulary.py`
  钉事件名。`bind()` 的 contextvars 今天接了 turn_id 和 intent_id，job_id 没有生产方。
- `obs/health.py`：探针注册表加 FastAPI 子应用。`obs/loop_lag.py`：事件循环晚醒超过 50ms 记警告。
- `clock.py`：`Clock` 协议让所有计时器可注入，`FakeClock.advance()` 按截止点顺序唤醒，沉淀窗口 32 轮。
- `paths.py`：数据目录只认 XDG，Windows 和 macOS 都落 `~/.local/share/bilisama`，有意留到打包时一起改。
- `side.py`：见第 5 节。`secrets.py`：见 9.4。

## 12. 测试与门禁

`scripts/gate.sh` 十步：black、ruff、mypy 全量、mypy --platform win32、单元、CLI 冒烟、profile 覆盖层、
集成（探 s2s venv）、界面（探 chromium）、eslint（探 `node_modules`）；没装的层黄字跳过并在最后一行
点名。`pyproject.toml` 运行时依赖只有八个，`tests/unit/test_gate.py:416` 用 AST 守「每个依赖都真被
import」。

`tests/fakes/mock_realtime.py` 按「假件必须比真服务器更丑」建模（永久卡死的注入陷阱、DashScope 要求
先有 user 消息），`mock_volcano.py` 同理，违规写进 `violations` 而不是裸 assert；`replay.py` 回放 JSONL
夹具。测试函数定义 1396 个，参数化展开后单元层约 1750 条；集成层 21 条 s2s 补丁、4 条真服务器契约、
4 条托管契约、18 条火山契约，要真凭据或跑着的服务，没有就逐条跳过。延迟基线一个数都没量
（`docs/latency-baseline.md`）。

## 13. 十个方向的现状矩阵

| 方向 | 现状 | 欠的 |
|---|---|---|
| realtime 抽象（provider） | 完成。SpeechLink + Capabilities + 注册表 + 工厂，四家都能拨号，火山端到端真机验过 | openai_ga 真端点没验过，且它兜底读的 `api_key` 是内网 key（#85）；火山自己复制了一套槽位 / 看门狗策略，等第三个非 OpenAI 协议再抽公共层 |
| 人设 | 完成。两层锚、两层生长三态开关、晋升口、五个随包人设、面板热切换 | 生长层默认全关，长期数据没有；`display_name` 的长度校验守错字段（#86） |
| memory | 两层都在，viewer 事实已被在场常客段读到 | `fact.tags` 只写不读；`streamer` scope 有读者无写入方；打断后不截记忆、不发 `item.truncate`（#35、#73） |
| 上下文管理 | 静态前缀加动态尾段、变了才推、语音规则和事件规则分作用域、旁路回复镜像写回历史、主播弹幕当共享上下文；她自起的回复说完的话和不接时的场景备注都进冷场话题的对话列表（2026-09-09） | 侧路调用不复用直播会话前缀缓存（#71，本质是架构决定）；主播说过什么在 s2s 产品路径上只剩十字备注 |
| 主动性 | 完成，接了房间活跃度、预算、让路、无侧路模型的兜底 | 戳桌宠绕过所有 speak 开关（#48） |
| 直播互动 | 完成并真房间验过：解析预算、付费侧袋、连击聚合、三态打分、单赢家窗口、延迟池、进房合并、VIP 提升、SC 撤回 | 出厂敏感词表是占位（#92）；follow / like / share 前端已置灰但后端无发声意图（#50）；msg_type 4/5/6 待真房间验证、blivedm 到期 2026-11-13（#95） |
| 打断判停 | VAD 和判停全在语音后端，P2 不跑 VAD；打断链是 SpeechStarted → cancel + PlaybackClear → 音频队列 flush 标记 → 页面 stopEverything → playback.cancelled；付费保护只在 s2s 真生效，DashScope 探明不可行，付费事件改成靠重排队；回声消除靠 Chromium，真机三组对照 0 次自我打断；她自起的回复过语音门（3.6），出口守卫和紧急闭嘴对它生效（2026-09-09） | 投机静默窗取常量而不是计划写的分支值（#33）；`audio.echo_guard` 那个能量门没写；延迟基线没量（#16）；受保护段由 `protect_paid_replies` 开关控制（默认关），只有 s2s 能真挡住打断，云端由 `config validate` 提醒（#91 已关）；语音门在真实服务上的判对率没量（#99），记号回复留在服务端历史（#97），火山的事件回复可能念出记号（#98） |
| 工具调用接口 | 只有收的一半：L2 能收 `ToolCall`（`client.py:541-551`），`dialect.tool_spec` 能翻译两种声明格式 | `tools/` 空包；`ToolRegistry`、`ToolSpec`、`get_stream_status` 不存在；`static_prefix` 的 `tool_block` 无调用方；pin/unpin（#21）和 BackgroundRunner（#64）没开工 |
| 统一可配置参数 | 完成：单一 toml、profile 覆盖层、用户层、面板热改、114 字段、14 条校验、125 条元数据、v1→v4 迁移 | 一批「配了没人读」的字段（9.5 节）；密钥没有非终端入口；装机后随包配置找不到（#43、#55） |
| 底层语音原子能力 | 已有：16k/20ms 采集、本地噪声门、Chromium 回声消除、回声探针、16k→24k 重采样、24k 播放（页面或本机 `_Speaker` 带 10 秒缓冲上限）、按段计数的播放回执、CoreAudio 默认输出跟随、设备归属仲裁 | 自研 TTS 链整条没有（TTSEngine / tagparser / chunker / mux）；形象驱动没有；说话动画是伪脉冲，`voice.level` 无生产者（#75）；`custom_tts.voice` 是假音色框（#66） |

## 14. 本轮发现的漂移与欠账指针

编号都指向计划 §16.8。

- 受保护段：2026-09-03 加了 `[interaction] protect_paid_replies` 开关（默认关），开着时 SC 和高额礼物的答谢在
  `sc_protect_ms` 内不被打断；只有 s2s 能真挡住，云端由 `config validate` 提醒（#91 已关）。
- 出厂敏感词表是占位，上线前必须换（#92）。
- 一批代码注释与文案漂移（#93）：2026-09-03 已修——`events.py` 的四个「NO CONSUMER YET」、「四个随包人设」的
  文案、`dev_talk.py` 模块头、`test_dependency_direction.py` 文件头、几处跨文件行号引用。
- `intents.py` 逐类规则与 `event_responses.md` 靠人手同步、没有测试（#94）。
- msg_type 4/5/6 映射待真房间验证；vendored blivedm 到期测试 2026-11-13 会红（#95）。
- 仓库内三处文档漂移本轮已修：`CONTRIBUTING.md` 门禁九步改十步；`docs/runbook.md` 的日志事件名
  `proactive.no_side_model` 改成实际的 `proactive.side_model_missing_fallback`；
  `config/bilisama.toml` 的 [avatar] 块注释改成两轴的说法。
- 语音门（2026-09-09，3.6）留下的三条：记号回复三家都会留在服务端历史里，会不会让她越来越倾向报「不是对
  我说的」要在真实服务上看（#97）；火山没有逐轮通道，事件回复共享会话上下文，可能念出记号（#98）；真实服务
  探测与判对率验收还没跑（#99）。
