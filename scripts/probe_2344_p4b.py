"""ACL-2344 P4b: why did the work tab lose its token? Local samples every 2 min, no Meta reads.

One Ads Manager load for the hand-over, then the capture tab is closed and the work tab is only
observed: URL (browser-side), isolated-world context alive?, helper token present?, document
time origin (changes on reload/navigation), heap. Needs ACL_2344_P3_CONFIRMED=owner-go.
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
        sys.exit("usage: ACL_2344_P3_CONFIRMED=owner-go python -m scripts.probe_2344_p4b <cdp_url> <act_id>")
    url, act = sys.argv[1:]
    PageTab.close_restored_ads_manager_tabs = lambda self, *a, **k: None
    work = PageTab.open(url)
    capture = None
    samples = []
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
        ctx, name = ig_w._ctx, ig_w.name
        capture.close()
        capture = None
        for i in range(7):
            row = {"t_min": i * 2}
            try:
                info = work.call("Target.getTargetInfo", {"targetId": work.target_id}).get("targetInfo") or {}
                row["url_ok"] = info.get("url") == light_probe.LIGHT_URL
                row["title"] = info.get("title")
                row["attached"] = info.get("attached")
            except Exception as exc:  # noqa: BLE001
                row["target_err"] = type(exc).__name__
            try:
                row["world"] = work.evaluate_in(
                    ctx, f"({{tok: !!globalThis[{json.dumps(name)}] && globalThis[{json.dumps(name)}].state().has_token,"
                         f" origin: Math.round(performance.timeOrigin), vis: document.visibilityState}})", timeout=10)
            except Exception as exc:  # noqa: BLE001
                row["world_err"] = type(exc).__name__ + ": " + str(exc)[:80]
            samples.append(row)
            if i < 6:
                time.sleep(120)
    finally:
        for t in (capture, work):
            if t is not None:
                try:
                    t.close()
                except Exception:  # noqa: BLE001
                    pass
    print(json.dumps(samples, indent=1))
