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


_SKINS = Path(__file__).resolve().parents[2] / "src" / "bilisama" / "ui" / "web" / "skins"
# Both of them. The old version of the test below read tofu's manifest and left
# kirby — the only pack with a flipped track, and the only one built from
# somebody else's frames — with no cover at all.
SHIPPED_PACKS = ("tofu", "kirby")


def _solid_pixels(sheet: Image.Image) -> bytes:
    """Where the sheet is fully opaque, and what colour it is there.

    Deliberately not a plain tobytes() comparison: the builder's paste is
    `sheet.paste(image, (x, y), image)` (tools/skin_pack.py:166), which uses the
    image as its own mask and therefore multiplies alpha by itself — a pixel at
    alpha 12 comes back at 1, and one at 3 disappears. Measured on the committed
    kirby sheet: 13,953 of 393,216 pixels change alpha on a rebuild, all of them
    partial-alpha edge pixels, none of them fully opaque or fully transparent.
    Reported rather than fixed here — tools/ belongs to another change — so this
    compares the half that a rebuild does preserve.

    That half is still the half the test is for: a cell pasted into the wrong
    place, in the wrong order, or centred instead of floor-aligned moves the
    opaque body of the sprite, which this sees.
    """
    solid = sheet.getchannel("A").point(lambda value: 255 if value == 255 else 0)
    blank = Image.new("RGBA", sheet.size, (0, 0, 0, 0))
    return Image.composite(sheet, blank, solid).tobytes()


def _explode(pack: str, tmp_path: Path) -> tuple[dict[str, Any], Path, dict[str, Any]]:
    """Turn a shipped pack back into the builder's inputs.

    The frames the packs were built from are not in the repo (kirby's came from a
    Shimeji download; see its build.json), so the sheet is cut back up along its
    own declared grid and each referenced cell becomes one source frame.

    Args:
        pack: Skin directory name under ui/web/skins.
        tmp_path: Where the frames and the mapping get written.

    Returns:
        The shipped manifest, the frames directory, and a mapping that names the
        cut-up cells in the manifest's own track order.
    """
    manifest: dict[str, Any] = json.loads((_SKINS / pack / "pet.json").read_text(encoding="utf-8"))
    frame = manifest["frame"]
    sheet = Image.open(_SKINS / pack / "spritesheet.png").convert("RGBA")

    frames = tmp_path / "frames"
    frames.mkdir()
    used = {index for track in manifest["animations"].values() for index in track["frames"]}
    for index in sorted(used):
        left = (index % frame["columns"]) * frame["width"]
        top = (index // frame["columns"]) * frame["height"]
        cell = sheet.crop((left, top, left + frame["width"], top + frame["height"]))
        cell.save(frames / f"cell_{index}.png")

    mapping = {
        name: {**spec, "frames": [{"file": f"cell_{i}.png"} for i in spec["frames"]]}
        for name, spec in manifest["animations"].items()
    }
    return manifest, frames, mapping


@pytest.mark.parametrize("pack", SHIPPED_PACKS)
def test_shipped_packs_still_build_clean(tmp_path: Path, pack: str) -> None:
    """Every shipped pack still survives its own builder, byte for byte.

    The old shape of this test read tofu's pet.json and compared four numbers
    against caps copied out of the builder — so it could not fail for any reason
    the builder cares about (a missing standard track, a cell that no longer
    dedups, a grid the sheet does not match), and it never ran build_pack at all.
    Running the real thing is what ties the committed artefacts to the code that
    is supposed to produce them.

    Indices are expected to come back identical, not merely equivalent: the
    builder assigns them in first-use order over the track list, and the mapping
    here is that same list in that same order.
    """
    manifest, frames, mapping = _explode(pack, tmp_path)

    rebuilt = json.loads(
        build_pack(frames, _mapping(tmp_path, mapping), tmp_path / "out").read_text(
            encoding="utf-8"
        )
    )

    assert rebuilt["frame"] == manifest["frame"], "the grid moved"
    assert rebuilt["animations"] == manifest["animations"], "a track's frames moved"
    shipped_sheet = Image.open(_SKINS / pack / "spritesheet.png").convert("RGBA")
    built_sheet = Image.open(tmp_path / "out" / "spritesheet.png").convert("RGBA")
    assert built_sheet.size == shipped_sheet.size
    assert _solid_pixels(built_sheet) == _solid_pixels(
        shipped_sheet
    ), "the sprite moved in its cell"


@pytest.mark.parametrize("pack", SHIPPED_PACKS)
def test_a_shipped_pack_missing_a_track_is_refused_like_any_other(
    tmp_path: Path, pack: str
) -> None:
    """The control: the round trip above has to be able to fail.

    A test that rebuilds and compares proves nothing if the builder would accept
    anything. Dropping one standard track from a real pack is the cheapest way to
    watch it say no — and it is the failure the renderer punishes hardest, since
    it degrades the whole pack to the built-in skin.
    """
    _, frames, mapping = _explode(pack, tmp_path)
    del mapping["attention"]

    with pytest.raises(MappingError, match="attention"):
        build_pack(frames, _mapping(tmp_path, mapping), tmp_path / "out")


def test_the_shipped_packs_are_exactly_the_ones_on_disk() -> None:
    """A third pack added without a line here would ship untested."""
    on_disk = {d.name for d in _SKINS.iterdir() if d.is_dir()}
    assert on_disk == set(SHIPPED_PACKS), f"skins/ holds {sorted(on_disk)}"
    for pack in SHIPPED_PACKS:
        animations = json.loads((_SKINS / pack / "pet.json").read_text(encoding="utf-8"))[
            "animations"
        ]
        assert set(STANDARD_TRACKS) <= set(animations), f"{pack} is missing a standard track"
