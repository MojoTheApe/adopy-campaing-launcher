"""Watch one ad account for restriction, screenshotting each check.

Built for the ban post-mortems: we keep losing accounts shortly after a
publish and can only guess at what stage it happened. This polls the
account's own campaigns page on a fixed cadence, saves a timestamped
screenshot every time, and prints a line ONLY when something changes —
so a quiet account stays quiet in the log and the moment it flips is
unmistakable.

Deliberately read-only. It never opens an ad editor: opening one makes
Meta re-select "Instant form (suggested)" and autosave it, which corrupts
the ad (proven 2026-08-02). Navigating the campaigns list does not.

Do NOT run this while an RPA publish is in flight — both drive the same
browser tab and would fight over navigation. Start it after the run.

Usage:
  python scripts/watch_account.py --act 1374118070911270 --label FacebookElf303
  python scripts/watch_account.py --act ... --interval 300 --hours 12
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.sync_api import sync_playwright

OUT_DIR = Path(__file__).resolve().parent.parent / "output" / "account_watch"

# Phrases Meta shows when an account is restricted / disabled, in the
# languages these profiles actually render in.
BAD_MARKERS = [
    "ad account has been disabled",
    "ad account is disabled",
    "account has been restricted",
    "account is restricted",
    "your account is disabled",
    "no longer run ads",
    "cuenta ha sido inhabilitada",
    "cuenta inhabilitada",
    "cuenta restringida",
    "restricted from advertising",
    "advertising access",
    "you can't run ads",
]
REVIEW_MARKERS = [
    "in review",
    "being reviewed",
    "en revisión",
    "pending review",
]
REJECT_MARKERS = [
    "rejected",
    "rechazado",
    "not approved",
    "no aprobado",
]

PROBE = """
() => {
  const norm = (x) => (x || '').replace(/\\s+/g, ' ').trim();
  const vis = [...document.querySelectorAll('*')]
    .filter(e => e.offsetParent !== null && e.children.length === 0)
    .map(e => norm(e.textContent)).filter(Boolean);
  const blob = vis.join(' | ').toLowerCase();
  return { blob: blob.slice(0, 20000), sample: [...new Set(vis)].slice(0, 60) };
}
"""


def classify(blob: str) -> tuple[str, str]:
    """(state, matched phrase) — worst finding wins."""
    for m in BAD_MARKERS:
        if m in blob:
            return "RESTRICTED", m
    for m in REJECT_MARKERS:
        if m in blob:
            return "REJECTED", m
    for m in REVIEW_MARKERS:
        if m in blob:
            return "IN_REVIEW", m
    return "OK", ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--act", required=True, help="ad account id, digits only")
    ap.add_argument("--label", default="account", help="name used in file names")
    ap.add_argument("--business", default="1053747160411465")
    ap.add_argument("--port", type=int, default=19222)
    ap.add_argument("--interval", type=int, default=300, help="seconds between checks")
    ap.add_argument("--hours", type=float, default=12.0, help="stop after this long")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    log_path = OUT_DIR / f"{args.label}_watch.jsonl"
    url = (f"https://adsmanager.facebook.com/adsmanager/manage/campaigns"
           f"?act={args.act}&business_id={args.business}&nav_source=no_referrer")

    deadline = time.time() + args.hours * 3600
    last_state = None
    checks = 0

    print(f"WATCH START {args.label} act={args.act} "
          f"every {args.interval}s for {args.hours}h", flush=True)

    with sync_playwright() as p:
        while time.time() < deadline:
            checks += 1
            stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
            state, matched, err = "UNKNOWN", "", ""
            try:
                b = p.chromium.connect_over_cdp(f"http://127.0.0.1:{args.port}", timeout=60000)
                ctx = b.contexts[0]
                page = ctx.pages[0]
                try:
                    page.evaluate("1")
                except Exception:
                    page = ctx.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(9000)
                shot = OUT_DIR / f"{args.label}_{stamp}_{{}}.png"
                probe = page.evaluate(PROBE)
                state, matched = classify(probe["blob"])
                page.screenshot(path=str(shot).format(state))
                b.close()
            except Exception as e:
                err = str(e)[:160]
                state = "PROBE_FAILED"

            entry = {"ts": dt.datetime.now().isoformat(timespec="seconds"),
                     "check": checks, "state": state, "matched": matched, "error": err}
            with log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

            # Print only on change — a quiet account should stay quiet.
            if state != last_state:
                print(f"[{entry['ts']}] {args.label}: {last_state or 'start'} -> {state}"
                      + (f"  ({matched})" if matched else "")
                      + (f"  {err}" if err else ""), flush=True)
                last_state = state

            time.sleep(args.interval)

    print(f"WATCH END {args.label} after {checks} checks, last state {last_state}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
