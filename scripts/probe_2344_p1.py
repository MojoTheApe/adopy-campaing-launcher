"""ACL-2344 P1: dummy hand-over between two light tabs of an ALREADY OPEN profile.

No request goes to Meta (the document is served by the browser, every other request of the tab is
failed). Needs the owner's explicit go: set ACL_2344_P1_CONFIRMED=doc120, pass the profile's loopback
CDP url and --profile-id equal to the Doc120 profile id from the machine's account map. Anything else
is refused (the same gate sits inside light_probe.run_p1).
"""
import json
import os
import sys

from src.fb_api import light_probe

USAGE = ("usage: ACL_2344_P1_CONFIRMED=doc120 python -m scripts.probe_2344_p1 "
         "http://127.0.0.1:<port> --profile-id <Doc120 profile id>")

if __name__ == "__main__":
    args = sys.argv[1:]
    if os.environ.get("ACL_2344_P1_CONFIRMED") != "doc120" or len(args) != 3 or args[1] != "--profile-id":
        sys.exit(USAGE)
    try:
        print(json.dumps(light_probe.run_p1(args[0], args[2]), indent=1))
    except light_probe.ProbeGateError as exc:
        sys.exit("refused: " + str(exc))
