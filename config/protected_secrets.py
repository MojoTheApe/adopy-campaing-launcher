"""User-bound Windows secret storage. No plaintext or non-Windows fallback.

Opaque references identify encrypted envelopes; owners and revisions are checked
inside those envelopes before reading or deleting. Callers commit a new reference
only after put succeeds, and retire metadata before deleting an old reference.
"""
from __future__ import annotations

import ctypes
import json
import os
import uuid
from pathlib import Path

from config import paths


class SecretUnavailable(RuntimeError):
    """Safe error: intentionally contains no input, OS message or path."""

    def __init__(self):
        super().__init__("Protected credential unavailable; save a new credential on this Windows user account.")


def _dpapi(data: bytes, *, decrypt: bool) -> bytes:
    if os.name != "nt":
        raise SecretUnavailable()
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob),
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    # CRYPTPROTECT_UI_FORBIDDEN; CurrentUser is the default (never LOCAL_MACHINE).
    try:
        if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
            raise SecretUnavailable()
        return ctypes.string_at(result.data, result.size)
    finally:
        if result.data:
            kernel.LocalFree(result.data)


def _protect(data: bytes) -> bytes:
    return _dpapi(data, decrypt=False)


def _unprotect(data: bytes) -> bytes:
    return _dpapi(data, decrypt=True)


def _file(reference: str, directory=None) -> Path:
    try:
        if str(uuid.UUID(reference)) != reference:
            raise ValueError
    except (ValueError, AttributeError, TypeError):
        raise SecretUnavailable() from None
    return Path(directory or paths.get_local_config_dir()) / "protected-secrets" / (reference + ".bin")


def put(owner: str, revision: int, values: dict[str, str], *, directory=None) -> str:
    """Write ciphertext first. Returned reference can then enter caller metadata."""
    try:
        if (not isinstance(owner, str) or not owner or len(owner) > 100
                or type(revision) is not int or revision < 1
                or not isinstance(values, dict) or not values
                or any(not isinstance(k, str) or not isinstance(v, str) for k, v in values.items())):
            raise ValueError
        envelope = json.dumps({"schema": 1, "owner": owner, "revision": revision,
                               "values": values}, ensure_ascii=True).encode("utf-8")
        ciphertext = _protect(envelope)
        reference = str(uuid.uuid4())
        paths._atomic_write(_file(reference, directory), ciphertext)
        return reference
    except Exception:
        raise SecretUnavailable() from None


def get(reference: str, owner: str, revision: int, *, directory=None) -> dict[str, str]:
    try:
        source = _file(reference, directory)
        if source.stat().st_size > 128 * 1024:
            raise ValueError
        document = json.loads(_unprotect(source.read_bytes()))
        if (not isinstance(document, dict) or set(document) != {"schema", "owner", "revision", "values"}
                or type(document.get("schema")) is not int or document.get("schema") != 1
                or not isinstance(document.get("owner"), str) or document.get("owner") != owner
                or type(document.get("revision")) is not int
                or document.get("revision") != revision):
            raise ValueError
        values = document["values"]
        if (not isinstance(values, dict) or not values
                or any(not isinstance(k, str) or not isinstance(v, str) for k, v in values.items())):
            raise ValueError
        return values
    except Exception:
        raise SecretUnavailable() from None


def delete(reference: str, owner: str, revision: int, *, directory=None) -> None:
    """Refuse to delete another owner's blob, including after metadata corruption."""
    source = _file(reference, directory)
    if not source.exists():
        return
    get(reference, owner, revision, directory=directory)
    try:
        source.unlink(missing_ok=True)
    except Exception:
        raise SecretUnavailable() from None
