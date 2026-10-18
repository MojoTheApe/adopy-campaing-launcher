"""Machine secrets in Windows protected storage (DPAPI, current user) - ACL-1358.

What is protected:
  * ``MULTILOGIN_AUTOMATION_TOKEN_*`` lines of the machine ``.env`` - the value
    becomes ``dpapi:v1:<base64>``; the process environment still holds the
    plain value after ``load_env`` so no reader has to change.
  * Google token files (``token_accounts.json``, ``token_telemetry.json``) -
    same path, content becomes ``ACLDPAPI1\\n`` + ciphertext.

DPAPI binds the ciphertext to the Windows user: another account or a copied
file cannot decrypt it, and a support bundle or backup of the folder carries no
usable secret. On a machine without DPAPI (tests, Linux, macOS) everything
falls back to the old plain-text behaviour, so nothing is lost there.

Migration is one-way but safe: the original bytes are first saved as an
encrypted backup, the protected result is decrypted again and compared, and
only then is the plain text replaced. Any failure leaves the file untouched.

Secret values are never logged or put into exception messages.
"""
from __future__ import annotations

import base64
import ctypes
import os
import re
import time
from pathlib import Path

ENV_PREFIX = "dpapi:v1:"
FILE_MAGIC = b"ACLDPAPI1\n"
SECRET_ENV_KEY = re.compile(r"^MULTILOGIN_AUTOMATION_TOKEN_[A-Z0-9]+$")
TOKEN_FILES = ("token_accounts.json", "token_telemetry.json")
BACKUP_DIR_NAME = "protected-backup"
BACKUPS_KEPT = 3
_ENTROPY = b"ACL-token-vault-v1"


class VaultError(RuntimeError):
    """Safe error: never contains a secret, an OS message or a path."""


def available() -> bool:
    return os.name == "nt"


def _dpapi(data: bytes, *, decrypt: bool) -> bytes:
    if not available():
        raise VaultError("protected storage is not available on this system")
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    def make(raw: bytes):
        buf = ctypes.create_string_buffer(raw, len(raw) or 1)
        return Blob(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))), buf

    source, keep1 = make(data)
    entropy, keep2 = make(_ENTROPY)
    result = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob),
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    try:
        # Flag 1 = CRYPTPROTECT_UI_FORBIDDEN. Scope is the current user.
        if not function(ctypes.byref(source), None, ctypes.byref(entropy), None, None, 1,
                        ctypes.byref(result)):
            raise VaultError("protected storage could not process the value")
        return ctypes.string_at(result.data, result.size)
    finally:
        if result.data:
            kernel.LocalFree(result.data)
        del keep1, keep2


def protect_bytes(data: bytes) -> bytes:
    return _dpapi(data, decrypt=False)


def unprotect_bytes(data: bytes) -> bytes:
    return _dpapi(data, decrypt=True)


# ── .env values ───────────────────────────────────────────────────────────────

def is_secret_env_key(key: str) -> bool:
    return bool(SECRET_ENV_KEY.fullmatch(key or ""))


def is_protected_value(value: str) -> bool:
    return isinstance(value, str) and value.startswith(ENV_PREFIX)


def encode_env_value(plain: str) -> str:
    """Protected form for a .env line, or the plain value where DPAPI is absent."""
    if not plain or is_protected_value(plain) or not available():
        return plain
    return ENV_PREFIX + base64.b64encode(protect_bytes(plain.encode("utf-8"))).decode("ascii")


def decode_env_value(value: str) -> str:
    """Plain value for a stored one. Raises VaultError if it cannot be opened."""
    if not is_protected_value(value):
        return value
    try:
        return unprotect_bytes(base64.b64decode(value[len(ENV_PREFIX):], validate=True)).decode("utf-8")
    except VaultError:
        raise
    except Exception:
        raise VaultError("protected value is damaged") from None


def decrypt_environment(environ=None) -> list[str]:
    """Replace protected secret values in ``environ`` by plain ones.

    Keys that cannot be opened (other Windows user, damaged value) are removed,
    so a reader sees "no token" and asks to reconnect instead of sending a blob
    as a bearer token. Returns the names of removed keys.
    """
    env = os.environ if environ is None else environ
    failed: list[str] = []
    for key in [k for k in list(env) if is_secret_env_key(k)]:
        value = env.get(key, "")
        if not is_protected_value(value):
            continue
        try:
            env[key] = decode_env_value(value)
        except VaultError:
            env.pop(key, None)
            failed.append(key)
    return failed


# ── secret files ──────────────────────────────────────────────────────────────

def is_protected_bytes(raw: bytes) -> bool:
    return raw.startswith(FILE_MAGIC)


def read_secret_bytes(path: str | Path) -> bytes:
    raw = Path(path).read_bytes()
    if is_protected_bytes(raw):
        return unprotect_bytes(raw[len(FILE_MAGIC):])
    return raw


def read_secret_text(path: str | Path) -> str:
    """Contents of a possibly protected file. OSError if absent, VaultError if sealed."""
    return read_secret_bytes(path).decode("utf-8")


def write_secret_text(path: str | Path, text: str) -> None:
    """Atomically write ``text`` protected (plain where DPAPI is absent)."""
    from config.paths import _atomic_write

    data = text.encode("utf-8")
    if available():
        data = FILE_MAGIC + protect_bytes(data)
    _atomic_write(path, data)


# ── migration ─────────────────────────────────────────────────────────────────

def _backup(original: bytes, name: str, config_dir: Path) -> Path:
    """Encrypted copy of the pre-migration bytes; the newest few are kept."""
    from config.paths import _atomic_write

    folder = config_dir / BACKUP_DIR_NAME
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    target = folder / f"{name}.{stamp}.dpapi"
    _atomic_write(target, FILE_MAGIC + protect_bytes(original))
    old = sorted(folder.glob(f"{name}.*.dpapi"))
    for stale in old[:-BACKUPS_KEPT]:
        try:
            stale.unlink()
        except OSError:
            pass
    return target


def migrate_file(path: str | Path, config_dir: str | Path) -> str | None:
    """Protect one plain secret file in place. Returns a note or None."""
    target = Path(path)
    if not available() or not target.is_file():
        return None
    from config.paths import _atomic_write

    try:
        raw = target.read_bytes()
        if is_protected_bytes(raw) or not raw.strip():
            return None
        _backup(raw, target.name, Path(config_dir))
        sealed = FILE_MAGIC + protect_bytes(raw)
        if unprotect_bytes(sealed[len(FILE_MAGIC):]) != raw:
            return f"{target.name}: protected copy did not verify; left as it was"
        _atomic_write(target, sealed)
        return f"{target.name} moved to protected storage (encrypted backup kept)"
    except Exception as exc:
        return f"{target.name}: could not move to protected storage ({type(exc).__name__}); left as it was"


_ENV_LINE = re.compile(r"^(\s*(?:export\s+)?)([A-Z][A-Z0-9_]*)(\s*=\s*)(.*)$")


def _unquote(raw: str) -> str:
    import json

    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        try:
            return json.loads(raw)
        except ValueError:
            return raw[1:-1]
    if len(raw) >= 2 and raw[0] == raw[-1] == "'":
        return raw[1:-1]
    return raw.split(" #", 1)[0].strip()


def migrate_env_file(env_path: str | Path, config_dir: str | Path) -> str | None:
    """Replace plain MULTILOGIN_AUTOMATION_TOKEN_* values in .env by protected ones."""
    import json

    from config.paths import _atomic_write

    target = Path(env_path)
    if not available() or not target.is_file():
        return None
    try:
        original = target.read_bytes()
        lines = original.decode("utf-8").splitlines()
        out: list[str] = []
        changed = 0
        for line in lines:
            m = _ENV_LINE.match(line)
            if m and is_secret_env_key(m.group(2)):
                plain = _unquote(m.group(4))
                if plain and not is_protected_value(plain):
                    sealed = encode_env_value(plain)
                    if decode_env_value(sealed) != plain:
                        return ".env: protected copy did not verify; left as it was"
                    line = f"{m.group(1)}{m.group(2)}{m.group(3)}{json.dumps(sealed)}"
                    changed += 1
            out.append(line)
        if not changed:
            return None
        _backup(original, target.name, Path(config_dir))
        _atomic_write(target, ("\n".join(out) + "\n").encode("utf-8"))
        return f".env: {changed} Multilogin token(s) moved to protected storage (encrypted backup kept)"
    except Exception as exc:
        return f".env: could not move tokens to protected storage ({type(exc).__name__}); left as it was"
