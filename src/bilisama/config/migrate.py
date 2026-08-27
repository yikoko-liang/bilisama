"""Bring an older `bilisama.toml` up to the shape this build reads.

Plan §7.7 asks for this before the first format change rather than after it: the
alternative is a streamer whose settings silently mean something else, and by
then the only fix is asking them what they used to have.

The machinery is also exercised against planted steps in
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

# The shipped persona was renamed, English and Chinese both, and the old
# directory went with it. Without this step an existing config still saying
# `id = "mia"` dies at startup on `FileNotFoundError: 人设文件缺失`, which is
# a rename presented as a missing file.
_RENAMED_PERSONAS = {"mia": "tofu"}


def _v1_rename_personas(raw: dict[str, Any]) -> dict[str, Any]:
    persona = raw.get("persona")
    if not isinstance(persona, dict):
        return raw
    new = _RENAMED_PERSONAS.get(str(persona.get("id", "")))
    if new is None:
        return raw
    return {**raw, "persona": {**persona, "id": new}}


# from-version -> the step that produces from-version + 1.
MIGRATIONS: dict[int, Step] = {1: _v1_rename_personas}

# What each step is worth saying out loud. A silent rewrite of somebody's
# persona id is the kind of help that reads as a bug — and the half this
# cannot do for them is the grown files, which live under the data home by the
# OLD name and would otherwise just stop being read.
_NOTES: dict[int, str] = {
    1: (
        "人设 mia 已改名 tofu（中文叫豆腐），配置里已经自动跟上。"
        "如果你攒过共同经历或口癖，它们还在数据目录的 personas/mia/ 下面，"
        "把那个目录改名成 personas/tofu/ 就能接着用。"
    )
}


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
        # Still one note per step. `_NOTES` describes the SHIPPED steps, so a
        # test's planted table gets the plain line rather than borrowing the
        # explanation of a migration it is not running.
        detail = _NOTES.get(version - 1) if steps is None else None
        line = f"配置已从 v{version - 1} 升到 v{version}"
        notes.append(f"{line}。{detail}" if detail else line)
    if "config_version" not in raw:
        # An older file predates the key. Recording it costs nothing and makes
        # the next migration's starting point explicit rather than implied.
        raw["config_version"] = version
    return raw, tuple(notes)
