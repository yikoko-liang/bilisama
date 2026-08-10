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

import contextlib
import fcntl
import os
import re
from collections.abc import Iterator
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
        if self._live_anchor_text(name) is not None:
            return self._data_dir / f"{name}.md"
        return self._template_dir / f"{name}.md"

    def _live_anchor_text(self, name: AnchorName) -> str | None:
        """The live copy's text, or None when absent, blank or unreadable.

        Unreadable is treated like blank on purpose (B12): a permission-broken
        live file must degrade to the template, not crash the whole assembly —
        an EMPTY file already fell back gracefully, and worse states should
        not behave worse.
        """
        live = self._data_dir / f"{name}.md"
        try:
            text = live.read_text(encoding="utf-8")
        except OSError:
            return None
        return text if text.strip() else None

    def anchor(self, name: AnchorName, variables: Mapping[str, str] | None = None) -> str:
        text = self._live_anchor_text(name)
        if text is None:
            template = self._template_dir / f"{name}.md"
            try:
                text = template.read_text(encoding="utf-8")
            except OSError as exc:
                raise FileNotFoundError(
                    f"人设文件缺失：{template}。"
                    "随包模板应该在 config/personas/ 下，检查 persona id 是否拼对。"
                ) from exc
        return _substitute(text, variables or {})

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
        with self._growth_lock():
            self._write_growth_unlocked(layer, entries)

    def _write_growth_unlocked(self, layer: GrowthLayer, entries: Sequence[str]) -> None:
        body = "\n".join(f"- {entry}" for entry in entries)
        text = f"{_GROWTH_HEADERS[layer]}\n{body}\n" if body else f"{_GROWTH_HEADERS[layer]}\n"
        self.growth_path(layer).write_text(text, encoding="utf-8")

    def _growth_entries_unlocked(self, layer: GrowthLayer) -> list[str]:
        return self.growth_entries(layer)

    @contextlib.contextmanager
    def _growth_lock(self) -> Iterator[None]:
        """Advisory lock shared by every growth writer.

        `persona review` runs in its own process, typically right after a
        stream — exactly when the end-of-stream distillation writes (B7).
        Unlocked, one side's read-modify-write silently resurrects what the
        other just changed.
        """
        self._data_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self._data_dir / ".growth.lock"
        with lock_path.open("w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    # ------------------------------------------------------------ proactive

    def proactive_prompt(
        self, default_path: Path, variables: Mapping[str, str] | None = None
    ) -> str:
        """The topic-loop prompt, most specific first.

        The streamer's own copy wins, then the persona's shipped one (its
        adapted yuan — each openhanako port thinks in its own scaffold), then
        the global default under config/prompts/. Empty means none anywhere.
        """
        candidates = (
            self._data_dir / "proactive.md",
            self._template_dir / "proactive.md",
            default_path,
        )
        for path in candidates:
            if path.is_file():
                text = path.read_text(encoding="utf-8").strip()
                if text:
                    return _substitute(text, variables or {})
        return ""

    # ------------------------------------------------------------ pinned

    def pinned_text(self) -> str:
        """The streamer's pinned memory. Not a growth layer — it is the one
        deterministic write channel (plan section 4.7), file-edited by hand
        until the pin/unpin tools arrive.

        Newlines collapse to 「；」 on the way out (B15): pinned is injected
        into the dynamic tail, and a multi-line file could otherwise fake a
        section header there. The redaction pass promised by the plan ships
        with the pin/unpin tools.
        """
        path = self._data_dir / "pinned.md"
        if not path.is_file():
            return ""
        text = path.read_text(encoding="utf-8").strip()
        return re.sub(r"\s*\n+\s*", "；", text)

    # ------------------------------------------------------------ promotion

    def promote(self, layer: GrowthLayer, entry: str) -> None:
        """Move one growth entry into personality.md. Human-invoked only.

        Called from `bilisama persona review` when the streamer says yes; the
        live personality copy is created from the template on first promotion
        so the shipped template itself stays pristine.
        """
        with self._growth_lock():
            self._promote_locked(layer, entry)

    def _promote_locked(self, layer: GrowthLayer, entry: str) -> None:
        entries = self._growth_entries_unlocked(layer)
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
        self._write_growth_unlocked(layer, entries)
