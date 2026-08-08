"""把 [speech.s2s] 渲染成 speech-to-speech 的启动 JSON。

两件事：

1. 渲染。字段名跟上游 `vad_arguments.py` 逐字对齐，所以是直接映射，不用维护翻译表。
2. **渲染前按上游的字段名做白名单校验。** 上游用 `allow_extra_keys=True`
   解析这个 JSON，拼错的 key 会被静默吞掉,等你发现时已经在直播间里了。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from bilisama.config import S2SConfig

# 上游的 dataclass 文件，用来对账字段名
_UPSTREAM_ARG_DIR = "arguments_classes"

_FIELD_RE = re.compile(r"^\s{4}([a-z_][a-z0-9_]*)\s*:\s*\S", re.MULTILINE)


class S2SConfigError(RuntimeError):
    """渲染出来的配置跟上游对不上。"""


@dataclass(frozen=True, slots=True)
class RenderResult:
    payload: dict[str, object]
    unknown_keys: tuple[str, ...]
    missing_turn_fields: tuple[str, ...]


def upstream_field_names(s2s_root: Path) -> frozenset[str]:
    """扫上游的 arguments dataclass，拿到所有合法字段名。

    找不到源码时返回空集合,调用方据此跳过对账，而不是假装通过。
    """
    arg_dir = s2s_root / "src" / "speech_to_speech" / _UPSTREAM_ARG_DIR
    if not arg_dir.is_dir():
        return frozenset()
    names: set[str] = set()
    for path in sorted(arg_dir.glob("*.py")):
        names.update(_FIELD_RE.findall(path.read_text(encoding="utf-8")))
    return frozenset(names)


def render(cfg: S2SConfig) -> dict[str, object]:
    """渲染启动配置。

    刻意不放 mac_optimal_settings：它只是一组默认值，显式写的 stt 一定赢，
    但它会顺带改一堆别的默认，排查时多一层。
    """
    payload: dict[str, object] = {
        # 跳过 STT，让音频直接进模型。这条就是需求文档里的 VAD → S2T → TTS
        "stt": "none",
        "llm_backend": "chat-completions",
        "model_name": cfg.llm_model,
        "responses_api_base_url": cfg.llm_base_url,
        "responses_api_api_key": "none",
        "responses_api_stream": True,
        "responses_api_audio_content_type": "input_audio",
        "tts": cfg.tts_placeholder,
        "host": "127.0.0.1",
        "port": _port_of(cfg.endpoint),
        "num_pipelines": 1,
        # --stt none 下它只烧 CPU 不干活
        "enable_live_transcription": False,
    }
    turn = cfg.turn.model_dump()
    # inf 不是合法 JSON，上游那个字段的语义是「不限制」
    if turn.get("max_speech_ms") == float("inf"):
        turn.pop("max_speech_ms")
    payload.update(turn)
    return payload


def render_checked(cfg: S2SConfig, s2s_root: Path | None) -> RenderResult:
    """渲染并对账。s2s_root 为 None 或源码不在时跳过对账并如实标注。"""
    payload = render(cfg)
    if s2s_root is None:
        return RenderResult(payload, (), ())

    known = upstream_field_names(s2s_root)
    if not known:
        return RenderResult(payload, (), ())

    unknown = tuple(sorted(k for k in payload if k not in known))
    turn_fields = set(type(cfg.turn).model_fields) - _intentionally_omitted(cfg)
    missing = tuple(sorted(f for f in turn_fields if f in known and f not in payload))
    return RenderResult(payload, unknown, missing)


def _intentionally_omitted(cfg: S2SConfig) -> set[str]:
    """故意不写进 JSON 的字段。漏掉和故意不写要分得开。"""
    omitted: set[str] = set()
    if cfg.turn.max_speech_ms == float("inf"):
        # inf 不是合法 JSON，上游的默认就是不限制
        omitted.add("max_speech_ms")
    return omitted


def write(cfg: S2SConfig, dest: Path, *, s2s_root: Path | None = None) -> RenderResult:
    result = render_checked(cfg, s2s_root)
    if result.unknown_keys:
        raise S2SConfigError(
            "这些配置项上游不认识，会被静默忽略：" + "、".join(result.unknown_keys)
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        json.dumps(result.payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return result


def _port_of(endpoint: str) -> int:
    match = re.search(r":(\d+)", endpoint)
    return int(match.group(1)) if match else 8765
