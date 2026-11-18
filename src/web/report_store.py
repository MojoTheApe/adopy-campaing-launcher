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
# ACL-2668 (решение владельца 16.11): мгновенно показываем только свежую копию,
# не старше суток. Более старая не показывается и удаляется — байер ждёт сборку.
DEFAULT_MAX_AGE_DAYS = 1
MAX_ENTRIES = 12                    # distinct views kept on disk
MAX_ENTRY_BYTES = 40 * 1024 * 1024  # compressed file; bigger pages are not stored
MAX_TOTAL_BYTES = 150 * 1024 * 1024  # all stored files together
MAX_PAGE_CHARS = 200 * 1024 * 1024   # decompressed guard against a gzip bomb

_SUFFIX = ".json.gz"
_LOCK = threading.Lock()  # one writer/pruner at a time inside this process

# Which differing key part says what to the person looking at the page.
REASON_TEXT = {
    "code": "dashboard updated since",
    "source": "newer statistics may exist",
    "settings": "settings changed",
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

# ACL-2782: вместо большой красной плашки внизу слева и красной рамки страницы —
# одна короткая строка факта. Новая страница несёт слот #acl-status-slot в верхней
# строке статуса (рядом с Last updated / Version): скрипт переносит чип туда.
# Страница от старой сборки слота не имеет — чип остаётся маленьким, закреплённым
# сверху (под ящиком билдера, z-index 880). Ни рамки, ни тени, ни абзацев.
_STYLE_BLOCK = (
    '<style id="acl-stored-style">'
    "#acl-stored-bar{position:fixed;top:6px;right:12px;z-index:870;box-sizing:border-box;"
    "width:max-content;max-width:calc(100vw - 24px);white-space:nowrap;overflow:hidden;"
    "text-overflow:ellipsis;padding:2px 8px;border-radius:999px;font:500 12px/1.5 system-ui,sans-serif;"
    "background:#fff4d6;color:#5c4300}"
    '#acl-stored-bar[data-state="error"]{color:#b42318;background:#fdecea}'
    "#acl-stored-bar.in-line{position:static;max-width:100%;white-space:normal;font:inherit;padding:1px 6px;border-radius:5px}"
    "#acl-stored-bar a{color:inherit;text-decoration:underline;cursor:pointer;margin-left:2px}"
    "</style>"
)

_SCRIPT = """
<script>
(function () {
  // ACL-2671: полный reload — только если страница отрисована другим кодом, чем работает сервер;
  // во всех остальных случаях цифры карты аккаунтов обновляются на месте.
  var RELOAD_ON_READY = __RELOAD_ON_READY__;
  var bar = document.getElementById('acl-stored-bar');
  var note = document.getElementById('acl-stored-note');
  var original = note.textContent;
  var prefix = bar.getAttribute('data-prefix') || '';
  var timer = null;
  // ACL-2782: слот в верхней строке статуса есть только у новой страницы.
  // Скрипт стоит в начале body, слот ниже по странице: переносим, когда разметка разобрана.
  function toSlot() {
    var slot = document.getElementById('acl-status-slot');
    if (slot && bar.parentNode) { slot.appendChild(bar); bar.classList.add('in-line'); }
  }
  if (document.readyState === 'loading') { document.addEventListener('DOMContentLoaded', toSlot); } else { toSlot(); }
  function stop() { if (timer) { clearInterval(timer); timer = null; } }
  // Свежий фрагмент /account-map вместо перезагрузки: черновики билдера и идущие задания не трогаем.
  // Любая ошибка — опрос уже остановлен, страница остаётся как была.
  function swapInPlace(s) {
    fetch('/account-map' + window.location.search, {cache: 'no-store', credentials: 'same-origin'})
      .then(function (r) { if (!r.ok) { throw new Error('account-map'); } return r.text(); })
      .then(function (html) {
        if (typeof window.aclSwapAccountMap !== 'function') { return; }
        window.aclSwapAccountMap(html);
        var at = document.getElementById('acl-built-at');
        if (at && s.built_at) { at.textContent = s.built_at; }
        bar.remove();
      })
      .catch(function () {});
  }
  function poll() {
    fetch('/report/status' + window.location.search, {cache: 'no-store'})
      .then(function (r) { return r.json(); })
      .then(function (s) {
        if (s.ready) { stop(); if (RELOAD_ON_READY) { window.location.reload(); } else { swapInPlace(s); } }
        else if (s.deferred) {
          note.textContent = prefix + ' \\u00b7 ' + (s.waiting_text || 'waiting for publishes to finish') + ' \\u00b7 updates by itself';
        }
        else if (s.error) {
          stop();
          bar.setAttribute('data-state', 'error');
          note.textContent = prefix + ' \\u00b7 refresh failed: ' + s.error;
        }
        else { note.textContent = original; }
      })
      .catch(function () {});
  }
  if (bar.getAttribute('data-polling') === '1') {
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


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def waiting_phrase(facts: Optional[Dict[str, Any]]) -> str:
    """«waiting for 2 publishes (A, B) and 1 open profile window» из фактов publish_jobs.busy_facts."""
    facts = facts or {}
    names = [str(n) for n in facts.get("publishes") or []]
    count = int(facts.get("publish_count") or len(names))
    windows = int(facts.get("windows") or 0)
    parts = []
    if count:
        shown = ", ".join(names[:3]) + (f" +{len(names) - 3} more" if len(names) > 3 else "")
        parts.append(_plural(count, "publish", "publishes") + (f" ({shown})" if shown else ""))
    if windows:
        parts.append(_plural(windows, "open profile window", "open profile windows"))
    if not parts:
        return "waiting for publishes to finish"
    return "waiting for " + " and ".join(parts)


def decorate(
    stored: StoredPage, *, error: Optional[str] = None, deferred: bool = False,
    waiting: Optional[Dict[str, Any]] = None,
) -> str:
    """The stored page with one compact status line; the stored text is untouched.

    ``error`` is a failed rebuild: shown in red, polling stops.
    ``deferred``: the rebuild waits for the publish to finish (ACL-2260); ``waiting`` are
    the facts (names, window count) for the line. Polling goes on and the numbers update
    in place once the new report is ready; a full reload happens only when the stored
    page was rendered by different code (ACL-2671).
    """
    state = "outdated" if stored.outdated else "cached"
    prefix = f"Saved {_age_text(stored.saved_at)}"
    if state == "outdated":
        why = "; ".join(REASON_TEXT[r] for r in stored.reasons if r in REASON_TEXT) or "outdated"
        text = f"{prefix} · outdated: {why} · updates by itself"
    else:
        text = f"{prefix} · refreshing · updates by itself"
    if deferred and not error:
        text = f"{prefix} · {waiting_phrase(waiting)} · updates by itself"
    if error:
        state = "error"
        text = f"{prefix} · refresh failed: {error}"
    bar = (
        f'<div id="acl-stored-bar" data-state="{state}" data-polling="{0 if error else 1}" '
        f'data-prefix="{html_lib.escape(prefix, quote=True)}" role="status">'
        f'<span id="acl-stored-note">{html_lib.escape(text)}</span> '
        # ACL-2311: the saved page is from the previous build and may not have the
        # Connections button; the dialog code comes from /static (always current).
        '<a id="acl-stored-connections" role="button" tabindex="0" '
        'onclick="if(window.aclConnections){window.aclConnections.open()}return false;">Connections</a>'
        "</div>"
        '<script src="/static/connections.js"></script>'
    )
    page = stored.html
    reload_on_ready = "true" if "code" in stored.reasons else "false"
    marker = _STYLE_BLOCK + bar + _SCRIPT.replace("__RELOAD_ON_READY__", reload_on_ready)
    lower = page.lower()
    body = lower.find("<body")
    end = lower.find(">", body) + 1 if body != -1 else 0
    return page[:end] + marker + page[end:]
