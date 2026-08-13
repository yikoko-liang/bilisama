"""Generate the built-in pixel robot skin (the theme renderer's real face).

The art is programmatic on purpose: a 26x28 pixel canvas drawn with fills,
scaled 6x with nearest-neighbour, packed by tools/skin_pack.py into the same
sprite format every other skin uses. This file IS the source art — tweak a
color or a pose here and rerun; no image editor in the loop.

Design language: Claude Code's pixel mascot in spirit, original in fact —
cream shell, dark visor, coral glow, side pods, stub feet.

Usage:
    python tools/gen_robot_skin.py            # writes src/bilisama/ui/web/skins/robot/
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).parent))
from skin_pack import build_pack

# Canvas in art pixels; every frame scales SCALE x with nearest-neighbour.
W, H = 26, 28
SCALE = 6

Color = tuple[int, int, int, int]

OUT: Color = (51, 49, 46, 255)  # outline
CREAM: Color = (240, 238, 230, 255)  # shell
SHADE: Color = (219, 215, 204, 255)  # shell shadow
SHEEN: Color = (250, 249, 245, 255)  # shell highlight
VISOR: Color = (40, 38, 35, 255)  # screen face
CORAL: Color = (217, 119, 87, 255)  # the glow
CORAL_HI: Color = (238, 158, 130, 255)
GRAY: Color = (150, 146, 138, 255)  # powered-down glow
WHITE: Color = (250, 249, 245, 255)


def rect(img: Image.Image, x0: int, y0: int, x1: int, y1: int, color: Color) -> None:
    """Inclusive-corner fill, clipped to the canvas."""
    for y in range(max(0, y0), min(H, y1 + 1)):
        for x in range(max(0, x0), min(W, x1 + 1)):
            img.putpixel((x, y), color)


def px(img: Image.Image, x: int, y: int, color: Color) -> None:
    if 0 <= x < W and 0 <= y < H:
        img.putpixel((x, y), color)


def rounded_block(
    img: Image.Image, x0: int, y0: int, x1: int, y1: int, fill: Color, outline: Color | None
) -> None:
    """A chunky rounded rect: fill plus outline ring, corner pixels dropped."""
    rect(img, x0, y0, x1, y1, fill)
    if outline is not None:
        rect(img, x0, y0, x1, y0, outline)
        rect(img, x0, y1, x1, y1, outline)
        rect(img, x0, y0, x0, y1, outline)
        rect(img, x1, y0, x1, y1, outline)
    for cx, cy in ((x0, y0), (x1, y0), (x0, y1), (x1, y1)):
        px(img, cx, cy, (0, 0, 0, 0))


# ------------------------------------------------------------ body parts


def draw_robot(
    *,
    dy: int = 0,
    eyes: str = "open",  # open | blink | up | wide | off
    mouth: str = "none",  # none | open | small
    antenna: str = "on",  # on | bright | off | bent
    pods: str = "dim",  # dim | lit
    arms: str = "none",  # none | right_up | right_mid | both_up
    feet: str = "stand",  # stand | walk_a | walk_b | tucked
    dots: str = "none",  # none | a | b        (thinking dots)
    zzz: str = "none",  # none | a | b
    bang: bool = False,  # the "!" of surprise
    powered: bool = True,
) -> Image.Image:
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    glow = CORAL if powered else GRAY
    glow_hi = CORAL_HI if powered else GRAY

    def Y(y: int) -> int:
        return y + dy

    # antenna
    if antenna != "off":
        rect(img, 12, Y(2), 13, Y(5), OUT)
        if antenna == "bent":
            px(img, 14, Y(2), OUT)  # the elbow keeps the tip attached
            rect(img, 14, Y(0), 15, Y(1), glow)
        else:
            rect(img, 12, Y(0), 13, Y(1), glow if antenna != "bright" else glow_hi)
            if antenna == "bright":
                px(img, 12, Y(0), WHITE)

    # head, with two-step corners for a softer silhouette
    rounded_block(img, 4, Y(6), 21, Y(17), CREAM, OUT)
    for cx, cy in ((5, 6), (4, 7), (20, 6), (21, 7), (4, 16), (5, 17), (21, 16), (20, 17)):
        px(img, cx, Y(cy), (0, 0, 0, 0))
    for cx, cy in ((5, 7), (20, 7), (5, 16), (20, 16)):
        px(img, cx, Y(cy), OUT)
    rect(img, 7, Y(7), 18, Y(7), SHEEN)
    rect(img, 5, Y(16), 20, Y(16), SHADE)

    # side pods
    rect(img, 2, Y(10), 2, Y(14), OUT)
    rect(img, 3, Y(10), 3, Y(14), SHADE)
    rect(img, 23, Y(10), 23, Y(14), OUT)
    rect(img, 22, Y(10), 22, Y(14), SHADE)
    pod_color = glow if pods == "lit" else SHADE
    px(img, 3, Y(12), pod_color)
    px(img, 22, Y(12), pod_color)

    # visor: a window in the shell, not the whole face
    rounded_block(img, 7, Y(9), 18, Y(13), VISOR, None)

    # eyes
    if eyes == "blink":
        rect(img, 9, Y(11), 10, Y(11), glow)
        rect(img, 15, Y(11), 16, Y(11), glow)
    elif eyes == "off":
        rect(img, 9, Y(11), 10, Y(11), GRAY)
        rect(img, 15, Y(11), 16, Y(11), GRAY)
    elif eyes == "up":
        rect(img, 10, Y(9), 11, Y(11), glow)
        rect(img, 16, Y(9), 17, Y(11), glow)
        px(img, 10, Y(9), glow_hi)
        px(img, 16, Y(9), glow_hi)
    elif eyes == "wide":
        rect(img, 9, Y(9), 11, Y(12), glow)
        rect(img, 14, Y(9), 16, Y(12), glow)
        px(img, 9, Y(9), WHITE)
        px(img, 14, Y(9), WHITE)
    else:  # open
        rect(img, 9, Y(10), 10, Y(12), glow)
        rect(img, 15, Y(10), 16, Y(12), glow)
        px(img, 9, Y(10), glow_hi)
        px(img, 15, Y(10), glow_hi)

    # mouth on the chin strip
    if mouth == "open":
        rect(img, 11, Y(15), 14, Y(16), CORAL)
    elif mouth == "small":
        rect(img, 12, Y(15), 13, Y(15), CORAL)

    # body
    rounded_block(img, 7, Y(18), 18, Y(22), CREAM, OUT)
    rect(img, 8, Y(21), 17, Y(21), SHADE)
    rect(img, 12, Y(19), 13, Y(20), glow)

    # arms
    if arms in ("right_up", "both_up"):
        rect(img, 22, Y(13), 23, Y(14), CREAM)  # hand
        rect(img, 22, Y(15), 22, Y(18), CREAM)
        rect(img, 23, Y(13), 23, Y(18), OUT)
    if arms == "both_up":
        rect(img, 2, Y(13), 3, Y(14), CREAM)
        rect(img, 3, Y(15), 3, Y(18), CREAM)
        rect(img, 2, Y(13), 2, Y(18), OUT)
    if arms == "right_mid":
        rect(img, 21, Y(17), 23, Y(18), CREAM)
        rect(img, 21, Y(19), 23, Y(19), OUT)

    # feet
    if feet == "stand":
        rect(img, 6, 23, 10, 25, VISOR)
        rect(img, 15, 23, 19, 25, VISOR)
    elif feet == "walk_a":
        rect(img, 4, 23, 8, 25, VISOR)
        rect(img, 16, 23, 20, 25, VISOR)
    elif feet == "walk_b":
        rect(img, 8, 23, 12, 25, VISOR)
        rect(img, 13, 23, 17, 25, VISOR)
    elif feet == "tucked":
        rect(img, 8, Y(23), 11, Y(24), VISOR)
        rect(img, 14, Y(23), 17, Y(24), VISOR)

    # thinking dots, drifting up beside the antenna
    if dots == "a":
        px(img, 20, Y(4), SHADE)
        px(img, 22, Y(3), CORAL)
        px(img, 24, Y(2), SHADE)
    elif dots == "b":
        px(img, 20, Y(4), CORAL)
        px(img, 22, Y(3), SHADE)
        px(img, 24, Y(2), CORAL)

    # zzz for powered-down naps
    if zzz != "none":
        ox = 0 if zzz == "a" else 1
        rect(img, 19 + ox, 2, 21 + ox, 2, GRAY)
        px(img, 20 + ox, 3, GRAY)
        rect(img, 19 + ox, 4, 21 + ox, 4, GRAY)

    # the "!" of surprise
    if bang:
        rect(img, 18, Y(0), 19, Y(3), CORAL)
        rect(img, 18, Y(5), 19, Y(5), CORAL)

    return img.resize((W * SCALE, H * SCALE), Image.Resampling.NEAREST)


# ------------------------------------------------------------ frames & tracks

FRAMES: dict[str, dict[str, object]] = {
    "idle_a": {},
    "idle_b": {"eyes": "blink"},
    "listen_a": {"eyes": "wide", "pods": "lit", "antenna": "bright"},
    "listen_b": {"eyes": "wide", "pods": "dim", "antenna": "on"},
    "think_a": {"eyes": "up", "dots": "a"},
    "think_b": {"eyes": "up", "dots": "b"},
    "speak_a": {"mouth": "open", "arms": "right_up", "antenna": "bright"},
    "speak_b": {"mouth": "small", "arms": "right_mid"},
    "jump": {"dy": -2, "feet": "tucked", "arms": "both_up", "mouth": "open"},
    "squash": {"dy": 1, "eyes": "blink", "antenna": "bent"},
    "cheer": {"arms": "both_up", "mouth": "open", "eyes": "wide", "antenna": "bright"},
    "sleep_a": {"eyes": "off", "antenna": "off", "powered": False, "zzz": "a"},
    "sleep_b": {"eyes": "off", "antenna": "off", "powered": False, "zzz": "b"},
    "walk_a": {"feet": "walk_a"},
    "walk_b": {"feet": "walk_b", "dy": -1},
    "shock_a": {"eyes": "wide", "bang": True},
    "shock_b": {"eyes": "wide"},
}


def _track(
    files: list[str], fps: float, *, loop: bool = True, flip: bool = False
) -> dict[str, object]:
    frames = [{"file": f"{name}.png", "flip": flip} for name in files]
    spec: dict[str, object] = {"frames": frames, "fps": fps}
    if not loop:
        spec["loop"] = False
    return spec


MAPPING: dict[str, object] = {
    "animations": {
        # 4:1 open-to-blink at 2.5fps: a blink every 1.6s that lasts 400ms.
        "idle": _track(["idle_a", "idle_a", "idle_a", "idle_b"], 2.5),
        "waiting": _track(["listen_a", "listen_b"], 2.0),
        "review": _track(["think_a", "think_b"], 2.0),
        "waving": _track(["speak_a", "speak_b"], 4.0),
        "jumping": _track(["jump", "squash", "cheer"], 6.0, loop=False),
        "failed": _track(["sleep_a", "sleep_b"], 1.0),
        "running-left": _track(["walk_a", "walk_b"], 5.0),
        "running-right": _track(["walk_a", "walk_b"], 5.0, flip=True),
        "running": _track(["walk_a", "walk_b"], 7.0),
        "working": _track(["walk_a", "walk_b"], 5.0),
        "attention": _track(["shock_a", "shock_b"], 3.0),
    }
}


def main() -> int:
    out_dir = Path(__file__).parent.parent / "src" / "bilisama" / "ui" / "web" / "skins" / "robot"
    with tempfile.TemporaryDirectory() as tmp:
        frames_dir = Path(tmp)
        for name, params in FRAMES.items():
            draw_robot(**params).save(frames_dir / f"{name}.png")  # type: ignore[arg-type]
        mapping_path = frames_dir / "mapping.json"
        mapping_path.write_text(json.dumps(MAPPING, ensure_ascii=False), encoding="utf-8")
        build_pack(frames_dir, mapping_path, out_dir)
    print(f"已生成内置机器人皮肤：{out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
