"""Persona: human anchors, machine-grown layers, prompt assembly."""

from bilisama.persona.growth import merge_relationship, merge_voice
from bilisama.persona.loader import PersonaAnchors, PersonaStore, default_data_dir
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
    "merge_relationship",
    "merge_voice",
    "static_prefix",
]
