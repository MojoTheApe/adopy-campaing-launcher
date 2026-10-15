"""Разбор новых отказов из Sentry со всех установок ACL (ACL-1325).

Что делает. Спрашивает Sentry (org `adopy-7t`, регион de), какие тикеты
появились или вернулись (regressed) после прошлого разбора, берёт у каждого
последнее событие — теги (шаг, вид причины, вид отказа, машина, релиз,
аккаунт), контекст прогона и вложение с вычищенным разделом страницы, —
складывает их по отпечатку «шаг + причина», сверяет с таблицей известных
задач (`config/sentry_triage_map.json`) и печатает короткую сводку: JSON для
агента и markdown для человека.

Чего НЕ делает. Ничего не пишет ни в Sentry, ни в GitHub: не закрывает, не
назначает, не комментирует. На диск без флагов не пишет ничего: вложение
скачивается в память, и скелет раздела строится из байтов (ревью PR #328,
F3). `--apply` только запоминает курсор (время разбора) в локальном файле
состояния, чтобы следующий запуск показал лишь новое. Сами разделы
сохраняются только в явно названную `--attachments-dir`, и там остаются
последние KEEP_SAVED_ISSUES тикетов. Без токена или без сети — честно говорит
«пропущено» и выходит с 0: расписанию незачем падать из-за того, что машина
сейчас без доступа.

  python scripts/sentry_triage.py                   # сводка с прошлого курсора (или за сутки)
  python scripts/sentry_triage.py --since 6h        # окно вместо курсора
  python scripts/sentry_triage.py --apply           # и запомнить курсор
  python scripts/sentry_triage.py --format json     # только JSON
  python scripts/sentry_triage.py --no-attachments  # не скачивать вложения
  python scripts/sentry_triage.py --attachments-dir DIR  # и сохранить разделы в DIR

Доступ: `SENTRY_AUTH_TOKEN` (личный токен владельца с правами org:read,
project:read, event:read; чтение вложений — роль не ниже member). Можно
держать в машинном `.env`. `SENTRY_ORG`, `SENTRY_BASE_URL`, `SENTRY_PROJECT`
перебивают значения по умолчанию. Реакция «сразу» — docs/system/SENTRY_TRIAGE.md.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

DEFAULT_ORG = "adopy-7t"
DEFAULT_BASE_URL = "https://de.sentry.io"
MAP_PATH = _ROOT / "config" / "sentry_triage_map.json"
#: Первый запуск без курсора смотрит назад на столько.
DEFAULT_SINCE = timedelta(hours=24)
#: Потолок тикетов за разбор — сводка должна читаться, а не листаться.
MAX_ISSUES = 100
#: Потолок одного скачанного вложения (у робота — 200 КБ, запас на чужие).
MAX_ATTACHMENT_BYTES = 1_000_000
#: Сколько папок тикетов держать в `--attachments-dir`. Разбор идёт каждые
#: десять минут; хранилище, которое только растёт, однажды кончится.
KEEP_SAVED_ISSUES = 50
#: Папка одного тикета в `--attachments-dir`. Подчищаются только такие папки
#: и только файлы разделов в них: папку назвал человек, чужое там не трогаем.
_ISSUE_DIR_PREFIX = "sentry_issue_"
_ISSUE_DIR_RE = re.compile(r"^sentry_issue_[A-Za-z0-9_-]{1,40}$")
TIMEOUT_S = 20

#: Ответ HTTP: код, тело, заголовки. Подменяется в тестах.
Fetch = Callable[[str, dict[str, str]], tuple[int, bytes, dict[str, str]]]


# ── HTTP ────────────────────────────────────────────────────────────────────

def _http_get(url: str, headers: dict[str, str]) -> tuple[int, bytes, dict[str, str]]:
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as resp:  # noqa: S310 - адрес наш
            return resp.status, resp.read(MAX_ATTACHMENT_BYTES + 1), dict(resp.headers.items())
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read() if hasattr(exc, "read") else b"", dict(exc.headers.items() if exc.headers else {})


class SentryUnavailable(Exception):
    """Нет токена, сети или прав — разбор пропускается, а не падает."""


_NEXT_RE = re.compile(r'<([^>]+)>;\s*rel="next";\s*results="true"')


class SentryClient:
    """Только чтение. Методов записи здесь нет намеренно."""

    def __init__(self, token: str, *, org: str = DEFAULT_ORG, base_url: str = DEFAULT_BASE_URL,
                 fetch: Fetch | None = None) -> None:
        if not token:
            raise SentryUnavailable("SENTRY_AUTH_TOKEN is not set")
        self.org = org
        self.base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        self._fetch = fetch or _http_get

    def _get(self, path_or_url: str, params: dict[str, Any] | None = None) -> tuple[bytes, dict[str, str]]:
        url = path_or_url if path_or_url.startswith("http") else self.base + path_or_url
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params, doseq=True)
        try:
            status, body, headers = self._fetch(url, dict(self._headers))
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise SentryUnavailable(f"network: {type(exc).__name__}") from exc
        if status in (401, 403):
            raise SentryUnavailable(f"HTTP {status}: token lacks access")
        if status >= 400:
            raise SentryUnavailable(f"HTTP {status} for {urllib.parse.urlsplit(url).path}")
        return body, {k.lower(): v for k, v in (headers or {}).items()}

    def _json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        body, _headers = self._get(path, params)
        return json.loads(body.decode("utf-8") or "null")

    def issues(self, query: str, *, project: str = "", limit: int = MAX_ISSUES) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"query": query, "statsPeriod": "14d", "limit": min(100, limit),
                                  "sort": "date"}
        if project:
            params["project"] = project
        url = f"/api/0/organizations/{self.org}/issues/"
        out: list[dict[str, Any]] = []
        while url and len(out) < limit:
            body, headers = self._get(url, params)
            params = None  # следующая страница уже несёт параметры в адресе
            page = json.loads(body.decode("utf-8") or "[]")
            out.extend(page if isinstance(page, list) else [])
            found = _NEXT_RE.search(headers.get("link", ""))
            url = found.group(1) if found else ""
        return out[:limit]

    def latest_event(self, issue_id: str) -> dict[str, Any]:
        return self._json(f"/api/0/organizations/{self.org}/issues/{issue_id}/events/latest/") or {}

    def attachments(self, project: str, event_id: str) -> list[dict[str, Any]]:
        got = self._json(f"/api/0/projects/{self.org}/{project}/events/{event_id}/attachments/")
        return got if isinstance(got, list) else []

    def attachment_bytes(self, project: str, event_id: str, attachment_id: str) -> bytes:
        body, _ = self._get(
            f"/api/0/projects/{self.org}/{project}/events/{event_id}/attachments/{attachment_id}/",
            {"download": "1"},
        )
        return body[:MAX_ATTACHMENT_BYTES]


# ── Состояние (курсор) ──────────────────────────────────────────────────────

def default_state_path() -> Path:
    try:
        from config.paths import get_local_config_dir

        return Path(get_local_config_dir()) / "sentry_triage_state.json"
    except Exception:
        return Path.home() / ".acl_sentry_triage_state.json"


def load_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def parse_since(value: str, *, now: datetime) -> datetime:
    """`6h`, `2d`, `90m` или ISO-время."""
    value = (value or "").strip()
    unit = re.fullmatch(r"(\d{1,4})([mhd])", value)
    if unit:
        n, u = int(unit.group(1)), unit.group(2)
        return now - {"m": timedelta(minutes=n), "h": timedelta(hours=n), "d": timedelta(days=n)}[u]
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# ── Известные задачи ────────────────────────────────────────────────────────

def load_map(path: Path = MAP_PATH) -> list[dict[str, str]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    rows = data.get("known") if isinstance(data, dict) else data
    return [r for r in rows or [] if isinstance(r, dict) and r.get("task")]


def known_task(step: str, reason: str, rows: list[dict[str, str]]) -> dict[str, str] | None:
    """Первая строка таблицы, чьи шаблоны шага и причины подходят (fnmatch)."""
    for row in rows:
        if fnmatch.fnmatchcase(step or "", row.get("step") or "*") and \
                fnmatch.fnmatchcase(reason or "", row.get("reason") or "*"):
            return row
    return None


# ── Вложение: что было на странице ──────────────────────────────────────────

class _Outline(HTMLParser):
    """Заголовки и подписи элементов управления — короткий «скелет» раздела."""

    CONTROL_ROLES = {"button", "radio", "checkbox", "combobox", "switch", "tab", "menuitem", "option"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.headings: list[str] = []
        self.controls: list[str] = []
        self._open: list[tuple[str, str, list[str], str]] = []  # (тег, роль, текст, состояние)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        role = (a.get("role") or "").lower()
        if role == "heading" or role in self.CONTROL_ROLES:
            label = a.get("aria-label") or ""
            state = ""
            if a.get("aria-checked") in ("true", "false"):
                state = f" [{'x' if a['aria-checked'] == 'true' else ' '}]"
            if a.get("aria-expanded") in ("true", "false"):
                state += f" ({'open' if a['aria-expanded'] == 'true' else 'closed'})"
            self._open.append((tag, role, [label] if label else [], state))
        elif self._open:
            self._open.append((tag, "", [], ""))

    def handle_endtag(self, tag):
        while self._open:
            t, role, text, state = self._open.pop()
            joined = " ".join(" ".join(text).replace("\u200b", " ").split())[:80]
            if role == "heading" and joined:
                self.headings.append(joined)
            elif role and joined:
                self.controls.append(f"{role}: {joined}{state}")
            elif self._open and text:
                self._open[-1][2].extend(text)
            if t == tag:
                break

    def handle_data(self, data):
        if self._open and data.strip():
            self._open[-1][2].append(data.strip())


def outline_attachment(html: str) -> dict[str, Any]:
    head = re.search(r"<!-- ACL section snapshot[^>]*-->", html or "")
    parser = _Outline()
    try:
        parser.feed(html or "")
    except Exception:
        pass

    def uniq(items: list[str], n: int) -> list[str]:
        seen: list[str] = []
        for item in items:
            if item not in seen:
                seen.append(item)
        return seen[:n]

    return {
        "header": head.group(0)[5:-4].strip() if head else "",
        "headings": uniq(parser.headings, 20),
        "controls": uniq(parser.controls, 30),
    }


# ── Разбор ──────────────────────────────────────────────────────────────────

def _tags(event: dict[str, Any]) -> dict[str, str]:
    raw = event.get("tags") or []
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    return {str(t.get("key")): str(t.get("value")) for t in raw if isinstance(t, dict)}


def _group_step(step: str) -> str:
    return re.sub(r"^ad\d+_", "ad_", step or "")


@dataclass
class Group:
    step: str
    reason: str
    issues: list[dict[str, Any]] = field(default_factory=list)
    known: dict[str, str] | None = None

    @property
    def key(self) -> str:
        return f"{self.step}:{self.reason}"


def _issue_row(issue: dict[str, Any], event: dict[str, Any], kind: str,
               attachment: dict[str, Any] | None) -> dict[str, Any]:
    tags = _tags(event)
    contexts = event.get("contexts") or {}
    run = contexts.get("run") or {}
    release = tags.get("release", "")
    if not release and isinstance(event.get("release"), dict):
        release = event["release"].get("version") or ""
    return {
        "id": str(issue.get("id") or ""),
        "short_id": issue.get("shortId") or "",
        "title": issue.get("title") or "",
        "link": issue.get("permalink") or "",
        "project": (issue.get("project") or {}).get("slug") or "",
        "change": kind,  # new / regressed
        "count": int(issue.get("count") or 0),
        "machines": int(issue.get("userCount") or 0),
        "first_seen": issue.get("firstSeen") or "",
        "last_seen": issue.get("lastSeen") or "",
        "latest_event": {
            "id": event.get("eventID") or event.get("id") or "",
            "step": tags.get("step", ""),
            "reason": tags.get("reason", ""),
            "failure_kind": tags.get("failure_kind", ""),
            "machine": tags.get("machine", ""),
            "account": tags.get("account", ""),
            "release": release,
            "job_phase": tags.get("job_phase", ""),
            "campaign": run.get("campaign", ""),
            "attempt": run.get("attempt"),
            "why": (contexts.get("failure") or {}).get("why", ""),
            "diag": contexts.get("diag") or {},
            "page_snapshot": contexts.get("page_snapshot") or {},
        },
        "attachment": attachment,
    }


def triage(client: SentryClient, *, since: datetime, rows: list[dict[str, str]],
           project: str = "", attachments: bool = True, attachments_dir: Path | None = None,
           seen: dict[str, str] | None = None) -> dict[str, Any]:
    """Сводка новых и вернувшихся тикетов с момента `since`.

    `attachments` — скачивать ли разделы (в память, для скелета);
    `attachments_dir` — куда их ещё и сохранить (None — никуда)."""
    stamp = _iso(since)
    found: dict[str, tuple[dict[str, Any], str]] = {}
    for issue in client.issues(f"is:unresolved firstSeen:>{stamp}", project=project):
        found[str(issue.get("id"))] = (issue, "new")
    for issue in client.issues(f"is:regressed lastSeen:>{stamp}", project=project):
        found.setdefault(str(issue.get("id")), (issue, "regressed"))
    seen = seen or {}

    groups: dict[str, Group] = {}
    for issue_id, (issue, kind) in found.items():
        # Уже разобранный тикет без новых событий — не шум в сводке.
        if seen.get(issue_id) and seen[issue_id] == (issue.get("lastSeen") or ""):
            continue
        try:
            event = client.latest_event(issue_id)
        except SentryUnavailable:
            event = {}
        attachment = None
        event_id = event.get("eventID") or event.get("id") or ""
        proj = (issue.get("project") or {}).get("slug") or project
        if attachments and event_id and proj:
            attachment = _fetch_section(client, proj, event_id, issue_id, attachments_dir)
        row = _issue_row(issue, event, kind, attachment)
        step = _group_step(row["latest_event"]["step"]) or (issue.get("title") or "").split(":", 1)[0]
        reason = row["latest_event"]["reason"] or "unknown"
        key = f"{step}:{reason}"
        if key not in groups:
            groups[key] = Group(step=step, reason=reason, known=known_task(step, reason, rows))
        groups[key].issues.append(row)

    ordered = sorted(groups.values(), key=lambda g: (g.known is not None, -sum(i["count"] for i in g.issues)))
    return {
        "since": stamp + "Z",
        "checked_at": _iso(datetime.now(timezone.utc)) + "Z",
        "issues": len(found),
        "groups": [
            {
                "key": g.key,
                "step": g.step,
                "reason": g.reason,
                "known_task": g.known,
                "new_kind": g.known is None,
                "events": sum(i["count"] for i in g.issues),
                "machines": sorted({i["latest_event"]["machine"] for i in g.issues if i["latest_event"]["machine"]}),
                "accounts": sorted({i["latest_event"]["account"] for i in g.issues if i["latest_event"]["account"]}),
                "releases": sorted({i["latest_event"]["release"] for i in g.issues if i["latest_event"]["release"]}),
                "issues": g.issues,
            }
            for g in ordered
        ],
    }


def _fetch_section(client: SentryClient, project: str, event_id: str, issue_id: str,
                   out_dir: Path | None) -> dict[str, Any] | None:
    """Раздел последнего события: скелет из байтов в памяти; на диск — только
    в названную `out_dir`."""
    try:
        listed = client.attachments(project, event_id)
    except SentryUnavailable as exc:
        return {"error": str(exc)}
    for att in listed:
        name = str(att.get("name") or "")
        if not name.startswith("section_") or not name.endswith(".html"):
            continue
        try:
            body = client.attachment_bytes(project, event_id, str(att.get("id")))
        except SentryUnavailable as exc:
            return {"name": name, "error": str(exc)}
        text = body.decode("utf-8", "replace")
        found = {"name": name, "bytes": len(body), **outline_attachment(text)}
        if out_dir is not None:
            safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name)[:80]
            folder = out_dir / (_ISSUE_DIR_PREFIX + re.sub(r"[^A-Za-z0-9_-]", "_", issue_id)[:40])
            try:
                folder.mkdir(parents=True, exist_ok=True)
                target = folder / safe
                target.write_bytes(body)
                # Перезапись файла на NTFS не двигает время папки — двигаем
                # сами, иначе вернувшийся старый тикет считался бы старейшим.
                os.utime(folder)
                found["saved"] = str(target)
            except OSError as exc:
                found["save_error"] = type(exc).__name__
            prune_saved(out_dir, protect=folder)
        return found
    return None


def prune_saved(out_dir: Path, keep: int = KEEP_SAVED_ISSUES,
                protect: Path | None = None) -> int:
    """Оставить в `out_dir` последние `keep` папок тикетов. Возвращает, сколько
    убрано. Трогает только папки `sentry_issue_*` и файлы `section_*.html` в
    них; папка, где лежит что-то ещё, остаётся на месте. Папка `protect`
    (только что записанная) не убирается никогда."""
    try:
        folders = sorted((d for d in out_dir.iterdir() if d.is_dir() and _ISSUE_DIR_RE.match(d.name)),
                         key=lambda d: d.stat().st_mtime)
    except OSError:
        return 0
    removed = 0
    for old in folders[: max(0, len(folders) - keep)]:
        if protect is not None and old.resolve() == Path(protect).resolve():
            continue
        try:
            for f in old.glob("section_*.html"):
                f.unlink()
            old.rmdir()
            removed += 1
        except OSError:
            continue
    return removed


# ── Вывод ───────────────────────────────────────────────────────────────────

def to_markdown(digest: dict[str, Any]) -> str:
    if digest.get("skipped"):
        return f"**Sentry triage skipped:** {digest['skipped']}"
    groups = digest.get("groups") or []
    lines = [f"## Sentry: {digest.get('issues', 0)} new/regressed issue(s) since {digest.get('since')}"]
    if not groups:
        lines.append("Nothing new.")
        return "\n".join(lines)
    for g in groups:
        task = g.get("known_task")
        badge = f"known: {task['task']}" if task else "NEW KIND"
        lines.append("")
        lines.append(f"### {g['step']}: {g['reason']} — {badge}")
        lines.append(f"- events: {g['events']}; machines: {', '.join(g['machines']) or '?'};"
                     f" accounts: {', '.join(g['accounts']) or '?'}; releases: {', '.join(g['releases']) or '?'}")
        if task and task.get("note"):
            lines.append(f"- note: {task['note']}")
        for issue in g["issues"][:5]:
            ev = issue["latest_event"]
            lines.append(f"- [{issue['short_id'] or issue['id']}]({issue['link']}) {issue['change']},"
                         f" {issue['count']} event(s); kind {ev['failure_kind'] or '?'};"
                         f" campaign {ev['campaign'] or '?'}")
            if ev.get("why"):
                lines.append(f"  - why: {str(ev['why'])[:200]}")
            att = issue.get("attachment") or {}
            if att.get("headings"):
                lines.append(f"  - page section: {' / '.join(att['headings'][:8])}")
            if att.get("controls"):
                lines.append(f"  - controls: {'; '.join(att['controls'][:8])}")
            if att.get("saved"):
                lines.append(f"  - snapshot: {att['saved']}")
            elif att.get("error"):
                lines.append(f"  - snapshot not downloaded: {att['error']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None, *, fetch: Fetch | None = None,
         now: datetime | None = None, out=None) -> int:
    parser = argparse.ArgumentParser(description="Digest of new ACL failures in Sentry (read-only).")
    parser.add_argument("--since", default="", help="window (6h, 2d) or ISO time; default: saved cursor or 24h")
    parser.add_argument("--state", default="", help="state file (default: machine config dir)")
    parser.add_argument("--map", default=str(MAP_PATH), help="known failures → tracker tasks")
    parser.add_argument("--project", default=os.environ.get("SENTRY_PROJECT", ""))
    parser.add_argument("--attachments-dir", default="",
                        help="also save the downloaded section snapshots here (never saved otherwise;"
                             f" the latest {KEEP_SAVED_ISSUES} issue folders are kept)")
    parser.add_argument("--no-attachments", action="store_true",
                        help="do not download section snapshots at all")
    parser.add_argument("--format", choices=("json", "md", "both"), default="both")
    parser.add_argument("--apply", action="store_true",
                        help="record the cursor in the state file (its only write)")
    args = parser.parse_args(argv)
    out = out or sys.stdout
    now = now or datetime.now(timezone.utc)

    if fetch is None:
        try:
            from config.paths import load_env

            load_env()
        except Exception:
            pass

    state_path = Path(args.state) if args.state else default_state_path()
    state = load_state(state_path)
    if args.since:
        since = parse_since(args.since, now=now)
    elif state.get("cursor"):
        since = parse_since(state["cursor"], now=now)
    else:
        since = now - DEFAULT_SINCE
    # Без явной папки разделы только в памяти: запуск каждые десять минут не
    # копит на диске копии страниц (ревью PR #328, F3).
    attachments_dir = None
    if args.attachments_dir and not args.no_attachments:
        attachments_dir = Path(args.attachments_dir)

    try:
        client = SentryClient(
            os.environ.get("SENTRY_AUTH_TOKEN", "").strip(),
            org=os.environ.get("SENTRY_ORG", "").strip() or DEFAULT_ORG,
            base_url=os.environ.get("SENTRY_BASE_URL", "").strip() or DEFAULT_BASE_URL,
            fetch=fetch,
        )
        digest = triage(client, since=since, rows=load_map(Path(args.map)), project=args.project,
                        attachments=not args.no_attachments, attachments_dir=attachments_dir,
                        seen=state.get("seen") or {})
    except SentryUnavailable as exc:
        digest = {"skipped": str(exc), "since": _iso(since) + "Z"}

    if args.format in ("json", "both"):
        out.write(json.dumps(digest, ensure_ascii=False, indent=2) + "\n")
    if args.format in ("md", "both"):
        out.write(to_markdown(digest) + "\n")

    if args.apply and not digest.get("skipped"):
        seen = dict(state.get("seen") or {})
        for g in digest.get("groups") or []:
            for issue in g["issues"]:
                seen[issue["id"]] = issue["last_seen"]
        # Ограничено: файл состояния не растёт вечно.
        seen = dict(list(seen.items())[-500:])
        save_state(state_path, {"cursor": _iso(now) + "Z", "seen": seen})
    return 0


if __name__ == "__main__":
    try:
        from scripts._console import use_utf8

        use_utf8()
    except Exception:
        pass
    raise SystemExit(main())
