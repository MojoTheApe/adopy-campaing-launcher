"""ACL-2344 P5 (redone): Graph request headers, Ads Manager tab vs light tab - no Graph call, no token, nothing sent.

One Ads Manager load in a capture tab (as a Check plan does); the light tab sends nothing to Meta. Both tabs run
the helper's fetch to a sentinel path that the browser fails before the network. Only header names and the
scrubbed Referer/Origin/Sec-Fetch values are printed. Needs the owner's go for this run:
ACL_2344_P5_CONFIRMED=owner-go python -m scripts.probe_2344_p5 http://127.0.0.1:<port> <act_id> <profile_id>
(profile_id of a pilot account, see `light_probe.PILOT_ACCOUNTS`). Other tabs of the profile are left alone.
"""
import json
import os
import sys

from src.fb_ads_rpa import light_probe
from src.fb_api.cdp import PageTab

if __name__ == "__main__":
    if os.environ.get("ACL_2344_P5_CONFIRMED") != "owner-go" or len(sys.argv) != 4:
        sys.exit(__doc__)
    PageTab.close_restored_ads_manager_tabs = lambda self, *a, **k: 0
    print(json.dumps(light_probe.run_p5(sys.argv[1], sys.argv[2], sys.argv[3]), indent=1))
