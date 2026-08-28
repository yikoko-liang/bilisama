"""Reconcile UI metadata against the schema.

Moving the metadata out of the schema created a second place to keep in sync. With
the settings page not built yet, nothing else would notice a field gaining an entry
in one and not the other, so this is the only thing keeping them honest.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from bilisama.config import UI_META, Settings
from bilisama.config._ui import Audience, Reload
from bilisama.config.derive import DerivedThresholds
from bilisama.config.enums import ProviderName
from bilisama.config.ui_meta import DERIVED_META, FieldMeta, check_ui_meta

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


def test_every_derived_threshold_is_declared() -> None:
    """§7.7 gate 3 needs something to guard.

    It used to guard nothing: no entry declared `derived_from`, so the test below
    skipped every run — the only skip in the unit layer, and a gate that has
    never once been asked a question. The five names come from `derive()`, which
    is the single writer.
    """
    assert set(DERIVED_META) == {f"_derived.{name}" for name in DerivedThresholds.model_fields}
    for path, meta in DERIVED_META.items():
        assert meta.derived_from, f"{path} sits in DERIVED_META without saying what it comes from"


def test_derived_fields_are_not_configurable() -> None:
    """Derived values must not also be configurable.

    The chattiness thresholds have exactly one source. Let the TOML pin one too and
    nothing defines whether the file or the slider wins. The other half of the rule
    — that the five names are absent from the schema, where InteractionConfig
    forbids extras — is test_the_toml_cannot_pin_a_derived_threshold in
    test_derive.py.
    """
    leaked = [p for p in DERIVED_META if p.split(".", 1)[-1] in ALL_FIELDS or p in ALL_PATHS]
    assert not leaked, f"derived values must not be configurable: {leaked}"


def test_derived_entries_name_a_source_that_exists() -> None:
    """`derived_from` is a field path, not prose: `config show` prints it as the
    answer to "then where does this number come from" (cli.py)."""
    for path, meta in DERIVED_META.items():
        assert meta.derived_from in ALL_FIELDS, f"{path} derives from a field that is gone"


def test_the_derived_table_stays_out_of_the_panel_snapshot() -> None:
    """Separate dicts on purpose. `ui/server.py:149` walks every UI_META path
    through `getattr(settings, part)`, so a `_derived.*` key in there would raise
    AttributeError on the config tab rather than render a read-only row."""
    assert not set(DERIVED_META) & set(UI_META)


# ------------------------------------------------------------ the export-time gate


def test_the_completeness_gate_covers_what_it_promises() -> None:
    """§7.7 gate 1 names four keys and the numeric bounds. `group` was the one it
    never actually looked at — all 108 entries happen to carry one today, so the
    gap was invisible."""
    assert check_ui_meta() == []


@pytest.mark.parametrize(
    ("path", "meta", "expected"),
    [
        pytest.param("room.room_id", FieldMeta(label="", group="房间"), "label", id="缺 label"),
        pytest.param("room.room_id", FieldMeta(label="房间号"), "group", id="缺 group"),
        pytest.param(
            "speech.dashscope.voice",
            FieldMeta(label="音色", group="语音", provider_scoped="nope"),
            "provider_scoped",
            id="provider_scoped 名字不存在",
        ),
        pytest.param(
            "room.room_id",
            FieldMeta(label="房间号", group="房间", provider_scoped="dashscope"),
            "provider_scoped",
            id="provider_scoped 跟路径对不上",
        ),
        pytest.param(
            "interaction.burst_uniques",
            FieldMeta(label="人数", group="互动", aliases=("人数",)),
            "aliases",
            id="别名跟标签重复",
        ),
    ],
)
def test_the_gate_catches_a_planted_violation(path: str, meta: FieldMeta, expected: str) -> None:
    """Every rule is also run over a violation. A gate whose teeth are never
    exercised is one nobody can trust — same reasoning as
    tests/unit/test_dev_talk_uplink.py."""
    complaints = check_ui_meta({path: meta})
    assert complaints, f"{expected} 这条规则没有咬住"
    assert any(expected in complaint for complaint in complaints), complaints


def test_the_gate_leaves_a_complete_entry_alone() -> None:
    """A gate that fires on legal metadata is a gate someone switches off."""
    assert check_ui_meta({"room.room_id": FieldMeta(label="房间号", group="房间")}) == []


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
    """The streamer view should stay under thirty controls.

    That is the whole point of the three audience tiers. Going over means
    something is tagged for the wrong audience. The bound was twenty until the
    control-centre rework landed its interaction knobs (reply length, gift
    tiers, entry-welcome groups, noise gate, stream intro) — all genuinely
    streamer-facing, rendered as grouped cards on the system page rather than
    one flat list, so the ceiling moved with the design instead of demoting
    real controls to hide the count.

    Counted per running provider, not as a union: `ui/server.py:174` hides the
    sections belonging to backends this session is not using, so nobody ever
    sees two sets of endpoints at once. Summing them all would fail this on the
    day a fourth provider is added no matter how tidily it was tagged — and
    would go on passing if one backend alone grew a dozen streamer knobs.
    """
    scoped = {p.value for p in ProviderName}
    shared = [path for path in _visible_controls(Audience.STREAMER) if _owner(path) is None]
    for provider in scoped:
        mine = [path for path in _visible_controls(Audience.STREAMER) if _owner(path) == provider]
        total = len(shared) + len(mine)
        assert total <= 30, f"跑 {provider} 时主播能看到 {total} 个控件：{sorted(shared + mine)}"


def _owner(path: str) -> str | None:
    """Which provider's section a path sits in, or None for the shared ones."""
    return UI_META[path].provider_scoped or None


def test_provider_scoped_sections_point_at_their_own_provider() -> None:
    """`ui/server.py:174` reads this to hide the backends this session is not
    using. Most entries no longer say it out loud — `_scope_by_path` derives it
    from the path — so this is what keeps the derivation and the explicit
    declarations agreeing: a hide rule that has quietly rotted is worse than no
    hide rule.
    """
    scoped = {path: meta.provider_scoped for path, meta in UI_META.items() if meta.provider_scoped}
    assert scoped, "provider_scoped 一条都没有了——是不是删过头了"
    for path, provider in scoped.items():
        assert path.startswith(f"speech.{provider}"), f"{path} 说自己属于 {provider}"


def test_every_provider_section_is_scoped() -> None:
    """The three sections that swap with the provider, none missing.

    Left off one section and the panel would keep showing it after the switch —
    the one thing this key exists to prevent.
    """
    for provider in ProviderName:
        assert UI_META[f"speech.{provider.value}"].provider_scoped == provider.value


def test_search_aliases_add_something_to_search_by() -> None:
    """`aliases` is the search index (§7.5). An alias that repeats the label or
    the path adds a row to that index and no way to find anything."""
    for path, meta in UI_META.items():
        for alias in meta.aliases:
            assert alias and alias != meta.label, f"{path} 的别名 {alias!r} 白写了"


def test_the_audio_hints_stay_honest_about_having_no_reader() -> None:
    """Three [audio] fields are written and never read (schema.py:150-180), and
    their hints now say so. If someone wires one up, this goes red and the hint
    gets rewritten in the same change — the echo_guard hint promised "520ms 降到
    90ms" for months while nothing read the field at all.

    Textual rather than clever: `getattr(settings.audio, name)` would slip past,
    but this is a gate on prose, and the shape it does catch is the shape the
    wiring would take.
    """
    root = Path(__file__).resolve().parents[2] / "src" / "bilisama"
    readers = [
        f"{path.relative_to(root)}:{n}"
        for path in sorted(root.rglob("*.py"))
        if "config/" not in str(path.relative_to(root))
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        for field in ("audio.input_device", "audio.output_device", "audio.echo_guard")
        if field in line and not line.lstrip().startswith("#")
    ]
    assert not readers, f"[audio] 有人读了，去把 ui_meta 里那三条提示改掉：{readers}"


def test_developer_sees_everything() -> None:
    """Every tier is in use. They nest rather than partition."""
    audiences = {m.audience for m in UI_META.values()}
    assert audiences == set(Audience), f"unused audience tier: {set(Audience) - audiences}"
