"""Generate assets/dashboard.ico from the pirate smiley design.

Draws the icon programmatically with Pillow (no SVG renderer needed).
Produces sizes 256, 48, 32, 16 packed into a single .ico file.

Usage:
    python scripts/convert_favicon_to_ico.py
"""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

_OUT = Path(__file__).resolve().parents[1] / "assets" / "dashboard.ico"
_SIZES = [256, 48, 32, 16]


def _draw_pirate(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size / 64.0  # scale factor from 64-px design

    # ── helpers ──────────────────────────────────────────────────────────
    def sc(v: float) -> float:
        return v * s

    def xy(x: float, y: float) -> tuple[float, float]:
        return sc(x), sc(y)

    def ellipse(cx: float, cy: float, rx: float, ry: float, **kw) -> None:
        d.ellipse(
            [xy(cx - rx, cy - ry), xy(cx + rx, cy + ry)],
            **kw,
        )

    # ── crossed bones (background, drawn below face) ──────────────────
    bone_fill = "#F5B800"
    bone_out = "#D9A400"
    # simulate two rotated rects as thick lines
    for angle in (-24, 24):
        rad = math.radians(angle)
        cx, cy = 32, 57.25
        half_w = 19  # half of width=38
        dx = math.cos(rad) * half_w * s
        dy = math.sin(rad) * half_w * s
        d.line(
            [(cx * s - dx, cy * s - dy), (cx * s + dx, cy * s + dy)],
            fill=bone_fill,
            width=max(2, int(sc(4.5))),
        )
    # four knob circles at bone ends
    for cx, cy in [(15.5, 50.5), (48.5, 50.5), (15.5, 64), (48.5, 64)]:
        ellipse(cx, cy, 3, 3, fill=bone_fill, outline=bone_out, width=max(1, int(sc(0.8))))

    # ── face circle ──────────────────────────────────────────────────
    face_top = "#FFE566"
    face_bot = "#FFCC00"
    # Pillow can't do linear gradients natively; blend row by row for large sizes
    if size >= 48:
        face_img = Image.new("RGBA", img.size, (0, 0, 0, 0))
        fd = ImageDraw.Draw(face_img)
        r = sc(22)
        cx, cy = sc(32), sc(21)
        top_rgb = (0xFF, 0xE5, 0x66)
        bot_rgb = (0xFF, 0xCC, 0x00)
        # draw gradient by overlapping horizontal slices (cheap but effective)
        for row in range(int(cy - r), int(cy + r) + 1):
            t = (row - (cy - r)) / (2 * r) if r > 0 else 0
            t = max(0.0, min(1.0, t))
            color = tuple(int(top_rgb[i] + (bot_rgb[i] - top_rgb[i]) * t) for i in range(3)) + (255,)
            chord_half = math.sqrt(max(0, r * r - (row - cy) ** 2))
            fd.line([(cx - chord_half, row), (cx + chord_half, row)], fill=color)
        # no-op filter pass (gradient already drawn row-by-row)
        img.alpha_composite(face_img)
        # outline
        d2 = ImageDraw.Draw(img)
        d2.ellipse(
            [xy(32 - 22, 21 - 22), xy(32 + 22, 21 + 22)],
            outline="#D9A400",
            width=max(1, int(sc(1.5))),
        )
        d = d2
    else:
        ellipse(32, 21, 22, 22, fill=face_top, outline="#D9A400", width=max(1, int(sc(1.5))))

    # ── left eye (open) ───────────────────────────────────────────────
    ellipse(23, 18, 3.8, 4.8, fill="#5C3D00")

    # ── eye patch (right eye) ─────────────────────────────────────────
    # filled dark polygon approximating the SVG path
    patch_pts = [
        xy(32, 12), xy(39, 11), xy(44, 14), xy(46, 21),
        xy(44, 27), xy(40, 30), xy(32, 29),
    ]
    d.polygon(patch_pts, fill="#2B1D0E")
    # strap across forehead
    d.line([xy(30, 14), xy(48, 8)], fill="#2B1D0E", width=max(1, int(sc(2.5))))

    # ── smile ─────────────────────────────────────────────────────────
    # approximate arc as polyline
    pts = []
    for i in range(21):
        t = i / 20.0
        x = 17 + t * (47 - 17)
        # quadratic bezier: P0=(17,28), P1=(32,39), P2=(47,28)
        bx = (1 - t) ** 2 * 17 + 2 * (1 - t) * t * 32 + t ** 2 * 47
        by = (1 - t) ** 2 * 28 + 2 * (1 - t) * t * 39 + t ** 2 * 28
        pts.append(xy(bx, by))
    d.line(pts, fill="#5C3D00", width=max(1, int(sc(2.8))))

    return img


def main() -> None:
    frames = [_draw_pirate(sz) for sz in _SIZES]
    _OUT.parent.mkdir(parents=True, exist_ok=True)
    # Save as ICO; Pillow uses the first frame as reference
    frames[0].save(
        _OUT,
        format="ICO",
        sizes=[(sz, sz) for sz in _SIZES],
        append_images=frames[1:],
    )
    print(f"[convert] Saved {_OUT}  ({', '.join(str(s) for s in _SIZES)} px)")


if __name__ == "__main__":
    main()
