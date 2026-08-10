# 操作手册

怎么把这个仓库里已经能跑的东西跑起来。每个重要节点的用法在落地当轮更新到这里
（CLAUDE.md 的流程纪律），所以这份文件永远反映当前状态。

## 环境

两个互相隔离的 Python 环境，别混：

| 环境 | 位置 | 装了什么 |
|---|---|---|
| BiliSama 本体 | 仓库下 `.venv/` | bilisama + 轻依赖（websockets、pydantic、pytest、sounddevice） |
| 语音引擎 | `~/.local/share/bilisama/engines/s2s/` | speech-to-speech 全家桶（torch、mlx、funasr），约 2GB |

模型缓存在 `~/.cache/huggingface/`（判停、TTS）和 `~/.cache/modelscope/`（paraformer），
首次启动自动下载，之后离线复用。

本地凭据在仓库根的 `path.sh`（已 gitignore，永不入库）：

```bash
export model_name=...       # 测试用 LLM 的模型名
export api_key=...          # 它的 key
export base_url=...         # OpenAI 兼容端点
export dashscope_url=...    # 阿里 MaaS 实例地址
export ali_api_key=...      # 它的 key
```

## 起本地语音服务器（官方三段管线）

```bash
source path.sh && export OPENAI_API_KEY="$api_key"
.venv/bin/python scripts/make_official_pipe_config.py
BILISAMA_S2S_CONFIG=config/s2s/official-pipe.local.json scripts/smoke_provider_b.sh serve
```

LLM 端点在公司内网、而系统代理劫持了 DNS 的话（Shadowrocket + EasyConnect 并存的场景），
serve 前多三行——**`no_proxy` 必须单独一行 export**，同行双赋值拿到的是旧值，
httpx 恰好优先读小写：

```bash
export BILISAMA_RESOLVE="llmapi.bilibili.co=<真实内网IP>"
export NO_PROXY="llmapi.bilibili.co,localhost,127.0.0.1,::1,.local"
export no_proxy="$NO_PROXY"
```

真实内网 IP 在能访问该域名的机器上 `nslookup` 一次即得。端口开了不等于模型加载完，
等日志里出现 `Uvicorn running` 才算就绪（全冷启动约 40 秒）。

## dev-talk：真人语音测试

拿真人声音测**我们自己的**语音链路（RealtimeClient + 方言 codec）。这也是目前
唯一能用嗓子测 DashScope 的方式——上游 `talk` 客户端只认 GA 事件名，对 beta
方言的 DashScope 连上也只有沉默。

```bash
source path.sh && export OPENAI_API_KEY="$api_key"

# 本地 s2s（先按上一节起服务器）
.venv/bin/bilisama dev-talk --provider s2s

# DashScope（qwen-audio / qwen-omni 系列 realtime 模型都可以）
.venv/bin/bilisama dev-talk --provider dashscope --model qwen-audio-3.0-realtime-flash
```

体验须知：

- **外放会让 AI 听到自己的声音**（管线没有回声消除），表现为"一点点声音就被打断"。
  戴耳机，或加 `--mute-while-speaking`（播放期间闭麦，代价是那期间插不了话）。
- 打断：正常说一句话（约 0.4 秒以上的连续语音）就能掐断播报；短促噪音不会。
- 不用麦克风也能测：`--wav 某段16k单声道.wav`，回复音频存在旁边的 `.reply.wav`。
- 音频设备用编号指定（`--input-device N`），看编号：
  `.venv/bin/python -m sounddevice`。麦克风建议用内置的，别用蓝牙耳机的麦。
- 首次运行终端会要麦克风权限。

上游自带的体验客户端（只适用本地 s2s，绕开我们的代码）：

```bash
~/.local/share/bilisama/engines/s2s/bin/speech-to-speech talk --url ws://127.0.0.1:8765/v1/realtime
```

## 人设与生长层

人设文件的活副本在 `~/.local/share/bilisama/personas/<id>/`（`persona.data_dir` 可改），
全是明文 markdown，随时可以打开手改：

| 文件 | 谁写 | 干什么 |
|---|---|---|
| identity.md / personality.md | 人 | 锚。不存在或清空时回退到 config/personas/ 的随包模板 |
| relationship.md / voice.md | 蒸馏 | 生长层。开关在 `[persona.growth]`，默认全关 |
| pinned.md | 人 | 置顶记忆，整段注入并声明始终保留 |

生长层三态：`off` 不长；`collect` 只攒进文件、不进提示词（先看几场、翻文件放心了再开）；
`on` 攒并注入。口癖层每场至多换 2 句，预算 12 句；共同经历 30 条 800 字，超了旧的出。

晋升口（锚只有人能动，这条命令就是那只手）：

```bash
.venv/bin/bilisama persona review                 # 列出生长层条目，带编号
.venv/bin/bilisama persona review --promote v1    # 点头：这条合并进 personality.md
.venv/bin/bilisama persona review --drop r2       # 划掉不喜欢的
```

蒸馏和主动话题都走 `[speech.side]` 的侧路模型。没配地址它们不干活：生长层开着时
`config validate` 会提醒；主动话题的缺配在运行期日志（`proactive.no_side_model`）
和 health 探针里报。health 端点本体在 `obs/health.py`，挂到 UI 服务器是阶段 5 的事。

## 门禁与测试

```bash
scripts/gate.sh          # 提交前必跑：black / ruff / mypy 全量 / 单测 / CLI 冒烟 / profile 覆盖层
```

装了 s2s 引擎它连集成层一起跑；没装会明说跳过了哪层。CI 上设
`BILISAMA_GATE_REQUIRE_INTEGRATION=1` 可以把"没装"直接判失败。

真机合同测试（要求服务器在跑）：

```bash
.venv/bin/python -m pytest tests/integration/test_real_server.py -m integration -q
```

服务器没起时它们会明确跳过，不弄红门禁。上游 checkout 的版本钉在该文件的
`UPSTREAM_DESCRIBE`，上游一动测试就提醒。

## 能力位探测

对新的 realtime 端点（换模型、换实例）验四件事：单响应槽、out-of-band 豁免、
`item.truncate`、判停类型。已知结论记录在 `src/bilisama/realtime/capabilities.py`
的注释里（**能力按模型分，不只按 provider 分**——semantic_vad 在 qwen3.5-omni 有、
在 qwen-audio-3.0 没有）。探测方法：连上后发两条并发 `response.create` 看第二条
的回应，具体脚本形状参考 `tests/integration/test_real_server.py` 的写法。

## 常见坑速查

| 症状 | 原因与解法 |
|---|---|
| HF 模型下载失败 | 网络能直连就别设镜像；要镜像用 `HF_ENDPOINT=https://hf-mirror.com` |
| LLM 预热 503 | 若 curl 直连是 200，多半是代理替内网域名回的——检查 `no_proxy` 是否**单独一行** export |
| LLM 回复是空的、思考不停 | 该部署只认 `reasoning_effort=none`，配置生成脚本已带；换端点要重验 |
| `session.update` 报 Unknown event | s2s 要求 `session.type="realtime"`（计划 §3.1 表在这点上写反了）；走 codec 的 `session_patch` 不会踩 |
| 对话没反应、服务端 audio=0.00s | 喂的不是真人声（正弦波 Silero 不认）；或麦克风权限没给终端 |
| NLTK LookupError | 它的下载器把假 IP 网段当 SSRF 拦了；用 curl 手动下数据包解压到 venv 的 `nltk_data/` |
