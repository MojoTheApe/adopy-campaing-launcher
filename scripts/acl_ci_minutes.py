"""Сколько минут GitHub Actions съел репозиторий: по дням, workflow и событиям (ACL-1649).

Данные берутся из Actions API (только чтение): запуски за период и задания каждого
запуска, включая все попытки (filter=all). Минуты считаются так же, как биллинг:
длительность каждого задания округляется вверх до минуты, Windows считается ×2
(в «Linux-эквиваленте»). Отдельно считается доля переиспользования (ACL-1565):
push-прогоны ci.yml на stage/main, у которых тестовые задания пропущены.

    python scripts/acl_ci_minutes.py --from 2026-10-05 --to 2026-10-06
    python scripts/acl_ci_minutes.py --from 2026-10-05 --to 2026-10-06 --json
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import math
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

DEFAULT_REPO = "MojoTheApe/adopy-campaing-launcher"
WINDOWS_FACTOR = 2
# Ветки, на которых push-прогон ci.yml считается «продвижением» для доли reuse.
PROMOTION_BRANCHES = ("stage", "main")
TEST_JOB_PREFIXES = ("linux tests", "windows tests")


def gh_api(path, attempts=4):
    """GET через gh; сетевые сбои (TLS timeout) повторяются, сам запрос только читает."""
    for attempt in range(attempts):
        try:
            return json.loads(subprocess.check_output(["gh", "api", path], text=True, encoding="utf-8",
                                                      stderr=subprocess.DEVNULL))
        except (subprocess.CalledProcessError, ValueError):
            if attempt == attempts - 1:
                raise
            time.sleep(2 * (attempt + 1))


def _time(value):
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def job_minutes(job):
    """Минуты задания, округлённые вверх; не начатые и пропущенные задания — 0."""
    start, end = _time(job.get("started_at")), _time(job.get("completed_at"))
    if not start or not end or end <= start:
        return 0
    return math.ceil((end - start).total_seconds() / 60)


def is_windows(job):
    labels = [str(x).lower() for x in job.get("labels") or []]
    return any("windows" in x for x in labels) or "windows" in str(job.get("runner_name") or "").lower()


def list_runs(api, repo, first, last):
    """Запуски, созданные с ``first`` по ``last`` включительно (даты UTC, YYYY-MM-DD)."""
    runs, page = [], 1
    while True:
        payload = api(f"repos/{repo}/actions/runs?created={first}..{last}&per_page=100&page={page}")
        batch = payload.get("workflow_runs") or []
        runs += batch
        if len(batch) < 100:
            return runs
        page += 1


def run_jobs(api, repo, run):
    jobs, page = [], 1
    while True:
        payload = api(f"repos/{repo}/actions/runs/{run['id']}/jobs?filter=all&per_page=100&page={page}")
        batch = payload.get("jobs") or []
        jobs += batch
        if len(batch) < 100:
            return jobs
        page += 1


def is_reused(run, jobs):
    """push-прогон ci.yml на stage/main, где тесты пропущены, а scope прошёл: reuse (ACL-1565)."""
    if run.get("event") != "push" or run.get("head_branch") not in PROMOTION_BRANCHES:
        return None
    if not str(run.get("path", "")).endswith("/ci.yml"):
        return None
    tests = [j for j in jobs if str(j.get("name", "")).startswith(TEST_JOB_PREFIXES)]
    if not tests:
        return None
    return all(j.get("conclusion") == "skipped" for j in tests)


def collect(api, repo, first, last, until=None):
    """Сводка: {day: {workflow: {linux, windows}}}, по событиям, число перезапусков и reuse."""
    days = collections.defaultdict(lambda: collections.defaultdict(lambda: {"linux": 0, "windows": 0, "runs": 0}))
    events = collections.defaultdict(lambda: {"linux": 0, "windows": 0})
    by_job = collections.defaultdict(lambda: {"linux": 0, "windows": 0, "jobs": 0})
    by_workflow_event = collections.defaultdict(lambda: {"linux": 0, "windows": 0, "runs": 0})
    reuse = collections.defaultdict(lambda: {"runs": 0, "reused": 0})
    retried = {"linux": 0, "windows": 0}
    runs = [r for r in list_runs(api, repo, first, last) if not (until and r.get("created_at", "") > until)]
    with ThreadPoolExecutor(max_workers=8) as pool:  # задания каждого запуска независимы
        all_jobs = list(pool.map(lambda r: run_jobs(api, repo, r), runs))
    for run, jobs in zip(runs, all_jobs):
        day = str(run.get("created_at", ""))[:10]
        name = run.get("name") or run.get("path") or "?"
        days[day][name]["runs"] += 1
        by_workflow_event[f"{name} / {run.get('event')}"]["runs"] += 1
        for job in jobs:
            minutes = job_minutes(job)
            kind = "windows" if is_windows(job) else "linux"
            days[day][name][kind] += minutes
            events[run.get("event")][kind] += minutes
            by_workflow_event[f"{name} / {run.get('event')}"][kind] += minutes
            if minutes:
                row = by_job[f"{name}: {job.get('name')}"]
                row[kind] += minutes
                row["jobs"] += 1
            if (job.get("run_attempt") or 1) > 1:
                retried[kind] += minutes
        flag = is_reused(run, jobs)
        if flag is not None:
            bucket = reuse[f"{run['head_branch']}"]
            bucket["runs"] += 1
            bucket["reused"] += int(flag)
    return {"days": {d: {w: dict(v) for w, v in sorted(ws.items())} for d, ws in sorted(days.items())},
            "events": dict(events), "retried_attempt_minutes": retried, "reuse": dict(reuse),
            "by_workflow_event": dict(sorted(by_workflow_event.items())),
            "by_job": dict(sorted(by_job.items(), key=lambda kv: -(kv[1]["linux"] + WINDOWS_FACTOR * kv[1]["windows"])))}


def totals(result):
    out = {}
    for day, workflows in result["days"].items():
        linux = sum(v["linux"] for v in workflows.values())
        win = sum(v["windows"] for v in workflows.values())
        out[day] = {"linux": linux, "windows": win, "linux_equivalent": linux + WINDOWS_FACTOR * win}
    return out


def render(result):
    lines = ["Actions minutes (each job rounded up; Linux-equivalent = Linux + 2 x Windows)", ""]
    for day, row in totals(result).items():
        lines.append(f"{day}: Linux {row['linux']}  Windows {row['windows']}  Linux-equivalent {row['linux_equivalent']}")
        for name, v in result["days"][day].items():
            lines.append(f"    {name:<38} runs {v['runs']:>3}  Linux {v['linux']:>5}  Windows {v['windows']:>5}")
    lines += ["", "By event: " + ", ".join(f"{e}: L{v['linux']}/W{v['windows']}" for e, v in sorted(result["events"].items(), key=str))]
    lines += ["", "By workflow / event:"]
    for key, v in result["by_workflow_event"].items():
        lines.append(f"    {key:<48} runs {v['runs']:>3}  Linux {v['linux']:>5}  Windows {v['windows']:>5}")
    lines += ["", "Heaviest jobs (Linux-equivalent minutes):"]
    for key, v in list(result["by_job"].items())[:15]:
        lines.append(f"    {key:<66} x{v['jobs']:<3} Linux {v['linux']:>5}  Windows {v['windows']:>5}")
    r = result["retried_attempt_minutes"]
    lines.append(f"Minutes spent in re-run attempts (attempt > 1): Linux {r['linux']}, Windows {r['windows']}")
    for branch, v in sorted(result["reuse"].items()):
        rate = (100 * v["reused"] // v["runs"]) if v["runs"] else 0
        lines.append(f"Reuse on {branch} pushes of ci.yml: {v['reused']} of {v['runs']} runs ({rate}%)")
    return "\n".join(lines)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    p.add_argument("--from", dest="first", default=today, help="first day, UTC (default: today)")
    p.add_argument("--to", dest="last", default=None, help="last day, UTC (default: --from)")
    p.add_argument("--until", default=None, help="ignore runs created after this ISO time (e.g. 2026-10-06T16:06:00Z)")
    p.add_argument("--repo", default=DEFAULT_REPO)
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    result = collect(gh_api, args.repo, args.first, args.last or args.first, args.until)
    result["totals"] = totals(result)
    print(json.dumps(result, indent=1) if args.json else render(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
