"""Persona files: two human-written anchors, two machine-grown layers.

The anchors (identity.md, personality.md) are read here and machine-written
nowhere — the drift lesson every surveyed framework converged on (plan
section 4.6): the one system that let agents rewrite their own persona ended
up with a community fix of marking it read-only. `promote()` below does append
to personality.md, but it only ever runs from `bilisama persona review`, the
streamer's own hand — there is no code path from the distiller to an anchor.

Reads go through a two-step fallback (ported from openhanako's lazy chain):
the live copy under the streamer's data dir wins, the shipped template under
config/personas/ backs it. Growth files live only in the data dir; templates
ship without them on purpose.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from bilisama.config.schema import PersonaConfig

__all__ = [
    "AnchorName",
    "GrowthLayer",
    "PersonaAnchors",
    "PersonaStore",
    "default_data_dir",
]

AnchorName = Literal["identity", "personality"]
GrowthLayer = Literal["relationship", "voice"]

_GROWTH_HEADERS: dict[GrowthLayer, str] = {
    "relationship": "# 共同经历",
    "voice": "# 口癖样本",
}

# Where promoted growth lines land inside personality.md, so hand-written
# personality and promoted habits stay visually separate for the streamer.
_PROMOTED_HEADER = "## 长出来的性格（persona review 合并）"


def default_data_dir(persona_id: str) -> Path:
    """The live persona directory: `<data home>/bilisama/personas/<id>`.

    Same data home the s2s engine install already uses, so a streamer looking
    for "their AI's files" finds everything under one roof.
    """
    base = os.environ.get("XDG_DATA_HOME", "")
    root = Path(base).expanduser() if base else Path.home() / ".local" / "share"
    return root / "bilisama" / "personas" / persona_id


@dataclass(frozen=True, slots=True)
class PersonaAnchors:
    """The two anchor texts, variables already substituted."""

    identity: str
    personality: str


def _substitute(text: str, variables: Mapping[str, str]) -> str:
    # Unknown {{names}} stay as-is: a typo in a template should read as a typo
    # in the prompt, not vanish silently.
    for name, value in variables.items():
        text = text.replace("{{" + name + "}}", value)
    return text


def _bullets(text: str) -> list[str]:
    """Bullet lines only; headers, blanks and stray prose are tolerated and
    dropped, because the streamer edits these files by hand."""
    out: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("- "):
            out.append(stripped[2:].strip())
    return out


class PersonaStore:
    """Reads anchors through the fallback chain; owns the growth files."""

    def __init__(self, data_dir: Path, template_dir: Path) -> None:
        self._data_dir = data_dir
        self._template_dir = template_dir

    @classmethod
    def from_config(cls, cfg: PersonaConfig, *, config_dir: Path) -> PersonaStore:
        """Build from settings. `config_dir` is the directory holding
        bilisama.toml; templates live in its `personas/<id>/`."""
        data_dir = (
            default_data_dir(cfg.id) if cfg.data_dir == "auto" else Path(cfg.data_dir).expanduser()
        )
        return cls(data_dir, config_dir / "personas" / cfg.id)

    # ------------------------------------------------------------ anchors

    def anchor_path(self, name: AnchorName) -> Path:
        """The file a read would actually use: live copy first, then template."""
        live = self._data_dir / f"{name}.md"
        if live.is_file() and live.read_text(encoding="utf-8").strip():
            return live
        return self._template_dir / f"{name}.md"

    def anchor(self, name: AnchorName, variables: Mapping[str, str] | None = None) -> str:
        path = self.anchor_path(name)
        if not path.is_file():
            raise FileNotFoundError(
                f"人设文件缺失：{path}。"
                "随包模板应该在 config/personas/ 下，检查 persona id 是否拼对。"
            )
        return _substitute(path.read_text(encoding="utf-8"), variables or {})

    def anchors(self, variables: Mapping[str, str] | None = None) -> PersonaAnchors:
        return PersonaAnchors(
            identity=self.anchor("identity", variables),
            personality=self.anchor("personality", variables),
        )

    # ------------------------------------------------------------ growth

    def growth_path(self, layer: GrowthLayer) -> Path:
        return self._data_dir / f"{layer}.md"

    def growth_entries(self, layer: GrowthLayer) -> list[str]:
        path = self.growth_path(layer)
        if not path.is_file():
            return []
        return _bullets(path.read_text(encoding="utf-8"))

    def write_growth(self, layer: GrowthLayer, entries: Sequence[str]) -> None:
        """Replace a growth file wholesale. Budgets are the caller's job
        (persona.growth merges); this only owns the file format."""
        self._data_dir.mkdir(parents=True, exist_ok=True)
        body = "\n".join(f"- {entry}" for entry in entries)
        text = f"{_GROWTH_HEADERS[layer]}\n{body}\n" if body else f"{_GROWTH_HEADERS[layer]}\n"
        self.growth_path(layer).write_text(text, encoding="utf-8")

    # ------------------------------------------------------------ pinned

    def pinned_text(self) -> str:
        """The streamer's pinned memory, verbatim. Not a growth layer — it is
        the one deterministic write channel (plan section 4.7), file-edited by
        hand until the pin/unpin tools arrive."""
        path = self._data_dir / "pinned.md"
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8").strip()

    # ------------------------------------------------------------ promotion

    def promote(self, layer: GrowthLayer, entry: str) -> None:
        """Move one growth entry into personality.md. Human-invoked only.

        Called from `bilisama persona review` when the streamer says yes; the
        live personality copy is created from the template on first promotion
        so the shipped template itself stays pristine.
        """
        entries = self.growth_entries(layer)
        if entry not in entries:
            raise ValueError(f"生长层 {layer} 里没有这条：{entry}")

        live = self._data_dir / "personality.md"
        if not live.is_file():
            self._data_dir.mkdir(parents=True, exist_ok=True)
            template = self._template_dir / "personality.md"
            live.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")

        text = live.read_text(encoding="utf-8").rstrip("\n")
        if _PROMOTED_HEADER not in text:
            text += f"\n\n{_PROMOTED_HEADER}\n"
        text += f"- {entry}\n"
        live.write_text(text, encoding="utf-8")

        entries.remove(entry)
        self.write_growth(layer, entries)
