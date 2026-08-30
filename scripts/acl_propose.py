"""Say what the evidence suggests changing — and change nothing on its own.

This is the "RPA improves itself" part of ACL, built with a deliberate limit:
it proposes, a human decides. Automatic promotion of a step variant based on
other people's runs is how one bad strategy reaches ten machines at once, and
the thing those machines drive is live ad accounts that get burned for less.

Two sources of evidence, kept apart because they answer different questions:

* **strategy_ranks.json** — variants this machine tried and the outcome. The
  learner already parks risky winners in `pending_approval`; they are listed
  here and promoted only by an explicit `--approve`.
* **the shared telemetry** — what is failing, since when, and on how many
  machines. Turned into concrete, addressable suggestions rather than raw
  counters.

  python scripts/acl_propose.py                 # proposals from the shared sheet
  python scripts/acl_propose.py --local         # this machine only
  python scripts/acl_propose.py --approve nav_ad_set:breadcrumb_ad_set
"""
from __future__ import annotations

import argparse
import statistics
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Windows consoles are not UTF-8 by default; without this a Russian line
# aborts the script instead of printing. See scripts/_console.py.
from scripts._console import use_utf8  # noqa: E402

use_utf8()

try:
    from dotenv import load_dotenv

    load_dotenv(_ROOT / ".env")
except Exception:
    pass

from src.fb_ads_rpa import telemetry_sync  # noqa: E402

SLOW_S = 60.0  # a step over a minute is a timeout waiting to happen


def _day(ts: str) -> str:
    return (ts or "")[:10]


def _ranks():
    try:
        from src.fb_ads_rpa.variations.ranks import StrategyRanks

        return StrategyRanks.load()
    except Exception as exc:
        print(f"(не удалось прочитать strategy_ranks: {exc})", file=sys.stderr)
        return None


def show_pending() -> int:
    ranks = _ranks()
    if ranks is None:
        return 0
    pending = ranks.list_pending()
    print("## Ждут вашего решения (варианты шагов)")
    print()
    if not pending:
        print("  Ничего не ждёт — рискованных победивших вариантов нет.")
        print()
        return 0
    for item in pending:
        step = item.get("step_id", "?")
        strat = item.get("strategy_id", "?")
        print(f"  {step}:{strat}")
        for k in ("note", "screenshot", "logged_at"):
            if item.get(k):
                print(f"      {k}: {item[k]}")
        print(f"      одобрить: python scripts/acl_propose.py --approve {step}:{strat}")
    print()
    return len(pending)


def approve(spec: str) -> int:
    if ":" not in spec:
        print("Формат: --approve шаг:стратегия", file=sys.stderr)
        return 2
    step_id, strategy_id = spec.split(":", 1)
    ranks = _ranks()
    if ranks is None:
        return 1
    if not ranks.approve_pending(step_id, strategy_id):
        print(f"Не найдено в ожидающих: {step_id}:{strategy_id}", file=sys.stderr)
        return 1
    ranks.save()
    print(f"Одобрено и сохранено: {step_id}:{strategy_id}")
    return 0


def proposals(rows: list[dict], days: int) -> list[dict]:
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    graded = [r for r in rows if r.get("ok") in ("0", "1") and r.get("step")]
    recent = [r for r in graded if _day(r["ts"]) >= cutoff]
    machines = {r.get("machine", "") for r in rows if r.get("machine")}

    out: list[dict] = []

    # 1. Steps that never pass any more.
    per_step: dict[str, dict] = defaultdict(lambda: {"ok": 0, "fail": 0, "machines": set()})
    for r in recent:
        s = per_step[r["step"]]
        s["fail" if r["ok"] == "0" else "ok"] += 1
        if r.get("machine"):
            s["machines"].add(r["machine"])
    for name, s in per_step.items():
        total = s["ok"] + s["fail"]
        if total >= 2 and s["ok"] == 0:
            out.append(
                {
                    "kind": "broken",
                    "step": name,
                    "why": f"{s['fail']} падений из {total} за {days} дн., ни одного успеха",
                    "machines": sorted(s["machines"]),
                    "action": "Шаг не работает вовсе — чинить локатор или логику.",
                }
            )

    # 2. Fails here, passes there. Only meaningful with several machines, and
    #    it is the difference between "fix the code" and "fix that computer".
    if len(machines) >= 2:
        by_machine: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(lambda: [0, 0]))
        for r in recent:
            b = by_machine[r["step"]][r.get("machine", "")]
            b[1 if r["ok"] == "0" else 0] += 1
        for step, per_m in by_machine.items():
            bad = [m for m, (ok, fail) in per_m.items() if fail and not ok]
            good = [m for m, (ok, fail) in per_m.items() if ok and not fail]
            if bad and good:
                out.append(
                    {
                        "kind": "machine_specific",
                        "step": step,
                        "why": f"падает у {', '.join(bad)}, но проходит у {', '.join(good)}",
                        "machines": sorted(bad),
                        "action": "Похоже на настройку конкретных машин, а не на код — "
                        "сверить у них .env, карту аккаунтов и профили Multilogin.",
                    }
                )

    # 3. Slow steps: today's timeouts are tomorrow's failures.
    durations: dict[str, list[float]] = defaultdict(list)
    for r in recent:
        try:
            durations[r["step"]].append(float(r.get("duration_s") or 0))
        except ValueError:
            continue
    for step, vals in durations.items():
        vals = [v for v in vals if v > 0]
        if len(vals) >= 3:
            worst = max(vals)
            if worst >= SLOW_S:
                out.append(
                    {
                        "kind": "slow",
                        "step": step,
                        "why": f"медиана {statistics.median(vals):.0f}s, максимум {worst:.0f}s",
                        "machines": [],
                        "action": "Близко к таймауту — либо ждём не того признака, либо "
                        "стоит поднять лимит осознанно.",
                    }
                )

    # 4. Steps that used to appear and no longer do — dead code, or a flow that
    #    silently stopped reaching them. Both are worth knowing.
    seen_before = {r["step"] for r in graded if _day(r["ts"]) < cutoff}
    seen_recent = {r["step"] for r in recent}
    for step in sorted(seen_before - seen_recent):
        out.append(
            {
                "kind": "gone",
                "step": step,
                "why": f"встречался раньше, но ни разу за {days} дн.",
                "machines": [],
                "action": "Либо шаг удалён из потока и код мёртв, либо поток до него "
                "больше не доходит.",
            }
        )

    order = {"broken": 0, "machine_specific": 1, "slow": 2, "gone": 3}
    out.sort(key=lambda p: (order.get(p["kind"], 9), p["step"]))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--local", action="store_true", help="only this machine's data")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--approve", metavar="STEP:STRATEGY", help="promote one pending variant")
    args = ap.parse_args()

    if args.approve:
        return approve(args.approve)

    show_pending()

    rows, source = telemetry_sync.fetch_rows(local=args.local)
    machines = sorted({r.get("machine", "") for r in rows if r.get("machine")})
    print(f"## Из телеметрии ({source}, {args.days} дн., машин: {len(machines)})")
    print()
    if len(machines) < 2:
        print("  Данные с одной машины: отличить общую поломку от локальной пока нельзя.")
        print()

    props = proposals(rows, args.days)
    if not props:
        print("  Предложений нет.")
        return 0
    for p in props:
        tag = {
            "broken": "СЛОМАН",
            "machine_specific": "МАШИНА",
            "slow": "МЕДЛЕННО",
            "gone": "ИСЧЕЗ",
        }[p["kind"]]
        print(f"  [{tag}] {p['step']}")
        print(f"      {p['why']}")
        if p["machines"]:
            print(f"      машины: {', '.join(p['machines'])}")
        print(f"      -> {p['action']}")
    print()
    print(f"Всего предложений: {len(props)}. Ничего не применено — это список для человека.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
