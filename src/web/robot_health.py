"""Здоровье робота: доля успеха каждого шага по аккаунтам и сборкам (ACL-1434).

Читает только локальную телеметрию (`output/rpa_telemetry.jsonl`) и логи
дешборда (`output/serve*.log`; у установленной сборки — `server.std*.log`
лаунчера в `%LOCALAPPDATA%/ACL/logs`) — ни одного обращения к аккаунтам. Отвечает на
вопрос «шаг X на аккаунте Y стал падать — с какой сборки и с какого прогона»,
вместо «раньше же работало».

Что считается шагом (решение ACL-1434)
--------------------------------------
Строка телеметрии — исход шага, только если у неё `ok` равно true/false и шаг
не диагностический. Не считаются:

- `ok = null` — информационные строки (`skip_*`, замеры, «что было на экране»);
- служебные фазы задания (`job_phase`), окно/вкладка/профиль/браузер
  (`window_*`, `tab_*`, `profile_*`, `browser_*`, `stall_*`) — это состояние
  машины и меры по его восстановлению, а не шаг мастера Meta;
- попытки и перепроверки внутри шага (`*_attempt`, `*_second_pass`,
  `*_recheck`, `languages_pick`, `create_quick_retry`, `fresh_tab`,
  `loading_modal_reload`) — исход шага пишет сам шаг;
- диагностические замечания, которые по построению всегда `false`
  (`advantage_plus_off` «still_on», `ad_copy_blocked` «before_first_try»,
  `click_target_covered`, `click_refused`) и обрывы по бюджету времени
  (`*_budget_exceeded`) — последние видны в сводке прогона как причина обрыва.

Если шаг в одном прогоне записан несколько раз (повтор после неудачи), исход
прогона — ПОСЛЕДНЯЯ его запись: шаг, который со второй попытки прошёл, для
кампании прошёл. Прогон, не дошедший до шага, в знаменатель шага не входит —
недошедший шаг не провален, провален тот, на котором прогон встал.

Окна: для каждого столбца (аккаунт или «all») берутся последние N прогонов
этого столбца, в которых был хоть один исход шага, и предыдущие N — для
тренда. Подсветка: падение доли на ≥ 30 пунктов (в обоих окнах ≥ 2 попыток —
одна попытка не тренд) или ≥ 2 провала подряд на конце ряда.

Сборка прогона: поле `build`/`flow_version` в строке телеметрии, если оно есть;
иначе — по логу дешборда: строка «RPA flow version: v96+13@410bd23» открывает
период сборки, и задание относится к той сборке, под которой лог впервые
упомянул его id. По времени сопоставить нельзя: в логе только часы без даты.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

DEFAULT_WINDOW = 10
DROP_POINTS = 30
STREAK_LEN = 2
MIN_SAMPLES = 2
ALL = "all"
UNKNOWN_ACCOUNT = "unknown"
UNKNOWN_BUILD = "unknown"

_DIAGNOSTIC_STEPS = frozenset({
    "job_phase", "drafts_pending", "overlays_cleared", "listbox_left_open",
    "click_target_covered", "click_refused", "advantage_plus_off",
    "ad_copy_blocked", "languages_pick", "create_quick_retry", "fresh_tab",
    "loading_modal_reload", "text_generation_off", "before_audience_location",
    "objective_wait", "further_limit_continue", "destination_sweep_cleared",
    "destination_sweep_skipped",
})
_DIAGNOSTIC_PREFIXES = ("window_", "tab_", "stall_", "profile_", "browser_")
_DIAGNOSTIC_SUFFIXES = ("_attempt", "_second_pass", "_recheck", "_budget_exceeded")
_BUILD_FIELDS = ("build", "flow_version", "rpa_flow_version")

_VERSION_RE = re.compile(r"RPA flow version:\s*(\S+)")
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_JOB_PATH_RE = re.compile(r"/publish-jobs/([0-9A-Za-z_-]+)")
_START_RE = re.compile(r"\[serve\] Dashboard URL:")
_START_FALLBACK_RE = re.compile(r"Started server process")
_RUN_ID_RE = re.compile(r"^[0-9A-Za-z_-]{1,64}$")


def is_outcome_step(step: str, ok) -> bool:
    """Исход шага мастера, а не диагностика (см. docstring модуля)."""
    if ok is None or not isinstance(ok, bool) or not step:
        return False
    if step in _DIAGNOSTIC_STEPS:
        return False
    if step.startswith(_DIAGNOSTIC_PREFIXES) or step.endswith(_DIAGNOSTIC_SUFFIXES):
        return False
    return True


def abort_label(step: str, note, diag) -> str:
    """Причина обрыва прогона по-человечески. ACL-1673: закрытая вкладка —
    «tab closed» с шагом, на котором её не стало (`diag.where`)."""
    where = diag.get("where") if isinstance(diag, dict) else ""
    if step == "browser_closed" or note == "page_closed":
        return "tab closed" + (f" at {where}" if where else "")
    return ""


def account_label(name: str) -> str:
    """Короткое имя аккаунта без id профиля: «Facebook_1443_6157…» → «Facebook_1443»,
    «FacebookDoc19 - Имя» → «FacebookDoc19»."""
    s = str(name or "").split(" - ")[0].strip()
    s = re.sub(r"_\d{10,}", "", s)
    return s or UNKNOWN_ACCOUNT


# ── чтение ──────────────────────────────────────────────────────────────────

def load_rows(paths: Iterable[Path | str]) -> list[dict]:
    """Строки телеметрии из одного или нескольких файлов; битые строки — мимо
    (журнал пишут параллельные процессы, оборванная запись не повод падать)."""
    rows: list[dict] = []
    for p in paths:
        p = Path(p)
        if not p.is_file():
            continue
        with p.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    return rows


def _read_lines(path: Path) -> list[str]:
    with path.open(encoding="utf-8", errors="replace") as fh:
        return fh.readlines()


def _jobs_in(line: str) -> list[str]:
    return _UUID_RE.findall(line) + _JOB_PATH_RE.findall(line)


def _companion_key(path: Path) -> str:
    """Общее имя пары stdout/stderr: serve3.log ↔ serve3.err.log,
    server.stdout.log ↔ server.stderr.log (лаунчер установленной сборки)."""
    name = path.name.lower()
    name = name.replace(".stderr.", ".std.").replace(".stdout.", ".std.")
    name = name.replace(".err.", ".").replace(".out.", ".")
    return str(path.parent.resolve() / name)


def job_builds_from_logs(paths: Iterable[Path | str]) -> dict[str, str]:
    """job id → сборка, под которой лог дешборда впервые упомянул задание.

    Строку версии serve.py печатает в stdout, а строки uvicorn с id заданий
    идут в stderr. Пока они в одном файле (`serve-live.log` с `2>&1`), версия
    просто открывает период. Если потоки в разных файлах (`serveN.log` +
    `serveN.err.log`, у установленной сборки `server.stdout.log` +
    `server.stderr.log` — оба дописываются при каждом запуске), файл без строк
    версии сопоставляется со своим парным по запускам: k-й с конца запуск в
    stderr («[serve] Dashboard URL», иначе «Started server process») — k-я с
    конца версия в stdout. С конца — потому что запуск, упавший до uvicorn,
    оставляет версию без запуска, и сдвиг тогда задевает только старые запуски.
    """
    files = []
    for p in paths:
        p = Path(p)
        if p.is_file():
            files.append((p, _read_lines(p)))
    versions_by_key: dict[str, list[str]] = defaultdict(list)
    for p, lines in files:
        versions_by_key[_companion_key(p)] += [m.group(1) for m in map(_VERSION_RE.search, lines) if m]

    out: dict[str, str] = {}
    for p, lines in files:
        own = [m.group(1) for m in map(_VERSION_RE.search, lines) if m]
        if own:
            current = ""
            for line in lines:
                m = _VERSION_RE.search(line)
                if m:
                    current = m.group(1)
                    continue
                if current:
                    for job in _jobs_in(line):
                        out.setdefault(job, current)
            continue
        versions = versions_by_key.get(_companion_key(p)) or []
        if not versions:
            continue
        starts = [i for i, line in enumerate(lines) if _START_RE.search(line)]
        if not starts:
            starts = [i for i, line in enumerate(lines) if _START_FALLBACK_RE.search(line)]
        if not starts:
            # Ни одного запуска не видно: однозначно только при одной версии.
            if len(versions) == 1:
                for line in lines:
                    for job in _jobs_in(line):
                        out.setdefault(job, versions[0])
            continue
        shift = len(versions) - len(starts)
        bounds = starts + [len(lines)]
        for k in range(len(starts)):
            if k + shift < 0:
                continue  # запуск старше первой версии в парном файле
            build = versions[k + shift]
            for line in lines[bounds[k]:bounds[k + 1]]:
                for job in _jobs_in(line):
                    out.setdefault(job, build)
    return out


# ── прогоны ─────────────────────────────────────────────────────────────────

@dataclass
class Run:
    run_id: str
    job: str = ""
    account: str = UNKNOWN_ACCOUNT
    build: str = UNKNOWN_BUILD
    started: str = ""
    ended: str = ""
    campaign: str = ""
    #: шаг → (исход последней записи, note последней записи), в порядке шагов
    outcomes: dict[str, tuple[bool, str]] = field(default_factory=dict)
    #: последний провал вне счёта шагов (обрыв по бюджету, профиль и т.п.)
    abort: str = ""
    #: человекочитаемая причина обрыва («tab closed at ad3_destination»)
    abort_label: str = ""

    @property
    def failed(self) -> list[str]:
        return [s for s, (ok, _) in self.outcomes.items() if not ok]

    @property
    def label(self) -> str:
        return account_label(self.account)


def build_runs(rows: list[dict], job_builds: dict[str, str] | None = None,
               current_build: tuple[str, str] | None = None) -> list[Run]:
    """Прогоны по возрастанию времени начала.

    `current_build` = (сборка, ISO-время старта процесса): прогоны без сборки,
    начатые после старта текущего дешборда, — его сборка.
    """
    job_builds = job_builds or {}
    job_account: dict[str, str] = {}
    for r in rows:
        diag = r.get("diag")
        job = r.get("job")
        if job and isinstance(diag, dict) and diag.get("account"):
            job_account.setdefault(job, str(diag["account"]))

    runs: dict[str, Run] = {}
    for r in sorted(rows, key=lambda x: str(x.get("ts") or "")):
        rid = str(r.get("run_id") or "")
        if not rid:
            continue
        run = runs.get(rid)
        ts = str(r.get("ts") or "")
        if run is None:
            run = runs[rid] = Run(run_id=rid, started=ts)
        run.ended = ts or run.ended
        if r.get("job") and not run.job:
            run.job = str(r["job"])
        if r.get("campaign") and not run.campaign:
            run.campaign = str(r["campaign"])
        for key in _BUILD_FIELDS:
            if r.get(key):
                run.build = str(r[key])
                break
        step = str(r.get("step") or "")
        ok = r.get("ok")
        if is_outcome_step(step, ok):
            run.outcomes[step] = (ok, str(r.get("note") or ""))
            run.abort = ""
            run.abort_label = ""
        elif ok is False and step != "job_phase":
            # Провал вне счёта шагов, после которого исходов больше не было, —
            # причина обрыва прогона (бюджет времени, профиль, вкладка).
            run.abort = step + (f" ({r.get('note')})" if r.get("note") else "")
            run.abort_label = abort_label(step, r.get("note"), r.get("diag"))

    for run in runs.values():
        if run.job in job_account:
            run.account = job_account[run.job]
        if run.build == UNKNOWN_BUILD:
            if run.job in job_builds:
                run.build = job_builds[run.job]
            elif current_build and current_build[0] and run.started >= current_build[1]:
                run.build = current_build[0]
    return sorted(runs.values(), key=lambda x: x.started)


# ── сводка ──────────────────────────────────────────────────────────────────

def _rate(ok: int, total: int) -> float | None:
    return round(100.0 * ok / total, 1) if total else None


def _step_order(runs: list[Run]) -> list[str]:
    """Шаги в порядке мастера: средняя позиция шага внутри прогонов."""
    pos: dict[str, list[float]] = defaultdict(list)
    for run in runs:
        n = max(len(run.outcomes) - 1, 1)
        for i, step in enumerate(run.outcomes):
            pos[step].append(i / n)
    return sorted(pos, key=lambda s: (sum(pos[s]) / len(pos[s]), s))


def _count(runs: list[Run], step: str) -> tuple[int, int, list[str]]:
    ok = total = 0
    failed: list[str] = []
    for run in runs:
        if step in run.outcomes:
            total += 1
            if run.outcomes[step][0]:
                ok += 1
            else:
                failed.append(run.run_id)
    return ok, total, failed


def _streak(runs: list[Run], step: str) -> list[str]:
    """run_id хвостовой серии провалов шага (последний прогон — последним)."""
    tail: list[str] = []
    for run in reversed(runs):
        if step not in run.outcomes:
            continue
        if run.outcomes[step][0]:
            break
        tail.append(run.run_id)
    return list(reversed(tail))


def build_report(runs: list[Run], window: int = DEFAULT_WINDOW,
                 recent_runs: int | None = None) -> dict:
    """Всё, что показывают страница и скрипт, одним словарём."""
    window = max(int(window or DEFAULT_WINDOW), 1)
    counted = [r for r in runs if r.outcomes]
    by_col: dict[str, list[Run]] = defaultdict(list)
    for run in counted:
        by_col[run.label].append(run)
    accounts = sorted(by_col, key=lambda a: (-len(by_col[a]), a))
    columns = [ALL] + accounts
    by_col[ALL] = counted
    steps = _step_order(counted)
    # Шаги, которые пишутся только при провале (успех уходит в лог под другим
    # именем, напр. objective_leads → leads): ok/total у них = 0/провалы.
    fail_only = sorted(s for s in steps
                       if not any(r.outcomes.get(s, (False,))[0] for r in counted))

    cells: dict[str, dict[str, dict]] = {}
    highlights: list[dict] = []
    for step in steps:
        cells[step] = {}
        for col in columns:
            seq = by_col[col]
            now, prev = seq[-window:], seq[-2 * window:-window] if len(seq) > window else []
            ok, total, failed = _count(now, step)
            pok, ptotal, _ = _count(prev, step)
            rate, prate = _rate(ok, total), _rate(pok, ptotal)
            trend = round(rate - prate, 1) if rate is not None and prate is not None else None
            # Серия считается внутри текущего окна: давняя серия, после которой
            # шаг больше не встречался, не тревога сегодняшнего дня.
            streak = _streak(now, step)
            flags = []
            if (trend is not None and trend <= -DROP_POINTS
                    and total >= MIN_SAMPLES and ptotal >= MIN_SAMPLES):
                flags.append("drop")
            if len(streak) >= STREAK_LEN:
                flags.append("streak")
            cell = {"ok": ok, "total": total, "rate": rate, "prev_ok": pok,
                    "prev_total": ptotal, "prev_rate": prate, "trend": trend,
                    "streak": streak, "failed_runs": failed, "flags": flags}
            cells[step][col] = cell
            if flags:
                highlights.append({"step": step, "column": col, **cell})

    builds_seen: list[str] = []
    for run in counted:
        if run.build not in builds_seen:
            builds_seen.append(run.build)
    build_runs_: dict[str, list[Run]] = defaultdict(list)
    for run in counted:
        build_runs_[run.build].append(run)
    by_build: dict[str, dict[str, dict]] = {}
    for step in steps:
        by_build[step] = {}
        prev = None
        for b in builds_seen:
            ok, total, failed = _count(build_runs_[b], step)
            rate = _rate(ok, total)
            flags = []
            if (prev and rate is not None and prev["rate"] is not None
                    and total >= MIN_SAMPLES and prev["total"] >= MIN_SAMPLES
                    and rate - prev["rate"] <= -DROP_POINTS):
                flags.append("drop")
                highlights.append({"step": step, "column": f"build {b}", "ok": ok,
                                   "total": total, "rate": rate, "prev_rate": prev["rate"],
                                   "trend": round(rate - prev["rate"], 1),
                                   "streak": [], "failed_runs": failed, "flags": flags})
            cell = {"ok": ok, "total": total, "rate": rate, "failed_runs": failed,
                    "flags": flags}
            by_build[step][b] = cell
            if total:
                prev = cell

    # Худшие шаги наверх: сначала подсвеченные, потом по числу провалов в «all».
    def _badness(step: str) -> tuple:
        c = cells[step][ALL]
        flagged = any(cells[step][col]["flags"] for col in columns)
        return (not flagged, -(c["total"] - c["ok"]), step)

    problem_steps = [s for s in sorted(steps, key=_badness)
                     if cells[s][ALL]["total"] > cells[s][ALL]["ok"]
                     or any(cells[s][c]["flags"] for c in columns)]

    highlights.sort(key=lambda h: (h["column"] != ALL, -len(h["streak"]),
                                   h["trend"] if h["trend"] is not None else 0))
    n_recent = recent_runs if recent_runs is not None else 2 * window
    return {
        "window": window,
        "columns": columns,
        "steps": steps,
        "cells": cells,
        "builds": builds_seen,
        "by_build": by_build,
        "highlights": highlights,
        "problem_steps": problem_steps,
        "fail_only": fail_only,
        "runs": [run_summary(runs, r.run_id, window) for r in reversed(runs[-n_recent:])],
        "n_runs": len(counted),
        "first_ts": counted[0].started if counted else "",
        "last_ts": counted[-1].ended if counted else "",
    }


def run_summary(runs: list[Run], run_id: str, window: int = DEFAULT_WINDOW) -> dict:
    """Что упало в прогоне и ново ли это.

    new — шаг не падал в предыдущих N прогонах ни на одном аккаунте;
    known — падал на этом же аккаунте; known elsewhere — падал только на других.
    """
    idx = next((i for i, r in enumerate(runs) if r.run_id == run_id), None)
    if idx is None:
        return {}
    run = runs[idx]
    before = [r for r in runs[:idx] if r.outcomes][-window:]
    same = [r for r in runs[:idx] if r.outcomes and r.label == run.label][-window:]
    failures = []
    for step in run.failed:
        mine = sum(1 for r in same if step in r.outcomes and not r.outcomes[step][0])
        other = sum(1 for r in before if r.label != run.label
                    and step in r.outcomes and not r.outcomes[step][0])
        status = "known" if mine else ("known elsewhere" if other else "new")
        failures.append({"step": step, "note": run.outcomes[step][1], "status": status,
                         "seen_before": mine + other})
    return {
        "run_id": run.run_id, "account": run.label, "build": run.build,
        "started": run.started, "ended": run.ended, "campaign": run.campaign,
        "n_steps": len(run.outcomes), "n_ok": len(run.outcomes) - len(run.failed),
        "failures": failures, "abort": run.abort, "abort_label": run.abort_label,
        "new_failures": sum(1 for f in failures if f["status"] == "new"),
    }


def capture_dirs(output_dir: Path | str, run_id: str) -> list[Path]:
    """Папки снимков прогона: `rpa_screenshots/<run>` и `rpa_pages/<run>`."""
    if not _RUN_ID_RE.match(str(run_id or "")):
        return []
    base = Path(output_dir)
    return [p for p in (base / "rpa_screenshots" / run_id, base / "rpa_pages" / run_id)
            if p.is_dir()]


# ── текст для скрипта и PR ──────────────────────────────────────────────────

def _cell_text(c: dict) -> str:
    if not c["total"]:
        return "-"
    s = f"{c['ok']}/{c['total']}"
    if c.get("trend") is not None and c["trend"]:
        s += f" ({c['trend']:+.0f})"
    if "streak" in c.get("flags", ()):
        s += " !!"
    elif "drop" in c.get("flags", ()):
        s += " !"
    return s


def render_markdown(report: dict, top: int | None = None, max_builds: int = 6) -> str:
    cols = report["columns"]
    steps = report["problem_steps"] if top is not None else report["steps"]
    if top is not None:
        steps = steps[:top]
    out = [f"Runs counted: {report['n_runs']} ({report['first_ts'][:16]} .. "
           f"{report['last_ts'][:16]} UTC), window N={report['window']}. "
           f"Cell = ok/total over the last N runs (trend vs previous N, points); "
           f"`!` drop >= {DROP_POINTS} pts, `!!` >= {STREAK_LEN} failures in a row.", ""]
    out.append("| step | " + " | ".join(cols) + " |")
    out.append("|---|" + "---|" * len(cols))
    fo = set(report.get("fail_only") or ())
    for s in steps:
        name = f"{s}*" if s in fo else s
        out.append(f"| {name} | " + " | ".join(_cell_text(report["cells"][s][c]) for c in cols) + " |")
    if fo & set(steps):
        out += ["", "`*` written only on failure (success is logged under another step), "
                "so the cell is 0/failures."]
    builds = report["builds"][-max_builds:]
    if builds:
        out += ["", "By build (all accounts, every run of the build):", ""]
        out.append("| step | " + " | ".join(builds) + " |")
        out.append("|---|" + "---|" * len(builds))
        for s in steps:
            out.append(f"| {s} | " + " | ".join(_cell_text(report["by_build"][s][b])
                                                 for b in builds) + " |")
    if report["highlights"]:
        out += ["", "Highlights:", ""]
        for h in report["highlights"][:20]:
            why = []
            if "streak" in h["flags"]:
                why.append(f"{len(h['streak'])} failures in a row (runs {', '.join(h['streak'][-3:])})")
            if "drop" in h["flags"]:
                why.append(f"{h.get('prev_rate')}% -> {h['rate']}%")
            out.append(f"- **{h['step']}** on {h['column']}: " + "; ".join(why))
    return "\n".join(out)


def render_runs_markdown(report: dict, limit: int = 10) -> str:
    out = ["| run | account | build | steps ok | failed (new/known) |", "|---|---|---|---|---|"]
    for r in report["runs"][:limit]:
        fails = ", ".join(f"{f['step']} [{f['status']}]" for f in r["failures"]) or (
            f"aborted: {r['abort']}" if r["abort"] else "—")
        out.append(f"| {r['run_id']} {r['started'][5:16]} | {r['account']} | {r['build']} | "
                   f"{r['n_ok']}/{r['n_steps']} | {fails} |")
    return "\n".join(out)


def launcher_logs_dir() -> Path:
    """Логи установленной сборки: лаунчер пишет stdout/stderr сервера в
    `%LOCALAPPDATA%/ACL/logs` (packaging/windows/launcher.py)."""
    from config.paths import get_local_config_dir
    return Path(get_local_config_dir()) / "logs"


def default_sources(output_dir: Path | str,
                    logs_dir: Path | str | None = None) -> tuple[list[Path], list[Path]]:
    """Телеметрия и логи дешборда: `output/serve*.log` (запуск руками) и,
    если дан `logs_dir`, `server.std*.log` лаунчера установленной сборки."""
    base = Path(output_dir)
    logs = sorted(base.glob("serve*.log"))
    if logs_dir is not None:
        logs += sorted(Path(logs_dir).glob("server.std*.log"))
    return [base / "rpa_telemetry.jsonl"], logs


def report_from_files(telemetry: Iterable[Path | str], logs: Iterable[Path | str] = (),
                      window: int = DEFAULT_WINDOW,
                      current_build: tuple[str, str] | None = None) -> tuple[dict, list[Run]]:
    runs = build_runs(load_rows(telemetry), job_builds_from_logs(logs), current_build)
    return build_report(runs, window), runs


# ── страница дешборда ───────────────────────────────────────────────────────

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}


def _sources(output_dir: Path | str, current_build, logs_dir=None) -> list[Run]:
    tel, logs = default_sources(output_dir, logs_dir)
    return build_runs(load_rows(tel), job_builds_from_logs(logs), current_build)


def page_context(output_dir: Path | str, window: int = DEFAULT_WINDOW,
                 current_build: tuple[str, str] | None = None,
                 logs_dir: Path | str | None = None) -> dict:
    window = min(max(int(window or DEFAULT_WINDOW), 1), 200)
    ctx = {"window": window, "drop_points": DROP_POINTS, "streak_len": STREAK_LEN,
           "run": None, "error": "", "report": None}
    runs = _sources(output_dir, current_build, logs_dir)
    if not any(r.outcomes for r in runs):
        ctx["error"] = ("No robot runs on this machine yet: the run log "
                        "output/rpa_telemetry.jsonl is empty or missing.")
        return ctx
    ctx["report"] = build_report(runs, window)
    return ctx


def run_context(output_dir: Path | str, run_id: str, window: int = DEFAULT_WINDOW,
                current_build: tuple[str, str] | None = None,
                logs_dir: Path | str | None = None) -> dict:
    window = min(max(int(window or DEFAULT_WINDOW), 1), 200)
    ctx = {"window": window, "drop_points": DROP_POINTS, "streak_len": STREAK_LEN,
           "run": None, "error": "", "report": None, "run_steps": [], "shots": [],
           "capture_dirs": []}
    runs = _sources(output_dir, current_build, logs_dir)
    summary = run_summary(runs, run_id, window) if _RUN_ID_RE.match(run_id or "") else {}
    if not summary:
        ctx["error"] = f"Run {run_id!r} is not in this machine's run log."
        return ctx
    run = next(r for r in runs if r.run_id == run_id)
    ctx["run"] = summary
    ctx["run_steps"] = [{"step": s, "ok": ok, "note": note}
                        for s, (ok, note) in run.outcomes.items()]
    dirs = capture_dirs(output_dir, run_id)
    ctx["capture_dirs"] = [str(d) for d in dirs]
    shots_dir = Path(output_dir) / "rpa_screenshots" / run_id
    if shots_dir.is_dir():
        ctx["shots"] = sorted(p.name for p in shots_dir.iterdir()
                              if p.is_file() and p.suffix.lower() in _IMAGE_EXTS)
    return ctx


def capture_file(output_dir: Path | str, run_id: str, name: str) -> Path | None:
    """Снимок прогона, если имя — картинка прямо в папке прогона (без `..`)."""
    if not _RUN_ID_RE.match(run_id or "") or not name or "/" in name or "\\" in name:
        return None
    folder = (Path(output_dir) / "rpa_screenshots" / run_id).resolve()
    path = (folder / name).resolve()
    if path.parent != folder or path.suffix.lower() not in _IMAGE_EXTS or not path.is_file():
        return None
    return path
