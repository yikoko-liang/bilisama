"""How long the side model really takes, and whether thinking mode is why.

The side model is the only path in the product that costs money per call and
the slowest thing in a live session by an order of magnitude: measured
2026-09-17 at 24.8 s fastest, 30.7 s median, 44.5 s slowest, plus one outright
timeout — for `max_tokens` of 45. Everything waits on it. A proactive topic
takes two such calls, one to pick the topic and one to write her line, and by
the time the words arrive the moment they were written for is thirty seconds
gone; the log for that session is a column of `proactive → cancelled@speaking`.

The suspicion this measures: `qwen3.7-flash` is a thinking model, and
`SideModelConfig.thinking` is pinned `"off"` in the PROMPT only —
side.py:44-48 says so in as many words, and that nothing in the OpenAI shape
carries it. A sentence asking a model not to think is a request. This script
asks the four ways a vendor might take it as a rule instead, against the
endpoint you actually use, and prints what each costs.

    source path.sh && .venv/bin/python tools/side_latency.py

It needs the same credentials dev-talk needs and sends a prompt the same size
as the real ghostwriter's, so the numbers are comparable to the log. Three
rounds each, because one sample of a cloud endpoint is a rumour. Nothing is
written anywhere; this only reads.
"""

from __future__ import annotations

import asyncio
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bilisama import secrets
from bilisama.config import load

# The ghostwriter's prompt is the public persona prefix plus the event rules.
# 5040 is not a guess: side.call_started logs system_len, and a live duplex
# session on 2026-09-21 shows exactly 5040 for the ghostwriter's calls (the
# 404s in the same log are the proactive topic picker, a different prompt).
#
# Counted in CHARACTERS, and the filler is three characters wide — which this
# script got wrong until 2026-09-21, multiplying the count by the filler and
# sending a prompt three times too long. A latency measurement whose input is
# 15k characters cannot be compared with a production call that sends 5k.
_SYSTEM_CHARS = 5040
_FILLER = "设定。"
_USER = "有观众问你今天播到几点。用她的口吻回一句，别超过三十个字。"
_MAX_TOKENS = 45
_ROUNDS = 3

# Four places a vendor might read it. Aliyun's OpenAI-compatible mode takes the
# top-level one for Qwen3; the others are what the same flag looks like on
# vLLM, on the OpenAI SDK's passthrough, and on a raw chat template.
_SHAPES: list[tuple[str, dict[str, Any]]] = [
    ("现在的发法（什么都不加）", {}),
    ("enable_thinking=false（顶层）", {"enable_thinking": False}),
    ("extra_body.enable_thinking", {"extra_body": {"enable_thinking": False}}),
    (
        "chat_template_kwargs.enable_thinking",
        {"chat_template_kwargs": {"enable_thinking": False}},
    ),
]


def _system_prompt() -> str:
    """A system prompt the same LENGTH as the ghostwriter's, not three times it."""
    head = "你叫豆腐，是个直播陪播。"
    return head + _FILLER * ((_SYSTEM_CHARS - len(head)) // len(_FILLER))


def _endpoint() -> tuple[str, str, str]:
    """Address, model and key, resolved the way dev-talk resolves them."""
    settings = load(Path("config/bilisama.toml"))
    cfg = settings.speech.side
    compat = os.environ.get("openai_compatible_url", "")  # noqa: SIM112
    base = cfg.base_url or compat
    # Lower case on purpose: path.sh names them that way, and matching the
    # file people actually edit beats matching the shell convention.
    named = os.environ.get("side_model_name", "")  # noqa: SIM112
    model = cfg.model or named or "qwen3.7-flash"
    # Mirrors dev_talk.py:1924-1936 branch for branch, which is the whole point
    # of this script: measuring an endpoint the product does not dial answers
    # nothing. [speech.side] wins when it is filled in; otherwise the Aliyun
    # compatible-mode fallback, whose key is `ali_api_key` — NOT OPENAI_API_KEY,
    # which is what this read before 2026-09-21 and why it could not find a
    # credential on a machine that had just run a session with one.
    if cfg.base_url:
        key = secrets.resolve(cfg.api_key_ref) or os.environ.get("OPENAI_API_KEY", "")
    else:
        key = os.environ.get("ali_api_key", "")  # noqa: SIM112
    if not base:
        raise SystemExit(
            "没有侧路模型地址。填 [speech.side] base_url，"
            "或者 source path.sh 让它读到 openai_compatible_url。"
        )
    if not key:
        raise SystemExit(
            "没有侧路模型凭据。填 [speech.side] 就 export "
            f"{cfg.api_key_ref}=...；走 path.sh 的兼容模式就要 ali_api_key。"
        )
    return base.rstrip("/"), model, key


async def _once(
    session: aiohttp.ClientSession, url: str, model: str, key: str, extra: dict[str, Any]
) -> tuple[float, int, dict[str, int]]:
    body: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": _system_prompt()},
            {"role": "user", "content": _USER},
        ],
        "max_tokens": _MAX_TOKENS,
        "stream": False,
        # The real call sends it on every request (side.py:66, pinned to "none"
        # by SideModelConfig). Leaving it off the baseline would compare a
        # request the product never makes against three that differ from it in
        # two ways at once.
        "tool_choice": "none",
        **extra,
    }
    started = time.monotonic()
    async with session.post(
        f"{url}/chat/completions", json=body, headers={"Authorization": f"Bearer {key}"}
    ) as resp:
        data = await resp.json()
    took = (time.monotonic() - started) * 1000
    if data.get("error"):
        raise RuntimeError(str(data["error"])[:160])
    said = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    usage = {k: v for k, v in (data.get("usage") or {}).items() if isinstance(v, int)}
    return took, len(said), usage


async def main() -> None:
    url, model, key = _endpoint()
    print(
        f"端点 {url}\n模型 {model}   系统提示 {len(_system_prompt())} 字"
        f"（实盘量到 {_SYSTEM_CHARS}）   max_tokens={_MAX_TOKENS}"
    )
    print(f"每种发法跑 {_ROUNDS} 轮\n")
    baseline: float | None = None
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as session:
        for label, extra in _SHAPES:
            times: list[float] = []
            chars = 0
            usage: dict[str, int] = {}
            try:
                for _ in range(_ROUNDS):
                    took, chars, usage = await _once(session, url, model, key, extra)
                    times.append(took)
                    await asyncio.sleep(0.4)
            except Exception as exc:
                print(f"  {label:<36} 不可用：{type(exc).__name__} {exc}")
                continue
            median = statistics.median(times)
            # The number that says whether the model is thinking: completion
            # tokens far above max_tokens means reasoning was billed too.
            spent = usage.get("completion_tokens", 0)
            verdict = ""
            if baseline is None:
                baseline = median
            elif median < baseline * 0.6:
                verdict = f"  ← 比现在快 {baseline / median:.1f} 倍"
            print(
                f"  {label:<36} 中位 {median:>7.0f}ms  "
                f"(最快 {min(times):.0f} 最慢 {max(times):.0f})  "
                f"出字 {chars}  completion_tokens={spent or '?'}{verdict}"
            )
    print(
        "\n读法：如果某一种明显更快，那就是思考模式在烧时间，"
        "\n     侧路模型该把这个字段发到线上，而不是只在提示词里请求它别想。"
    )


if __name__ == "__main__":
    asyncio.run(main())
