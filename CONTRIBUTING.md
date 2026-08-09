# 参与开发

需求文档里那十五条规范是这个仓库的底线，这里只补充能被工具检查的部分，以及一条
项目追加的语言约定。

## 提交前跑一遍门禁

```bash
scripts/gate.sh
```

它跑 `black --check`、`ruff`、`mypy`（全量，含 `tests` 和 `tools`）、单元测试、
CLI 冒烟、profile 覆盖层断言，最后是需要 speech-to-speech 的集成层。

CLI 冒烟和 profile 覆盖层不是凑数：拆 `config` 包那次，`validate.py` 少了一个运行时
import，52 个单元测试全绿,因为当时**没有一个测试构造过 `Settings`**。是 CLI 冒烟
抓到的。在覆盖缺口补上之前，这一层不能省。

### 集成层：装了就自动跑

集成层管的是 s2s 补丁的自检，要一个单独的 venv（约 385 MiB），所以门禁没法无条件跑
它。规矩不是「一定要跑」，而是「不跑就得说出来」：venv 在，门禁自己就把这一层跑了；
不在，它会打一条显眼的跳过提示，并且**不会**在最后声称全部通过。

装一次就够，之后 `scripts/gate.sh` 就覆盖了这一层：

```bash
scripts/smoke_provider_b.sh install
```

默认装到 `~/.local/share/bilisama/engines/s2s`；换地方用 `BILISAMA_S2S_VENV`，
门禁按同一个变量找。想在门禁之外单独跑这批，原来的命令照旧：

```bash
.venv/bin/python -m pytest -m integration
```

门禁最后一行说的是这次到底跑了哪几层，别扫一眼绿色就走：

| 最后一行 | 意思 |
|---|---|
| `全部通过（含集成层）` | 两层都跑了 |
| `单元层全部通过，集成层没跑（见上）` | 没装 s2s，那一层这次没验过 |

CI 上「没装所以跳过」不是个能接受的答案，所以那边要设
`BILISAMA_GATE_REQUIRE_INTEGRATION=1`：venv 找不到就直接判门禁失败，而不是跳过。
它比的是 `0`,所以设成 `0` 就是明确关掉，本机不想被拦的时候用。

## 语言：代码用英文，给人看的用中文

| 什么 | 语言 |
|---|---|
| 注释、docstring | 英文 |
| commit message | 英文 |
| 变量名、函数名、模块名 | 英文 |
| CLI 输出、报错提示 | 中文 |
| 设置界面的 label / hint | 中文 |
| 人设文本、AI 说的话 | 中文 |
| 测试数据里的弹幕内容 | 中文 |

分界是"给谁看"。主播、运营和观众都是中文用户，他们看到的每一个字都该是中文；
写代码的人看的东西用英文。

注释要说**为什么**，不是**是什么**。`# increment counter` 配 `i += 1` 等于没写。

`pyproject.toml` 里豁免了 `RUF001/002/003`（全角标点告警），那是给用户可见的中文
文案用的，**不是给注释开的后门**。想自查有没有漏网的中文注释，临时把豁免删掉跑
`ruff`,剩下的告警应该全部落在界面文案和测试数据里。

## 勘察结论要带出处

这个仓库里有一批注释记录着从上游代码里挖出来的结论，比如：

```python
# Bilibili masks uid to 0 for privacy; uid_hash is then the only stable
# per-room identity. N.E.K.O drops those events outright
# (neko_live/modules/live_events/module.py:238), which silences the whole
# danmaku stream once masking kicks in.
```

这类注释的价值全在那个 `file:line`。没有出处的断言，下一个人无从核实，
半年后就没人敢动那段代码了。写这类注释时**必须带上出处**。

同样地，没核实过的事不要写成结论。要么去核实，要么明确标成待办。

## commit 怎么写

首行祈使句、不超过 72 字符、不带句号。空一行。正文说清楚**为什么这么改**，
而不是复述 diff,diff 自己会说话。

**改了行为的和没改行为的分开提交。** 纯重构的 commit 正文里要写明"行为不变，
现有测试原样通过，一个断言都没改"。这样 review 的人能一眼分清哪些需要仔细看。

发现了 bug 但这次不修，就写进计划的待办清单，别混在重构里顺手改掉。

## 已知缺陷

阶段 0 的代码评审找出 8 处功能缺陷，都记在实施计划的 §16.6，修的时候先写一个
复现测试（先红后绿）。代码里对应的位置标了 `KNOWN BROKEN` 并指向那份清单。
