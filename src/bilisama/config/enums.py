"""配置里的纯枚举。

schema 和 validate 都要用，放在这里避免两边互相 import。
"""

from __future__ import annotations

from enum import StrEnum


class ProviderName(StrEnum):
    S2S = "s2s"
    DASHSCOPE = "dashscope"
    OPENAI_GA = "openai_ga"


class Chattiness(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
