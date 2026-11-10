"""ACL-2344 P3-late: token validity of the light work tab at +60 min and +6 h.

One Ads Manager load for the hand-over, then the work tab is left alone. Exactly two single reads
(+60 min, +6 h), no polling, no retry; any non-200 or login/checkpoint stops the run. Local state
checks only between reads. Needs ACL_2344_P3_CONFIRMED=owner-go.
"""
import json
import os
import secrets
import sys
import time

from src.fb_api import light_probe
from src.fb_api.cdp import PageTab
from src.fb_api.token_capture import InPageGraph

if __name__ == "__main__":
    if os.environ.get("ACL_2344_P3_CONFIRMED") != "owner-go" or len(sys.argv) != 3:
        sys.exit("usage: ACL_2344_P3_CONFIRMED=owner-go python -m scripts.probe_2344_p6 <cdp_url> <act_id>")
    url, act = sys.argv[1:]
    PageTab.close_restored_ads_manager_tabs = lambda self, *a, **k: None
    out = {"started": time.strftime("%H:%M")}

    def save():
        print(json.dumps(out, indent=1), flush=True)

    work = PageTab.open(url)
    capture = None
    try:
        assert light_probe.open_light_document(work)["ok"]
        capture = PageTab.open(url)
        ig_c, ig_w = InPageGraph(capture), InPageGraph(work)
        ig_c.navigate(act)
        st = ig_c.wait_ready(timeout=90.0)
        assert st.get("has_token") and st.get("kind") == "ads_manager"
        n = [json.dumps(secrets.token_urlsafe(16)) for _ in range(3)]
        assert ig_w._eval_helper(f".listen({','.join(n)})") is True
        assert ig_c._eval_helper(f".handOver({','.join(n)},8000)", timeout=20) is True
        capture.close()
        capture = None
        out["handover_done"] = time.strftime("%H:%M")
        save()
        t0 = time.monotonic()
        for label, at in (("plus_60min", 3600), ("plus_6h", 21600)):
            time.sleep(max(0, at - (time.monotonic() - t0)))
            row = {"at": time.strftime("%H:%M")}
            try:
                info = work.call("Target.getTargetInfo", {"targetId": work.target_id}).get("targetInfo") or {}
                row["tab_url_ok"] = info.get("url") == light_probe.LIGHT_URL
                row["has_token"] = bool(ig_w.state().get("has_token"))
            except Exception as exc:  # noqa: BLE001
                row["tab_error"] = type(exc).__name__
            if row.get("has_token"):
                r = ig_w.request("GET", "/me/adaccounts", {"fields": "id", "limit": "1"}, timeout=15)
                row["read"] = light_probe._brief(r)
            out[label] = row
            save()
            if row.get("read", {}).get("status") != 200:
                out["stopped_at"] = label
                break
    except Exception as exc:  # noqa: BLE001
        out["stopped_at"] = type(exc).__name__
    finally:
        for t in (capture, work):
            if t is not None:
                try:
                    t.close()
                except Exception:  # noqa: BLE001
                    pass
    out["finished"] = time.strftime("%H:%M")
    save()
