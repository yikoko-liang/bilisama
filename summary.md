# 下午讨论总结：BiliSama 与实时语音框架

这份文档用产品视角回答今天下午反复讨论的几个问题：LiveKit、Pipecat、
Qwen-Audio-Agent、Hugging Face Speech-to-Speech 分别是什么层级；BiliSama 当前用了
什么；它和 Open-LLM-VTuber、N.E.K.O 有什么关系；未来什么时候值得换框架。

## 一、先记住这张分层图

```text
最终产品
BiliSama / Qwen-Audio-Agent / Open-LLM-VTuber / N.E.K.O
                         │
对话编排与 Agent 运行时
Pipecat / LiveKit Agents / 各项目自研编排
                         │
语音流水线或语音服务器
Hugging Face Speech-to-Speech / 各家 Realtime Provider
                         │
实时音视频传输
LiveKit Core（房间、WebRTC、弱网、跨设备）
                         │
模型
Qwen / 豆包 / GPT Realtime / ASR / TTS / 自研音频模型
```

这些边界不是绝对的。例如 LiveKit 除了通信底座，还有 LiveKit Agents；
Qwen-Audio-Agent 也同时覆盖界面、Gateway、实时语音和后台任务。但这张图足够帮助产品
判断“它是模型、框架、服务器还是最终产品”。

## 二、四个最容易混淆的概念

| 名称 | 一句话定位 | 类比 |
|---|---|---|
| LiveKit Core | 实时音视频通信底座，负责房间、WebRTC、跨端、弱网和扩容 | 道路和通信网络 |
| LiveKit Agents | 建在 LiveKit 房间上的语音 Agent 运行框架 | 带车队调度的驾驶系统 |
| Pipecat | 自由组装 VAD、STT、LLM、TTS 和传输的语音/多模态编排框架 | 组装流水线的工具箱 |
| Hugging Face Speech-to-Speech | 已经组装好的低延迟语音对话服务器 | 可直接启动的动力总成 |

真正适合直接比较的是 `Pipecat ↔ LiveKit Agents`。LiveKit Core 更靠下，Pipecat 也可以
把 LiveKit 当作传输层。官方资料：[LiveKit Agents](https://docs.livekit.io/agents/)、
[Pipecat Pipeline](https://docs.pipecat.ai/pipecat/learn/pipeline)、
[Pipecat Transport](https://docs.pipecat.ai/pipecat/learn/transports)。

### Hugging Face Speech-to-Speech 不是一个模型

这里指的是 [huggingface/speech-to-speech](https://github.com/huggingface/speech-to-speech)
这个开源项目。它是一个可启动的本地语音服务器，通常把下面的环节串起来：

```text
音频 → Silero VAD → SmartTurn → STT/音频模型 → LLM → TTS → 音频
```

它和 Pipecat 处在相近的语音运行层，但形态不同：Speech-to-Speech 提供一条已经装好的
管线，Pipecat 提供搭建任意管线的框架。

### Qwen-Audio-Agent 不是 Qwen 模型

[Qwen-Audio-Agent](https://github.com/QwenAudio/qwen-audio-agent) 是一套可运行的桌面
语音 AI 助手和参考实现。它的核心是“前台接待＋后台办事”：

```text
实时语音前台：听、聊、打断、即时回答
               ↓ 复杂任务
后台 Agent：读文件、调用工具、执行长任务
               ↓
任务结果回到当前对话，再由前台说出来
```

因此它比 Pipecat 更接近产品，比 Qwen 模型高很多层。它默认围绕 Qwen Realtime，但新版本
支持自定义 Realtime Provider；这意味着能接豆包，但需要开发豆包协议适配器，不能只改
模型名或 API Key。扩展入口见
[自定义 Provider 文档](https://github.com/QwenAudio/qwen-audio-agent/blob/main/docs/voice-frontends/custom-provider.zh.md)。

## 三、BiliSama 当前到底用了什么

BiliSama 没有采用一个与 Pipecat 或 LiveKit 对等的总框架。它使用基础库和自研分层拼出
了一条固定的伴播语音链路：

```text
主播本地麦克风
      ↓
BiliSama L3：弹幕、礼物、人设、记忆、回复优先级、说话时机
      ↓
BiliSama L2 realtime/：统一接口、协议翻译、连接、打断、超时
      ↓ WebSocket
Hugging Face Speech-to-Speech 或托管 Realtime Provider
      ↓
模型与 TTS
      ↓
本地扬声器/虚拟声卡
```

项目依赖中没有 `pipecat-ai` 或 `livekit`，而是直接使用 `websockets`、FastAPI、Uvicorn
等基础库，见 `pyproject.toml:8-22`。当前架构的 L3、L2 和语音服务器边界见
`docs/architecture.md`。

### `realtime/` 是什么

`src/bilisama/realtime/` 是 BiliSama 自研的 L2 实时语音适配层，不是模型，也不是独立
服务器。

- `link.py`：对上提供统一的 `SpeechLink`，见 `src/bilisama/realtime/link.py:182-210`。
- `client.py`：管理 WebSocket、回复槽、取消、25 秒看门狗、晚到数据和重连。
- `dialect.py`：翻译不同版本的 OpenAI Realtime 风格协议。
- `capabilities.py`：记录各 Provider 是否自带 TTS、是否只有一个回复槽等差异。
- `providers/s2s.py`、`providers/hosted.py`：把具体服务包装成相同的 `SpeechLink`。

它只覆盖 Pipecat 的连接、Provider、协议和部分回合状态能力，不包含完整的 VAD、模型和
TTS 流水线。

### 哪一部分对标 Pipecat

不是某一个目录，而是下面三部分合起来：

```text
realtime/
＋ Hugging Face Speech-to-Speech
＋ director/ 中的 SpeakingFloor、Scheduler
```

对应关系如下：

| Pipecat 能力 | BiliSama 当前实现 |
|---|---|
| Transport | 本地麦克风/扬声器＋`RealtimeClient` WebSocket |
| Frame/统一事件 | `LinkEvent`、`ReplySpec` |
| Provider/Service | `realtime/providers/` |
| VAD、判停 | Speech-to-Speech 中的 Silero、SmartTurn |
| STT/LLM/TTS | Speech-to-Speech 或托管 Realtime 模型 |
| 打断和回合控制 | `RealtimeClient＋SpeakingFloor＋Scheduler` |

BiliSama 没有 Pipecat 那种通用 `Pipeline` 和 `FrameProcessor` 抽象，现有实现是为伴播业务
写定的一条链路。因此“功能有重叠”不等于“BiliSama 已经实现了一个 Pipecat 框架”。

## 四、为什么当前不需要 LiveKit

当前语音闭环都在主播电脑上：本地采麦、本地进程处理、本地播放或送入虚拟声卡。连接
B 站直播间主要增加弹幕、礼物、SC 等事件源，并不自动把 B 站播放器音轨当成麦克风，见
`docs/runbook.md:319-328`。

所以现在没有多人房间、跨公网麦克风、移动端接入、电话或大规模房间调度，普通本地音频
接口和 WebSocket 已经够用。出现下面的目标时再评估 LiveKit：

- BiliSama 部署到云端，主播从网页或手机传麦克风；
- 多主播连麦或远程运营加入；
- 一个服务同时管理大量直播间；
- 需要弱网、跨设备、WebRTC 房间或 SIP 电话。

即使未来直接分析 B 站直播音轨，也可以走拉流解码或虚拟声卡，不会因此自动需要 LiveKit。

## 五、能不能把 Speech-to-Speech 换成 Pipecat

技术上可以，但这是语音基础设施重构，不是换一个包名或 URL。

相对稳妥的形态是保留进程边界：

```text
BiliSama L3
    ↓ SpeechLink
新的 Pipecat 适配器
    ↓
独立 Pipecat Server
    ↓
VAD / SmartTurn / 自研模型 / TTS
```

需要重新验证：音频输入、判停、单回复槽、打断、取消、主动注入、保护回复、断线恢复、
超时和旧回复丢弃。当前 Speech-to-Speech 的特殊规则集中在
`src/bilisama/realtime/providers/s2s.py:1-25`。

当前建议：

| 目标 | 建议 |
|---|---|
| 只接豆包或另一家 Realtime 模型 | 新增 Provider，不换 Pipecat |
| 只换 TTS | 增加 TTS 适配，不换整条管线 |
| 接即将上线的自研音频模型 | 延续当前 `stt=none` 音频直送路径 |
| 未来频繁组合多家 ASR、LLM、TTS | 再评估 Pipecat |
| 做云端、多端、多人房间 | 评估 Pipecat＋LiveKit |

## 六、BiliSama 从 Qwen-Audio-Agent 借了什么

项目在 `NOTICE:17-24` 中公开记录了来源。当前代码中，接近直接移植的主要有两块：

1. `AnnouncementWindow → SpeakingFloor`：几乎逐行移植后增加直播所需的静默、冷却、
   隐式回复和边沿保护，见 `src/bilisama/director/floor.py:1-12`。
2. 桌宠精灵动画：把 Qwen-Audio-Agent 的 `sprite-orb.js` 动画轨道模型移植到
   `src/bilisama/ui/web/js/skins/sprite.js:1-5`，再增加皮肤校验和降级。

另外还参考了：

- Provider / Protocol / Capabilities 分层；
- 命令串行队列；
- 已结束回复的 tombstone，阻止晚到数据让旧回复“复活”，见
  `src/bilisama/realtime/client.py:23-29`；
- 桌宠状态优先级、本地随机 Token 路径、Electron 透明置顶窗口。

没有复制的是 BiliSama 的直播产品核心：弹幕挑选、礼物与 SC 优先级、付费保护、洪峰调度、
主动话题、人设和记忆。调度器也明确说明 Qwen-Audio-Agent 面对单用户和低频通知，没有
BiliSama 的“礼物风暴抢一个回复槽”问题，见 `src/bilisama/director/scheduler.py:1-7`。

## 七、与 Open-LLM-VTuber、N.E.K.O 的关系

### Open-LLM-VTuber

[Open-LLM-VTuber](https://github.com/Open-LLM-VTuber/Open-LLM-VTuber) 是完整的本地虚拟人
产品，自己使用 FastAPI/WebSocket 串 ASR、Agent、TTS 和 Live2D，没有直接依赖 LiveKit
或 Pipecat。它和 BiliSama 是同类产品参考，不是 BiliSama 的底层框架。

BiliSama 的 `NOTICE:39-41` 记录了后续计划借鉴的 CJK 分句、TTS 有序交付和打断后的
记忆改写；这些条目仍标记为 planned，不能说已经落地。

### N.E.K.O

[N.E.K.O](https://github.com/Project-N-E-K-O/N.E.K.O) 是本地优先的 AI 陪伴运行时，覆盖
语音、视觉、Live2D/VRM、记忆、Agent、电脑操作和插件。它已经自己实现 FastAPI、
WebSocket、ZeroMQ、Realtime/Offline Client 和 TTS Worker，因此没有使用 LiveKit 或
Pipecat 整套框架。

但 N.E.K.O 和 BiliSama 的 Speech-to-Speech 都使用了 Pipecat 团队发布的 SmartTurn
v3.2 判停模型。这叫“使用 Pipecat 生态的一个模型”，不等于“使用 Pipecat 框架”。

BiliSama 从 N.E.K.O 实际落地借鉴的是弹幕和付费事件处理：打分顺序、窗口选一条、付费
通道，以及 0.35 秒去重、60 秒单用户冷却、3 次/60 秒熔断、1 秒礼物连击归并和 600 秒
抑制，见 `NOTICE:32-37`、`src/bilisama/ingest/bilibili/scoring.py:1-13` 和
`src/bilisama/ingest/bilibili/safety.py:1-37`。

## 八、最终判断

当前 BiliSama 的路线可以概括为：

```text
本地优先的直播产品
＋ 自研 L3 直播编排
＋ 自研 L2 Realtime 适配
＋ Hugging Face Speech-to-Speech/托管 Realtime 语音后端
```

短期没有必要为了“用了知名框架”而迁移。Pipecat 的价值出现在语音组件组合复杂度明显上升
之后；LiveKit 的价值出现在伴播从本机工具变成云端、多端、多人实时服务之后。在这之前，
新增 Provider 比更换整套基础设施更符合当前产品阶段。
