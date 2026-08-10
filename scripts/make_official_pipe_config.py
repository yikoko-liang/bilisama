#!/usr/bin/env python3
"""Render the launch config for the official three-stage pipeline test.

This is a test fixture, not the product path: the product renderer
(`bilisama.bootstrap.s2s_launch`) only knows stt=none, and deliberately does
not grow options for this (plan section 17.5 E). The official pipeline —
VAD -> paraformer STT -> a hosted chat-completions LLM -> Qwen3-TTS — exists
to prove the server genuinely starts and behaves, before our own model is
ready to slot in.

Credentials come from path.sh at the repo root (gitignored):

    export base_url="https://.../v4/"
    export api_key="..."
    export model_name="..."

The key never lands in the JSON: the caller must export it as OPENAI_API_KEY,
which upstream's OpenAI client reads natively. This script refuses to write a
config at all when the key is missing, so a server without credentials fails
here rather than mid-conversation.

Turn-detection values are read from config/bilisama.toml so the test server
runs the same tuned endpointing as the product path (1200/400/2 rather than
upstream defaults).

Usage (the split-tunnel case: a system proxy owns DNS, the LLM endpoint
lives on a corp VPN — verified working 2026-08-10 with Shadowrocket untouched):

    source path.sh && export OPENAI_API_KEY="$api_key"
    .venv/bin/python scripts/make_official_pipe_config.py
    export BILISAMA_RESOLVE="llmapi.bilibili.co=<真实内网IP>"   # 进程内钉死域名
    export NO_PROXY="llmapi.bilibili.co,localhost,127.0.0.1,::1,.local"
    export no_proxy="$NO_PROXY"    # 单独一行：httpx 优先读小写，同行双赋值拿到旧值
    BILISAMA_S2S_CONFIG=config/s2s/official-pipe.local.json \
      scripts/smoke_provider_b.sh serve

Models are all cached after the first run; only the LLM call needs a network.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "config" / "s2s" / "official-pipe.local.json"

sys.path.insert(0, str(REPO / "src"))

from bilisama.config import load  # noqa: E402


def main() -> int:
    # Lowercase on purpose: these names come from the user's own path.sh and
    # renaming them here would break that file. SIM112 wants BASE_URL.
    base_url = os.environ.get("base_url", "")  # noqa: SIM112
    model_name = os.environ.get("model_name", "")  # noqa: SIM112
    missing = [name for name, v in (("base_url", base_url), ("model_name", model_name)) if not v]
    if missing:
        print(f"缺环境变量：{'、'.join(missing)}。先 source path.sh 再跑。", file=sys.stderr)
        return 2
    if not os.environ.get("OPENAI_API_KEY"):
        print(
            '缺 OPENAI_API_KEY。跑：source path.sh && export OPENAI_API_KEY="$api_key"。\n'
            "key 只走环境变量，这个脚本不会把它写进任何文件。",
            file=sys.stderr,
        )
        return 2

    settings = load(REPO / "config" / "bilisama.toml")
    turn = settings.speech.s2s.turn.model_dump()
    # inf is not valid JSON; upstream reads a missing value as "no limit".
    if turn.get("max_speech_ms") == float("inf"):
        turn.pop("max_speech_ms")

    payload: dict[str, object] = {
        # The official cascade, Chinese end to end: paraformer hears the
        # streamer, the hosted LLM answers, Qwen3-TTS speaks.
        "stt": "paraformer",
        "llm_backend": "chat-completions",
        "model_name": model_name,
        "responses_api_base_url": base_url,
        "responses_api_stream": True,
        # This gateway ignores chat_template_kwargs (upstream's default lever)
        # and honours reasoning_effort instead; verified live on 2026-08-10.
        # Without it deepseek replies arrive as reasoning_content and the
        # pipeline reads an empty content stream.
        "responses_api_reasoning_effort": "none",
        "tts": "qwen3",
        # The CustomVoice model generates UNCONDITIONED when no speaker reaches
        # it — a different random voice per reply (measured live 2026-08-11:
        # same sentence twice, median F0 240 Hz vs 276 Hz). The arguments-class
        # default is None despite its help text naming "Aiden", so the speaker
        # must be pinned here. Truth lives in bilisama.toml ([speech.s2s]
        # server_tts_speaker); the tts_speaker env var stays a per-run
        # override. Supported names sit
        # in the model config's talker_config.spk_id: serena, vivian, uncle_fu,
        # ryan, aiden, ono_anna, sohee, eric (四川话), dylan (北京话).
        "qwen3_tts_speaker": os.environ.get(
            "tts_speaker", settings.speech.s2s.server_tts_speaker  # noqa: SIM112
        ),
        "host": "127.0.0.1",
        "port": 8765,
        "num_pipelines": 1,
        **turn,
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已写入 {OUT.relative_to(REPO)}（key 不在里面，走 OPENAI_API_KEY）")
    print("起服务：")
    print("  HF_ENDPOINT=https://hf-mirror.com \\")
    print(f"  BILISAMA_S2S_CONFIG={OUT.relative_to(REPO)} \\")
    print("    scripts/smoke_provider_b.sh serve")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
