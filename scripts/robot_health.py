"""Таблица здоровья робота по телеметрии (ACL-1434).

    python scripts/robot_health.py                       # своя output/
    python scripts/robot_health.py --n 10 --top 15       # худшие 15 шагов
    python scripts/robot_health.py --telemetry A.jsonl B.jsonl --log serve-live.log
    python scripts/robot_health.py --run 4121938e6dc3    # сводка одного прогона
    python scripts/robot_health.py --json                # всё одним JSON

Только чтение: телеметрия и лог дешборда, ни одного обращения к аккаунтам.
Выход — Markdown (годится в PR/Issue). Подписи аккаунтов без id профиля.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.web import robot_health as rh  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Robot health: step success rate per account and build.")
    ap.add_argument("--telemetry", nargs="*", help="rpa_telemetry.jsonl files (default: output/ of this install)")
    ap.add_argument("--log", nargs="*", default=None, help="dashboard logs with 'RPA flow version' lines")
    ap.add_argument("--output-dir", default=None, help="output folder to read (default: runtime workspace)")
    ap.add_argument("--n", type=int, default=rh.DEFAULT_WINDOW, help="runs per window (default 10)")
    ap.add_argument("--top", type=int, default=None, help="only the N worst steps")
    ap.add_argument("--runs", type=int, default=10, help="recent runs to summarise")
    ap.add_argument("--run", default=None, help="summary of one run id")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    if a.output_dir:
        out_dir = Path(a.output_dir)
        logs_dir = None  # явная папка — читаем только её
    else:
        from config.paths import get_runtime_workspace
        out_dir = get_runtime_workspace() / "output"
        logs_dir = rh.launcher_logs_dir()  # установленная сборка пишет логи туда
    tel_default, log_default = rh.default_sources(out_dir, logs_dir)
    tel = a.telemetry or tel_default
    logs = a.log if a.log is not None else log_default

    report, runs = rh.report_from_files(tel, logs, a.n)
    if a.run:
        summary = rh.run_summary(runs, a.run, a.n)
        if not summary:
            print(f"run {a.run} not found", file=sys.stderr)
            return 1
        summary["captures"] = [str(p) for p in rh.capture_dirs(out_dir, a.run)]
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    if a.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if not report["n_runs"]:
        print("No runs with step outcomes in the telemetry.")
        return 0
    print(rh.render_markdown(report, top=a.top))
    print()
    print(f"Recent runs (new = step did not fail in the previous {report['window']} runs):")
    print()
    print(rh.render_runs_markdown(report, a.runs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
