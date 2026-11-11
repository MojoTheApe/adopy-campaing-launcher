"""ACL-2344: P1 and P3 again, on the final light-tab code (after review).

p1 - the light document of `src/fb_api/light_tab.py`: an origin fetch and a reload are answered locally,
     the reload is noticed; nothing is sent to Meta.
p3 - the production lease path once: ONE Ads Manager load in a capture tab, hand-over to the light tab,
     capture tab closed, then `/me/adaccounts` and one account read from the light tab (no repeats).
Only codes, counts and HTTP statuses are printed. Needs the owner's go for this run:
ACL_2344_FINAL_CONFIRMED=owner-go python -m scripts.probe_2344_final p1 http://127.0.0.1:<port> <profile_id>
ACL_2344_FINAL_CONFIRMED=owner-go python -m scripts.probe_2344_final p3 http://127.0.0.1:<port> <profile_id> <act_id>
(profile_id of a pilot account, see `light_probe.PILOT_ACCOUNTS`). Other tabs of the profile are left alone.
"""
import json
import os
import sys

from src.fb_ads_rpa import light_probe
from src.fb_api.cdp import PageTab
from src.fb_api.graph import GraphStop

if __name__ == "__main__":
    args = sys.argv[1:]
    ok = (os.environ.get("ACL_2344_FINAL_CONFIRMED") == "owner-go"
          and ((len(args) == 3 and args[0] == "p1") or (len(args) == 4 and args[0] == "p3")))
    if not ok:
        sys.exit(__doc__)
    PageTab.close_restored_ads_manager_tabs = lambda self, *a, **k: 0
    try:
        if args[0] == "p1":
            got = light_probe.run_final_p1(args[1], args[2])
        else:
            got = light_probe.run_final_p3(args[1], args[3], args[2])
    except light_probe.ProbeGateError as exc:
        sys.exit("refused: " + str(exc))
    except GraphStop as exc:              # login/checkpoint/identity: stop, no retry; only the class name
        got = {"stopped_at": type(exc).__name__}
    print(json.dumps(got, indent=1))
