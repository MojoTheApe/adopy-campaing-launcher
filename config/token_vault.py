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

Rollback: ``unprotect_*`` write the plain text back for the same Windows user
(``scripts/acl_migrate_config.py --unprotect``), from the live files or, when a
live file cannot be opened, from the newest encrypted backup in
``protected-backup/``. Run it before going back to a version older than ACL-1358.

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
TOKEN_LOCK_NAME = ".tokens.lock"
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
    from config.paths import _atomic_write, get_local_config_dir, machine_file_lock

    data = text.encode("utf-8")
    if available():
        data = FILE_MAGIC + protect_bytes(data)
    with machine_file_lock(Path(get_local_config_dir()) / TOKEN_LOCK_NAME):
        _atomic_write(path, data)
    reset_secret_cache()


# ── migration ─────────────────────────────────────────────────────────────────

def backup_key(path: str | Path, config_dir: str | Path) -> str:
    """Backup name for a file: its name inside the config folder, name plus a
    short hash of the full path for a file living elsewhere (GSHEET_OAUTH_TOKEN)."""
    import hashlib

    target = Path(path)
    try:
        inside = target.resolve().parent == Path(config_dir).resolve()
    except OSError:
        inside = True
    if inside:
        return target.name
    digest = hashlib.sha1(str(target.resolve()).lower().encode("utf-8", "replace")).hexdigest()[:8]
    return f"{target.name}.{digest}"


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
        _backup(raw, backup_key(target, config_dir), Path(config_dir))
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
        _backup(original, backup_key(target, config_dir), Path(config_dir))
        _atomic_write(target, ("\n".join(out) + "\n").encode("utf-8"))
        return f".env: {changed} Multilogin token(s) moved to protected storage (encrypted backup kept)"
    except Exception as exc:
        return f".env: could not move tokens to protected storage ({type(exc).__name__}); left as it was"


# ── rollback / restore ────────────────────────────────────────────────────────

def backups_of(name: str, config_dir: str | Path) -> list[Path]:
    """Encrypted backups of one file, oldest first."""
    folder = Path(config_dir) / BACKUP_DIR_NAME
    return sorted(folder.glob(f"{name}.*.dpapi")) if folder.is_dir() else []


def _open_backup(path: Path) -> bytes:
    raw = path.read_bytes()
    if not is_protected_bytes(raw):
        raise VaultError("backup is not in protected form")
    return unprotect_bytes(raw[len(FILE_MAGIC):])


def _newest_backup_bytes(name: str, config_dir: Path) -> bytes | None:
    for candidate in reversed(backups_of(name, config_dir)):
        try:
            return _open_backup(candidate)
        except Exception:
            continue
    return None


def unprotect_file(path: str | Path, config_dir: str | Path, *, from_backup: bool = False) -> str | None:
    """Write one protected secret file back as plain text. Returns a note or None.

    ``from_backup`` (or a live file that cannot be opened) restores the newest
    readable encrypted backup instead. A plain file is left alone.
    """
    target = Path(path)
    if not available():
        return None
    from config.paths import _atomic_write

    try:
        plain: bytes | None = None
        source = "protected file"
        if target.is_file() and not from_backup:
            raw = target.read_bytes()
            if not is_protected_bytes(raw):
                return None
            try:
                plain = unprotect_bytes(raw[len(FILE_MAGIC):])
            except VaultError:
                plain = None
        if plain is None:
            plain = _newest_backup_bytes(backup_key(target, config_dir), Path(config_dir))
            source = "encrypted backup"
        if plain is None:
            if target.is_file() and not from_backup:
                return f"{target.name}: cannot be opened and has no readable backup; left as it was"
            return None
        _atomic_write(target, plain)
        return f"{target.name} restored to plain text from the {source}"
    except Exception as exc:
        return f"{target.name}: could not restore ({type(exc).__name__}); left as it was"


def _env_tokens_from_backup(target: Path, config_dir: str | Path) -> str | None:
    """Put the token lines of the newest .env backup back; other settings stay as they are now."""
    from config.paths import _atomic_write, _dotenv_line

    backup = _newest_backup_bytes(backup_key(target, config_dir), Path(config_dir))
    if backup is None:
        return None
    wanted: dict[str, str] = {}
    for line in backup.decode("utf-8", "replace").splitlines():
        m = _ENV_LINE.match(line)
        if m and is_secret_env_key(m.group(2)):
            value = _unquote(m.group(4))
            if value:
                wanted[m.group(2)] = value
    if not wanted:
        return None
    current = target.read_bytes().decode("utf-8").splitlines() if target.is_file() else []
    out: list[str] = []
    for line in current:
        m = _ENV_LINE.match(line)
        if m and m.group(2) in wanted:
            continue  # replaced below; also drops duplicate assignments
        out.append(line)
    out.extend(_dotenv_line(key, value) for key, value in wanted.items())
    _atomic_write(target, ("\n".join(out) + "\n").encode("utf-8"))
    return f".env: {len(wanted)} Multilogin token(s) restored from the encrypted backup (other settings kept)"


def unprotect_env_file(env_path: str | Path, config_dir: str | Path, *, from_backup: bool = False) -> str | None:
    """Replace ``dpapi:v1:`` values in .env by plain ones (same Windows user)."""
    target = Path(env_path)
    if not available():
        return None
    from config.paths import _atomic_write, _dotenv_line

    try:
        if from_backup:
            return _env_tokens_from_backup(target, config_dir)
        if not target.is_file():
            return None
        lines = target.read_bytes().decode("utf-8").splitlines()
        out: list[str] = []
        changed = 0
        unreadable = 0
        for line in lines:
            m = _ENV_LINE.match(line)
            if m and is_secret_env_key(m.group(2)):
                stored = _unquote(m.group(4))
                if is_protected_value(stored):
                    try:
                        line = _dotenv_line(m.group(2), decode_env_value(stored))
                        changed += 1
                    except VaultError:
                        unreadable += 1
            out.append(line)
        if unreadable and not changed:
            note = _env_tokens_from_backup(target, config_dir)
            if note:
                return note
            return ".env: tokens cannot be opened (other Windows user?) and there is no readable backup; left as it was"
        if not changed:
            return None
        _atomic_write(target, ("\n".join(out) + "\n").encode("utf-8"))
        note = f".env: {changed} Multilogin token(s) restored to plain text"
        if unreadable:
            note += f"; {unreadable} could not be opened and stay protected"
        return note
    except Exception as exc:
        return f".env: could not restore tokens ({type(exc).__name__}); left as it was"


# ── configured values, for scrubbers ──────────────────────────────────────────

_GOOGLE_SECRET_KEYS = frozenset({"refresh_token", "token", "access_token", "id_token", "client_secret"})
_file_cache: dict[str, tuple[tuple[int, int], frozenset[str]]] = {}


def _google_values(path: Path) -> frozenset[str]:
    import json

    try:
        info = path.stat()
        signature = (info.st_mtime_ns, info.st_size)
    except OSError:
        return frozenset()
    cached = _file_cache.get(str(path))
    if cached and cached[0] == signature:
        return cached[1]
    values: frozenset[str] = frozenset()
    try:
        document = json.loads(read_secret_text(path))
        if isinstance(document, dict):
            values = frozenset(v for k, v in document.items()
                               if k in _GOOGLE_SECRET_KEYS and isinstance(v, str) and len(v) >= 8)
    except Exception:
        values = frozenset()
    _file_cache[str(path)] = (signature, values)
    return values


_SECRETS_TTL_S = 5.0
_secrets_cache: tuple[float, frozenset[str], tuple[str, ...]] | None = None


def reset_secret_cache() -> None:
    """Forget the cached values (tokens were written, a test changed the machine)."""
    global _secrets_cache
    _secrets_cache = None
    _file_cache.clear()


def _collect_secret_values() -> set[str]:
    values: set[str] = set()
    try:
        for key, value in list(os.environ.items()):
            if is_secret_env_key(key) and value:
                if is_protected_value(value):
                    try:
                        value = decode_env_value(value)
                    except VaultError:
                        continue
                if len(value) >= 8:
                    values.add(value)
    except Exception:
        pass
    try:
        from config.paths import get_google_oauth_token_path, get_local_config_dir

        folder = Path(get_local_config_dir())
        paths = {Path(get_google_oauth_token_path())} | {folder / name for name in TOKEN_FILES}
        for path in paths:
            values |= _google_values(path)
    except Exception:
        pass
    return values


def _secrets_snapshot() -> tuple[frozenset[str], tuple[str, ...]]:
    global _secrets_cache
    now = time.monotonic()
    cached = _secrets_cache
    if cached is not None and 0 <= now - cached[0] < _SECRETS_TTL_S:
        return cached[1], cached[2]
    values = frozenset(_collect_secret_values())
    ordered = tuple(sorted(values, key=len, reverse=True))
    _secrets_cache = (now, values, ordered)
    return values, ordered


def configured_secret_values() -> set[str]:
    """Plain values of this machine's tokens, for scrubbers that must hide them.

    Multilogin automation tokens from the process environment and the secrets
    in the Google token files. Never raises, never logs; a value shorter than
    8 characters is ignored so a placeholder cannot blank ordinary text. The
    answer is cached for a few seconds: scrubbers call this per string, and the
    environment walk plus file checks must not run each time.
    """
    return set(_secrets_snapshot()[0])


def hide_configured_secrets(text: str, replacement: str = "<hidden>") -> str:
    """``text`` without any configured token value (longest first). Never raises."""
    try:
        for value in _secrets_snapshot()[1]:
            if value in text:
                text = text.replace(value, replacement)
    except Exception:
        pass
    return text
