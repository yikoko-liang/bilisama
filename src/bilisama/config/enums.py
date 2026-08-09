"""Plain enums shared by schema and validate.

They live here so those two modules do not have to import each other.
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
