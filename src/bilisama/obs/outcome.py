"""发言尝试的终局协议。

「为什么小助手刚才没说话」是直播产品的头号支持问题。答案不能靠翻日志猜，
所以每条 Intent 从产生到消失，最终必然落到一个 (outcome, phase) 上，
控制面板的事件流直接显示。

读法：``skipped@gated`` 是闸门没放行，``cancelled@speaking`` 是说到一半被主播打断，
``expired@queued`` 是排太久过期了。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Outcome(StrEnum):
    SPOKEN = "spoken"  # 说出来了，观众听到了
    SKIPPED = "skipped"  # 没派发就被丢弃
    CANCELLED = "cancelled"  # 派发了但被取消（打断、抢占、panic）
    FAILED = "failed"  # provider 或工具报错
    EXPIRED = "expired"  # 过了 expires_at 还没轮到
    TIMED_OUT = "timed_out"  # 看门狗超时


class Phase(StrEnum):
    """终局发生在哪一步。跟 Outcome 组合起来才有意义。"""

    SELECTED = "selected"  # 刚被 L4 选中，还没进调度器
    QUEUED = "queued"  # 在优先级堆里
    GATED = "gated"  # 被说话权闸门挡住
    DISPATCHED = "dispatched"  # 已发给 provider，等首个 delta
    GENERATING = "generating"  # 模型在吐字
    SPEAKING = "speaking"  # 音频在播
    PLAYED = "played"  # 播完了


class SkipReason(StrEnum):
    """skipped / expired 时的稳定原因串。

    这些字符串会出现在控制面板上，也会被聚合成统计，所以只能加不能改。
    """

    LOW_VALUE = "selection.low_value"
    DUPLICATE = "selection.duplicate"
    RATE_LIMITED = "selection.rate_limited"
    QUEUE_FULL = "selection.queue_full"
    SPEAK_DISABLED = "policy.speak_disabled"
    HOST_SPEAKING = "gate.host_speaking"
    TURN_PENDING = "gate.turn_pending"
    AUDIO_QUEUED = "gate.audio_queued"
    INJECTION_GATE = "gate.injection_window"
    COOLDOWN = "gate.cooldown"
    PREEMPTED = "scheduler.preempted"
    RESULT_EXPIRED = "background.result_expired"
    PANIC_MUTE = "policy.panic_mute"
    OUTPUT_BLOCKED = "safety.output_blocked"


@dataclass(frozen=True, slots=True)
class Verdict:
    """一条 Intent 的终局。调度器在丢弃或完成时产出，永远恰好产出一次。"""

    intent_id: str
    source: str
    outcome: Outcome
    phase: Phase
    reason: SkipReason | None = None
    detail: str = ""
    waited_s: float = 0.0
    spoken_ms: int = 0

    def __str__(self) -> str:
        base = f"{self.outcome}@{self.phase}"
        return f"{base}({self.reason})" if self.reason else base
