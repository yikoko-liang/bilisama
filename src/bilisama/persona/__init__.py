"""Persona: human anchors, machine-grown layers, prompt assembly."""

from bilisama.persona.growth import merge_relationship, merge_voice
from bilisama.persona.loader import (
    PersonaAnchors,
    PersonaStore,
    default_data_dir,
    live_event_rules,
    live_voice_rules,
)
from bilisama.persona.prompt import (
    LIVE_RULES,
    DynamicContext,
    assemble,
    assemble_scoped,
    dynamic_tail,
    static_prefix,
)

__all__ = [
    "LIVE_RULES",
    "DynamicContext",
    "PersonaAnchors",
    "PersonaStore",
    "assemble",
    "assemble_scoped",
    "default_data_dir",
    "dynamic_tail",
    "live_event_rules",
    "live_voice_rules",
    "merge_relationship",
    "merge_voice",
    "static_prefix",
]
