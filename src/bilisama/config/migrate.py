"""Bring an older `bilisama.toml` up to the shape this build reads.

Plan §7.7 asks for this before the first format change rather than after it: the
alternative is a streamer whose settings silently mean something else, and by
then the only fix is asking them what they used to have.

The table is empty today because there has only ever been one version. The
machinery is not — it is exercised against planted steps in
`tests/unit/test_config_loader.py`, because a migration path nobody has walked
is not a path.

Two things deliberately do NOT happen here:

- Nothing is written back. `load()` reads; a loader that rewrites the file it
  was handed surprises everyone the first time it runs on a copy.
- A file from the FUTURE is not refused here. That one is reported as an
  ordinary config problem (`validate.check`) so that both callers already
  handle it — `strict=False` would otherwise turn "cannot read this" into
  silence (dev_talk.py loads that way on purpose).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from bilisama.config.schema import CURRENT_VERSION
from bilisama.config.validate import ConfigError, ConfigProblem

__all__ = ["CURRENT_VERSION", "MIGRATIONS", "Step", "migrate"]

# One version's whole file in, the next version's whole file out. Steps take the
# merged mapping rather than a Settings object: the point of a migration is to
# handle shapes the current schema would refuse.
Step = Callable[[dict[str, Any]], dict[str, Any]]

# from-version -> the step that produces from-version + 1.
MIGRATIONS: dict[int, Step] = {}


def migrate(
    raw: dict[str, Any],
    *,
    steps: dict[int, Step] | None = None,
    current: int = CURRENT_VERSION,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Upgrade a parsed config to `current`, one version at a time.

    Args:
        raw: The merged TOML mapping, before schema validation.
        steps: The migration table. Defaults to the shipped one; tests pass
            their own so the machinery is covered while the table is empty.
        current: The version to arrive at.

    Returns:
        The upgraded mapping and one note per step applied, in the order they
        ran. No steps means the file was already current.

    Raises:
        ConfigError: The file's version has no way forward. Unreachable with the
            shipped table — `test_every_shipped_version_has_a_way_forward`
            is what keeps it that way — but guessing would be worse.
    """
    table = MIGRATIONS if steps is None else steps
    version = raw.get("config_version", 1)
    if not isinstance(version, int) or isinstance(version, bool):
        # Type errors have one reporter and it is the schema, which says which
        # field and in plain Chinese (cli.py:44-59). Saying it twice, differently,
        # is worse than saying it once.
        return raw, ()

    notes: list[str] = []
    while version < current:
        step = table.get(version)
        if step is None:
            raise ConfigError(
                [
                    ConfigProblem(
                        field="config_version",
                        message=f"这份配置是 v{version} 的，本机没有从 v{version} 往上升的步骤。",
                        fix="换一个装得更全的版本重试，并把这份配置文件一起附上报给我们。",
                    )
                ]
            )
        raw = step(raw)
        version += 1
        raw["config_version"] = version
        notes.append(f"配置已从 v{version - 1} 升到 v{version}")
    if "config_version" not in raw:
        # An older file predates the key. Recording it costs nothing and makes
        # the next migration's starting point explicit rather than implied.
        raw["config_version"] = version
    return raw, tuple(notes)
