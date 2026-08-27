"""The fixed Mia assistant and its two editable persona profiles."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from bilisama.config.schema import Settings
from bilisama.persona.loader import AnchorName, PersonaStore, default_data_dir

__all__ = [
    "PersonaProfileDefinition",
    "active_persona_store",
    "assistant_snapshot",
    "persona_profile",
    "profile_config_changes",
    "save_anchor",
]


@dataclass(frozen=True, slots=True)
class PersonaProfileDefinition:
    id: str
    name: str
    description: str


_PROFILES = (
    PersonaProfileDefinition(
        id="default",
        name="默认人设",
        description="Mia 当前的简洁伴播人设",
    ),
    PersonaProfileDefinition(
        id="modified",
        name="改动人设",
        description="更丰富的高情商元气伴播人设",
    ),
)


def persona_profile(profile_id: str) -> PersonaProfileDefinition:
    for item in _PROFILES:
        if item.id == profile_id:
            return item
    raise ValueError(f"没有「{profile_id}」这个人设方案")


def profile_config_changes(profile_id: str) -> tuple[tuple[str, str], ...]:
    """Fields changed by a live profile selection.

    Avatar and speech settings intentionally cannot enter this list: profile
    selection only refreshes Mia's prompt inside the existing voice session.
    """
    profile = persona_profile(profile_id)
    return (("persona.profile", profile.id),)


def _store(config_dir: Path, settings: Settings, profile_id: str) -> PersonaStore:
    profile = persona_profile(profile_id)
    if profile.id == "default":
        return PersonaStore.from_config(settings.persona, config_dir=config_dir)
    base = (
        default_data_dir(settings.persona.id)
        if settings.persona.data_dir == "auto"
        else Path(settings.persona.data_dir).expanduser()
    )
    return PersonaStore(
        base / "profiles" / profile.id,
        config_dir / "personas" / settings.persona.id / "profiles" / profile.id,
    )


def active_persona_store(settings: Settings, config_dir: Path) -> PersonaStore:
    """Resolve the prompt store for Mia's currently selected profile."""
    return _store(config_dir, settings, settings.persona.profile)


def assistant_snapshot(settings: Settings, config_dir: Path) -> list[dict[str, object]]:
    """Return one fixed Mia card with two independently editable profiles."""
    profiles: list[dict[str, object]] = []
    for item in _PROFILES:
        anchors = _store(config_dir, settings, item.id).anchors()
        profiles.append(
            {
                "id": item.id,
                "name": item.name,
                "description": item.description,
                "identity": anchors.identity,
                "personality": anchors.personality,
                "current": settings.persona.profile == item.id,
            }
        )
    if not any(bool(item["current"]) for item in profiles):
        profiles[0]["current"] = True
    return [
        {
            "id": "mia",
            "name": "mia",
            "description": "元气、会接梗的 AI 伴播搭子",
            "avatar": {"renderer": "tofu", "model_id": ""},
            "current": True,
            "profiles": profiles,
        }
    ]


def save_anchor(
    settings: Settings,
    config_dir: Path,
    profile_id: str,
    name: AnchorName,
    text: str,
) -> Path:
    return _store(config_dir, settings, profile_id).write_anchor(name, text)
