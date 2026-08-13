"""tools/skin_pack.py: the build-side contract for sprite skin packs.

The renderer validates every pack at load time and silently degrades to the
built-in skin on any violation — so the builder must catch the same problems
at build time, with the numbers in the error. These tests pin that promise:
mapping errors, the renderer's size caps, (file, flip) deduplication and the
feet-on-the-floor cell alignment.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
from skin_pack import STANDARD_TRACKS, MappingError, build_pack


def _frames_dir(tmp_path: Path, sizes: dict[str, tuple[int, int]] | None = None) -> Path:
    frames = tmp_path / "frames"
    frames.mkdir()
    for name, (w, h) in (sizes or {"a.png": (16, 16), "b.png": (16, 16)}).items():
        Image.new("RGBA", (w, h), (255, 0, 0, 255)).save(frames / name)
    return frames


def _mapping(tmp_path: Path, animations: dict[str, Any]) -> Path:
    path = tmp_path / "mapping.json"
    path.write_text(json.dumps({"animations": animations}), encoding="utf-8")
    return path


def _full_mapping(spec: dict[str, Any] | None = None) -> dict[str, Any]:
    base: dict[str, Any] = {
        name: {"frames": [{"file": "a.png"}], "fps": 4} for name in STANDARD_TRACKS
    }
    if spec:
        base.update(spec)
    return base


# ------------------------------------------------------------ happy path


def test_build_dedups_file_flip_pairs_and_emits_a_valid_manifest(tmp_path: Path) -> None:
    frames = _frames_dir(tmp_path)
    mapping = _mapping(
        tmp_path,
        _full_mapping(
            {
                "idle": {"frames": [{"file": "a.png"}, {"file": "b.png"}], "fps": 2},
                # The same file flipped is its own cell; repeated uses reuse it.
                "running-right": {
                    "frames": [{"file": "a.png", "flip": True}, {"file": "a.png", "flip": True}],
                    "fps": 4,
                },
                "jumping": {"frames": [{"file": "b.png"}], "fps": 5, "loop": False},
            }
        ),
    )
    manifest_path = build_pack(frames, mapping, tmp_path / "out")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    # cells: a, b, a-flipped -> 3 unique
    assert manifest["frame"] == {"width": 16, "height": 16, "columns": 8, "rows": 1}
    sheet = Image.open(tmp_path / "out" / "spritesheet.png")
    assert sheet.size == (16 * 8, 16)
    tracks = manifest["animations"]
    assert set(tracks) == set(STANDARD_TRACKS)
    assert tracks["running-right"]["frames"] == [2, 2]
    assert tracks["jumping"]["loop"] is False


def test_mixed_frame_heights_land_feet_on_the_cell_floor(tmp_path: Path) -> None:
    frames = _frames_dir(tmp_path, {"tall.png": (10, 20), "short.png": (10, 10)})
    animations: dict[str, Any] = {
        name: {"frames": [{"file": "tall.png"}], "fps": 4} for name in STANDARD_TRACKS
    }
    animations["idle"] = {"frames": [{"file": "tall.png"}, {"file": "short.png"}], "fps": 2}
    mapping = _mapping(tmp_path, animations)
    build_pack(frames, mapping, tmp_path / "out")
    sheet = Image.open(tmp_path / "out" / "spritesheet.png")
    # Cell is 10x20. The short frame must sit at the BOTTOM of cell 1: its top
    # half transparent, its bottom half painted — a centered paste would bob
    # the walk cycle against the ground line.
    top = sheet.getpixel((15, 2))
    bottom = sheet.getpixel((15, 15))
    assert isinstance(top, tuple) and top[3] == 0  # top of the short cell: empty
    assert isinstance(bottom, tuple) and bottom[3] == 255  # bottom: painted


# ------------------------------------------------------------ mapping errors


def test_missing_standard_tracks_are_refused_with_their_names(tmp_path: Path) -> None:
    frames = _frames_dir(tmp_path)
    animations = _full_mapping()
    del animations["attention"]
    with pytest.raises(MappingError, match="attention"):
        build_pack(frames, _mapping(tmp_path, animations), tmp_path / "out")


@pytest.mark.parametrize(
    ("broken", "match"),
    [
        ({"idle": []}, "要是对象"),
        ({"idle": {"frames": []}}, "非空数组"),
        ({"idle": {"frames": [{"file": "a.png"}], "fps": "fast"}}, "不是数字"),
        ({"idle": {"frames": [{"file": "a.png"}], "fps": 500}}, "fps 非法"),
        ({"idle": {"frames": [{"file": "missing.png"}]}}, "不存在"),
        ({"idle": {"frames": [{"file": "a.png"}], "fallback": "nope"}}, "不存在的轨道"),
    ],
)
def test_broken_track_specs_become_mapping_errors(
    tmp_path: Path, broken: dict[str, Any], match: str
) -> None:
    """Malformed input draws a MappingError with the fix, never a traceback."""
    frames = _frames_dir(tmp_path)
    with pytest.raises(MappingError, match=match):
        build_pack(frames, _mapping(tmp_path, _full_mapping(broken)), tmp_path / "out")


# ------------------------------------------------------------ renderer caps


def test_oversized_frames_are_refused_at_build_time(tmp_path: Path) -> None:
    """The renderer caps frames at 512px; a pack past that would "build fine"
    and then silently degrade to the built-in skin at load time."""
    frames = _frames_dir(tmp_path, {"a.png": (520, 100), "b.png": (16, 16)})
    with pytest.raises(MappingError, match="512"):
        build_pack(frames, _mapping(tmp_path, _full_mapping()), tmp_path / "out")


def test_shipped_packs_still_build_clean(tmp_path: Path) -> None:
    """The committed tofu pack passes its own builder's validation — the caps
    added later must not have outlawed what we ship."""
    manifest = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "src"
            / "bilisama"
            / "ui"
            / "web"
            / "skins"
            / "tofu"
            / "pet.json"
        ).read_text(encoding="utf-8")
    )
    frame = manifest["frame"]
    assert frame["width"] <= 512 and frame["height"] <= 512
    assert frame["rows"] <= 32 and frame["columns"] <= 32
    assert set(STANDARD_TRACKS) <= set(manifest["animations"])
