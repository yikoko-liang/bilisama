"""UI 元数据。

Electron 的设置界面从这份 schema 生成，不手写表单。加一个配置项只改一处。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class Audience(StrEnum):
    """谁该看见这个字段。主播只该看到十几项，开发能看到全部。"""

    STREAMER = "streamer"
    OPERATOR = "operator"
    DEVELOPER = "developer"


class Reload(StrEnum):
    """改了之后什么时候生效。UI 据此决定直播中要不要置灰这个控件。"""

    LIVE = "live"  # 立刻生效
    RECONNECT = "reconnect"  # 需要重连语音链路
    ENGINE = "engine"  # 需要重启 P3'
    RESTART = "restart"  # 需要重启整个应用


def ui(
    *,
    label: str,
    help: str = "",
    audience: Audience = Audience.DEVELOPER,
    reload: Reload = Reload.RESTART,
    group: str = "",
    order: int = 0,
    unit: str = "",
    widget: str = "",
    provider_scoped: str = "",
    derived_from: str = "",
    secret: bool = False,
    wizard_step: int = 0,
    aliases: tuple[str, ...] = (),
) -> dict[str, Any]:
    """给字段挂 UI 元数据。

    控件类型默认从 schema 结构推导（布尔→开关、有界数值→滑块、枚举→下拉），
    只有推导不出来时才显式给 widget。
    """
    return {
        "ui": {
            "label": label,
            "help": help,
            "audience": audience.value,
            "reload": reload.value,
            "group": group,
            "order": order,
            "unit": unit,
            "widget": widget,
            "provider_scoped": provider_scoped,
            "derived_from": derived_from,
            "secret": secret,
            "wizard_step": wizard_step,
            "aliases": list(aliases),
        }
    }
