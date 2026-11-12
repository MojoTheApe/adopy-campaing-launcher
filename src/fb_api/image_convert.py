"""ACL-2575: image creatives Meta's ad image upload does not take (WebP, AVIF, HEIC) are converted locally.

Meta's `/adimages` accepts JPEG, PNG, GIF, BMP and TIFF. A WebP file used to pass the checks, the paused campaign and
ad set were created, and only then did the upload fail (Graph error 100/1487411 FileTypeNotSupported). The conversion
runs in the preflight, on bytes in memory: the buyer's file on disk is never touched. Transparent images become PNG,
the rest JPEG. A file that cannot be decoded is refused before any write, with its name and the reason.
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

#: Types Meta takes as they are (sniffed from the bytes, not from the file name).
ACCEPTED = frozenset({"jpeg", "png", "gif", "bmp", "tiff"})
#: Extensions that name a type we convert (the bytes decide first; the name is only a fallback).
CONVERTIBLE_EXT = frozenset({".webp", ".avif", ".heic", ".heif"})
EXT_OF = {"jpeg": ".jpg", "png": ".png", "gif": ".gif", "bmp": ".bmp", "tiff": ".tif"}
_NEEDS_CONVERSION = frozenset({"webp", "avif", "heic"})
JPEG_QUALITY = 95


class ImageConversionError(ValueError):
    """The file needs converting and cannot be (no decoder, damaged, too big once converted)."""


@dataclass(frozen=True)
class Converted:
    data: bytes
    ext: str
    note: str


def sniff(data: bytes) -> str:
    """Image type from the magic bytes; "" when not recognised (the caller then trusts the file name)."""
    head = bytes(data[:32])
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if head.startswith(b"BM"):
        return "bmp"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"avif", b"avis"):
            return "avif"
        if brand in (b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1"):
            return "heic"
    return ""


def needs_conversion(path: str, data: bytes) -> str:
    """The source type ("webp"/"avif"/"heic") when this image must be converted, else ""."""
    kind = sniff(data)
    if kind in ACCEPTED:
        return ""
    if kind in _NEEDS_CONVERSION:
        return kind
    ext = Path(path).suffix.lower()
    if not kind and ext in CONVERTIBLE_EXT:
        return {".webp": "webp", ".avif": "avif"}.get(ext, "heic")
    return ""


def convert(path: str, data: bytes, limit: int, who: str) -> Converted | None:
    """PNG (transparent) or JPEG bytes for a WebP/AVIF/HEIC image; None when `data` needs no conversion.

    `limit` is the same size cap as for the original file. Raises `ImageConversionError` with a buyer-facing reason."""
    source = needs_conversion(path, data)
    if not source:
        return None
    label = source.upper() if source != "webp" else "WebP"
    name = Path(path).name
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:  # pragma: no cover - Pillow is in requirements.txt
        raise ImageConversionError(f"{who}: {name} is a {label} image and this machine cannot convert it "
                                   "(image library missing). Use a JPG or PNG file.") from exc
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.load()
            animated = getattr(img, "n_frames", 1) > 1
            # Phone photos carry an EXIF Orientation tag; apply it so the pixels are upright in the output.
            img = ImageOps.exif_transpose(img)
            alpha = img.mode in ("RGBA", "LA", "PA") or "transparency" in img.info
            frame = img.convert("RGBA" if alpha else "RGB")
    except Exception as exc:  # noqa: BLE001 - any decoder failure is a refusal with the reason
        raise ImageConversionError(
            f"{who}: {name} is a {label} image that cannot be converted ({type(exc).__name__}: "
            f"{str(exc)[:120]}). Use a JPG or PNG file.") from exc
    buf = io.BytesIO()
    if alpha:
        frame.save(buf, "PNG", optimize=True)
        target = "PNG"
    else:
        frame.save(buf, "JPEG", quality=JPEG_QUALITY, optimize=True)
        target = "JPEG"
    out = buf.getvalue()
    if not out or len(out) > limit:
        raise ImageConversionError(
            f"{who}: {name} is larger than {limit // (1024 * 1024)} MB after converting {label} to {target}. "
            "Use a smaller JPG or PNG file.")
    note = f"{who}: {label} converted to {target} for Meta."
    if animated:
        note += " Only the first frame is used."
    return Converted(out, ".png" if target == "PNG" else ".jpg", note)
