"""ACL-2571: the background picture of a SHARED form template lives on THIS computer only.

The team library keeps just a hash (`cover_local_sha256`); the image bytes never go there. The file is
stored in the machine config folder, keyed by template id + content hash, and uploaded to each campaign's
Page at publish time. The Page photo id returned by that upload is cached per Page + hash (a photo id works
only on its own Page), so a second publish to the same Page does not upload again.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from pathlib import Path
from typing import Any

from src.fb_api import write_policy as _wp

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TID_RE = re.compile(r"^[0-9a-f]{12}$")
_PAGE_RE = re.compile(r"^[0-9]{1,25}$")
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
CACHE_NAME = "form_cover_photos.json"
STAGED = "_staged"

_lock = threading.RLock()


def store_dir() -> Path:
    from config.paths import get_local_config_dir

    return Path(get_local_config_dir()) / "form_covers"


def sniff_image(data: bytes) -> str:
    """MIME by the first bytes (the file name and the browser's type are not trusted); empty = not JPEG/PNG."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == _PNG_MAGIC:
        return "image/png"
    return ""


def check_file(data: bytes) -> tuple[str, str]:
    """(mime, refusal). An empty refusal = the file fits. Same limits as the write policy."""
    if not data:
        return "", "The image file is empty."
    mime = sniff_image(data)
    if not mime:
        return "", "The background image must be a JPEG or PNG file."
    if len(data) > _wp.FORM_PHOTO_MAX_BYTES:
        return "", f"The background image is larger than {_wp.FORM_PHOTO_MAX_BYTES // (1024 * 1024)} MB."
    return mime, ""


def _final(tid: str, sha: str) -> Path:
    if not (_TID_RE.match(str(tid or "")) and SHA256_RE.match(str(sha or ""))):
        raise ValueError("bad template id or picture hash")
    return store_dir() / f"{tid}-{sha}.bin"


def _staged(sha: str) -> Path:
    if not SHA256_RE.match(str(sha or "")):
        raise ValueError("bad picture hash")
    return store_dir() / STAGED / f"{sha}.bin"


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def stage(data: bytes) -> dict[str, Any]:
    """Keep a chosen file until the template is saved (the template id may not exist yet)."""
    mime, refusal = check_file(data)
    if refusal:
        return {"ok": False, "message": refusal}
    sha = hashlib.sha256(data).hexdigest()
    with _lock:
        _write_atomic(_staged(sha), data)
    return {"ok": True, "sha256": sha, "bytes": len(data), "mime": mime}


def adopt(tid: str, sha: str) -> bool:
    """After the template is saved: the staged file becomes this template's picture; older files of it go."""
    if not (_TID_RE.match(str(tid or "")) and SHA256_RE.match(str(sha or ""))):
        return False
    with _lock:
        final, staged = _final(tid, sha), _staged(sha)
        if staged.exists():
            final.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged, final)
        if final.exists():
            for other in final.parent.glob(f"{tid}-*.bin"):
                if other != final:
                    other.unlink(missing_ok=True)
            return True
    return False


def exists(tid: str, sha: str) -> bool:
    try:
        return _final(tid, sha).is_file()
    except ValueError:
        return False


def read(tid: str, sha: str) -> bytes | None:
    """The stored bytes, or None when the file is not on this computer or does not match its hash."""
    try:
        data = _final(tid, sha).read_bytes()
    except (ValueError, OSError):
        return None
    return data if hashlib.sha256(data).hexdigest() == sha else None


def copy_to(from_tid: str, to_tid: str, sha: str) -> bool:
    """A duplicated template keeps its picture."""
    data = read(from_tid, sha)
    if data is None:
        return False
    with _lock:
        _write_atomic(_final(to_tid, sha), data)
    return True


# ───────────────────── Page photo id cache: one upload per Page + picture ─────────────────────

def _cache_path() -> Path:
    return store_dir() / CACHE_NAME


def _cache_load() -> dict[str, str]:
    try:
        raw = json.loads(_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}


def _key(page_id: str, sha: str) -> str:
    if not (_PAGE_RE.match(str(page_id or "")) and SHA256_RE.match(str(sha or ""))):
        raise ValueError("bad Page id or picture hash")
    return f"{page_id}:{sha}"


def cached_photo(page_id: str, sha: str) -> str:
    try:
        got = _cache_load().get(_key(page_id, sha), "")
    except ValueError:
        return ""
    return got if _PAGE_RE.match(got) else ""


def remember_photo(page_id: str, sha: str, photo_id: str) -> None:
    if not _PAGE_RE.match(str(photo_id or "")):
        raise ValueError("bad photo id")
    with _lock:
        data = _cache_load()
        data[_key(page_id, sha)] = str(photo_id)
        _write_atomic(_cache_path(), json.dumps(data, sort_keys=True).encode("utf-8"))


def forget_photo(page_id: str, sha: str) -> None:
    """The cached photo was refused by Meta (deleted from the Page): the next publish uploads again."""
    try:
        key = _key(page_id, sha)
    except ValueError:
        return
    with _lock:
        data = _cache_load()
        if data.pop(key, None) is not None:
            _write_atomic(_cache_path(), json.dumps(data, sort_keys=True).encode("utf-8"))
