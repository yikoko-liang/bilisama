"""Credential resolution: one function, not a system.

Config fields like `api_key_ref` never hold a secret — they hold a reference
this function turns into one. Today the only backend is environment variables,
because that is all the CLI and the stage-1 adapters need. The OS keychain
arrives with the Electron shell (plan section 6.5) behind this same signature,
which is the entire reason the seam exists now: callers written against
`resolve()` will not change when the backend does.

Reference forms:
    "env:NAME"  read the environment variable NAME
    "NAME"      shorthand for the same, tried as BILISAMA_KEY_NAME then NAME
"""

from __future__ import annotations

import os

__all__ = ["resolve"]


def resolve(ref: str) -> str | None:
    """Turn a credential reference into the credential, or None when unset.

    Args:
        ref: A reference like "env:DASHSCOPE_API_KEY" or a bare name.

    Returns:
        The secret, or None when the reference is empty or nothing is set.
        Never raises on a missing value: whether that is fatal depends on the
        caller (a validate run reports it, an adapter refuses to connect).
    """
    if not ref:
        return None
    if ref.startswith("env:"):
        return os.environ.get(ref[4:]) or None
    prefixed = os.environ.get(f"BILISAMA_KEY_{ref.upper()}")
    if prefixed:
        return prefixed
    return os.environ.get(ref) or None
