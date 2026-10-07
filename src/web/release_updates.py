"""Updates for the packaged Windows build from GitHub Releases (ACL-1240).

A git checkout updates through ``acl_update`` and ``start_dashboard.bat``. The
installed ``ACL.exe`` has neither git nor that script, so its channel is the
GitHub Release that ``.github/workflows/release.yml`` publishes for every
promoted ``acl-release-*`` tag: the installer, its SHA-256 and ``release.json``.

What happens here:

* **check** — at most once a day (and on demand) read ``release.json`` of the
  latest release; offer it when its version differs from the running one;
* **trust** — ``release.json`` must carry a publisher signature
  (``release.json.sig``) that verifies with a key pinned in this client
  (``release_signing``, ACL-1357). Staged: today a missing signature is
  reported and tolerated, a present-but-wrong one is refused; enforcement
  refuses both;
* **download** — fetch the installer into ``%LOCALAPPDATA%\\ACL\\updates`` and
  accept it only if size and SHA-256 match the (signed) ``release.json``;
* **hand-off** — start ``ACL.exe --apply-update`` (``packaging/windows/updater.py``)
  and let the server exit: the helper installs silently, starts the new version
  and, if it does not come up healthy, starts the previous one again.

The read-only token and the releases repository come from the build
(``acl-update-channel.json`` next to the bundle) or, for tests and support,
from ``ACL_RELEASES_REPO`` / ``ACL_UPDATE_TOKEN``. The token never leaves this
module: no response, log line or error message carries it.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config.paths import get_local_config_dir, _atomic_write
from src.web import release_signing

API = "https://api.github.com"
CHECK_EVERY_SECONDS = 24 * 3600
CHANNEL_FILE = "acl-update-channel.json"
_VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z._-]{0,63}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_lock = threading.Lock()
apply_lock = threading.Lock()
MAX_INSTALLER_BYTES = 2 * 1024 * 1024 * 1024


class UpdateError(RuntimeError):
    """Something the manager can be told in one sentence."""


class UntrustedRelease(UpdateError):
    """The release failed the publisher-signature check (ACL-1357)."""


@dataclass(frozen=True)
class Channel:
    repo: str
    token: str = field(repr=False)


def updates_dir() -> Path:
    return Path(get_local_config_dir()) / "updates"


def _bundle_channel() -> dict:
    root = getattr(sys, "_MEIPASS", None)
    if not root:
        return {}
    try:
        data = json.loads((Path(root) / CHANNEL_FILE).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def channel() -> Channel | None:
    built = _bundle_channel()
    repo = os.environ.get("ACL_RELEASES_REPO") or built.get("repo") or ""
    token = os.environ.get("ACL_UPDATE_TOKEN") or built.get("token") or ""
    if not isinstance(repo, str) or not isinstance(token, str):
        raise UpdateError("The update channel configuration is invalid.")
    repo, token = repo.strip(), token.strip()
    if repo and token:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or any(c in token for c in "\r\n"):
            raise UpdateError("The update channel configuration is invalid.")
        return Channel(repo, token)
    return None


def installed_version() -> str:
    from src.packaged_runtime import build_identity
    return build_identity().get("tag", "")


# ── GitHub ────────────────────────────────────────────────────────────────

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D401 - urllib hook
        return None


def _request(url: str, ch: Channel, *, accept: str, timeout: float = 30.0,
             destination: Path | None = None, limit: int = 2 * 1024 * 1024) -> bytes:
    """GET with the token; a redirect to storage is followed WITHOUT the token.

    Release assets of a private repository answer with a 302 to a signed
    storage URL; sending the GitHub token there is both a leak and a 400.
    """
    parsed, api = urlsplit(url), urlsplit(API)
    if (parsed.scheme, parsed.netloc) != (api.scheme, api.netloc) or parsed.username or parsed.password:
        raise UpdateError("The release asset is not on the configured GitHub API.")

    def receive(response):
        if destination is None:
            data = response.read(limit + 1)
            if len(data) > limit:
                raise UpdateError("The update response is larger than expected.")
            return data
        size = 0
        with destination.open("wb") as output:
            while chunk := response.read(1 << 20):
                size += len(chunk)
                if size > limit:
                    raise UpdateError("The downloaded installer does not match its published SHA-256 size.")
                output.write(chunk)
        return b""

    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {ch.token}", "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "acl-dashboard"})
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=timeout) as r:
            return receive(r)
    except urllib.error.HTTPError as exc:
        if exc.code in (301, 302, 303, 307, 308) and exc.headers.get("Location"):
            location = exc.headers["Location"]
            redirect = urlsplit(location)
            if redirect.scheme != "https" and not (api.scheme == "http" and redirect.netloc == api.netloc):
                raise UpdateError("The download redirect must use HTTPS.")
            plain = urllib.request.Request(location, headers={"User-Agent": "acl-dashboard"})
            try:
                with urllib.request.urlopen(plain, timeout=timeout) as r:
                    return receive(r)
            except (urllib.error.URLError, OSError) as inner:
                raise UpdateError(f"The update could not be downloaded: {getattr(inner, 'reason', inner)}") from inner
        if exc.code in (401, 403):
            raise UpdateError("The update channel refused access: the build's release token is missing or expired.") from exc
        if exc.code == 404:
            raise UpdateError("No published release was found in the update channel.") from exc
        raise UpdateError(f"GitHub answered HTTP {exc.code}.") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise UpdateError(f"GitHub is unreachable: {getattr(exc, 'reason', exc)}") from exc


def _asset(release: dict, name: str) -> dict:
    for asset in release.get("assets") or []:
        if isinstance(asset, dict) and asset.get("name") == name and isinstance(asset.get("url"), str):
            return asset
    raise UpdateError(f"The latest release has no {name}.")


def parse_release(raw: bytes) -> dict:
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or not isinstance(data.get("installer"), dict):
            raise ValueError("expected objects")
        inst = data["installer"]
        ok = (type(data.get("schema")) is int and data["schema"] == 1
              and isinstance(data.get("version"), str) and _VERSION.fullmatch(data["version"])
              and isinstance(inst.get("name"), str) and inst["name"].lower().endswith(".exe")
              and not re.search(r'[<>:"/\\|?*\x00-\x1f]', inst["name"])
              and not re.fullmatch(r"(?i:con|prn|aux|nul|com[1-9]|lpt[1-9])\.exe", inst["name"])
              and type(inst.get("size")) is int and 0 < inst["size"] <= MAX_INSTALLER_BYTES
              and _SHA256.fullmatch(str(inst.get("sha256", ""))))
        if not ok:
            raise ValueError("unexpected fields")
        fixed = data.setdefault("fixed", [])
        if not isinstance(fixed, list) or any(
                not isinstance(entry, dict) or not isinstance(entry.get("issue"), str)
                or not isinstance(entry.get("notes", []), list)
                or not all(isinstance(note, str) for note in entry.get("notes", [])) for entry in fixed):
            raise ValueError("invalid release notes")
        return data
    except (ValueError, KeyError, TypeError) as exc:
        raise UpdateError("The published release.json is not valid; nothing was installed.") from exc


def verify_signature(release: dict, raw_manifest: bytes, ch: Channel) -> dict[str, str]:
    """Check the publisher signature of release.json; raise UpdateError if refused.

    The signature is checked over the exact downloaded bytes before anything in
    them is believed — including the installer SHA-256 the download relies on.
    """
    signature = None
    for asset in release.get("assets") or []:
        if (isinstance(asset, dict) and asset.get("name") == release_signing.SIGNATURE_ASSET
                and isinstance(asset.get("url"), str)):
            signature = _request(asset["url"], ch, accept="application/octet-stream", limit=4096)
            break
    verdict = release_signing.verify_manifest(raw_manifest, signature)
    current = release_signing.mode(_bundle_channel())
    release_signing.report(verdict, current, str(release.get("tag_name") or ""))
    allowed, message = release_signing.decide(verdict, current)
    if not allowed:
        raise UntrustedRelease(message)
    return {"status": verdict.status, "mode": current, "key_id": verdict.key_id, "note": message}


def fetch_latest(ch: Channel) -> tuple[dict, dict]:
    """(release.json, GitHub release object) of the latest published release."""
    release = json.loads(_request(f"{API}/repos/{ch.repo}/releases/latest", ch,
                                  accept="application/vnd.github+json"))
    if not isinstance(release, dict) or not isinstance(release.get("assets"), list):
        raise UpdateError("The GitHub release response is invalid.")
    raw = _request(_asset(release, "release.json")["url"], ch, accept="application/octet-stream")
    trust = verify_signature(release, raw, ch)
    manifest = parse_release(raw)
    # Подписанный release.json другого выпуска, подложенный в этот, — тоже подмена.
    # Для подписанного манифеста tag обязателен: подпись без привязки к выпуску
    # позволила бы переложить старый подписанный release.json в новый релиз.
    tag = release.get("tag_name")
    if trust["status"] == release_signing.VALID:
        if not isinstance(tag, str) or not tag or manifest.get("tag") != tag:
            raise UntrustedRelease(
                "The published release.json belongs to a different release; nothing was installed.")
    elif isinstance(tag, str) and tag and manifest.get("tag") and manifest["tag"] != tag:
        raise UntrustedRelease("The published release.json belongs to a different release; nothing was installed.")
    release["_acl_signature"] = trust
    return manifest, release


# ── проверка раз в сутки ──────────────────────────────────────────────────

def _state_path() -> Path:
    return updates_dir() / "check.json"


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path: Path, data: dict) -> None:
    _atomic_write(path, (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))


def last_attempt() -> dict:
    """What the update helper wrote about the most recent install, if anything."""
    return _read_json(updates_dir() / "last_update.json")


def _check(*, force: bool = False, now: float | None = None) -> dict[str, Any]:
    """Offer the latest release if it is not the running one. Never raises."""
    now = time.time() if now is None else now
    current = {"version": installed_version()}
    base = {"ok": True, "available": False, "channel": "release", "current": current,
            "candidate": None, "fixed": [], "supervised": True, "last_attempt": last_attempt()}
    ch = channel()
    if ch is None:
        return {**base, "note": "Updates are not configured in this build."}
    with _lock:
        state = _read_json(_state_path())
        if state.get("repo") != ch.repo:
            state = {}
        fresh = now - float(state.get("checked_at", 0) or 0) < CHECK_EVERY_SECONDS
        if force or not fresh or state.get("installed") != current["version"]:
            try:
                manifest, release = fetch_latest(ch)
                state = {"repo": ch.repo, "checked_at": now, "installed": current["version"], "release": manifest,
                         "signature": release.get("_acl_signature") or {}, "error": ""}
            except UntrustedRelease as exc:
                # Отвергнутый выпуск не остаётся в кэше «доступным»: иначе кнопка
                # предлагала бы то, что проверка подписи только что отклонила.
                state = {"repo": ch.repo, "checked_at": now, "installed": current["version"], "error": str(exc)}
            except (UpdateError, ValueError, TypeError, KeyError, OSError) as exc:
                state = {**state, "repo": ch.repo, "checked_at": now, "installed": current["version"], "error": str(exc)}
            _write_json(_state_path(), state)
    manifest = state.get("release")
    if not manifest:
        return {**base, "ok": False, "note": state.get("error") or "No release information yet."}
    manifest = parse_release(json.dumps(manifest).encode("utf-8"))
    if not is_newer(manifest["version"], current["version"]):
        return {**base, "note": "ACM is up to date."}
    return {**base, "available": True, "signature": state.get("signature") or {},
            "candidate": {"tag": manifest.get("tag", ""), "version": manifest["version"],
                          "published_at": manifest.get("published_at", "")},
            "fixed": manifest.get("fixed", []),
            "note": state.get("error") or f"New version {manifest['version']} is available."}


def is_newer(candidate: str, installed: str) -> bool:
    # Releases use date versions with optional suffixes; numeric components
    # also order preview builds without treating 10 as older than 9.
    def key(value):
        return tuple((1, int(part)) if part.isdigit() else (0, part.casefold())
                     for part in re.findall(r"\d+|\D+", value))
    return bool(candidate and key(candidate) > key(installed))


def check(*, force: bool = False, now: float | None = None) -> dict[str, Any]:
    try:
        return _check(force=force, now=now)
    except (UpdateError, ValueError, TypeError, KeyError, OSError) as exc:
        note = str(exc) if isinstance(exc, UpdateError) else "Update information could not be read; try again later."
        return {"ok": False, "available": False, "channel": "release", "supervised": True, "note": note}


# ── скачивание и передача помощнику ───────────────────────────────────────

def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(ch: Channel | None = None) -> tuple[Path, dict]:
    """The verified installer of the latest release, downloaded now or earlier."""
    ch = ch or channel()
    if ch is None:
        raise UpdateError("Updates are not configured in this build.")
    manifest, release = fetch_latest(ch)
    if not is_newer(manifest["version"], installed_version()):
        raise UpdateError("ACM is already on this release or a newer version.")
    inst = manifest["installer"]
    target = updates_dir() / inst["name"]
    if target.is_file() and target.stat().st_size == inst["size"] and _sha256(target) == inst["sha256"]:
        return target, manifest
    target.parent.mkdir(parents=True, exist_ok=True)
    import tempfile
    descriptor, name = tempfile.mkstemp(prefix=".installer-", suffix=".partial", dir=target.parent)
    os.close(descriptor)
    partial = Path(name)
    try:
        _request(_asset(release, inst["name"])["url"], ch, accept="application/octet-stream", timeout=600,
                 destination=partial, limit=inst["size"])
        if partial.stat().st_size != inst["size"] or _sha256(partial) != inst["sha256"]:
            raise UpdateError("The downloaded installer does not match its published SHA-256; it was discarded.")
        os.replace(partial, target)
    finally:
        partial.unlink(missing_ok=True)
    return target, manifest


def start_helper(setup: Path, manifest: dict) -> None:
    """Start the detached helper that installs after this server exits."""
    if not getattr(sys, "frozen", False):
        raise UpdateError("Only the installed ACL.exe can install release updates.")
    command = [sys.executable, "--apply-update", str(setup),
               "--expect-version", manifest["version"], "--previous-exe", sys.executable]
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008  # DETACHED_PROCESS
    pending = updates_dir() / "pending.json"
    _write_json(pending, {"from": installed_version(), "to": manifest["version"],
                         "requested_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat()})
    try:
        subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True, creationflags=flags,
                         env={**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"})
    except OSError as exc:
        pending.unlink(missing_ok=True)
        raise UpdateError("The update helper could not start. ACM is still running.") from exc
