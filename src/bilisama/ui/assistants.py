"""The assistant page's data: every shipped persona, switchable and editable.

This branch ships four persona packages (config/personas/<id>/), each a pair
of human-written anchors with a live copy under the data home. The page lists
them as cards, switches the ACTIVE one mid-stream (persona.id is a hooked
live edit), and edits anchors through PersonaStore.write_anchor — the live
copy only, so deleting it is always a way back to the shipped text.

Deliberately NOT yiko's one-assistant × two-profiles shape: this branch
already has multiple personas, and a second axis (default/modified per
persona) buys complexity without a second capability. Switching a persona
changes prompts only — avatar and voice stay whatever the config says, the
same contract yiko's profile_config_changes kept.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from bilisama.config.schema import Settings
from bilisama.persona.loader import AnchorName, PersonaStore, template_variables

__all__ = ["assistant_snapshot", "list_personas", "save_anchor"]

# The live-rules directory lives beside the persona packages and is not one.
_NOT_A_PERSONA = {"live"}


def list_personas(config_dir: Path) -> list[str]:
    """Shipped persona ids, in name order. A package is a directory carrying
    at least identity.md; anything else under personas/ is ignored."""
    root = config_dir / "personas"
    if not root.is_dir():
        return []
    return sorted(
        entry.name
        for entry in root.iterdir()
        if entry.is_dir() and entry.name not in _NOT_A_PERSONA and (entry / "identity.md").is_file()
    )


def _store_for(settings: Settings, config_dir: Path, persona_id: str) -> PersonaStore:
    cfg = settings.persona.model_copy(update={"id": persona_id})
    if settings.persona.data_dir != "auto" and persona_id != settings.persona.id:
        # An explicit data_dir names the ACTIVE persona's live directory.
        # Reusing it for the others would write hanako's edit over tofu's
        # live copy; the non-current ones take their per-id default instead.
        cfg = cfg.model_copy(update={"data_dir": "auto"})
    return PersonaStore.from_config(cfg, config_dir=config_dir)


def _card_description(identity_text: str) -> str:
    """The first prose line after the heading, clipped for the card."""
    seen_heading = False
    for line in identity_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            seen_heading = True
            continue
        if seen_heading or not stripped.startswith("#"):
            return stripped.lstrip("-").strip()[:60]
    return ""


def assistant_snapshot(settings: Settings, config_dir: Path) -> list[dict[str, Any]]:
    """Every persona as the page renders it: current one first-class, anchors
    included so the editor never needs a second fetch.

    Two faces on purpose. The CARD (name/description) is for reading, so its
    description renders through the same {{...}} variables the live prompt
    uses — a raw {{agentName}} on the card face is a bug the streamer sees.
    The EDITOR fields (identity/personality) stay the raw template:
    substitution happens at prompt time, and what you edit is the template.

    The card name is the persona id, not the identity file's first heading:
    the shipped packages all head with {{agentName}}, and the agent's name is
    ONE handle across every persona (人设与名字是两层) — rendered, three
    cards would read identically. The id is what tells the flavors apart, and
    what the runbook calls them.
    """
    variables = template_variables(settings.persona, reply_length=settings.interaction.reply_length)
    cards: list[dict[str, Any]] = []
    for persona_id in list_personas(config_dir):
        store = _store_for(settings, config_dir, persona_id)
        try:
            identity = store.anchor("identity")
            personality = store.anchor("personality")
        except FileNotFoundError:
            continue  # half a package is not a persona
        cards.append(
            {
                "id": persona_id,
                "name": persona_id,
                "description": _card_description(store.anchor("identity", variables)),
                "identity": identity,
                "personality": personality,
                "current": persona_id == settings.persona.id,
            }
        )
    return cards


def save_anchor(
    settings: Settings, config_dir: Path, persona_id: str, anchor: AnchorName, text: str
) -> Path:
    """Persist one anchor edit to that persona's live copy.

    Raises:
        ValueError: Unknown persona, unknown anchor name, or empty text.
    """
    if persona_id not in list_personas(config_dir):
        raise ValueError(f"没有「{persona_id}」这个人设")
    if anchor not in ("identity", "personality"):
        raise ValueError(f"没有「{anchor}」这个人设文件")
    return _store_for(settings, config_dir, persona_id).write_anchor(anchor, text)
