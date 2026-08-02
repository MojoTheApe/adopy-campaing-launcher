"""Heal Browser add-ons on draft ads: set "None" as the LAST touch before publish.

Why this exists (proven live 2026-08-02, see Obsidian "RPA Publish — Дневник тестов"):
Meta pre-checks "Instant form (suggested)" in the ad editor UI on EVERY open,
and the editor autosaves that state — so merely opening an ad and leaving
corrupts it with `call_to_action[value][lead_gen_form_id]` (#100). Any review
pass (human or automated) breaks every ad it opens.

Therefore the only workable protocol is: review ads however you like, then run
this script LAST, then publish WITHOUT reopening the editors. The script:
  open editor -> keyboard-select "None" -> wait for autosave -> verify
  in-session (None checked, error banner gone) -> leave.
It deliberately does NOT re-open ads to verify: a verification visit would
corrupt them again.

Usage (profile must already be open in Multilogin with the debug port):
  python scripts/heal_browser_addons.py --ad-ids 1202...213,1202...214 \
      [--act 1566229345092982] [--business 1053747160411465] [--port 19222]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.sync_api import sync_playwright

from src.fb_ads_rpa.meta_dom import (
    _browser_addon_none_checked,
    _select_browser_addon_none,
    wait_editor_saved,
)

SCROLL = """
(frac) => {
  const c = [...document.querySelectorAll('*')].find((e) => {
    const st = getComputedStyle(e);
    return (st.overflowY === 'auto' || st.overflowY === 'scroll')
      && e.scrollHeight > e.clientHeight + 50 && e.scrollHeight > 1500;
  });
  if (c) c.scrollTop = Math.floor(c.scrollHeight * frac);
}
"""

ERR_VISIBLE = """
() => [...document.querySelectorAll('*')]
  .filter((e) => e.offsetParent !== null && e.children.length === 0)
  .some((e) => /lead_gen_form_id/.test(e.textContent || ''))
"""


def heal_ad(page, act: str, business: str, ad_id: str) -> tuple[bool, str]:
    edit = ("https://adsmanager.facebook.com/adsmanager/manage/ads/edit/standalone"
            f"?act={act}&business_id={business}&selected_ad_ids={ad_id}"
            "&nav_source=no_referrer&current_step=0")
    page.goto(edit, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(9000)

    state = None
    for frac in (0.4, 0.55, 0.7, 0.85, 0.3):
        page.evaluate(SCROLL, frac)
        page.wait_for_timeout(1100)
        state = _browser_addon_none_checked(page)
        if state is not None:
            break
    if state is None:
        return False, "browser-addons group not found"

    if not _select_browser_addon_none(page):
        return False, "could not select None"
    wait_editor_saved(page)
    page.wait_for_timeout(2000)

    if _browser_addon_none_checked(page) is not True:
        return False, "None did not stick after save"
    if page.evaluate(ERR_VISIBLE):
        return False, "lead_gen error still visible after save"
    return True, "ok"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ad-ids", required=True,
                    help="comma-separated ad ids to heal (order = heal order)")
    ap.add_argument("--act", default="1566229345092982")
    ap.add_argument("--business", default="1053747160411465")
    ap.add_argument("--port", type=int, default=19222)
    args = ap.parse_args()

    ad_ids = [x.strip() for x in args.ad_ids.split(",") if x.strip()]
    campaigns_url = ("https://adsmanager.facebook.com/adsmanager/manage/campaigns"
                     f"?act={args.act}&business_id={args.business}&nav_source=no_referrer")

    failures = []
    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp(f"http://127.0.0.1:{args.port}")
        ctx = b.contexts[0]
        page = ctx.pages[0]
        try:
            page.evaluate("1")
        except Exception:
            page = ctx.new_page()
        session = ctx.new_cdp_session(page)
        session.send("Emulation.setFocusEmulationEnabled", {"enabled": True})

        for ad_id in ad_ids:
            ok, why = heal_ad(page, args.act, args.business, ad_id)
            print(f"{'OK ' if ok else 'FAIL'} {ad_id}: {why}")
            if not ok:
                failures.append(ad_id)

        page.goto(campaigns_url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(2500)
        b.close()

    if failures:
        print(f"\n{len(failures)} ad(s) NOT healed: {','.join(failures)}")
        return 1
    print(f"\nAll {len(ad_ids)} ad(s) healed. Publish WITHOUT reopening the ad editors —"
          " every editor open re-corrupts the ad.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
