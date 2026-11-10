"""ACL-2344 P1: dummy hand-over between two light tabs of an ALREADY OPEN profile.

No request goes to Meta (the document is served by the browser, everything else is blocked).
Needs the owner's explicit go: pass the profile's CDP url and set ACL_2344_P1_CONFIRMED=owner-go.
Other tabs of the profile are left alone (no restored-Ads-Manager clean-up).
"""
import json
import os
import sys

from src.fb_api import light_probe
from src.fb_api.cdp import PageTab

if __name__ == "__main__":
    if os.environ.get("ACL_2344_P1_CONFIRMED") != "owner-go" or len(sys.argv) not in (2, 3):
        sys.exit("usage: ACL_2344_P1_CONFIRMED=owner-go python -m scripts.probe_2344_p1 http://127.0.0.1:<port>")
    PageTab.close_restored_ads_manager_tabs = lambda self, *a, **k: None
    if len(sys.argv) == 3 and os.environ.get("ACL_2344_P3_CONFIRMED") == "owner-go":
        print(json.dumps(light_probe.run_p3(sys.argv[1], sys.argv[2]), indent=1))     # P3: two real Graph reads
    else:
        print(json.dumps(light_probe.run_p1(sys.argv[1]), indent=1))
