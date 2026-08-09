"""UI 元数据跟 schema 的对账。

元数据搬出 schema 之后就多了一处真相要同步。没有消费者的时候，这条门禁是唯一
能防止它腐烂的东西,Electron 设置页还没开工，schema 加了字段没人会立刻发现
元数据漏了。
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from bilisama.config import UI_META, Settings
from bilisama.config._ui import Audience, Reload

# 这些字段不上设置界面：版本号是迁移用的，profile 名由下拉框单独处理
_NOT_IN_UI = {"config_version"}


def _walk(model: type[BaseModel], prefix: str = "") -> tuple[list[tuple[str, Any]], list[str]]:
    """铺平嵌套 model。

    Returns:
        (叶子字段, 容器路径)。容器指嵌套的 BaseModel 本身,它也有元数据，
        携带 provider_scoped（整段随 provider 显示隐藏）和分区标题。
    """
    leaves: list[tuple[str, Any]] = []
    containers: list[str] = []
    for name, info in model.model_fields.items():
        path = f"{prefix}.{name}" if prefix else name
        annotation = info.annotation
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            containers.append(path)
            sub_leaves, sub_containers = _walk(annotation, path)
            leaves.extend(sub_leaves)
            containers.extend(sub_containers)
        else:
            leaves.append((path, info))
    return leaves, containers


_LEAVES, _CONTAINERS = _walk(Settings)
ALL_FIELDS = dict(_LEAVES)
ALL_PATHS = set(ALL_FIELDS) | set(_CONTAINERS)


def test_every_field_has_metadata() -> None:
    """schema 加了字段但没加元数据 → 红。"""
    missing = sorted(set(ALL_FIELDS) - set(UI_META) - _NOT_IN_UI)
    assert not missing, f"这些字段没有 UI 元数据：{missing}"


def test_no_orphan_metadata() -> None:
    """元数据留了已删字段的条目 → 红。"""
    orphans = sorted(set(UI_META) - ALL_PATHS)
    assert not orphans, f"这些元数据对应的字段已经不存在了：{orphans}"


@pytest.mark.parametrize("path", sorted(UI_META))
def test_metadata_is_usable(path: str) -> None:
    """每条元数据都要能真的渲染出一个控件。"""
    meta = UI_META[path]
    assert meta.label, f"{path} 没有 label，设置界面上会显示成空白"
    assert isinstance(meta.audience, Audience)
    assert isinstance(meta.reload, Reload)


def test_numeric_fields_declare_bounds() -> None:
    """数值字段至少要有下界。

    只要下界不要上界，是因为有些字段天然没有上限（房间号、金瓜子门槛），
    强行编一个反而是错的。上界只在要渲染成滑块时才必需,那一条等 Electron
    设置页开工时再加。
    """
    unbounded: list[str] = []
    for path, info in ALL_FIELDS.items():
        if path in _NOT_IN_UI or info.annotation not in (int, float):
            continue
        marks = {type(m).__name__ for m in info.metadata}
        if not ({"Ge", "Gt"} & marks):
            unbounded.append(path)
    assert not unbounded, f"这些数值字段连下界都没有：{unbounded}"


def test_secret_fields_are_marked() -> None:
    """名字里带 key / credential / token 的字段必须标 secret。

    标了才会走钥匙串、才不会在设置界面上回显、才不会进日志。
    """
    unmarked = [
        path
        for path, meta in UI_META.items()
        if any(m in path for m in ("api_key", "credential", "token")) and not meta.secret
    ]
    assert not unmarked, f"这些字段看起来是密钥但没标 secret：{unmarked}"


def test_derived_fields_are_not_configurable() -> None:
    """标了 derived_from 的字段不该出现在 schema 里。

    §7.4 的单一写者：话痨度派生的那五个阈值只有一张查找表，不能在 TOML 里再写一份，
    否则滑块和配置文件谁赢没有定义。
    """
    derived = [path for path, meta in UI_META.items() if meta.derived_from]
    leaked = [p for p in derived if p in ALL_FIELDS]
    assert not leaked, f"这些是派生值，不该是可配置字段：{leaked}"


def test_wizard_steps_are_contiguous() -> None:
    """首次向导的步骤号不能跳。跳了说明有一步被删了却没重排。"""
    steps = sorted({m.wizard_step for m in UI_META.values() if m.wizard_step})
    assert steps == list(range(1, len(steps) + 1)), f"向导步骤不连续：{steps}"


def _visible_controls(audience: Audience) -> list[str]:
    """某一档观众实际看到几个控件。

    数控件不数路径：开关矩阵是**一个**控件，它下面那十一个开关不各占一行。
    """
    matrices = {p for p, m in UI_META.items() if m.widget == "switch_matrix"}
    return [
        path
        for path, meta in UI_META.items()
        if meta.audience is audience and not any(path.startswith(f"{m}.") for m in matrices)
    ]


def test_streamer_sees_a_manageable_number_of_controls() -> None:
    """主播视图不该超过 20 个控件。

    §7.5 的 audience 三档就是为了这个:主播只该看到十几项，其余归运营和开发。
    超了说明有字段的 audience 标错了。
    """
    controls = _visible_controls(Audience.STREAMER)
    assert len(controls) <= 20, f"主播视图有 {len(controls)} 个控件，太多了：{sorted(controls)}"


def test_developer_sees_everything() -> None:
    """开发视图是全集,三档是包含关系，不是互斥分组。"""
    audiences = {m.audience for m in UI_META.values()}
    assert audiences == set(Audience), f"有一档没人用：{set(Audience) - audiences}"
