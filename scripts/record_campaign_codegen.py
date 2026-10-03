"""Record a Facebook Ads campaign flow with Playwright's Inspector recorder,
running INSIDE the real Multilogin (MLX) browser.

The Playwright `codegen` CLI cannot attach to an existing browser over CDP, so it
would launch a fresh Chromium with none of your MLX fingerprint or FB login. This
helper instead reuses the RPA's MLX launcher to open the profile *with automation*
(a visible window with a CDP port), connects Playwright over CDP, navigates to
Ads Manager, and calls `page.pause()` — which opens the Playwright Inspector with
a **Record** button. Click Record, walk through creating a campaign, and copy the
generated Python from the Inspector.

On exit the browser window is LEFT OPEN (hand-off) — nothing is closed.

Usage (from repo root):
    python scripts/record_campaign_codegen.py --label "FacebookElf191 SA"
    python scripts/record_campaign_codegen.py --label "FacebookElf191 SA" --url https://adsmanager.facebook.com/adsmanager/manage/campaigns
    python scripts/record_campaign_codegen.py --label "FacebookElf191 SA" --target python-async
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Ensure repo root is importable when run as a script.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from config.paths import get_ad_account_map_path
from src.fb_ads_rpa.browser import MultiloginAttachOnlyProvider
from src.meta_campaign_agent.ad_account_map import (
    load_ad_account_map,
    resolve_account_entry,
)


def _campaigns_url(entry) -> str:
    act = (entry.ad_account_id or "").replace("act_", "")
    if act:
        return (
            "https://adsmanager.facebook.com/adsmanager/manage/campaigns"
            f"?act={act}"
        )
    return "https://adsmanager.facebook.com/adsmanager/manage/campaigns"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--label",
        required=True,
        help="Account name exactly as in config/ad_account_map.csv "
        '(e.g. "FacebookElf191 SA")',
    )
    parser.add_argument(
        "--url",
        default=None,
        help="Start URL. Defaults to the account's Ads Manager campaigns page.",
    )
    parser.add_argument(
        "--target",
        default="python",
        help="Generated language shown in the Inspector "
        "(python, python-async, python-pytest, javascript, ...). "
        "You can also switch it in the Inspector dropdown.",
    )
    args = parser.parse_args()

    account_map = load_ad_account_map(get_ad_account_map_path())
    entry = resolve_account_entry(account_map, args.label)
    if entry is None:
        print(
            f"[ERROR] Account {args.label!r} not found in "
            f"{get_ad_account_map_path()}",
            file=sys.stderr,
        )
        return 2
    if not entry.profile_id:
        print(
            f"[ERROR] Account {args.label!r} has no profile_id in the account map.",
            file=sys.stderr,
        )
        return 2

    # Codegen recorder language for page.pause() Inspector.
    import os

    os.environ.setdefault("PW_LANG", args.target)

    provider = MultiloginAttachOnlyProvider()
    print(f"[record] Opening MLX profile for {args.label!r} with automation…")
    # ACL-1388 (ревью PR #396, F3): запись разработчика окно, открытое руками,
    # не закрывает — только публикация.
    conn = provider.open(entry, args.label, allow_kill=False)
    page = conn.page

    start_url = args.url or _campaigns_url(entry)
    print(f"[record] Navigating to: {start_url}")
    try:
        page.goto(start_url, wait_until="domcontentloaded", timeout=60_000)
    except Exception as exc:  # noqa: BLE001
        print(f"[record] Navigation warning (continuing): {exc}")

    print(
        "\n"
        "==================================================================\n"
        " Playwright Inspector is opening.\n"
        "   1. Click the 'Record' button (top-left of the Inspector).\n"
        "   2. Walk through creating the campaign in the browser window.\n"
        "   3. Copy the generated code from the Inspector.\n"
        "   4. Close the Inspector / press Resume when done.\n"
        " The browser window will be LEFT OPEN afterwards.\n"
        "==================================================================\n"
    )

    # Opens the Playwright Inspector with the recorder against the live page.
    page.pause()

    # Hand-off: disconnect without closing the MLX window.
    try:
        conn.close(keep_browser_open=True)
    except Exception:
        pass
    print("[record] Done. Browser window left open.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
