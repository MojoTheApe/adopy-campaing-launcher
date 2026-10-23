"""Does this ACL install actually start? A few seconds, no side effects.

Run right after an update, before the new code is declared working. Guards
against the failure the dependency check alone cannot catch: everything
installed cleanly and the app still does not come up — a module renamed, a
package that resolved to a version this code does not work with, a checkout
that arrived half-written.

Deliberately imports and nothing more. No browser, no Multilogin, no Google,
no Ads Manager: a start-up check that touched a live account would be a worse
problem than the one it detects.

Exit code 0 = starts. 1 = does not, with the first failure named.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Third-party first: when a dependency refresh half-happened, this is where it
# shows, and the message should say "pandas is missing" rather than fail deep
# inside our own import chain.
_PACKAGES = [
    "fastapi",
    "uvicorn",
    "jinja2",
    "multipart",
    "pandas",
    "openpyxl",
    "dotenv",
    "playwright",
    "psutil",
    "requests",
]

# Then the app itself: the dashboard's request path and the RPA entry point.
# Importing these executes every module they pull in, which is what makes a
# renamed or half-written file show up here instead of in front of a buyer.
_MODULES = [
    "config.paths",
    "src.web.app",
    "src.analysis_agent.morning_report",
    "src.fb_ads_rpa.ads_manager_flow",
    "src.fb_ads_rpa.retry_policy",
    "scripts.acl_runtime_manager",
]


def main() -> int:
    failures: list[str] = []

    for name in _PACKAGES:
        try:
            importlib.import_module(name)
        except Exception as exc:
            failures.append(f"dependency {name}: {exc}")

    if failures:
        # No point importing our own code against a broken environment: it
        # would produce a second, more confusing error about the same cause.
        print("ACL smoke check FAILED - dependencies are not usable:")
        for f in failures:
            print(f"  {f}")
        return 1

    for name in _MODULES:
        try:
            importlib.import_module(name)
        except Exception as exc:
            failures.append(f"{name}: {type(exc).__name__}: {exc}")

    # ACL-2122: the Meta rules registry ships as data files; a build without it
    # would publish with no Meta rules protection, so it counts as "does not start".
    try:
        from src.campaign_bridge import meta_rules

        meta_rules.load_rules()
    except Exception as exc:
        failures.append(f"Meta rules registry: {type(exc).__name__}: {exc}")

    if failures:
        print("ACL smoke check FAILED - this version does not start:")
        for f in failures:
            print(f"  {f}")
        return 1

    print("ACL smoke check OK - dashboard and RPA both import.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
