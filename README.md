# BiliSama

B 站直播的 AI 伴播：听得见主播说话、看得见弹幕礼物、用自然语音参与直播，
把单人直播变成双人节目。

当前进度：阶段 0-3 已关闭（地基、L2 语音链路、L3 调度骨架、人设与记忆）。后续顺序
2026-08-10 调整为：TTS 形象 → Electron → 弹幕 → 交付 → 联调（核心对话体验先闭环，
直播间外围后挂），详见实施计划 §9 / §15。
现在就能跑的东西和跑法见 [docs/runbook.md](docs/runbook.md)。

## 快速开始

```bash
scripts/gate.sh                                 # 全套检查：格式、类型、测试
.venv/bin/bilisama dev-talk --provider s2s      # 真人语音对话（先起服务器，见 runbook）
```

## 文档索引

| 文档 | 干什么 |
|---|---|
| 实施计划（`~/.claude/plans/` 下，路径见 CLAUDE.md 会话上下文） | 架构与全部决策（§1-14）、进度台账（§15）、欠账清单（§16.8） |
| 计划归档（同目录 `*.archive.md`） | 每一轮的完整过程记录，回溯用 |
| [CLAUDE.md](CLAUDE.md) | 会话准则：指导原则、代码规范、文风、流程纪律 |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 提交门禁、代码语言分界、commit 写法 |
| [docs/runbook.md](docs/runbook.md) | 操作手册：起服务器、dev-talk、测试、常见坑 |
| [docs/latency-baseline.md](docs/latency-baseline.md) | 延迟测量设计（待实施，`branch_rate` 是第一个要量的数） |
| [NOTICE](NOTICE) / [LICENSE](LICENSE) | 六个上游项目的署名；Apache-2.0 |
| [config/bilisama.toml](config/bilisama.toml) | 唯一配置真相源，注释即文档 |
| `path.sh`（本地，不入库） | LLM / DashScope 凭据，格式见 runbook |

新增重要文档时在这张表挂号（CLAUDE.md 的流程纪律）。
