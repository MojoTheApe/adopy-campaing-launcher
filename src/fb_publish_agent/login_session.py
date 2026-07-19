"""Bootstrap the saved browser session for Agent 10.

Opens a real, visible Chromium window and waits for you to log into Facebook
by hand (including any 2FA / checkpoint) — the agent never handles credentials
or verification codes itself (docs/agent10_fb_ads_playwright_upload.md §1.3, §4.2).
Once you confirm in the terminal, the session (cookies/local storage) is saved
to disk so future runs reuse it without logging in again.

Run this again whenever Facebook forces a re-login or the saved session expires.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_proj_root = Path(__file__).resolve().parents[2]
if str(_proj_root) not in sys.path:
    sys.path.insert(0, str(_proj_root))

from config.paths import get_fb_ads_auth_state_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Agent 10: save a logged-in Facebook browser session for reuse")
    parser.add_argument("-o", "--auth-state", default=None, help="Where to save the session (default: FB_ADS_AUTH_STATE / config)")
    args = parser.parse_args()

    auth_state_path = (args.auth_state or "").strip() or get_fb_ads_auth_state_path()
    auth_state_path = str(_proj_root / auth_state_path) if not os.path.isabs(auth_state_path) else auth_state_path
    os.makedirs(os.path.dirname(auth_state_path) or ".", exist_ok=True)

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto("https://www.facebook.com/login")

        print()
        print("A browser window opened. Log into Facebook by hand (email/password, 2FA, checkpoint —")
        print("whatever it asks). Once you can see your Facebook home/Ads Manager, come back here")
        print("and press Enter to save the session.")
        input("Press Enter when logged in... ")

        context.storage_state(path=auth_state_path)
        browser.close()

    print(f"Session saved: {auth_state_path}")
    print("Runs of run_publish_to_facebook.py will now reuse this session (no repeated logins).")


if __name__ == "__main__":
    main()
