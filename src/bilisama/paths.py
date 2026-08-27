"""Where BiliSama keeps a streamer's files.

One roof for everything that outlives a session: personas, memory, the s2s
engine install, the UI endpoint file, logs. A streamer looking for "their AI's
files" should find them in one place, and we should compute that place once.

This function used to exist twice — persona/loader.py and ui/server.py had the
same four lines each — and the log directory would have made it three. Moved
here verbatim: the platform question (XDG only, so Windows and macOS both land
in ~/.local/share rather than their own conventions) is real and is ledger #43's
to answer, alongside packaging. Fixing it here would move a streamer's existing
persona files out from under them as a side effect of a logging change.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["data_home", "log_dir"]


def data_home() -> Path:
    """`$XDG_DATA_HOME/bilisama`, or `~/.local/share/bilisama`."""
    base = os.environ.get("XDG_DATA_HOME", "")
    root = Path(base).expanduser() if base else Path.home() / ".local" / "share"
    return root / "bilisama"


def log_dir() -> Path:
    """Where the rolling JSON log lives.

    Not created here: a caller that only wants to show the path — the panel
    does — should not leave an empty directory behind on a machine that never
    logged anything.
    """
    return data_home() / "logs"
