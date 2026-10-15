"""Turn the shared telemetry into a readable summary, and file it in Obsidian.

The hand-written diary stays hand-written: it holds reasoning, dead ends and
decisions, none of which can be derived from step counters. This produces the
other half — the part that IS mechanical, and that no human should be
assembling by eye once several machines are reporting.

The question it exists to answer: **did Meta change the UI, or did something
break on one machine?** On 2026-08-24 that took a day to establish by hand for
a single machine. The signature is a step whose failure rate jumps on the same
day across several machines; the same jump on one machine is a local problem.
The report never collapses those two into one verdict — with only one machine
reporting it says so, rather than implying a fleet-wide conclusion it cannot
support.

  python scripts/acl_telemetry_report.py              # from the shared sheet
  python scripts/acl_telemetry_report.py --local      # this machine's JSONL
  python scripts/acl_telemetry_report.py --days 14
  python scripts/acl_telemetry_report.py --no-write   # print only
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Windows consoles are not UTF-8 by default; without this a Russian line
# aborts the script instead of printing. See scripts/_console.py.
from scripts._console import use_utf8  # noqa: E402

use_utf8()

try:
    from config.paths import load_env

    load_env()
except Exception:
    pass

from src.fb_ads_rpa import telemetry_sync  # noqa: E402

from config import paths  # noqa: E402

NOTE_NAME = "RPA — Сводка телеметрии (генерируется).md"


def _day(ts: str) -> str:
    return (ts or "")[:10]


def analyse(rows: list[dict], days: int) -> dict:
    cutoff = (date.today() - timedelta(days=days)).isoformat()

    graded = [r for r in rows if r.get("ok") in ("0", "1") and r.get("step")]
    recent = [r for r in graded if _day(r["ts"]) >= cutoff]

    machines = sorted({r.get("machine", "") for r in rows if r.get("machine")})

    per_step: dict[str, dict] = defaultdict(lambda: {"ok": 0, "fail": 0, "machines": set()})
    for r in recent:
        s = per_step[r["step"]]
        s["fail" if r["ok"] == "0" else "ok"] += 1
        if r.get("machine"):
            s["machines"].add(r["machine"])

    steps = []
    for name, s in per_step.items():
        total = s["ok"] + s["fail"]
        steps.append(
            {
                "step": name,
                "runs": total,
                "fail": s["fail"],
                "rate": s["fail"] / total if total else 0.0,
                "machines": sorted(s["machines"]),
            }
        )
    steps.sort(key=lambda x: (-x["rate"], -x["fail"], x["step"]))

    # A step that used to pass and now fails is the interesting shape. Compare
    # the last `days` against everything before them, per step.
    before: dict[str, list[int]] = defaultdict(lambda: [0, 0])  # [ok, fail]
    after: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in graded:
        bucket = after if _day(r["ts"]) >= cutoff else before
        bucket[r["step"]][1 if r["ok"] == "0" else 0] += 1

    regressions = []
    for name, (a_ok, a_fail) in after.items():
        b_ok, b_fail = before.get(name, [0, 0])
        if a_ok + a_fail < 2 or b_ok + b_fail < 2:
            continue  # too little history either side to claim a change
        rate_before = b_fail / (b_ok + b_fail)
        rate_after = a_fail / (a_ok + a_fail)
        if rate_before <= 0.2 and rate_after >= 0.6:
            first_bad = min(
                (_day(r["ts"]) for r in graded if r["step"] == name and r["ok"] == "0" and _day(r["ts"]) >= cutoff),
                default="",
            )
            regressions.append(
                {
                    "step": name,
                    "before": rate_before,
                    "after": rate_after,
                    "machines": sorted(
                        {r.get("machine", "") for r in graded
                         if r["step"] == name and r["ok"] == "0" and _day(r["ts"]) >= cutoff}
                        - {""}
                    ),
                    "since": first_bad,
                }
            )
    regressions.sort(key=lambda x: (-x["after"], x["step"]))

    runs = {r.get("run_id") for r in rows if r.get("run_id")}
    return {
        "rows": len(rows),
        "graded": len(graded),
        "recent": len(recent),
        "machines": machines,
        "runs": len(runs),
        "steps": steps,
        "regressions": regressions,
        "days": days,
        "cutoff": cutoff,
    }


def render(a: dict, source: str) -> str:
    single = len(a["machines"]) < 2
    lines = [
        "---",
        "tags: [rpa, telemetry, generated]",
        f"updated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "---",
        "",
        "# RPA — Сводка телеметрии (генерируется)",
        "",
        "> Файл создаётся скриптом `scripts/acl_telemetry_report.py`. Правки будут",
        "> затёрты при следующем запуске — выводы и разборы пишите в",
        "> [[RPA Publish — Дневник тестов]].",
        "",
        f"Источник: **{source}** · окно: последние **{a['days']} дн.** (с {a['cutoff']})",
        "",
        f"- машин, приславших данные: **{len(a['machines'])}** "
        + (f"({', '.join(a['machines'])})" if a["machines"] else ""),
        f"- прогонов: **{a['runs']}** · записей: **{a['rows']}** (из них с оценкой успеха: {a['graded']})",
        "",
    ]

    lines += ["## Подозрение на смену интерфейса Meta", ""]
    if not a["regressions"]:
        lines += ["Шагов, которые раньше проходили, а теперь стабильно падают, не найдено.", ""]
    else:
        if single:
            lines += [
                "⚠️ **Данные только с одной машины.** Отличить смену интерфейса у всех от",
                "локальной поломки невозможно — это станет видно, когда телеметрию начнут",
                "присылать несколько байеров.",
                "",
            ]
        lines += ["| Шаг | Было | Стало | С какого дня | Машины |", "|---|---|---|---|---|"]
        for r in a["regressions"]:
            lines.append(
                f"| `{r['step']}` | {r['before']:.0%} падений | **{r['after']:.0%}** | "
                f"{r['since'] or '—'} | {len(r['machines'])}: {', '.join(r['machines']) or '—'} |"
            )
        lines.append("")
        multi = [r for r in a["regressions"] if len(r["machines"]) >= 2]
        if multi:
            lines += [
                "**Падает у нескольких машин сразу — это почти наверняка Meta, а не локальная",
                f"проблема:** {', '.join('`' + r['step'] + '`' for r in multi)}",
                "",
            ]

    lines += [f"## Надёжность шагов за {a['days']} дн. (худшие сверху)", ""]
    worst = [s for s in a["steps"] if s["fail"]][:20]
    if not worst:
        lines += ["Падений нет.", ""]
    else:
        lines += ["| Шаг | Прогонов | Падений | Доля | Машин |", "|---|---|---|---|---|"]
        for s in worst:
            lines.append(
                f"| `{s['step']}` | {s['runs']} | {s['fail']} | {s['rate']:.0%} | {len(s['machines'])} |"
            )
        lines.append("")

    lines += ["## Связанные заметки", "", "- [[RPA Publish — Дневник тестов]]", "- [[00 - Индекс]]", ""]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--local", action="store_true", help="use this machine's JSONL, not the sheet")
    ap.add_argument("--days", type=int, default=7, help="window for the 'recent' half")
    ap.add_argument("--vault", default=None,
                    help="Obsidian folder (default: ACL_OBSIDIAN_DIR or the known locations)")
    ap.add_argument("--no-write", action="store_true", help="print, do not touch Obsidian")
    args = ap.parse_args()

    rows, source = telemetry_sync.fetch_rows(local=args.local)

    if not rows:
        print("Нет данных.")
        return 1

    a = analyse(rows, args.days)
    text = render(a, source)

    if args.no_write:
        print(text)
        return 0

    vault = args.vault or paths.get_obsidian_dashboard_dir()
    if not vault:
        print(f"{paths.OBSIDIAN_MISSING_WARNING}. Вывод ниже.\n", file=sys.stderr)
        print(text)
        return 1

    target = Path(vault) / NOTE_NAME
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        print(f"Записано: {target}")
    except Exception as exc:
        print(f"Не удалось записать в Obsidian ({exc}). Вывод ниже.\n", file=sys.stderr)
        print(text)
        return 1

    print(
        f"машин: {len(a['machines'])} · прогонов: {a['runs']} · "
        f"подозрений на смену UI: {len(a['regressions'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
