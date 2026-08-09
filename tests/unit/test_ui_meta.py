"""Reconcile UI metadata against the schema.

Moving the metadata out of the schema created a second place to keep in sync. With
the settings page not built yet, nothing else would notice a field gaining an entry
in one and not the other, so this is the only thing keeping them honest.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from bilisama.config import UI_META, Settings
from bilisama.config._ui import Audience, Reload

# Not surfaced as settings: the version is for migrations, and the active profile
# gets its own dropdown.
_NOT_IN_UI = {"config_version"}


def _walk(model: type[BaseModel], prefix: str = "") -> tuple[list[tuple[str, Any]], list[str]]:
    """Flatten nested models.

    Returns:
        (leaf fields, container paths). Containers are the nested models themselves,
        which carry metadata of their own — provider_scoped hides a whole section
        when the provider changes.
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
    """A field without metadata fails here."""
    missing = sorted(set(ALL_FIELDS) - set(UI_META) - _NOT_IN_UI)
    assert not missing, f"no UI metadata for: {missing}"


def test_no_orphan_metadata() -> None:
    """Metadata for a field that no longer exists fails here."""
    orphans = sorted(set(UI_META) - ALL_PATHS)
    assert not orphans, f"metadata points at fields that no longer exist: {orphans}"


@pytest.mark.parametrize("path", sorted(UI_META))
def test_metadata_is_usable(path: str) -> None:
    """Every entry must be enough to render a control."""
    meta = UI_META[path]
    assert meta.label, f"{path} has no label and would render blank"
    assert isinstance(meta.audience, Audience)
    assert isinstance(meta.reload, Reload)


def test_numeric_fields_declare_bounds() -> None:
    """Numeric fields need at least a lower bound.

    Lower only, because some genuinely have no ceiling — room ids, coin thresholds —
    and inventing one would be worse than leaving it open. Upper bounds matter for
    slider rendering and can wait until the settings page needs them.
    """
    unbounded: list[str] = []
    for path, info in ALL_FIELDS.items():
        if path in _NOT_IN_UI or info.annotation not in (int, float):
            continue
        marks = {type(m).__name__ for m in info.metadata}
        if not ({"Ge", "Gt"} & marks):
            unbounded.append(path)
    assert not unbounded, f"numeric fields with no lower bound: {unbounded}"


def test_secret_fields_are_marked() -> None:
    """Anything named like a credential must be marked secret.

    The flag is what routes it to the keychain, keeps it out of the settings UI and
    keeps it out of the logs.
    """
    unmarked = [
        path
        for path, meta in UI_META.items()
        if any(m in path for m in ("api_key", "credential", "token")) and not meta.secret
    ]
    assert not unmarked, f"these look like secrets but are not marked: {unmarked}"


def test_derived_fields_are_not_configurable() -> None:
    """Derived values must not also be configurable.

    The chattiness thresholds have exactly one source. Let the TOML pin one too and
    nothing defines whether the file or the slider wins.

    Nothing sets `derived_from` yet, so this checks an empty set and says so rather
    than passing in silence — a vacuous gate reads exactly like a satisfied one.
    What actually enforces the rule today is that the five names are absent from the
    schema and InteractionConfig forbids extras, covered by
    test_the_toml_cannot_pin_a_derived_threshold in test_derive.py. This one starts
    biting the moment a derived value gains a UI entry, which is when the marker
    becomes the only thing standing between it and a second writer.
    """
    derived = [path for path, meta in UI_META.items() if meta.derived_from]
    if not derived:
        pytest.skip(
            "§7.5: no field declares derived_from — enforcement is currently "
            "schema absence plus extra='forbid', see test_derive.py"
        )
    leaked = [p for p in derived if p in ALL_FIELDS]
    assert not leaked, f"derived values must not be configurable: {leaked}"


def test_wizard_steps_are_contiguous() -> None:
    """Wizard step numbers must not skip — a gap means a step was removed without
    renumbering."""
    steps = sorted({m.wizard_step for m in UI_META.values() if m.wizard_step})
    assert steps == list(range(1, len(steps) + 1)), f"wizard steps are not contiguous: {steps}"


def _visible_controls(audience: Audience) -> list[str]:
    """How many controls one audience actually sees.

    Counts controls, not paths: a switch matrix is one control, not eleven rows.
    """
    matrices = {p for p, m in UI_META.items() if m.widget == "switch_matrix"}
    return [
        path
        for path, meta in UI_META.items()
        if meta.audience is audience and not any(path.startswith(f"{m}.") for m in matrices)
    ]


def test_streamer_sees_a_manageable_number_of_controls() -> None:
    """The streamer view should stay under twenty controls.

    That is the whole point of the three audience tiers. Going over means something
    is tagged for the wrong audience.
    """
    controls = _visible_controls(Audience.STREAMER)
    assert len(controls) <= 20, f"streamer view has {len(controls)} controls: {sorted(controls)}"


def test_developer_sees_everything() -> None:
    """Every tier is in use. They nest rather than partition."""
    audiences = {m.audience for m in UI_META.values()}
    assert audiences == set(Audience), f"unused audience tier: {set(Audience) - audiences}"
