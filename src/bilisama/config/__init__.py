"""统一配置入口（计划 §7）。

`bilisama.toml` 是唯一真相源，密钥除外（密钥在系统钥匙串）。s2s 的启动 JSON
是从这里渲染出来的产物，不手改。

这个包按职责分成五块：

- `_ui`      UI 元数据的定义（Audience / Reload / ui）
- `enums`    纯枚举,schema 和 validate 都要用
- `schema`   配置的类型与默认值
- `ui_meta`  UI 元数据，按字段路径索引
- `derive`   话痨度派生出的那五个阈值
- `validate` 跨字段校验
- `loader`   TOML 加载与覆盖层合并

对外只从这里导入,拆包是内部结构，调用方不该关心。
"""

from __future__ import annotations

from bilisama.config._ui import Audience, Reload, ui
from bilisama.config.derive import DerivedThresholds, derive
from bilisama.config.enums import Chattiness, ProviderName
from bilisama.config.loader import load
from bilisama.config.schema import (
    AudioConfig,
    AvatarConfig,
    HostedConfig,
    InteractionConfig,
    MemoryConfig,
    PersonaConfig,
    RoomConfig,
    RuntimeConfig,
    S2SConfig,
    SafetyConfig,
    Settings,
    SideModelConfig,
    SpeakSwitches,
    SpeechConfig,
    TTSConfig,
    TurnConfig,
)
from bilisama.config.ui_meta import UI_META, FieldMeta
from bilisama.config.validate import ConfigProblem, check

__all__ = [
    "UI_META",
    "Audience",
    "AudioConfig",
    "AvatarConfig",
    "Chattiness",
    "ConfigProblem",
    "DerivedThresholds",
    "FieldMeta",
    "HostedConfig",
    "InteractionConfig",
    "MemoryConfig",
    "PersonaConfig",
    "ProviderName",
    "Reload",
    "RoomConfig",
    "RuntimeConfig",
    "S2SConfig",
    "SafetyConfig",
    "Settings",
    "SideModelConfig",
    "SpeakSwitches",
    "SpeechConfig",
    "TTSConfig",
    "TurnConfig",
    "check",
    "derive",
    "load",
    "ui",
]
