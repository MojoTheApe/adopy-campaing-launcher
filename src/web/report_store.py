"""The last finished dashboard page, kept on disk (ACL-2207).

After a restart the in-memory report cache is empty and the first page used to
be the "getting your dashboard ready" screen for 3-5 minutes. This module keeps
the last finished page per view (campaign manager + period + compare inputs)
under the machine config folder so a restart can show it at once while the real
report is rebuilt in the background.

What is stored is the rendered page (text), not the report object: the report
holds DataFrames and a 70k-row workbook, the page is what the buyer saw. The
file is gzip-compressed JSON with a SHA-256 of the page, so a damaged or foreign
file is just "no stored page"; nothing in it is ever executed or unpickled.

A stored page is never presented as current when it may not be. Every stored
page carries the key it was built under:

  code      the running version (release tag / git sha) and the template files
  source    BI revision and stats workbook version
  settings  currencies / timezones, hidden accounts, the account map

``compare`` lists the parts that differ from now. Nothing differs -> "cached"
(badge "from cache, refreshing"); anything differs -> "outdated" with a red
marker that names why. Either way the caller rebuilds in the background.
"""
from __future__ import annotations

import gzip
import hashlib
import html as html_lib
import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from config.paths import _atomic_write, get_local_config_dir

SCHEMA = 1

# Caps. A page with every country is a few MB of HTML, ~10x smaller gzipped.
DEFAULT_MAX_AGE_DAYS = 14
MAX_ENTRIES = 12                    # distinct views kept on disk
MAX_ENTRY_BYTES = 40 * 1024 * 1024  # compressed file; bigger pages are not stored
MAX_TOTAL_BYTES = 150 * 1024 * 1024  # all stored files together
MAX_PAGE_CHARS = 200 * 1024 * 1024   # decompressed guard against a gzip bomb

_SUFFIX = ".json.gz"
_LOCK = threading.Lock()  # one writer/pruner at a time inside this process

# Which differing key part says what to the person looking at the page.
REASON_TEXT = {
    "code": "the dashboard has been updated since",
    "source": "newer statistics may be available",
    "settings": "currency, timezone, hidden accounts or the account map changed",
}


def enabled() -> bool:
    return (os.environ.get("ACL_REPORT_DISK_CACHE") or "1").strip().lower() not in {"0", "false", "off", "no"}


def max_age_seconds() -> float:
    try:
        days = float(os.environ.get("ACL_REPORT_CACHE_DAYS") or DEFAULT_MAX_AGE_DAYS)
    except ValueError:
        days = DEFAULT_MAX_AGE_DAYS
    return max(days, 0.0) * 86400.0


def cache_dir() -> Path:
    """Next to the other machine files, never inside the checkout."""
    return Path(get_local_config_dir()) / "report_cache"


def _normalize(value: Any) -> Any:
    """What a value looks like after a JSON round trip (tuples -> lists)."""
    return json.loads(json.dumps(value, default=str, sort_keys=True))


def entry_name(inputs: Dict[str, str]) -> str:
    raw = json.dumps(_normalize(inputs), sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32] + _SUFFIX


def compare(stored: Dict[str, Any], current: Dict[str, Any]) -> List[str]:
    """Names of the key parts that differ; empty means the stored page is current."""
    reasons = []
    for part in ("code", "source", "settings"):
        now = current.get(part)
        # A part that could not be determined now cannot vouch for the page.
        if now is None or _normalize(stored.get(part)) != _normalize(now):
            reasons.append(part)
    return reasons


@dataclass
class StoredPage:
    html: str
    saved_at: float
    reasons: List[str] = field(default_factory=list)

    @property
    def outdated(self) -> bool:
        return bool(self.reasons)


def _read_file(path: Path) -> Optional[Dict[str, Any]]:
    """The decoded entry, or None for anything that is not a valid one."""
    try:
        if path.stat().st_size > MAX_ENTRY_BYTES:
            return None
        with gzip.open(path, "rb") as fh:
            raw = fh.read(MAX_PAGE_CHARS + 1)
        if len(raw) > MAX_PAGE_CHARS:
            return None
        doc = json.loads(raw.decode("utf-8"))
        page = doc["html"]
        if (
            not isinstance(doc, dict) or doc.get("schema") != SCHEMA
            or not isinstance(page, str) or not isinstance(doc.get("key"), dict)
            or not isinstance(doc.get("inputs"), dict)
            or not isinstance(doc.get("saved_at"), (int, float))
            or hashlib.sha256(page.encode("utf-8")).hexdigest() != doc.get("html_sha256")
        ):
            return None
        return doc
    except Exception:  # noqa: BLE001 — damaged, truncated, foreign: rebuild instead
        return None


def _discard(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def exists(inputs: Dict[str, str]) -> bool:
    """Cheap stat-only check, safe on the event loop: is there a stored page at all."""
    try:
        return enabled() and (cache_dir() / entry_name(inputs)).is_file()
    except OSError:
        return False


def load(inputs: Dict[str, str], current_key: Dict[str, Any]) -> Optional[StoredPage]:
    """The stored page for this view, with what differs from now, or None."""
    if not enabled():
        return None
    path = cache_dir() / entry_name(inputs)
    if not path.is_file():
        return None
    doc = _read_file(path)
    if doc is None:
        _discard(path)
        return None
    if _normalize(doc["inputs"]) != _normalize(inputs):  # name clash or a swapped file
        return None
    if time.time() - float(doc["saved_at"]) > max_age_seconds():
        _discard(path)
        return None
    return StoredPage(doc["html"], float(doc["saved_at"]), compare(doc["key"], current_key))


def save(inputs: Dict[str, str], key: Dict[str, Any], page: str) -> bool:
    """Store the finished page atomically; False when skipped or failed.

    Never raises: losing the on-disk copy must not break the page that was
    just rendered.
    """
    if not enabled() or not page or any(key.get(p) is None for p in ("code", "source", "settings")):
        return False
    try:
        doc = {
            "schema": SCHEMA,
            "inputs": _normalize(inputs),
            "key": _normalize(key),
            "saved_at": time.time(),
            "html_sha256": hashlib.sha256(page.encode("utf-8")).hexdigest(),
            "html": page,
        }
        blob = gzip.compress(json.dumps(doc, ensure_ascii=False).encode("utf-8"), compresslevel=5)
        if len(blob) > MAX_ENTRY_BYTES:
            return False
        directory = cache_dir()
        with _LOCK:
            directory.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(directory, 0o700)  # no-op on Windows; the profile ACL applies there
            except OSError:
                pass
            _atomic_write(directory / entry_name(inputs), blob)
            _prune(directory)
        return True
    except Exception:  # noqa: BLE001
        return False


def _prune(directory: Path) -> None:
    """Drop expired and surplus entries and leftover temp files (oldest first)."""
    now = time.time()
    limit = max_age_seconds()
    kept = []
    for path in directory.iterdir():
        try:
            st = path.stat()
        except OSError:
            continue
        if path.name.endswith(".tmp"):
            if now - st.st_mtime > 3600:
                _discard(path)
        elif path.name.endswith(_SUFFIX):
            if now - st.st_mtime > limit:
                _discard(path)
            else:
                kept.append((st.st_mtime, st.st_size, path))
    kept.sort(key=lambda item: item[0], reverse=True)  # newest first
    total = 0
    for index, (_mtime, size, path) in enumerate(kept):
        total += size
        if index >= MAX_ENTRIES or total > MAX_TOTAL_BYTES:
            _discard(path)
            total -= size


# ── The marker injected into a stored page ─────────────────────────────

_BAR_STYLE = (
    "position:sticky;top:0;z-index:2147483000;display:flex;flex-wrap:wrap;gap:6px 14px;"
    "align-items:center;padding:9px 16px;font:600 13px/1.4 system-ui,sans-serif;"
    "border-bottom:2px solid {border};background:{bg};color:{fg};"
)
_STYLES = {
    "cached": dict(bg="#fff4d6", fg="#5c4300", border="#d9a400"),
    "outdated": dict(bg="#b91c1c", fg="#ffffff", border="#7f1212"),
}

_SCRIPT = """
<script>
(function () {
  var note = document.getElementById('acl-stored-note');
  var timer = null;
  function stop() { if (timer) { clearInterval(timer); timer = null; } }
  function poll() {
    fetch('/report/status' + window.location.search, {cache: 'no-store'})
      .then(function (r) { return r.json(); })
      .then(function (s) {
        if (s.ready) { stop(); window.location.reload(); }
        else if (s.error) {
          stop();
          note.textContent = 'Refresh failed: ' + s.error + ' This is the last saved report.';
        }
      })
      .catch(function () {});
  }
  if (document.getElementById('acl-stored-bar').getAttribute('data-polling') === '1') {
    timer = setInterval(poll, 3000);
    poll();
  }
})();
</script>
"""


def _age_text(saved_at: float) -> str:
    seconds = max(0, int(time.time() - saved_at))
    if seconds < 90:
        return "just now"
    if seconds < 5400:
        return f"{round(seconds / 60)} min ago"
    if seconds < 172800:
        return f"{round(seconds / 3600)} h ago"
    return f"{round(seconds / 86400)} days ago"


def decorate(stored: StoredPage, *, error: Optional[str] = None) -> str:
    """The stored page with a visible state bar; the stored text is untouched.

    ``error`` is a failed rebuild: shown in the bar, polling stops.
    """
    state = "outdated" if stored.outdated else "cached"
    if state == "outdated":
        why = "; ".join(REASON_TEXT[r] for r in stored.reasons if r in REASON_TEXT)
        head = "OUTDATED"
        text = (
            f"Saved report from {_age_text(stored.saved_at)} - {why}. "
            "Do not rely on these numbers; the report is being rebuilt."
        )
    else:
        head = "From cache, refreshing…"
        text = f"Saved report from {_age_text(stored.saved_at)}. The page reloads when the new report is ready."
    if error:
        text = f"Refresh failed: {error} This is the last saved report ({_age_text(stored.saved_at)})."
    style = _BAR_STYLE.format(**_STYLES[state])
    bar = (
        f'<div id="acl-stored-bar" data-state="{state}" data-polling="{0 if error else 1}" '
        f'role="status" style="{style}">'
        f'<span style="letter-spacing:.04em">{html_lib.escape(head)}</span>'
        f'<span id="acl-stored-note" style="font-weight:400">{html_lib.escape(text)}</span>'
        "</div>"
    )
    frame = (
        '<style>html{box-shadow:inset 0 0 0 4px #b91c1c}</style>' if state == "outdated" else ""
    )
    page = stored.html
    marker = bar + frame + _SCRIPT
    lower = page.lower()
    body = lower.find("<body")
    end = lower.find(">", body) + 1 if body != -1 else 0
    return page[:end] + marker + page[end:]
