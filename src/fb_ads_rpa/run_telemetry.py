"""One append-only telemetry log for every RPA run — CLI and dashboard alike.

Replaces the ad-hoc debug dumps that grew per-bug during the 2026-07-25..29
debugging push (`debug_ad_copy_locator_miss.json`,
`debug_create_ad.json`, `debug_ad_level_soft_fails.txt`). Those each had a
different format, only one was append-only, and answering "how often does
this step actually fail, and with what diagnosis" meant reading PNG
screenshots one at a time.

Format: one JSON object per line (JSONL), append-only, never rewritten, so
concurrent runs and crashed runs can't lose or truncate history.

Each record:
    run_id     — groups records from one publish run
    ts         — UTC ISO timestamp
    step       — step name, normalised (no after_/fail_ prefix)
    ok         — True (step succeeded), False (failed), None (informational)
    campaign   — campaign name, when known
    duration_s — seconds since the previous step of the same run
    diag       — free-form dict with whatever the failing site knows
    note       — short free text
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
from config.paths import get_runtime_workspace
TELEMETRY_PATH = get_runtime_workspace() / "output" / "rpa_telemetry.jsonl"


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def normalise_step(label: str) -> tuple[str, bool | None]:
    """Split a screenshot label into (step, ok).

    The recorded flow already names every screenshot `after_<step>` on
    success and `fail_<step>` on failure, so those labels are a complete,
    zero-maintenance description of the run — no separate instrumentation
    to keep in sync with the steps themselves.
    """
    if label.startswith("after_"):
        return label[len("after_"):], True
    if label.startswith("fail_"):
        return label[len("fail_"):], False
    if label.startswith("success_"):
        return label[len("success_"):], True
    return label, None


def record(
    step: str,
    ok: bool | None = None,
    *,
    run_id: str = "",
    campaign: str = "",
    duration_s: float | None = None,
    diag: dict[str, Any] | None = None,
    note: str = "",
    path: Path | None = None,
) -> None:
    """Append one record. Never raises — telemetry must not break a run."""
    try:
        p = path or TELEMETRY_PATH
        p.parent.mkdir(parents=True, exist_ok=True)
        rec: dict[str, Any] = {
            "run_id": run_id,
            "ts": datetime.now(timezone.utc).isoformat(),
            "step": step,
            "ok": ok,
        }
        if campaign:
            rec["campaign"] = campaign
        if duration_s is not None:
            rec["duration_s"] = round(duration_s, 1)
        if diag:
            rec["diag"] = diag
        if note:
            rec["note"] = note
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def load(path: Path | None = None) -> list[dict[str, Any]]:
    p = path or TELEMETRY_PATH
    if not p.is_file():
        return []
    out: list[dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def summarize(path: Path | None = None, last_runs: int | None = None) -> list[dict[str, Any]]:
    """Per-step reliability, worst first — the question screenshots can't answer."""
    rows = load(path)
    if last_runs:
        seen: list[str] = []
        for r in rows:
            rid = r.get("run_id") or ""
            if rid and rid not in seen:
                seen.append(rid)
        keep = set(seen[-last_runs:])
        rows = [r for r in rows if (r.get("run_id") or "") in keep]

    stats: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r.get("ok") is None:
            continue
        s = stats.setdefault(
            r["step"], {"step": r["step"], "ok": 0, "fail": 0, "slowest_s": 0.0, "last_diag": None}
        )
        if r["ok"]:
            s["ok"] += 1
        else:
            s["fail"] += 1
            if r.get("diag"):
                s["last_diag"] = r["diag"]
        d = r.get("duration_s")
        if isinstance(d, (int, float)) and d > s["slowest_s"]:
            s["slowest_s"] = float(d)

    out = list(stats.values())
    for s in out:
        total = s["ok"] + s["fail"]
        s["runs"] = total
        s["fail_rate"] = round(s["fail"] / total, 2) if total else 0.0
    out.sort(key=lambda s: (-s["fail_rate"], -s["fail"], s["step"]))
    return out


def _main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Summarise RPA run telemetry.")
    ap.add_argument("--last-runs", type=int, default=None, help="only the N most recent runs")
    args = ap.parse_args()

    rows = summarize(last_runs=args.last_runs)
    if not rows:
        print(f"No telemetry yet at {TELEMETRY_PATH}")
        return
    print(f"{'step':38s} {'runs':>5s} {'fail':>5s} {'rate':>5s} {'slowest':>8s}")
    print("-" * 70)
    for s in rows:
        print(
            f"{s['step'][:38]:38s} {s['runs']:5d} {s['fail']:5d} "
            f"{s['fail_rate']:5.2f} {s['slowest_s']:7.1f}s"
        )
    worst = [s for s in rows if s["fail"] and s["last_diag"]]
    if worst:
        print("\nlast diagnosis per failing step:")
        for s in worst[:8]:
            print(f"  {s['step']}: {json.dumps(s['last_diag'], ensure_ascii=False)[:160]}")


if __name__ == "__main__":
    _main()
