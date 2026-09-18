# 语音后端这一层：接口、协议、和它们的对应关系

写给隔了一阵子回来、对这层已经陌生的人。不教编程，只画地图：有哪几块、缝为什么开在这儿、
一句话进来之后到底走了哪些地方。

和另外两份的分工：[runbook.md](runbook.md) 是「怎么跑」，
[architecture-status.md](architecture-status.md) 是「现在做到哪、欠着什么」，
这份是「**这是个什么东西**」。

每条说法都能在代码里找到出处。行号会漂，模块名和函数名不会 —— 找不到就搜名字。

---

## 一、四个功能域，不是四层栈

最容易先入为主的一点：它们**不是摞起来的**。编号大不等于更底层。

```
L4  ingest/                弹幕、礼物从这里进来            ← 数据来源
     ↓
L3  director/ persona/     决定谁说、什么时候说、说什么     ← 编排
    memory/ proactive.py
     ↓
L2  realtime/              跟后端讲协议，声音从这里出去     ← 出口
    
L1  ui/                    桌宠和面板                     ← 和 L3 双向
```

调用方向是 **L4 → L3 → L2**，L1 和 L3 双向。`architecture-status.md` §1.2 专门为这个误解
立了一节。

### 只有一条硬规矩，而且只守一个方向

**L3 不许看见协议。** 具体是：`director/ persona/ memory/ tools/ ui/` 这五个包里

- 不许 `import bilisama.realtime.providers`
- 不许出现 `"response."`、`"conversation.item"`、`"input_audio_buffer"` 这类协议字面量

守门的是 `tests/unit/test_dependency_direction.py`。它用 AST 解析而不是 grep —— grep 会被
注释和 docstring 里提到协议的地方误伤，也抓不住 `from ..realtime import providers` 和
`("response" ".create")` 这种写法。它还专门测了「种一个违规进去能不能被抓到」，证明门禁有牙。

反方向「**L2 不许碰产品策略**」**只写在散文里，没有测试**。今天的实际情况是干净的
（`realtime/` 一处都没 import `director/`），但那是事实，不是门禁。

这条规矩在实践中长什么样：「弹幕的回复由谁来写」是产品决定，所以它住在 L3；
「`ClientInterrupt` 是 515 号事件」是协议细节，所以它住在 L2。adapter 悄悄替产品做决定，
就是这条规矩要防的事。

---

## 二、五个后端，其实是五种协议

这是最容易黑盒的地方。**它们不是「同一个东西的五个供应商」。**

| | 是什么 | 线上长什么样 | 自带 TTS？ |
|---|---|---|---|
| `s2s` | 我们自己跑的开源语音模型，在本机 | WebSocket + JSON | ❌ 给文字，我们合成 |
| `dashscope` | 阿里云托管 | WebSocket + JSON（OpenAI 那套事件名） | ✅ |
| `openai_ga` | OpenAI 官方 | 同上 | ✅ |
| `volcano` | 火山「豆包端到端语音」 | **二进制帧 + 数字事件号** | ✅ |
| `volcano_duplex` | 火山**另一条**协议（全双工 3.0） | JSON，事件名像 OpenAI，规矩完全不同 | ✅ |

**火山那两条是两个协议，不是一个东西的两代。** 共用一个账号、一个 API Key，此外
音色表不通用、模型号不同、地址不同、连握手顺序都相反。

前三家共用 `realtime/client.py`（`RealtimeClient`）加 `realtime/dialect.py`（方言表）——
它们的差别小到可以用一张事件名对照表抹平。火山那两条各有各的 adapter：
一个说二进制数字号，一个的握手是**客户端先开口**，都塞不进那个客户端的形状。

---

## 三、那份合同：`SpeechLink`

在 `realtime/link.py`。模块头注一句话说清了它的定位：

> **What L3 is allowed to know about a speech backend.**

注意不是「所有后端能力的并集」，是「**上层被允许知道的那部分**」。

### 我们往下调的（11 个方法）

| 方法 | 干什么 |
|---|---|
| `connect()` / `aclose()` | 开机 / 关机 |
| `suspend()` / `resume()` | 面板「暂停」的链路半边。不拆链路，只是不让帧动；人设和对话续接都留着。`resume()` 允许抛错，抛错表示「还没通」，调用方就继续保持暂停，而不是假装恢复 |
| `set_context(text)` | 推人设 |
| `push_audio(pcm)` | 主播的麦克风。**合同是永不断流** —— 静音时也要发静音帧，adapter 根本没有「停止上行」这句话可说 |
| `add_context_item(text)` | 把一条弹幕写进对话历史。**知道有这回事 ≠ 让她开口** |
| `request_reply(spec)` | 请她说一段，返回一个 `ReplyHandle` |
| `cancel(handle)` | 掐掉这一条 |
| `end_protection()` | 受保护回复说完之后，把后端自己的打断机制装回去 |
| `events()` | **唯一的上行口** |

`ReplySpec` 描述的是**意图不是协议** —— s2s 的 adapter 把 `write_history` 变成两步注入，
托管的 adapter 可以换别的做法，L3 分辨不出来。

### 它往上报的（12 种事件，全是读数）

```
主播：  SpeechStarted   SpeechStopped   UserTranscriptDelta/Done
她：    ReplyStarted    ReplyTextDelta  ReplyAudioDelta  ReplyDone
链路：  LinkUp          LinkDown        LinkError
待用：  ToolCall        （只有生产者，没有消费者）
```

**这些全是从下往上的读数，我们从不往后端发它们。** 后端说「主播开口了」，adapter 翻译成
`SpeechStarted`，上层照着做反应 —— 上层不知道底下是 459 号二进制事件还是一帧 JSON。

三个容易混的：

- **`UserTranscript*` 为什么单列**：专门钉「这是主播说的话，不是她说的」。记忆层靠这个区分。
- **`LinkDown` 和 `LinkError` 为什么分开**：一个是「某件事失败了」，一个是「链路现在是断的」。
  L3 对这两件事的处置完全不同 —— `LinkDown` 到达时所有在飞的回复已经被判成 FAILED，
  调度器会清空队列并强制复位说话权，否则重连之后闸门永远关着。
- **`LinkUp` 比「连上了」更强**：它是「socket 通了**而且**会话已经重建完」，adapter 已经把
  引导帧和上下文重放过。看见它就可以立刻发东西。

---

## 四、抹不平的差异：`Capabilities`

有些事不是翻译能抹平的。它们写在 `realtime/capabilities.py`，一个后端一行。

模块头注给了**归属判据**：

> 只有 adapter 自己读的字段不是能力，是 adapter 常量。

采样率、超时、会话上限、错误码模式都没通过这个判据，所以它们留在 adapter 里。通过的那些：

| 位 | 意思 | 谁因为它长了分支 |
|---|---|---|
| `owns_tts` | 它自己出声，还是给文字让我们合成 | ⚠️ 见下 |
| `single_response_slot` | 同时只能有一条回复在生成 | 调度器 |
| `out_of_band_exempt_from_slot` | 旁路回复占不占那个名额 | 客户端 |
| `item_truncate` | 能不能裁剪模型侧的历史 | ⚠️ 见下 |
| `acknowledges_session_update` | 改配置有没有回执可等 | 客户端 |
| `per_reply_base_instructions` | 有没有逐轮指令通道 | 装配层 |
| `composes_on_demand` | **能不能在我们想让她说的时候让她说** | 调度器 + 装配层 |

两个例外，已经在字段旁注明：`item_truncate` 在 `src/` 里**一个读者都没有**；
`owns_tts` 只有 `config/validate.py` 读，而且它是自己按 s2s 的补丁列表重算的，
并不读这张表。

最后那一位是这两天所有问题的根源：**duplex 上它是 `False`** ——
只有主播的麦克风能让她开口，我们叫不动她。

---

## 五、接口和协议帧的对应

同一个方法，五家发出去的东西完全不一样。这就是 adapter 层存在的全部意义。

| `SpeechLink` | s2s / dashscope / openai_ga | volcano（数字号） | volcano_duplex |
|---|---|---|---|
| `connect` | 开 socket，等 `session.created` | `StartSession` **100** | 我们先发 `session.create` |
| `set_context` | `session.update` | `UpdateConfig`（SC2.0 改不了，只能换会话） | `session.update` |
| `push_audio` | `input_audio_buffer.append` | 二进制音频帧 | `input_audio_buffer.append` |
| `add_context_item` | `conversation.item.create` | **本地攒着** | **本地攒着** |
| `request_reply` | `response.create` | `ChatTextQuery` **501** | `speech_text_buffer.replacement` ⚠️ |
| `cancel` | `response.cancel` | `ClientInterrupt` **515** | `response.cancel` |
| `end_protection` | 只打一条警告 | 空操作 | 空操作 |
| `aclose` | 关 socket | `FinishSession` **102** | `session.close` |

### 三处值得单独记住的

**① 火山旧协议把两步合成了一步。** 别家是「先写历史，再请她说」两帧；火山没有
`response.create`，`ChatTextQuery` 一帧同时干两件事。所以它的 `add_context_item` 只在本地
记着，等 `request_reply` 时拼进去一起发。

**② `add_context_item` 在两家是「假的」。** 名字听着像「发一条上下文过去」，但在火山和
duplex 上什么都不发：火山是要和 `request_reply` 拼在一起；duplex 是中途发了服务端会收，
**但她看不见** —— 只有建会话那一刻的 `dialog_context` 进得去。

**③ duplex 的 `request_reply` 不是「请她说」。** `speech_text_buffer.replacement` 的意思是
「照这段文字念」，没有「基于这段文字组织一句话」这个动作。而且**这一帧在麦克风开着时从来
没被念出来过**（八场实盘 40 次，零音频帧），所以这条路今天在装配层被拒绝 ——
详见 runbook 的「火山引擎（全双工）」一节。duplex 上唯一活着的事件回复路是工具拉取。

**④ `end_protection` 三家各有各的不做。** s2s 是真的在受保护回复期间关掉后端的打断开关、
结束再打开；托管两家只打一条警告（这个字段真端点没探过）；火山两条是明写的空操作 ——
协议里压根没有这个开关。所以「SC 答谢不被主播打断」这件事，**只有 s2s 真做得到**。

---

## 六、两条主线，端到端

### A：主播说话 → 她回应

```
麦克风
  → ui/audio.py 的 AudioInputSwitch（暂停闸 → 噪声门 → 启用开关）
  → SpeechLink.push_audio
  → 后端
  ⋯ 后端自己判停，自己开始答 ⋯
  → adapter 报 ReplyStarted / ReplyTextDelta / ReplyAudioDelta
  → dev_talk 的扇出
  → 语音门（director/voice_turn.py）：这句是对她说的吗？
  → 喇叭
```

**关键：这条路上我们没有「让她说」这个动作。** 是后端自己判停之后决定要答的。我们能做的
只有**事后掐掉** —— 这就是语音门干的事：它扣住回复的头几帧，解码开头有没有 `[SKIP]` 标记，
判定这一轮该不该出声。

开关是 `[interaction] voice_reply`（`always` / `when_addressed`），面板上在
**高级页 → 全量配置 → 「主播说话时接不接」**，改了立刻生效。

### B：弹幕 → 她回

```
B 站 → ingest/ → 打分 → 选窗（一个窗口只出一个赢家）
  → director/intents.py 造一个 Intent
  → scheduler.submit → 堆里排队（按优先级、再按到达顺序）
  → SpeakingFloor：现在能开口吗？
  → _dispatch 两步：add_context_item（发生了什么）+ request_reply（请她说）
  → 后端 → 喇叭
```

**每一条 Intent 最后一定有且只有一个「判决」（Verdict）。** 这是为了回答那个最常问的问题：
**「刚才那条弹幕她为什么没理？」** 判决会写清是被哪道闸挡的、还是过期了、还是被打断了，
而不是笼统一句「过期」。

### 说话权（SpeakingFloor）到底管什么

管的是**路线 B**，不管路线 A。

你说话的时候，弹幕照常到达、照常打分、照常进记忆 —— **被推迟的只是「她开口回这条」**。
它拦在调度器把意图递给后端之前的最后一道。

五个条件任一成立，闸门就是关的：主播在说、她自己在说、音频还在播、刚说完的安静窗口、
话痨度冷却。

---

## 七、想改东西时先看哪里

| 想干的事 | 去哪 |
|---|---|
| 加一个新后端 | `config/enums.py`、`PROFILES`、`capabilities.py`、`factory.py` 的一个 arm、`quiet_window_s`、`schema.py`、`validate.py`、`ui_meta.py`、`config/bilisama.toml` —— **九处**，`factory.py` 头注里列着 |
| 改「她什么时候该说话」 | `director/floor.py`（时机）、`director/scheduler.py`（谁说） |
| 改「主播说话接不接」 | `director/voice_turn.py` + `config/personas/live/voice_addressing.md` |
| 改某个后端的协议细节 | `realtime/providers/<名字>.py`，**只改这一个文件** |
| 加一个面板控件 | `config/ui_meta.py`（元数据）+ 系统页是手工挑的卡，全量配置在高级页 |

---

## 八、这份文档没讲的

- **每个后端各自的坑**（音色配错会怎样、哪个版本配哪种音色、duplex 的各种实测事实）——
  在 [runbook.md](runbook.md) 各后端那几节。
- **现在哪些功能是残的、欠着什么** —— 在 [architecture-status.md](architecture-status.md)。
- **判决、闸门、优先级的完整语义** —— 在 `director/scheduler.py` 的文件头，那段是
  全仓最该读的注释之一。
