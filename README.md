# BiliSama

B 站直播的 AI 伴播：听得见主播说话、看得见弹幕礼物、用自然语音参与直播，
把单人直播变成双人节目。

当前进度：阶段 0–3 已完成（地基、L2 语音链路、L3 调度骨架、人设与记忆），阶段 6
（弹幕接入）提前做完了，阶段 5 的桌宠界面、Electron 壳和音频进浏览器也已交付，只剩
Live2D；阶段 4 的自研 TTS 还没开工。施工顺序跟阶段编号对不上，看状态别按编号推。
详见实施计划 §9 / §15。
现在就能跑的东西和跑法见 [docs/runbook.md](docs/runbook.md)。

## 从零开始

需要 Python 3.12 以上和 [uv](https://docs.astral.sh/uv/)（`brew install uv`）。装依赖只要一条命令，
它会自己建好 `.venv` 并按 `uv.lock` 里锁定的版本安装：

```bash
uv sync
```

装完花一秒确认一下装对了——能打出版本号、配置能读通，就说明环境是好的：

```bash
.venv/bin/bilisama --version && .venv/bin/bilisama config validate
```

接下来看你想怎么试。**想最快听到声音**就走云端，只要一份 DashScope 凭据，不用下模型：

```bash
source path.sh                                  # 凭据文件，格式见 .env.example
.venv/bin/bilisama dev-talk --director --provider dashscope --model qwen-audio-3.0-realtime-flash
```

**想跑全本地**（识别、对话、合成都在自己机器上）就得先装语音引擎，约 2 GB，
首次启动还要下模型，步骤和排错都在 [docs/runbook.md](docs/runbook.md)：

```bash
scripts/smoke_provider_b.sh install             # 装引擎（一次就够）
# 起服务器的命令见 runbook「起本地语音服务器」一节
.venv/bin/bilisama dev-talk --director
```

两条路的 `--director` 都会把人设、记忆、调度整套立起来；去掉它就只测语音链路本身。
终端打字模拟弹幕、`/sc` 模拟付费消息这些玩法，runbook 里有完整清单。

`--director` 还自带一个桌宠网页界面（形象、说话气泡、面板），启动横幅里有地址，
加 `--open` 自动开浏览器；桌面悬浮窗形态在 `desktop/preview/`。用法见 runbook
「桌宠预览」一节。面板的「测试」页内置简单意图与困难多轮两套测试集，自动回放语音、
穿插直播事件，逐卡人工判定；用法见 [意图测试页](docs/intent-test-console.md)。同一页还能打开「直播 Mock」，共享一个带音频的 Chrome
标签页、填真实房间号，用直播画面的声音代替麦克风做全链路验证——两样的用法和
通过标准见 [docs/mvp-validation-plan.md](docs/mvp-validation-plan.md)。

要改代码的话，提交前跑一遍门禁 `scripts/gate.sh`（格式、类型、单测、CLI 冒烟，
装了就连集成层、浏览器界面层和 eslint 一起跑，十步），规矩写在 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 文档索引

| 文档 | 干什么 |
|---|---|
| 实施计划（`~/.claude/plans/` 下，路径见 CLAUDE.md 会话上下文） | 架构与全部决策（§1-14）、进度台账（§15）、欠账清单（§16.8） |
| 计划归档（同目录 `*.archive.md`） | 每一轮的完整过程记录，回溯用 |
| [CLAUDE.md](CLAUDE.md) | 会话准则：指导原则、代码规范、文风、流程纪律 |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 提交门禁、代码语言分界、commit 写法 |
| [docs/runbook.md](docs/runbook.md) | 操作手册：一键启动、起服务器、dev-talk、面板六页、直播 Mock、常见坑 |
| [docs/architecture-status.md](docs/architecture-status.md) | 架构现状梳理（2026-09-03）：四个功能域逐模块实现到哪、接缝在哪、哪些字段有名无实；跟计划对着读 |
| [docs/mvp-validation-plan.md](docs/mvp-validation-plan.md) | MVP 验证方案：四道门、测试卡、业务评分标准、历次回归记录 |
| [docs/intent-classification.md](docs/intent-classification.md) | 原始互动场景验收设计：8种主播状态、3种参与方式；当前二分类实现见 Planner 合并说明 |
| [docs/planner-integration.md](docs/planner-integration.md) | Planner 合并说明：接话/先听二分类、播放拦截流程、完整语音意图 Prompt |
| [docs/voice-event-linkage-plan.md](docs/voice-event-linkage-plan.md) | 主播语音与直播事件联动计划：逐条需求、状态管理、主动契机及待执行验收矩阵 |
| [docs/voice-event-linkage-acceptance.md](docs/voice-event-linkage-acceptance.md) | 本轮联动验收：区分无需报告和必需状态报告，检查具体事件、静默、120秒征集与中断续接；程序证据与真实效果分开 |
| [docs/current-interaction-prompts.md](docs/current-interaction-prompts.md) | 当前公开 Prompt 快照：豆腐人设、动态回复长度、完整语音规则、各类事件、主动话题及按后台状态变化调用的独立报告；不包含本机私人记忆 |
| [docs/intent-test-console.md](docs/intent-test-console.md) | 当前测试页：简单意图与困难多轮场景；真实语音回放、事件注入和人工判定，新增联动检查见本轮验收文档 |
| [docs/intent-scenarios.md](docs/intent-scenarios.md) | 原始48条多轮互动测试设计；预期与当前实现风险分开，当前可运行版本见意图测试页文档 |
| [docs/architecture.html](docs/architecture.html) | 架构展示页：进程全景、五条调用链、调度核心、模型清单（浏览器打开即看） |
| [docs/latency-baseline.md](docs/latency-baseline.md) | 延迟测量设计（待实施，`branch_rate` 是第一个要量的数） |
| [docs/live2d-licensing.md](docs/live2d-licensing.md) | Live2D 对外发布前必须先办完的五件事，以及现在走到哪 |
| [docs/plan-diff-2026-08-17.html](docs/plan-diff-2026-08-17.html) | 2026-08-17 那轮计划改动的 diff 视图 |
| [NOTICE](NOTICE) / [LICENSE](LICENSE) | 六个上游项目的署名；Apache-2.0 |
| [config/bilisama.toml](config/bilisama.toml) | 唯一配置真相源，注释即文档 |
| [.env.example](.env.example) | 所有环境变量的清单与说明；真值写进本地的 `path.sh`，永不入库 |

新增重要文档时在这张表挂号（CLAUDE.md 的流程纪律）。
