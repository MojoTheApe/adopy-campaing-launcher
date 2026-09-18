"""Updates for the packaged Windows build from GitHub Releases (ACL-1240).

A git checkout updates through ``acl_update`` and ``start_dashboard.bat``. The
installed ``ACL.exe`` has neither git nor that script, so its channel is the
GitHub Release that ``.github/workflows/release.yml`` publishes for every
promoted ``acl-release-*`` tag: the installer, its SHA-256 and ``release.json``.

What happens here:

* **check** — at most once a day (and on demand) read ``release.json`` of the
  latest release; offer it when its version differs from the running one;
* **download** — fetch the installer into ``%LOCALAPPDATA%\\ACL\\updates`` and
  accept it only if size and SHA-256 match ``release.json``;
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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config.paths import get_local_config_dir

API = "https://api.github.com"
CHECK_EVERY_SECONDS = 24 * 3600
CHANNEL_FILE = "acl-update-channel.json"
_VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z._-]{0,63}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_lock = threading.Lock()


class UpdateError(RuntimeError):
    """Something the manager can be told in one sentence."""


@dataclass(frozen=True)
class Channel:
    repo: str
    token: str


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
    repo = (os.environ.get("ACL_RELEASES_REPO") or built.get("repo") or "").strip()
    token = (os.environ.get("ACL_UPDATE_TOKEN") or built.get("token") or "").strip()
    return Channel(repo, token) if repo and token else None


def installed_version() -> str:
    from src.packaged_runtime import build_identity
    return build_identity().get("tag", "")


# ── GitHub ────────────────────────────────────────────────────────────────

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D401 - urllib hook
        return None


def _request(url: str, ch: Channel, *, accept: str, timeout: float = 30.0) -> bytes:
    """GET with the token; a redirect to storage is followed WITHOUT the token.

    Release assets of a private repository answer with a 302 to a signed
    storage URL; sending the GitHub token there is both a leak and a 400.
    """
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {ch.token}", "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "acl-dashboard"})
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (301, 302, 303, 307, 308) and exc.headers.get("Location"):
            plain = urllib.request.Request(exc.headers["Location"], headers={"User-Agent": "acl-dashboard"})
            try:
                with urllib.request.urlopen(plain, timeout=timeout) as r:
                    return r.read()
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
        if asset.get("name") == name:
            return asset
    raise UpdateError(f"The latest release has no {name}.")


def parse_release(raw: bytes) -> dict:
    try:
        data = json.loads(raw)
        inst = data["installer"]
        ok = (data.get("schema") == 1 and _VERSION.fullmatch(str(data["version"]))
              and isinstance(inst.get("name"), str) and inst["name"].lower().endswith(".exe")
              and "/" not in inst["name"] and "\\" not in inst["name"]
              and isinstance(inst.get("size"), int) and inst["size"] > 0
              and _SHA256.fullmatch(str(inst.get("sha256", ""))))
        if not ok:
            raise ValueError("unexpected fields")
        data.setdefault("fixed", [])
        return data
    except (ValueError, KeyError, TypeError) as exc:
        raise UpdateError("The published release.json is not valid; nothing was installed.") from exc


def fetch_latest(ch: Channel) -> tuple[dict, dict]:
    """(release.json, GitHub release object) of the latest published release."""
    release = json.loads(_request(f"{API}/repos/{ch.repo}/releases/latest", ch,
                                  accept="application/vnd.github+json"))
    manifest = parse_release(_request(_asset(release, "release.json")["url"], ch,
                                      accept="application/octet-stream"))
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
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def last_attempt() -> dict:
    """What the update helper wrote about the most recent install, if anything."""
    return _read_json(updates_dir() / "last_update.json")


def check(*, force: bool = False, now: float | None = None) -> dict[str, Any]:
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
        fresh = now - float(state.get("checked_at", 0) or 0) < CHECK_EVERY_SECONDS
        if force or not fresh or state.get("installed") != current["version"]:
            try:
                manifest, _ = fetch_latest(ch)
                state = {"checked_at": now, "installed": current["version"], "release": manifest, "error": ""}
            except UpdateError as exc:
                state = {**state, "checked_at": now, "installed": current["version"], "error": str(exc)}
            _write_json(_state_path(), state)
    manifest = state.get("release")
    if not manifest:
        return {**base, "ok": False, "note": state.get("error") or "No release information yet."}
    if manifest["version"] == current["version"]:
        return {**base, "note": "ACL is up to date."}
    return {**base, "available": True,
            "candidate": {"tag": manifest.get("tag", ""), "version": manifest["version"],
                          "published_at": manifest.get("published_at", "")},
            "fixed": manifest.get("fixed", []),
            "note": f"New version {manifest['version']} is available."}


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
    inst = manifest["installer"]
    target = updates_dir() / inst["name"]
    if target.is_file() and target.stat().st_size == inst["size"] and _sha256(target) == inst["sha256"]:
        return target, manifest
    data = _request(_asset(release, inst["name"])["url"], ch, accept="application/octet-stream", timeout=600)
    if len(data) != inst["size"] or hashlib.sha256(data).hexdigest() != inst["sha256"]:
        raise UpdateError("The downloaded installer does not match its published SHA-256; it was discarded.")
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".partial")
    partial.write_bytes(data)
    os.replace(partial, target)
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
    subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, close_fds=True, creationflags=flags)
    _write_json(updates_dir() / "pending.json", {
        "from": installed_version(), "to": manifest["version"],
        "requested_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat()})
