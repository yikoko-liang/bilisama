# BiliSama

B 站直播的 AI 伴播：听得见主播说话、看得见弹幕礼物、用自然语音参与直播，
把单人直播变成双人节目。

当前进度：阶段 0–3 已完成（地基、L2 语音链路、L3 调度骨架、人设与记忆）。后续顺序
2026-08-10 调整为：TTS 形象 → Electron → 弹幕 → 交付 → 联调——先把核心对话体验做完整，
直播间外围功能往后排。详见实施计划 §9 / §15。
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

要改代码的话，提交前跑一遍门禁 `scripts/gate.sh`（格式、类型、测试、集成层一条龙），
规矩写在 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 文档索引

| 文档 | 干什么 |
|---|---|
| 实施计划（`~/.claude/plans/` 下，路径见 CLAUDE.md 会话上下文） | 架构与全部决策（§1-14）、进度台账（§15）、欠账清单（§16.8） |
| 计划归档（同目录 `*.archive.md`） | 每一轮的完整过程记录，回溯用 |
| [CLAUDE.md](CLAUDE.md) | 会话准则：指导原则、代码规范、文风、流程纪律 |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 提交门禁、代码语言分界、commit 写法 |
| [docs/runbook.md](docs/runbook.md) | 操作手册：起服务器、dev-talk、测试、常见坑 |
| [docs/architecture.html](docs/architecture.html) | 架构展示页：进程全景、五条调用链、调度核心、模型清单（浏览器打开即看） |
| [docs/latency-baseline.md](docs/latency-baseline.md) | 延迟测量设计（待实施，`branch_rate` 是第一个要量的数） |
| [NOTICE](NOTICE) / [LICENSE](LICENSE) | 六个上游项目的署名；Apache-2.0 |
| [config/bilisama.toml](config/bilisama.toml) | 唯一配置真相源，注释即文档 |
| [.env.example](.env.example) | 所有环境变量的清单与说明；真值写进本地的 `path.sh`，永不入库 |

新增重要文档时在这张表挂号（CLAUDE.md 的流程纪律）。
