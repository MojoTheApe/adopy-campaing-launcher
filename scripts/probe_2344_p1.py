"""ACL-2344 P1: dummy hand-over between two light tabs of an ALREADY OPEN profile.

No request goes to Meta (the document is served by the browser, everything else is blocked).
Needs the owner's explicit go: pass the profile's CDP url and set ACL_2344_P1_CONFIRMED=doc120.
"""
import json
import os
import sys

from src.fb_api import light_probe

if __name__ == "__main__":
    if os.environ.get("ACL_2344_P1_CONFIRMED") != "doc120" or len(sys.argv) != 2:
        sys.exit("usage: ACL_2344_P1_CONFIRMED=doc120 python -m scripts.probe_2344_p1 http://127.0.0.1:<port>")
    print(json.dumps(light_probe.run_p1(sys.argv[1]), indent=1))
