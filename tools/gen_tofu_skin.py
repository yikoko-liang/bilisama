"""Generate tofu, the built-in pixel robot skin.

The name is a font-engineering joke made flesh: the missing-glyph box "□" is
called tofu (Noto literally means "no tofu"). Ours got a face, two antennas
and the bili-pink glow instead of being eliminated.

The art is programmatic on purpose: a 26x28 pixel canvas drawn with fills,
scaled 6x with nearest-neighbour, packed by tools/skin_pack.py into the same
sprite format every other skin uses. This file IS the source art — tweak a
color or a pose here and rerun; no image editor in the loop.

Design language, third pass: ONE tofu block — head and body merged, no legs.
White tofu body, a black-tofu face plate set in with a visible seam, a
black-tofu base layer at the bottom (the 黑白豆腐 stack, and the visual
ground a legless block needs), a soy-cream top bevel. Bili pink carries
emotion (eyes, cheeks, mouth, antenna tips); bili blue appears in exactly one
place — the thinking dots. Hands exist only while she gestures: talking
alternates two little mitts, cheering and jumping raise both.
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

OUT: Color = (40, 36, 32, 255)  # outline, warm ink
CREAM: Color = (250, 247, 240, 255)  # white tofu, soy-tinted
SHADE: Color = (233, 227, 214, 255)  # tofu shadow, warm — no pink cast
SHEEN: Color = (255, 255, 255, 255)  # the lit top face of the block
PLATE: Color = (47, 43, 39, 255)  # black tofu: face plate and base layer
PINK: Color = (251, 114, 153, 255)  # bili pink, the emotion accent
PINK_HI: Color = (255, 163, 192, 255)
CHEEK: Color = (250, 189, 207, 255)  # soft blush on the white tofu
BLUE: Color = (35, 173, 229, 255)  # bili blue, thinking only
GRAY: Color = (155, 151, 156, 255)  # powered-down glow
WHITE: Color = (255, 255, 255, 255)


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


# ------------------------------------------------------------ the tofu


def draw_robot(
    *,
    dy: int = 0,
    lean: int = 0,  # face/antenna x-shift: the legless waddle
    eyes: str = "open",  # open | blink | up | wide | off
    mouth: str = "none",  # none | open | small
    antenna: str = "on",  # on | bright | off
    hands: str = "none",  # none | talk_a | talk_b | up
    dots: str = "none",  # none | a | b        (thinking dots, bili blue)
    zzz: str = "none",  # none | a | b
    bang: bool = False,  # the "!" of surprise
    powered: bool = True,
) -> Image.Image:
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    glow = PINK if powered else GRAY
    glow_hi = PINK_HI if powered else GRAY

    def Y(y: int) -> int:
        return y + dy

    def X(x: int) -> int:
        return x + lean

    # the 小电视 signature: two short antennas off the block's top corners
    if antenna != "off":
        tip = glow_hi if antenna == "bright" else glow
        for sx, sy in ((7, 7), (7, 6), (7, 5), (6, 4)):
            px(img, X(sx), Y(sy), OUT)
        rect(img, X(5), Y(2), X(5), Y(3), tip)
        for sx, sy in ((18, 7), (18, 6), (18, 5), (19, 4)):
            px(img, X(sx), Y(sy), OUT)
        rect(img, X(20), Y(2), X(20), Y(3), tip)
        if antenna == "bright":
            px(img, X(5), Y(2), WHITE)
            px(img, X(20), Y(2), WHITE)

    # the block: one 18x18 rounded square, softer two-step corners
    rounded_block(img, 4, Y(8), 21, Y(25), CREAM, OUT)
    for cx, cy in ((5, 8), (4, 9), (20, 8), (21, 9), (4, 24), (5, 25), (21, 24), (20, 25)):
        px(img, cx, Y(cy), (0, 0, 0, 0))
    for cx, cy in ((5, 9), (20, 9), (5, 24), (20, 24)):
        px(img, cx, Y(cy), OUT)
    rect(img, 6, Y(9), 19, Y(9), SHEEN)  # lit top face

    # black-tofu base layer, with a soft seam above it — the stack that also
    # grounds a block with no legs
    rect(img, 5, Y(21), 20, Y(21), SHADE)
    rect(img, 5, Y(22), 20, Y(24), PLATE)

    # black-tofu face plate, set in with a 1px white seam all around
    rounded_block(img, X(6), Y(11), X(19), Y(16), PLATE, None)

    # cheeks on the white tofu, under the plate corners
    if powered:
        px(img, X(6), Y(18), CHEEK)
        px(img, X(19), Y(18), CHEEK)

    # eyes on the plate
    if eyes == "blink":
        rect(img, X(9), Y(13), X(10), Y(13), glow)
        rect(img, X(15), Y(13), X(16), Y(13), glow)
    elif eyes == "off":
        rect(img, X(9), Y(13), X(10), Y(13), GRAY)
        rect(img, X(15), Y(13), X(16), Y(13), GRAY)
    elif eyes == "up":
        rect(img, X(10), Y(11), X(11), Y(13), glow)
        rect(img, X(16), Y(11), X(17), Y(13), glow)
        px(img, X(10), Y(11), glow_hi)
        px(img, X(16), Y(11), glow_hi)
    elif eyes == "wide":
        rect(img, X(9), Y(11), X(11), Y(14), glow)
        rect(img, X(14), Y(11), X(16), Y(14), glow)
        px(img, X(9), Y(11), WHITE)
        px(img, X(14), Y(11), WHITE)
    else:  # open
        rect(img, X(9), Y(12), X(10), Y(14), glow)
        rect(img, X(15), Y(12), X(16), Y(14), glow)
        px(img, X(9), Y(12), glow_hi)
        px(img, X(15), Y(12), glow_hi)

    # mouth on the white chin strip
    if mouth == "open":
        rect(img, X(11), Y(18), X(14), Y(19), PINK)
    elif mouth == "small":
        rect(img, X(12), Y(18), X(13), Y(18), PINK)

    # hands, only when she gestures. Talking alternates the two mitts —
    # the 巴拉巴拉 hand-waving.
    def hand(side: str, y: int) -> None:
        if side == "left":
            rect(img, 2, Y(y), 3, Y(y + 1), CREAM)
            rect(img, 1, Y(y), 1, Y(y + 1), OUT)
        else:
            rect(img, 22, Y(y), 23, Y(y + 1), CREAM)
            rect(img, 24, Y(y), 24, Y(y + 1), OUT)

    if hands == "talk_a":
        hand("left", 12)
        hand("right", 16)
    elif hands == "talk_b":
        hand("left", 16)
        hand("right", 12)
    elif hands == "up":
        hand("left", 11)
        hand("right", 11)

    # thinking dots, drifting up beside the right antenna — the one place
    # bili blue appears
    if dots == "a":
        px(img, 22, Y(4), SHADE)
        px(img, 23, Y(2), BLUE)
        px(img, 25, Y(1), SHADE)
    elif dots == "b":
        px(img, 22, Y(4), BLUE)
        px(img, 23, Y(2), SHADE)
        px(img, 25, Y(1), BLUE)

    # zzz for powered-down naps
    if zzz != "none":
        ox = 0 if zzz == "a" else 1
        rect(img, 19 + ox, 2, 21 + ox, 2, GRAY)
        px(img, 20 + ox, 3, GRAY)
        rect(img, 19 + ox, 4, 21 + ox, 4, GRAY)

    # the "!" of surprise, between the antennas
    if bang:
        rect(img, 12, Y(0), 13, Y(3), PINK)
        rect(img, 12, Y(5), 13, Y(5), PINK)

    return img.resize((W * SCALE, H * SCALE), Image.Resampling.NEAREST)


# ------------------------------------------------------------ frames & tracks

FRAMES: dict[str, dict[str, object]] = {
    "idle_a": {},
    "idle_b": {"eyes": "blink"},
    "listen_a": {"eyes": "wide", "antenna": "bright"},
    "listen_b": {"eyes": "wide", "antenna": "on"},
    "think_a": {"eyes": "up", "dots": "a"},
    "think_b": {"eyes": "up", "dots": "b"},
    "speak_a": {"mouth": "open", "hands": "talk_a", "antenna": "bright"},
    "speak_b": {"mouth": "small", "hands": "talk_b"},
    "jump": {"dy": -2, "hands": "up", "mouth": "open"},
    "squash": {"dy": 1, "eyes": "blink"},
    "cheer": {"hands": "up", "eyes": "wide", "mouth": "open", "antenna": "bright"},
    "sleep_a": {"eyes": "off", "antenna": "off", "powered": False, "zzz": "a"},
    "sleep_b": {"eyes": "off", "antenna": "off", "powered": False, "zzz": "b"},
    "walk_a": {"lean": -1},
    "walk_b": {"lean": 1, "dy": -1},
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
    out_dir = Path(__file__).parent.parent / "src" / "bilisama" / "ui" / "web" / "skins" / "tofu"
    with tempfile.TemporaryDirectory() as tmp:
        frames_dir = Path(tmp)
        for name, params in FRAMES.items():
            draw_robot(**params).save(frames_dir / f"{name}.png")  # type: ignore[arg-type]
        mapping_path = frames_dir / "mapping.json"
        mapping_path.write_text(json.dumps(MAPPING, ensure_ascii=False), encoding="utf-8")
        build_pack(frames_dir, mapping_path, out_dir)
    print(f"已生成豆腐皮肤：{out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
