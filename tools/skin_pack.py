"""Build a sprite skin pack (pet.json + spritesheet.png) from loose frames.

The pack format is the one ui/web/js/skins/sprite.js renders: an 8-column
grid sheet plus a manifest whose `animations` name frame indices per track.
The eleven standard track names must all be present, because the renderer
validates its default tracks against the sheet's frame count — a pack that
overrides only some of them fails validation and degrades to the theme skin.

The mapping file names source frames per track, so any frame dump works —
a shimeji set, GIF extractions, hand-drawn art:

    {
      "animations": {
        "idle": {"frames": [{"file": "shime30.png"},
                             {"file": "shime1.png", "flip": true}],
                 "fps": 1.6},
        "jumping": {"frames": [...], "fps": 6, "loop": false}
      }
    }

Usage:
    python tools/skin_pack.py --frames-dir DIR --mapping MAP.json --out SKIN_DIR
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from PIL import Image

# Must match ui/web/js/skins/sprite.js defaultAnimations() key for key.
STANDARD_TRACKS = (
    "idle",
    "running-right",
    "running-left",
    "waving",
    "jumping",
    "failed",
    "waiting",
    "running",
    "review",
    "working",
    "attention",
)

_COLUMNS = 8
_DEFAULT_FPS = 8.0
_MAX_FPS = 60.0

# The renderer's hard caps (ui/web/js/skins/sprite.js MAX_GRID / MAX_FRAME_PX /
# MAX_SHEET_PX). A pack past these "builds fine" and then silently degrades to
# the built-in skin at load time — refuse at build time with the numbers.
_MAX_GRID = 32
_MAX_FRAME_PX = 512
_MAX_SHEET_PX = 4096


class MappingError(ValueError):
    """A mapping file problem, reported in full before giving up."""


def _load_mapping(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MappingError(f"映射文件不是合法 JSON：{path}（{exc}）") from exc
    animations = raw.get("animations")
    if not isinstance(animations, dict) or not animations:
        raise MappingError("映射文件要有非空的 animations 对象")
    missing = [name for name in STANDARD_TRACKS if name not in animations]
    if missing:
        raise MappingError(
            "缺这些标准轨道（渲染器会按全量校验，缺一个整包降级）："
            + "、".join(missing)
            + "。想偷懒的轨道直接复用别的帧即可。"
        )
    return animations


def _collect_cells(
    animations: dict[str, Any], frames_dir: Path
) -> tuple[list[tuple[Path, bool]], dict[tuple[str, bool], int]]:
    """Deduplicate (file, flip) pairs into grid cells, in first-use order."""
    cells: list[tuple[Path, bool]] = []
    index: dict[tuple[str, bool], int] = {}
    for name, spec in animations.items():
        if not isinstance(spec, dict):
            raise MappingError(f"轨道 {name} 要是对象，拿到 {type(spec).__name__}")
        frames = spec.get("frames")
        if not isinstance(frames, list) or not frames:
            raise MappingError(f"轨道 {name} 的 frames 要是非空数组")
        try:
            fps = float(spec.get("fps", _DEFAULT_FPS))
        except (TypeError, ValueError) as exc:
            raise MappingError(f"轨道 {name} 的 fps 不是数字：{spec.get('fps')!r}") from exc
        if not 0 < fps <= _MAX_FPS:
            raise MappingError(f"轨道 {name} 的 fps 非法：{fps}（要在 0 到 {_MAX_FPS} 之间）")
        fallback = spec.get("fallback")
        if fallback is not None and fallback not in animations:
            raise MappingError(f"轨道 {name} 的 fallback 指向不存在的轨道：{fallback}")
        for frame in frames:
            file_name = frame.get("file") if isinstance(frame, dict) else None
            if not isinstance(file_name, str):
                raise MappingError(f"轨道 {name} 有一帧缺 file 字段")
            flip = bool(frame.get("flip", False)) if isinstance(frame, dict) else False
            key = (file_name, flip)
            if key in index:
                continue
            source = frames_dir / file_name
            if not source.is_file():
                raise MappingError(f"帧文件不存在：{source}")
            index[key] = len(cells)
            cells.append((source, flip))
    return cells, index


def build_pack(frames_dir: Path, mapping_path: Path, out_dir: Path) -> Path:
    """Assemble the sheet and manifest.

    Args:
        frames_dir: Directory holding the source frame images.
        mapping_path: The track-to-frames mapping JSON.
        out_dir: Skin directory to write pet.json and spritesheet.png into.

    Returns:
        The written pet.json path.

    Raises:
        MappingError: on any mapping problem, with the fix in the message.
    """
    animations = _load_mapping(mapping_path)
    cells, index = _collect_cells(animations, frames_dir)

    images: list[Image.Image] = []
    for source, flip in cells:
        image = Image.open(source).convert("RGBA")
        if flip:
            image = image.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        images.append(image)

    cell_w = max(image.width for image in images)
    cell_h = max(image.height for image in images)
    rows = (len(images) + _COLUMNS - 1) // _COLUMNS
    if cell_w > _MAX_FRAME_PX or cell_h > _MAX_FRAME_PX:
        raise MappingError(f"单帧 {cell_w}x{cell_h} 超出渲染器上限 {_MAX_FRAME_PX}px——先把源帧缩小")
    if rows > _MAX_GRID:
        raise MappingError(f"帧数太多：{len(images)} 帧要 {rows} 行，渲染器上限 {_MAX_GRID} 行")
    if cell_w * _COLUMNS > _MAX_SHEET_PX or cell_h * rows > _MAX_SHEET_PX:
        raise MappingError(
            f"整图 {cell_w * _COLUMNS}x{cell_h * rows} 超出渲染器上限 {_MAX_SHEET_PX}px"
        )
    sheet = Image.new("RGBA", (_COLUMNS * cell_w, rows * cell_h), (0, 0, 0, 0))
    for i, image in enumerate(images):
        x = (i % _COLUMNS) * cell_w + (cell_w - image.width) // 2
        # Feet on the cell floor, not centered: a walk cycle with frames of
        # different heights would otherwise bob against the ground line.
        y = (i // _COLUMNS) * cell_h + (cell_h - image.height)
        sheet.paste(image, (x, y), image)

    manifest: dict[str, Any] = {
        "frame": {"width": cell_w, "height": cell_h, "columns": _COLUMNS, "rows": rows},
        "image": "spritesheet.png",
        "animations": {},
    }
    for name, spec in animations.items():
        frame_indices = [
            index[(frame["file"], bool(frame.get("flip", False)))] for frame in spec["frames"]
        ]
        entry: dict[str, Any] = {"frames": frame_indices}
        if "fps" in spec:
            entry["fps"] = float(spec["fps"])
        if spec.get("loop") is False:
            entry["loop"] = False
        if "fallback" in spec:
            entry["fallback"] = str(spec["fallback"])
        manifest["animations"][name] = entry

    out_dir.mkdir(parents=True, exist_ok=True)
    sheet.save(out_dir / "spritesheet.png", optimize=True)
    manifest_path = out_dir / "pet.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="skin_pack", description="把散帧打包成 sprite 皮肤包（pet.json + 精灵图）"
    )
    parser.add_argument("--frames-dir", type=Path, required=True, help="源帧目录")
    parser.add_argument("--mapping", type=Path, required=True, help="轨道→帧映射 JSON")
    parser.add_argument("--out", type=Path, required=True, help="皮肤包输出目录")
    args = parser.parse_args(argv)
    try:
        manifest_path = build_pack(args.frames_dir, args.mapping, args.out)
    except MappingError as exc:
        print(f"打包失败：{exc}", file=sys.stderr)
        return 2
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    frame = manifest["frame"]
    print(
        f"已生成 {manifest_path.parent}：{frame['columns']}x{frame['rows']} 网格，"
        f"单帧 {frame['width']}x{frame['height']}，{len(manifest['animations'])} 条轨道"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
