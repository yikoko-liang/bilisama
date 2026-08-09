"""s2s 启动配置的渲染与对账。

最后那个 test_turn_fields_match_upstream 是计划 §7.7 的 CI 门禁之一：
它同时守住「判停参数全量透传一个不落」和「拼错的 key 会被静默吞掉」两个承诺。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from bilisama.bootstrap import s2s_launch
from bilisama.config import S2SConfig, TurnConfig

# 上游检出通常是 BiliSama 的兄弟目录。环境变量优先，便于 CI 指到别处。
_REPO_ROOT = Path(__file__).resolve().parents[2]
S2S_ROOT = Path(os.environ.get("BILISAMA_S2S_ROOT", _REPO_ROOT.parent / "speech-to-speech"))


def _cfg(**kw: object) -> S2SConfig:
    return S2SConfig(llm_model="our-s2t-v1", **kw)


def test_render_skips_stt_and_pins_chat_completions() -> None:
    payload = s2s_launch.render(_cfg())
    assert payload["stt"] == "none"
    assert payload["llm_backend"] == "chat-completions"
    assert payload["num_pipelines"] == 1
    # --stt none 下它只烧 CPU
    assert payload["enable_live_transcription"] is False


def test_render_never_sets_mac_optimal_settings() -> None:
    # 它会顺带改一堆别的默认，排查时多一层
    assert "mac_optimal_settings" not in s2s_launch.render(_cfg())


def test_render_drops_infinite_max_speech() -> None:
    # inf 不是合法 JSON；上游那个字段的语义就是「不限制」
    assert "max_speech_ms" not in s2s_launch.render(_cfg())
    payload = s2s_launch.render(_cfg(turn=TurnConfig(max_speech_ms=30_000)))
    assert payload["max_speech_ms"] == 30_000


def test_render_carries_every_turn_field() -> None:
    payload = s2s_launch.render(_cfg())
    for name in TurnConfig.model_fields:
        if name == "max_speech_ms":
            continue  # 默认是 inf，上面单独测
        assert name in payload, f"判停参数 {name} 没有透传下去"


def test_port_parsed_from_endpoint() -> None:
    assert s2s_launch.render(_cfg(endpoint="ws://127.0.0.1:9999/v1/realtime"))["port"] == 9999
    assert s2s_launch.render(_cfg(endpoint="ws://127.0.0.1/v1/realtime"))["port"] == 8765


def test_write_rejects_unknown_keys(tmp_path: Path) -> None:
    class Bogus(S2SConfig):
        pass

    original = s2s_launch.render

    def patched(cfg: S2SConfig) -> dict[str, object]:
        payload = original(cfg)
        payload["totally_made_up_flag"] = True
        return payload

    s2s_launch.render = patched
    try:
        with pytest.raises(s2s_launch.S2SConfigError, match="上游不认识"):
            s2s_launch.write(_cfg(), tmp_path / "c.json", s2s_root=S2S_ROOT)
    finally:
        s2s_launch.render = original


@pytest.mark.skipif(not S2S_ROOT.exists(), reason="本地没有 speech-to-speech 检出")
def test_turn_fields_match_upstream() -> None:
    """CI 门禁：我们的判停字段名必须跟上游 vad_arguments.py 逐字对上。

    上游改了字段名，这条会红；我们拼错了，这条也会红。
    """
    known = s2s_launch.upstream_field_names(S2S_ROOT)
    assert known, "没能从上游源码里扫出任何字段名，对账形同虚设"

    ours = set(TurnConfig.model_fields)
    unknown = ours - known
    assert not unknown, f"这些字段上游不认识，会被静默吞掉：{sorted(unknown)}"

    # 反向：上游 VAD 那个 dataclass 里我们漏了哪些
    vad_file = S2S_ROOT / "src/speech_to_speech/arguments_classes/vad_arguments.py"
    upstream_vad = set(s2s_launch._FIELD_RE.findall(vad_file.read_text(encoding="utf-8")))
    # 这两个由 module_arguments 覆盖，我们不透传
    upstream_vad -= {"enable_realtime_transcription", "realtime_processing_pause"}
    missing = upstream_vad - ours
    assert not missing, f"上游新增了判停参数但我们没跟：{sorted(missing)}"


@pytest.mark.skipif(not S2S_ROOT.exists(), reason="本地没有 speech-to-speech 检出")
def test_render_checked_reports_clean() -> None:
    result = s2s_launch.render_checked(_cfg(), S2S_ROOT)
    assert result.unknown_keys == ()
    assert result.missing_turn_fields == ()
