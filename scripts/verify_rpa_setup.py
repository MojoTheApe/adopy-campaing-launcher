#!/usr/bin/env python3
"""Quick check: RPA stack installed and config files present."""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:
    from config.paths import load_env

    load_env()
except Exception:
    pass


def multilogin_a_check() -> tuple[bool, str]:
    """Подключён ли слот A: токен автоматизации или сохранённый вход (ACL-1335)."""
    import os

    from src.meta_campaign_agent.mlx_credentials import has_saved_session

    if (os.environ.get("MULTILOGIN_AUTOMATION_TOKEN_A") or "").strip():
        return True, "automation token set in env/.env"
    if has_saved_session("A"):
        return True, "signed in (renews automatically)"
    return False, "not connected — press Connect Multilogin in Accounts"


def main() -> int:
    ok = True

    def report(name: str, passed: bool, detail: str = "") -> None:
        nonlocal ok
        if not passed:
            ok = False
        mark = "OK" if passed else "FAIL"
        line = f"[{mark}] {name}"
        if detail:
            line += f" — {detail}"
        print(line)

    # Packages
    try:
        import playwright  # noqa: F401
        report("playwright package", True)
    except ImportError as e:
        report("playwright package", False, str(e))

    try:
        import yaml  # noqa: F401
        report("PyYAML", True)
    except ImportError as e:
        report("PyYAML", False, str(e))

    try:
        from playwright.sync_api import sync_playwright
        from src.fb_ads_rpa.config import ensure_playwright_browsers_path

        ensure_playwright_browsers_path()
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            b.close()
        report("playwright chromium", True)
    except Exception as e:
        report("playwright chromium", False, str(e))

    # Code imports
    try:
        from src.fb_ads_rpa.rpa_publisher import RpaPublisher
        from src.fb_ads_rpa.browser import MultiloginBrowserProvider
        from src.fb_ads_rpa.selectors import load_selectors

        n = len(load_selectors().get("selectors", {}))
        report("fb_ads_rpa imports", True, f"{n} selector groups")
    except Exception as e:
        report("fb_ads_rpa imports", False, str(e))

    # Config files
    from config.paths import get_ad_account_map_path, get_creatives_root, get_upload_fb_excel_path

    map_path = Path(get_ad_account_map_path())
    report("ad_account_map.csv", map_path.is_file(), str(map_path))

    upload_path = Path(get_upload_fb_excel_path())
    report("Upload FB.xlsx", upload_path.is_file(), str(upload_path))

    creatives = Path(get_creatives_root())
    report("creatives folder", creatives.is_dir(), str(creatives))

    if map_path.is_file():
        from src.meta_campaign_agent.ad_account_map import load_ad_account_map

        m = load_ad_account_map(map_path)
        report("accounts in map", len(m) > 0, f"{len(m)} account(s)")
        incomplete = [
            name
            for name, e in m.items()
            if not (e.profile_id and e.folder_id and e.ad_account_id)
        ]
        if incomplete:
            report(
                "multilogin profile mapping",
                False,
                f"incomplete: {', '.join(incomplete[:3])}"
                + ("…" if len(incomplete) > 3 else ""),
            )
        else:
            report("multilogin profile mapping", True, "folder_id + profile_id set")

    # Multilogin: токен автоматизации или сохранённый вход (только наличие)
    report("Multilogin A", *multilogin_a_check())

    # PoC CLI
    cli = _ROOT / "scripts" / "run_rpa_publish.py"
    report("run_rpa_publish.py", cli.is_file(), str(cli))

    print()
    if ok:
        print("All checks passed — ready for --preview-only and --dry-run.")
    else:
        print("Some checks failed — fix items marked FAIL before browser PoC.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
