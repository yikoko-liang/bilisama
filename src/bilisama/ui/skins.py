"""The skin picker's data: which packs exist, and where each one lives.

Mirrors ui/assistants.py's shape — pure functions over directories, a marker
file to qualify as a package, a denylist for directories that are not what
they sit next to. Discovery here, loading stays in the page: the front end
fetches ``skins/<id>/pet.json`` (user) before ``assets/skins/<id>/pet.json``
(packaged), which is also why the same id resolves to the USER pack in the
listing — with one exception, "tofu": asking for the built-in by name means
the packaged copy (renderer.js packagedOnly), so the listing never attributes
it to a user directory either.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

__all__ = ["list_skin_packs", "packaged_skins_root"]

# The built-in default; always listed, always first, never shadowed.
_BUILTIN = "tofu"

# Packaged directories that must not appear in the picker. kirby is an
# internal preview whose licensing has not been re-assessed for release
# (ledger #31) — `--skin kirby` and a hand-edited model_id still work, the
# picker just does not advertise it.
_UNLISTED = {"kirby"}

# What makes a directory a skin pack — the same file the page's fetch race
# resolves (skins/sprite.js).
_MARKER = "pet.json"


def packaged_skins_root() -> Path:
    """The in-repo packs, served under ``assets/skins/``."""
    return Path(__file__).resolve().parent / "web" / "skins"


def _pack_ids(root: Path | None) -> list[str]:
    if root is None or not root.is_dir():
        return []
    return sorted(
        entry.name for entry in root.iterdir() if entry.is_dir() and (entry / _MARKER).is_file()
    )


def list_skin_packs(packaged_root: Path, user_root: Path | None) -> list[dict[str, Any]]:
    """Every pack the picker may offer: built-in tofu first, then name order.

    A user pack with a packaged pack's name wins the listing (the page's
    fetch order already loads it), except "tofu" which stays builtin by the
    packagedOnly rule. Unlisted packaged packs (see _UNLISTED) are skipped;
    a USER pack that happens to share such a name is the streamer's own
    asset and lists normally.
    """
    user = set(_pack_ids(user_root))
    packaged = [
        name for name in _pack_ids(packaged_root) if name not in _UNLISTED and name != _BUILTIN
    ]
    cards: list[dict[str, Any]] = [{"id": _BUILTIN, "source": "builtin"}]
    for name in sorted(set(packaged) | user):
        if name == _BUILTIN:
            continue  # the built-in card above already covers it
        cards.append({"id": name, "source": "user" if name in user else "builtin"})
    return cards
