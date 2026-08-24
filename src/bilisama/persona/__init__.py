"""Persona: human anchors, machine-grown layers, prompt assembly."""

from bilisama.persona.growth import merge_relationship, merge_voice
from bilisama.persona.loader import (
    PersonaAnchors,
    PersonaStore,
    default_data_dir,
    live_event_rules,
)
from bilisama.persona.prompt import (
    LIVE_RULES,
    DynamicContext,
    assemble,
    dynamic_tail,
    static_prefix,
)

__all__ = [
    "LIVE_RULES",
    "DynamicContext",
    "PersonaAnchors",
    "PersonaStore",
    "assemble",
    "default_data_dir",
    "dynamic_tail",
    "live_event_rules",
    "merge_relationship",
    "merge_voice",
    "static_prefix",
]
