#!/usr/bin/env python
"""ACL-1782: ежедневный наблюдатель за официальной документацией Meta.

Читает публичные страницы из config/meta_rules/sources.json (только чтение,
без токенов и без обращений к рекламным кабинетам), вытаскивает основной
текст, сравнивает со снимком прошлого запуска и пишет дайджест (JSON + Markdown).
Снимки лежат машинно-локально: <get_local_config_dir()>/meta_watch (не в git).

Запуск:
    python scripts/meta_docs_watch.py [--dry-run] [--sources FILE] [--out DIR]
                                      [--snapshots DIR] [--category CAT ...]

Каталог переопределяется флагами или переменной ACL_META_WATCH_DIR.
Дайджест — сигнал для навыка meta-builder-sync; сам наблюдатель реестр
правил и код не трогает.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import difflib
import hashlib
import json
import os
import re
import sys
import time
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCES = ROOT / "config" / "meta_rules" / "sources.json"
USER_AGENT = "ACL-meta-docs-watch/1.0 (read-only)"
TIMEOUT = 30
MAX_LINES_IN_SUMMARY = 6

# Порядок важен: более строгий статус проверяется раньше.
STATUS_PATTERNS = [
    ("deprecated", re.compile(r"deprecat|sunset|no longer (?:supported|available)|end of support|removed", re.I)),
    ("beta", re.compile(r"\bbeta\b", re.I)),
    ("limited", re.compile(r"limited (?:availability|release|rollout)|gradual(?:ly)? roll", re.I)),
    ("test", re.compile(r"\btesting\b|\bpilot\b|\bexperiment", re.I)),
    ("announced", re.compile(r"\bannounc|coming soon|will (?:be )?(?:launch|available|roll)|\bupcoming\b", re.I)),
    ("ga", re.compile(r"generally available|\bnow available\b|\bavailable to all\b|\bGA\b", re.I)),
]
FIELD_RE = re.compile(r"\b(?:[A-Z][A-Z0-9]+(?:_[A-Z0-9]+)+|[a-z]+(?:_[a-z0-9]+)+|v\d{2}\.\d)\b")
SKIP_TAGS = {"script", "style", "noscript", "nav", "header", "footer", "svg", "form", "aside", "template"}
BLOCK_TAGS = {"p", "div", "li", "tr", "br", "h1", "h2", "h3", "h4", "h5", "h6",
              "section", "article", "pre", "table", "td", "th"}


class _TextExtractor(HTMLParser):
    """Достаёт текст из <main>/<article>, а если их нет — из всего документа."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip = 0
        self._stack: list[str] = []
        self.parts_all: list[str] = []
        self.parts_main: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self._skip += 1
        if tag in ("main", "article") or dict(attrs).get("role") == "main":
            self._stack.append(tag)
        if tag in BLOCK_TAGS:
            self._emit("\n")

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS and self._skip:
            self._skip -= 1
        if tag in ("main", "article") and tag in self._stack:
            self._stack.remove(tag)
        if tag in BLOCK_TAGS:
            self._emit("\n")

    def handle_data(self, data):
        if not self._skip:
            self._emit(data)

    def _emit(self, s: str) -> None:
        self.parts_all.append(s)
        if self._stack:
            self.parts_main.append(s)


def extract_main_text(html: str) -> str:
    """HTML -> нормализованный текст (по строке на блок, без пустых и дублей подряд)."""
    p = _TextExtractor()
    p.feed(html)
    parts = p.parts_main if "".join(p.parts_main).strip() else p.parts_all
    lines: list[str] = []
    for raw in "".join(parts).split("\n"):
        line = re.sub(r"\s+", " ", raw).strip()
        if line and (not lines or lines[-1] != line):
            lines.append(line)
    return "\n".join(lines)


def http_fetch(url: str) -> str:
    """Боевой загрузчик: GET публичной страницы, до двух попыток."""
    last: Exception | None = None
    for _ in range(2):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read()
                charset = resp.headers.get_content_charset() or "utf-8"
            return raw.decode(charset, errors="replace")
        except Exception as exc:  # noqa: BLE001 - любая сетевая ошибка = ошибка источника
            last = exc
            time.sleep(1.5)
    raise last  # type: ignore[misc]


def default_watch_dir() -> Path:
    env = os.environ.get("ACL_META_WATCH_DIR", "").strip()
    if env:
        return Path(env)
    sys.path.insert(0, str(ROOT))
    from config.paths import get_local_config_dir
    return Path(get_local_config_dir()) / "meta_watch"


def load_sources(path: Path, categories: list[str] | None = None) -> list[dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    items = data["sources"] if isinstance(data, dict) else data
    if categories:
        items = [s for s in items if s.get("category") in categories]
    return items


def _snap_path(snap_dir: Path, url: str) -> Path:
    return snap_dir / (hashlib.sha1(url.encode("utf-8")).hexdigest()[:16] + ".json")


def _load_snapshot(snap_dir: Path, url: str) -> dict | None:
    p = _snap_path(snap_dir, url)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None  # битый снимок = как первый запуск


def classify_status(added_lines: list[str]) -> str:
    text = "\n".join(added_lines)
    for label, rx in STATUS_PATTERNS:
        if rx.search(text):
            return label
    return "unknown"


def diff_texts(old: str, new: str) -> tuple[list[str], list[str]]:
    added: list[str] = []
    removed: list[str] = []
    for ln in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0):
        if ln.startswith(("+++", "---", "@@")):
            continue
        (added if ln.startswith("+") else removed).append(ln[1:])
    return added, removed


def _summary(added: list[str], removed: list[str]) -> str:
    def short(lines):
        return [(x if len(x) <= 200 else x[:197] + "...") for x in lines[:MAX_LINES_IN_SUMMARY]]
    out = [f"+{len(added)} / -{len(removed)} lines"]
    out += ["+ " + x for x in short(added)]
    out += ["- " + x for x in short(removed)]
    return "\n".join(out)


def run_watch(sources: list[dict], fetch, snap_dir: Path, out_dir: Path | None,
              today: str | None = None, dry_run: bool = False, delay: float = 0.0) -> dict:
    """Один проход. `fetch(url) -> html`. Ошибка одного источника не прерывает остальные."""
    today = today or _dt.date.today().isoformat()
    changes: list[dict] = []
    errors: list[dict] = []
    baselined: list[str] = []
    unchanged = 0
    weak_sources = [x["url"] for x in sources if x.get("weak")]
    if not dry_run:
        snap_dir.mkdir(parents=True, exist_ok=True)
    for i, src in enumerate(sources):
        url = src["url"]
        if delay and i:
            time.sleep(delay)
        try:
            text = extract_main_text(fetch(url))
            if not text.strip():
                raise ValueError("empty text after extraction (JS-only page?)")
        except Exception as exc:  # noqa: BLE001
            errors.append({"source_url": url, "error": f"{type(exc).__name__}: {exc}"[:300]})
            continue
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        prev = _load_snapshot(snap_dir, url)
        if prev is None:
            baselined.append(url)
        elif prev.get("hash") == digest:
            unchanged += 1
        else:
            added, removed = diff_texts(prev.get("text", ""), text)
            fields = sorted({m for ln in added for m in FIELD_RE.findall(ln)})[:40]
            changes.append({
                "source_url": url,
                "title": src.get("title", ""),
                "category": src.get("category", ""),
                "date_seen": today,
                "summary": _summary(added, removed),
                "status_label": classify_status(added),
                "affected_fields": fields,
                "weak": bool(src.get("weak")),
            })
        if not dry_run:
            _snap_path(snap_dir, url).write_text(json.dumps(
                {"url": url, "fetched": today, "hash": digest, "text": text}, ensure_ascii=False),
                encoding="utf-8")
    result = {
        "generated": today,
        "dry_run": dry_run,
        "sources_total": len(sources),
        "ok": len(sources) - len(errors),
        "failed": len(errors),
        "baselined": len(baselined),
        "unchanged": unchanged,
        "weak_sources": weak_sources,
        "changes": changes,
        "errors": errors,
    }
    if out_dir is not None and not dry_run:
        write_digest(result, out_dir)
    return result


def render_markdown(d: dict) -> str:
    lines = [f"# Meta docs digest {d['generated']}", "",
             f"Sources: {d['sources_total']} | ok: {d['ok']} | failed: {d['failed']} | "
             f"first-seen baselines: {d['baselined']} | unchanged: {d['unchanged']} | "
             f"changed: {len(d['changes'])}", ""]
    if not d["changes"] and not d["errors"]:
        lines.append("No changes.")
    if d.get("weak_sources"):
        lines += ["", f"WEAK sources ({len(d['weak_sources'])}): JS shells with almost no text, "
                  "so 'no change' there is NOT meaningful:"]
        lines += [f"- {u}" for u in d["weak_sources"]]
        lines.append("")
    for c in d["changes"]:
        lines += [f"## [{c['status_label']}]{' [WEAK]' if c.get('weak') else ''} "
                  f"{c['title'] or c['source_url']}", "",
                  f"- Source: {c['source_url']}", f"- Category: {c['category']}",
                  f"- Seen: {c['date_seen']}",
                  f"- Affected fields: {', '.join(c['affected_fields']) or '-'}", "",
                  "```", c["summary"], "```", ""]
    if d["errors"]:
        lines += ["## Fetch errors", ""]
        lines += [f"- {e['source_url']}: {e['error']}" for e in d["errors"]]
    return "\n".join(lines) + "\n"


OBSIDIAN_NOTE = "ACL — Meta изменения.md"


def default_vault_dir() -> Path | None:
    """Общая папка «дешборд» в Obsidian; None, если ни один путь не существует."""
    sys.path.insert(0, str(ROOT))
    from config.paths import get_obsidian_dashboard_dir
    return get_obsidian_dashboard_dir()


def append_obsidian(d: dict, vault: Path | None) -> bool:
    """Дописывает датированную секцию в заметку дневника. Пустой дайджест и нет vault — no-op."""
    if vault is None or not Path(vault).is_dir():
        sys.path.insert(0, str(ROOT))
        from config.paths import OBSIDIAN_MISSING_WARNING
        print(f"obsidian: {OBSIDIAN_MISSING_WARNING}, skipped", file=sys.stderr)
        return False
    if not d["changes"] and not d["errors"]:
        return False
    lines = ["", f"## {d['generated']}", "",
             f"- Источников: {d['sources_total']}, изменилось: {len(d['changes'])}, "
             f"ошибок: {d['failed']}, новых снимков: {d['baselined']}"]
    for c in d["changes"]:
        lines.append(f"- [{c['status_label']}]{' [слабый источник]' if c.get('weak') else ''} "
                     f"{c['title'] or c['source_url']} — {c['source_url']}")
    for e in d["errors"]:
        lines.append(f"- ошибка: {e['source_url']}: {e['error']}")
    note = Path(vault) / OBSIDIAN_NOTE
    fresh = not note.exists() or note.stat().st_size == 0
    with note.open("a", encoding="utf-8", newline="\n") as f:
        if fresh:
            f.write("# ACL — Meta изменения\n")
        f.write("\n".join(lines) + "\n")
    return True


def write_digest(d: dict, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    js = json.dumps(d, ensure_ascii=False, indent=1)
    md = render_markdown(d)
    for stem in (f"digest-{d['generated']}", "latest"):
        (out_dir / f"{stem}.json").write_text(js, encoding="utf-8")
        (out_dir / f"{stem}.md").write_text(md, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Daily Meta docs watcher (read-only)")
    ap.add_argument("--sources", type=Path, default=DEFAULT_SOURCES, help="sources.json")
    ap.add_argument("--out", type=Path, help="digest dir (default: <watch dir>/digests)")
    ap.add_argument("--snapshots", type=Path, help="snapshot dir (default: <watch dir>/snapshots)")
    ap.add_argument("--category", action="append", help="only these categories (repeatable)")
    ap.add_argument("--dry-run", action="store_true", help="fetch and diff, write nothing")
    ap.add_argument("--obsidian", action=argparse.BooleanOptionalAction, default=None,
                    help="append summary to Obsidian note (default: on when the vault exists)")
    ap.add_argument("--vault", type=Path, help="Obsidian 'дешборд' dir (default: ACL_OBSIDIAN_DIR or standard)")
    ap.add_argument("--delay", type=float, default=0.5, help="seconds between requests")
    a = ap.parse_args(argv)
    base = default_watch_dir() if not (a.out and a.snapshots) else None
    snap = a.snapshots or base / "snapshots"
    out = a.out or base / "digests"
    d = run_watch(load_sources(a.sources, a.category), http_fetch, snap, out,
                  dry_run=a.dry_run, delay=a.delay)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if a.obsidian is not False and not a.dry_run:
        vault = a.vault if a.vault else default_vault_dir()
        append_obsidian(d, vault)
    print(render_markdown(d) if a.dry_run else
          f"ok={d['ok']} failed={d['failed']} changed={len(d['changes'])} "
          f"baselined={d['baselined']} -> {out}")
    return 0 if d["failed"] < d["sources_total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
