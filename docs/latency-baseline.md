# 延迟基线：测量设计（待实施）

状态：**设计已定，测量未做**。这份文档把散在实施计划 §2.8 / §15.5 / §15.8 里的
测量设计收进一处，方便以后开工时追溯。对应待办见计划 §16.8。

延迟基线原本是阶段 0 的验收之一，2026-08-10 改判为待办：先把真服务器跑起来
（它是测量的前提），测量本身随时可以补。

## 为什么要测，先测什么

计划 §2.8 把每一跳的延迟按代码里的真实常量算了一遍（十跳预算表见计划原文），
结论有两条：

1. **方差比均值致命。** 稳定的 1.2 秒读作「在思考」；0.8 / 2.9 / 1.1 / 2.5 读作
   「这玩意坏了」。方差的唯一来源是 SmartTurn 判 complete（等 800ms）还是
   incomplete（等 2000ms）这一个二元分支。
2. **所以第一个要量的数是 `branch_rate`**——SmartTurn 判 incomplete 的占比。
   这个数决定 `smart_turn_max_wait_ms` 该压到多少。注意：我们已经在没有这个数的
   情况下把它从 2000 压到 1200（config/bilisama.toml），这是个先行决定，测出来
   不对就要改回去。同理 `speculative_reopen_ms` 800→400 被有意押后，就是在等它。

## 测量原则（照 §2.8，一条都别省）

- **一个单调时钟、一个关联 id、量到「观众耳朵」而不是「socket 发出」。**
- **探针点的名字直接复用结构化日志的 event 名**（`src/bilisama/obs/logging.py`
  的事件词汇），一次投入两处收益。
- **跨进程比较前先做偏移标定**：连接时和每 30 秒一次 ping-pong。能用音频时钟就
  别用墙上时钟——s2s 已经把 `audio_end_ms` 从样本数算好了，「主播最后一个字」用
  它当基准，免疫时钟偏斜。
- **SmartTurn 的 `inference_ms` 和 complete/incomplete 分支直接解析它自己的日志**，
  不改 s2s。
- **`t_grace` = 首个 delta − speech_stopped − 推理耗时**。这个值应该正好等于
  800 或 2000；不等于，就说明模型首包时间捅穿了宽限，真正的瓶颈找到了。
- **不用真麦克风**——走 `input_audio_buffer.append` 喂 WAV 语料保证可复现。
  语料要覆盖：干净的句末下降调、拖尾语气词（「那个…」「然后…」）、句中 500ms
  停顿、2s / 5s / 12s 三种长度。
- **先校准再相信**：给 Mock 配一个已知的 800ms 延迟，harness 必须报出 800ms，
  才可以拿它去量真东西。
- 输出按跳的堆叠柱状图，不是一个总数——这件事的全部意义就是找出该打哪一跳。

## 现在能量到什么，被什么挡着

| 能量的 | 挡着的 | 等什么 |
|---|---|---|
| s2s 内部五跳（VAD 软结束、SmartTurn 推理、宽限、LLM 首包、TTS 首包） | — | 真服务器起来即可 |
| `branch_rate`、`t_grace` | — | 同上 |
| — | 采集分帧、回传、播放那几跳（预算表第 1、2、3、10 跳） | P1 Electron，阶段 6 |
| — | 文本 → TTS 首包那一跳（第 9 跳） | 我们自己的 TTS 链，阶段 5 |
| — | 闸门等待直方图（`t_gate` 按五个条件分别出） | SpeakingFloor，阶段 2 |

所以第一版 bench 交付的是「s2s 内部五跳 + branch_rate + 探针名字表」，
不是十跳全图。这一点要写进 bench 的 docstring，免得下一个人以为它坏了。

## A 类调优的落地状态（§2.8 优化清单）

| 动作 | 状态 |
|---|---|
| `smart_turn_max_wait_ms` 2000→1200 | 已落地（config/bilisama.toml，进了渲染出的启动 JSON） |
| `smart_turn_incomplete_delay_ms` 600→400 | 已落地 |
| `smart_turn_cpu_count` 1→2 | 已落地 |
| TTS 走云端 `qwen3_cloud` | 已落地（schema 默认值） |
| chunker 首句逗号切分保持开启 | 上游默认即开，无需动作 |
| `speculative_reopen_ms` 800→400 | **有意押后**，等 `branch_rate` |
| provider (a) 的 `silence_duration_ms` 500→300 | **落不了地**——`HostedConfig` 还没有判停段，字段加了才能设（计划 §17.5 B 组第 6 条） |
| 能量门 duck 默认开 | 已落地（`echo_guard` 默认 `duck`）。它的前提「AEC 实测干净」要等 P1 就位，归阶段 6 验 |

## 探针点名字表（预留，与 obs 日志事件名对齐）

第一版 bench 用到的探针，名字即结构化日志的 event 名：

```
vad.speech_started          主播开口（s2s 事件到达 P2 的时刻）
vad.speech_stopped          主播停口（audio_end_ms 为基准时钟）
turn.smart_turn_verdict     SmartTurn 判定（complete/incomplete + inference_ms）
turn.grace_elapsed          宽限窗口实际等待时长
reply.first_delta           首个文本/音频增量到达
reply.done                  response.done
```

后续阶段补：`playback.started` / `playback.ended`（P1 回执）、`tts.first_pcm`
（阶段 5）、`gate.blocked`（阶段 2，按五个闸门条件分桶）。
