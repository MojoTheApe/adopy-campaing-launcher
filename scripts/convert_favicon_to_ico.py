"""Generate assets/dashboard.ico from the Adopy Campaign Manager mark (the "A" mark, #2563eb).

Внимание: релиз 1 ребрендинга (ACL-1705) положил в assets/dashboard.ico готовый
набор из kit (синяя метка на белом скруглённом квадрате, 16..256). Этот скрипт
рисует старую метку ACL и больше не является источником иконки; не запускать,
не заменив рисование.


Draws the icon programmatically with Pillow (no SVG renderer needed).
Produces sizes 256, 48, 32, 16 packed into a single .ico file.

Usage:
    python scripts/convert_favicon_to_ico.py
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

_OUT = Path(__file__).resolve().parents[1] / "assets" / "dashboard.ico"
_STATIC_ICO = Path(__file__).resolve().parents[1] / "src" / "web" / "static" / "favicon.ico"
_SIZES = [256, 48, 32, 16]

_BG_TOP = (0x1D, 0x4E, 0xD8)
_BG_BOT = (0x43, 0x38, 0xCA)
_GLOW = (0x38, 0xBD, 0xF8)
_TEXT = (0xFF, 0xFF, 0xFF, 255)
_LINE = (0x93, 0xC5, 0xFD, 115)


def _font(size: int, *, bold: bool = True) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        r"C:\Windows\Fonts\segoeuib.ttf" if bold else r"C:\Windows\Fonts\segoeui.ttf",
        r"C:\Windows\Fonts\arialbd.ttf" if bold else r"C:\Windows\Fonts\arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    ]
    px = max(8, int(size * 0.34))
    for path in candidates:
        if os.path.isfile(path):
            try:
                return ImageFont.truetype(path, px)
            except OSError:
                continue
    return ImageFont.load_default()


def _rounded_rect(
    draw: ImageDraw.ImageDraw,
    box: tuple[float, float, float, float],
    radius: float,
    fill: tuple[int, ...],
) -> None:
    draw.rounded_rectangle(box, radius=radius, fill=fill)


def _draw_acl(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size / 64.0

    def sc(v: float) -> float:
        return v * s

    pad = sc(2)
    _rounded_rect(
        d,
        (pad, pad, size - pad, size - pad),
        sc(15),
        _BG_TOP,
    )
    # simple vertical blend overlay for depth
    overlay = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    for row in range(size):
        t = row / max(size - 1, 1)
        color = tuple(
            int(_BG_TOP[i] + (_BG_BOT[i] - _BG_TOP[i]) * t) for i in range(3)
        ) + (255,)
        od.line([(0, row), (size, row)], fill=color)
    mask = Image.new("L", (size, size), 0)
    md = ImageDraw.Draw(mask)
    md.rounded_rectangle((pad, pad, size - pad, size - pad), radius=sc(15), fill=255)
    img = Image.composite(overlay, img, mask)
    d = ImageDraw.Draw(img)

    # launch chevron accent
    glow_pts = [
        (sc(44), sc(11)),
        (sc(56), sc(23)),
        (sc(48), sc(23)),
        (sc(44), sc(19)),
    ]
    d.polygon(glow_pts, fill=_GLOW + (255,))
    d.polygon([(sc(48), sc(23)), (sc(56), sc(23)), (sc(56), sc(31))], fill=(14, 165, 233, 140))

    font = _font(size)
    label = "ACL"
    bbox = d.textbbox((0, 0), label, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    tx = (size - tw) / 2 - bbox[0]
    ty = sc(41) - th / 2 - bbox[1]
    d.text((tx, ty), label, font=font, fill=_TEXT)

    d.line([(sc(16), sc(47)), (sc(48), sc(47))], fill=_LINE, width=max(1, int(sc(2))))

    return img


def main() -> None:
    frames = [_draw_acl(sz) for sz in _SIZES]
    _OUT.parent.mkdir(parents=True, exist_ok=True)
    frames[0].save(
        _OUT,
        format="ICO",
        sizes=[(sz, sz) for sz in _SIZES],
        append_images=frames[1:],
    )
    shutil.copy2(_OUT, _STATIC_ICO)
    print(f"[convert] Saved {_OUT}  ({', '.join(str(s) for s in _SIZES)} px)")
    print(f"[convert] Copied to {_STATIC_ICO}")


if __name__ == "__main__":
    main()
