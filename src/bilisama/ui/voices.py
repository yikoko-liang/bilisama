"""The voice picker's data: what the current provider lets a streamer change.

Same contract as ui/skins.py — pure assembly over Settings, shipped to the
page on panel.state, no new event frames. The page renders one of three
shapes and never guesses at provider rules itself:

- ``select``: a vetted list rides along (DashScope's fifteen, volcano O2.0's
  four officials) and the edit goes through the shared config channel.
- ``text``: there is a live path but no list to offer — volcano SC2.0 takes
  account-specific cloned ids, so the page shows a free field with the
  prefix rule spelled out.
- ``none``: nothing to offer. The s2s engine's voice is fixed at start
  (ENGINE reload), and OpenAI GA has no vetted list yet.
"""

from __future__ import annotations

from typing import Any

from bilisama.config.enums import ProviderName
from bilisama.config.schema import Settings
from bilisama.config.voices import DASHSCOPE_VOICES, VOLCANO_OFFICIAL_SPEAKERS

__all__ = ["voice_options"]


def voice_options(settings: Settings) -> dict[str, Any]:
    """What the「形象与声音」card's voice half should render right now."""
    provider = settings.speech.provider
    if provider is ProviderName.DASHSCOPE:
        return {
            "mode": "select",
            "path": "speech.dashscope.voice",
            "current": settings.speech.dashscope.voice,
            "options": [{"id": vid, "hint": f"{hz}Hz"} for vid, hz in DASHSCOPE_VOICES],
            "hint": "数字是实测基频，越小越低沉；换完下一句就是新嗓子",
        }
    if provider is ProviderName.VOLCANO:
        if settings.speech.volcano.model == "1.2.1.1":
            return {
                "mode": "select",
                "path": "speech.volcano.speaker",
                "current": settings.speech.volcano.speaker,
                "options": [{"id": speaker, "hint": ""} for speaker in VOLCANO_OFFICIAL_SPEAKERS],
                "hint": "正在说的那句说完后切换",
            }
        return {
            "mode": "text",
            "path": "speech.volcano.speaker",
            "current": settings.speech.volcano.speaker,
            "hint": "SC2.0 只认克隆音色（saturn_ / ICL_ / 自己的 S_ 开头）；正在说的那句说完后切换",
        }
    if provider is ProviderName.S2S:
        return {"mode": "none", "hint": "本地引擎的音色在启动时就定了，直播中换不了"}
    return {"mode": "none", "hint": "这个语音服务还没有可选的音色清单"}
