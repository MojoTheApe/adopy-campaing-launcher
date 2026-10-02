"""Итоги кампаний со всех машин — из Sentry (ACL-1447).

Что делает. Читает события `kind:run_summary` (их шлёт
`src/fb_ads_rpa/run_summary.py` в конце каждой кампании) через Discover API
Sentry и складывает по машинам (по `machine_id`, с ярлыком): сколько кампаний, с каким итогом (clean /
warnings / do-not-publish / stopped), когда последняя, на каком релизе и под
каким именем менеджера. Это режим `--from-sentry` таблицы здоровья робота:
локальный `rpa_telemetry.jsonl` видит только свою машину.

ACL-1434 (`scripts/robot_health.py`) на момент ACL-1447 в main не влит —
поэтому чтение живёт здесь, отдельным скриптом с функцией
`machines_from_sentry()`, которую таблица здоровья сможет вызвать сама.

Ничего не пишет ни в Sentry, ни на диск. Без токена или сети — честно
говорит «пропущено» и выходит с 0.

  python scripts/run_summaries.py --from-sentry            # за 7 дней
  python scripts/run_summaries.py --from-sentry --period 24h --format json

Доступ — как у `sentry_triage.py`: `SENTRY_AUTH_TOKEN` (event:read),
`SENTRY_ORG`, `SENTRY_BASE_URL`, `SENTRY_PROJECT`.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.sentry_triage import (  # noqa: E402
    DEFAULT_BASE_URL,
    DEFAULT_ORG,
    Fetch,
    SentryClient,
    SentryUnavailable,
    _NEXT_RE,
)

FIELDS = ("timestamp", "machine", "machine_id", "manager", "result", "release", "account", "campaign", "editor")
RESULTS = ("clean", "warnings", "do-not-publish", "stopped")
MAX_EVENTS = 2000


def _field(row: dict[str, Any], name: str) -> str:
    value = row.get(name)
    if value in (None, ""):
        value = row.get(f"tags[{name}]")
    return "" if value is None else str(value)


def summary_events(client: SentryClient, *, period: str = "7d", project: str = "",
                   limit: int = MAX_EVENTS) -> list[dict[str, str]]:
    """Строки событий-итогов: по полю на каждое из FIELDS."""
    params: dict[str, Any] | None = {
        "field": list(FIELDS), "query": "kind:run_summary", "statsPeriod": period,
        "per_page": 100, "sort": "-timestamp", "dataset": "errors",
    }
    if project:
        params["project"] = project
    url = f"/api/0/organizations/{client.org}/events/"
    out: list[dict[str, str]] = []
    while url and len(out) < limit:
        body, headers = client._get(url, params)
        params = None  # следующая страница уже несёт параметры в адресе
        page = json.loads(body.decode("utf-8") or "{}")
        rows = page.get("data") if isinstance(page, dict) else None
        for row in rows or []:
            out.append({name: _field(row, name) for name in FIELDS})
        found = _NEXT_RE.search(headers.get("link", ""))
        url = found.group(1) if found else ""
    return out[:limit]


def machines(events: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Сложить итоги по машине: одна строка на машину, свежие сверху.

    Ключ — `machine_id` (ревью PR #450): ярлык `machine` теперь равен имени
    менеджера, и два компьютера одного менеджера (или переименованный ярлык)
    по ярлыку слились бы в одну строку. Ярлык показываем последний. Старые
    события без `machine_id` группируются по ярлыку, как раньше.
    """
    by_machine: dict[str, dict[str, Any]] = {}
    for ev in events:
        machine_id = ev.get("machine_id") or ""
        label = ev.get("machine") or ""
        key = f"id:{machine_id}" if machine_id else f"label:{label or 'unknown'}"
        row = by_machine.setdefault(key, {
            "machine": label or "unknown", "machine_id": machine_id, "manager": "", "runs": 0,
            **{r: 0 for r in RESULTS}, "last_run": "", "release": "",
            "last_account": "", "last_campaign": "", "last_result": "",
        })
        row["runs"] += 1
        result = ev.get("result") or ""
        if result in RESULTS:
            row[result] += 1
        ts = ev.get("timestamp") or ""
        if ts >= row["last_run"]:
            row.update(last_run=ts, release=ev.get("release") or row["release"],
                       machine=label or row["machine"],
                       manager=ev.get("manager") or row["manager"],
                       last_account=ev.get("account") or "", last_campaign=ev.get("campaign") or "",
                       last_result=result)
    return sorted(by_machine.values(), key=lambda r: r["last_run"], reverse=True)


def machines_from_sentry(*, period: str = "7d", project: str = "",
                         fetch: Fetch | None = None) -> dict[str, Any]:
    """`{"machines": [...]}` или `{"skipped": причина}` — для таблицы здоровья."""
    try:
        client = SentryClient(
            os.environ.get("SENTRY_AUTH_TOKEN", "").strip(),
            org=os.environ.get("SENTRY_ORG", "").strip() or DEFAULT_ORG,
            base_url=os.environ.get("SENTRY_BASE_URL", "").strip() or DEFAULT_BASE_URL,
            fetch=fetch,
        )
        events = summary_events(client, period=period, project=project)
    except SentryUnavailable as exc:
        return {"skipped": str(exc), "period": period}
    return {"period": period, "events": len(events), "machines": machines(events)}


def to_text(digest: dict[str, Any]) -> str:
    if digest.get("skipped"):
        return f"Run summaries from Sentry skipped: {digest['skipped']}"
    rows = digest.get("machines") or []
    if not rows:
        return f"No run summaries in Sentry for the last {digest.get('period')}."
    lines = [f"Run summaries from Sentry, last {digest.get('period')} ({digest.get('events')} campaigns)",
             f"{'machine':22s} {'machine id':12s} {'manager':14s} {'runs':>4s} {'clean':>5s} {'warn':>4s} "
             f"{'dnp':>4s} {'stop':>4s}  {'last run':20s} release"]
    for r in rows:
        lines.append(
            f"{r['machine'][:22]:22s} {(r['machine_id'] or '-')[:12]:12s} {(r['manager'] or '-')[:14]:14s} {r['runs']:4d} {r['clean']:5d} "
            f"{r['warnings']:4d} {r['do-not-publish']:4d} {r['stopped']:4d}  "
            f"{r['last_run'][:19]:20s} {r['release']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None, *, fetch: Fetch | None = None, out=None) -> int:
    parser = argparse.ArgumentParser(description="Per-machine run summaries (read-only).")
    parser.add_argument("--from-sentry", action="store_true",
                        help="read run summaries of all machines from Sentry (the only source for now)")
    parser.add_argument("--period", default="7d", help="Sentry statsPeriod, e.g. 24h, 7d, 14d")
    parser.add_argument("--project", default=os.environ.get("SENTRY_PROJECT", ""))
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)
    out = out or sys.stdout
    if not args.from_sentry:
        parser.error("only --from-sentry is supported; the local journal is read by run_telemetry")
    if fetch is None:
        try:
            from config.paths import load_env

            load_env()
        except Exception:
            pass
    digest = machines_from_sentry(period=args.period, project=args.project, fetch=fetch)
    if args.format == "json":
        out.write(json.dumps(digest, ensure_ascii=False, indent=2) + "\n")
    else:
        out.write(to_text(digest) + "\n")
    return 0


if __name__ == "__main__":
    try:
        from scripts._console import use_utf8

        use_utf8()
    except Exception:
        pass
    raise SystemExit(main())
