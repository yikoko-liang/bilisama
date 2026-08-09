"""话痨度派生出的阈值。

这五个数不出现在 TOML 里，否则配置写死一个值、滑块又要改它，谁赢没有定义。
"""

from __future__ import annotations

from pydantic import BaseModel

from bilisama.config.enums import Chattiness


class DerivedThresholds(BaseModel):
    """chattiness 派生出来的五个数。

    它们**不出现在 TOML 里**,否则配置写死一个值、滑块又要改它，谁赢没有定义。
    `bilisama config show` 对这些标 derived:chattiness。
    """

    idle_threshold_s: int
    danmaku_window_s: int
    score_threshold: float
    cooldown_s: int
    max_output_tokens: int


_CHATTINESS_TABLE: dict[Chattiness, DerivedThresholds] = {
    Chattiness.LOW: DerivedThresholds(
        idle_threshold_s=180,
        danmaku_window_s=30,
        score_threshold=0.55,
        cooldown_s=20,
        max_output_tokens=70,
    ),
    Chattiness.MEDIUM: DerivedThresholds(
        idle_threshold_s=90,
        danmaku_window_s=20,
        score_threshold=0.35,
        cooldown_s=12,
        max_output_tokens=120,
    ),
    Chattiness.HIGH: DerivedThresholds(
        idle_threshold_s=45,
        danmaku_window_s=12,
        score_threshold=0.2,
        cooldown_s=5,
        max_output_tokens=120,
    ),
}


def derive(chattiness: Chattiness) -> DerivedThresholds:
    return _CHATTINESS_TABLE[chattiness]
