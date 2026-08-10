"""Credential resolution: env today, keychain later behind the same signature."""

from __future__ import annotations

import pytest

from bilisama import secrets


def test_env_prefix_reads_the_named_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test")
    assert secrets.resolve("env:DASHSCOPE_API_KEY") == "sk-test"


def test_bare_name_prefers_the_bilisama_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    """BILISAMA_KEY_<NAME> wins over a same-named plain variable, so our keys
    cannot be shadowed by unrelated environment noise."""
    monkeypatch.setenv("BILISAMA_KEY_SIDE", "ours")
    monkeypatch.setenv("SIDE", "theirs")
    assert secrets.resolve("side") == "ours"


def test_bare_name_falls_back_to_the_plain_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BILISAMA_KEY_OPENAI", raising=False)
    monkeypatch.setenv("OPENAI", "plain")
    assert secrets.resolve("OPENAI") == "plain"


@pytest.mark.parametrize("ref", ["", "env:", "env:NOT_SET_ANYWHERE", "not_set_anywhere"])
def test_missing_or_empty_resolves_to_none_without_raising(
    ref: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing is a caller decision, not an exception: validate reports it, an
    adapter refuses to connect, neither wants a traceback from here."""
    monkeypatch.delenv("NOT_SET_ANYWHERE", raising=False)
    monkeypatch.delenv("BILISAMA_KEY_NOT_SET_ANYWHERE", raising=False)
    assert secrets.resolve(ref) is None


def test_empty_string_values_count_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMPTY_KEY", "")
    assert secrets.resolve("env:EMPTY_KEY") is None
