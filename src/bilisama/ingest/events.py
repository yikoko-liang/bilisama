"""直播事件模型。

**这是事件枚举的唯一定义处。** 配置的 key、UI 协议的 speak 开关、fixture 文件名
全部引用这一份，不许各写各的。

三个相对参考实现必须修正的点，每一条不改上线即坏：

1. **绝不因 uid == 0 丢弃**。B 站隐私掩码下 uid 就是 0，回退到 uid_hash 做身份
   和去重，并置 is_anonymous。N.E.K.O 那句 `if not uid: return` 会把整条弹幕流静音。
2. 现役事件是 V2（base64 protobuf），v1 只当遗留回退。
3. 第一天就要有登录态路径,匿名能连，但每个观众都是 uid 0 加 `***`，
   per-viewer 记忆、点名、per-uid 冷却全废，那等于废掉伴播的核心。
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class EventKind(StrEnum):
    """canonical 事件枚举。加一个类型要同步改 UI 协议和配置 schema。"""

    DANMAKU = "danmaku"
    GIFT = "gift"
    SUPER_CHAT = "super_chat"
    GUARD_BUY = "guard_buy"
    VIP_ENTER = "vip_enter"  # 舰长 / 高能榜 / 送过大额礼物的观众进房
    ENTRY = "entry"  # 普通观众进房
    FOLLOW = "follow"
    LIKE = "like"
    SHARE = "share"
    ROOM_STATE = "room_state"


class GuardLevel(StrEnum):
    """大航海等级。数值越小越贵，所以不用 IntEnum 免得被误当成分数。"""

    NONE = "none"
    GOVERNOR = "governor"  # 总督
    ADMIRAL = "admiral"  # 提督
    CAPTAIN = "captain"  # 舰长

    @classmethod
    def from_wire(cls, value: int) -> GuardLevel:
        return {1: cls.GOVERNOR, 2: cls.ADMIRAL, 3: cls.CAPTAIN}.get(value, cls.NONE)

    @property
    def is_patron(self) -> bool:
        return self is not GuardLevel.NONE


@dataclass(frozen=True, slots=True)
class Medal:
    name: str = ""
    level: int = 0
    up_name: str = ""
    anchor_room_id: int = 0

    @property
    def is_this_room(self) -> bool:
        return bool(self.name) and self.anchor_room_id > 0


@dataclass(frozen=True, slots=True)
class Viewer:
    """观众身份。

    uid 为 0 不代表"没有身份"，而是"平台掩码了"。这时 uid_hash 才是稳定标识。
    identity 永远返回一个可用的 key，调用方不用自己判空。
    """

    uid: int = 0
    uid_hash: str = ""  # info[0][7]，掩码时的稳定 per-room 标识
    name: str = ""  # 可能是 "***"
    face_url: str = ""
    user_level: int = 0
    wealth_level: int = 0
    guard_level: GuardLevel = GuardLevel.NONE
    is_admin: bool = False
    medal: Medal | None = None

    @property
    def is_anonymous(self) -> bool:
        return self.uid == 0

    @property
    def identity(self) -> str:
        """去重和记忆用的 key。掩码时回退到 hash，永远不返回空。"""
        if self.uid:
            return f"uid:{self.uid}"
        if self.uid_hash:
            return f"hash:{self.uid_hash}"
        return "anon"

    @property
    def display_name(self) -> str:
        return self.name or "一位观众"


@dataclass(frozen=True, slots=True)
class Gift:
    gift_id: int = 0
    name: str = ""
    num: int = 1
    coin_type: str = ""  # gold | silver | ""
    total_coin: int = 0  # 1000 金瓜子 = 1 元
    combo_id: str = ""
    combo_count: int = 0
    combo_end: bool | None = None
    aggregated_count: int = 1  # >1 表示多条轻礼物被合并了

    @property
    def is_paid(self) -> bool:
        return self.coin_type == "gold" and self.total_coin > 0


@dataclass(frozen=True, slots=True)
class LiveEvent:
    """一种结构装下所有事件类型。

    raw 只供调试，**绝对不能进 LLM prompt**,那是未经清洗的平台原始负载。
    """

    kind: EventKind
    room_id: int = 0  # 真实房间号，不是短号
    viewer: Viewer = field(default_factory=Viewer)
    text: str = ""  # 弹幕或 SC 正文；礼物为空
    gift: Gift | None = None
    value_cny: float = 0.0  # 统一货币口径
    event_id: str = ""  # 去重主键
    ts_ms: int = 0  # 平台时间戳
    recv_at: float = 0.0  # 本地单调时钟
    session_generation: int = 0  # 重连后作废迟到事件
    raw: dict[str, Any] | None = None

    @property
    def is_anonymous(self) -> bool:
        return self.viewer.is_anonymous

    @property
    def dedup_key(self) -> str:
        """去重键。event_id 优先，没有就用身份加内容凑一个。"""
        if self.event_id:
            return f"{self.kind}:{self.event_id}"
        return f"{self.kind}:{self.viewer.identity}:{self.text[:32]}:{self.ts_ms // 1000}"

    @property
    def is_paid(self) -> bool:
        return self.value_cny > 0

    def redacted(self) -> LiveEvent:
        """去掉 raw 的副本。进任何会流向模型的地方之前都过一次。

        用 replace 而不是手抄字段：以后给 LiveEvent 加字段时不会漏，
        漏了的话新字段会被静默清成默认值，而这个方法的调用场景恰恰
        是「进 prompt 之前」，丢字段没有任何报错。
        """
        if self.raw is None:
            return self
        return dataclasses.replace(self, raw=None)


def cny_from_gold(total_coin: int) -> float:
    """金瓜子换算成元。1000 金瓜子 = 1 元。"""
    return total_coin / 1000.0


def is_vip_entry(viewer: Viewer, *, lifetime_gift_cny: float = 0.0) -> bool:
    """进房该不该点名欢迎。

    舰长以上、或者历史送过钱的算 VIP,这两类进 L2 的付费车道；
    普通观众进房归 L4，默认只上字幕不发声。

    Args:
        viewer: 进房的观众。
        lifetime_gift_cny: 这个人历史累计送了多少钱，由记忆层查出来。

    Returns:
        True 表示值得点名欢迎。
    """
    return viewer.guard_level.is_patron or lifetime_gift_cny > 0
