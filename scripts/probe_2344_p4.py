"""ACL-2344 P4: memory of the profile's browser tree - capture tab alive vs closed, work tab 10 min.

Local measurements only (OS process memory + Performance.getMetrics). One Ads Manager load
(same as a Check plan), no Graph reads. Needs ACL_2344_P3_CONFIRMED=owner-go.
"""
import json
import os
import sys
import time

import psutil

from src.fb_api import light_probe
from src.fb_api.cdp import PageTab
from src.fb_api.token_capture import InPageGraph

PORT_FLAG = "--remote-debugging-port="


def tree(port: str) -> dict:
    mb, kinds = 0.0, {}
    for p in psutil.process_iter(["cmdline", "memory_full_info"]):
        try:
            cmd = p.info["cmdline"] or []
            if PORT_FLAG + port not in cmd:
                continue
            kind = next((a.split("=", 1)[1] for a in cmd if a.startswith("--type=")), "browser")
            mem = p.info["memory_full_info"]
            mb += (getattr(mem, "uss", None) or mem.rss) / 1048576
            kinds[kind] = kinds.get(kind, 0) + 1
        except (psutil.Error, TypeError):
            continue
    return {"tree_mb": round(mb), "procs": kinds}


def heap(tab) -> float | None:
    try:
        tab.call("Performance.enable")
        rows = tab.call("Performance.getMetrics").get("metrics") or []
        return next(round(r["value"] / 1048576, 1) for r in rows if r["name"] == "JSHeapUsedSize")
    except Exception:  # noqa: BLE001
        return None


if __name__ == "__main__":
    if os.environ.get("ACL_2344_P3_CONFIRMED") != "owner-go" or len(sys.argv) != 4:
        sys.exit("usage: ACL_2344_P3_CONFIRMED=owner-go python -m scripts.probe_2344_p4 <cdp_url> <port> <act_id>")
    url, port, act = sys.argv[1:]
    PageTab.close_restored_ads_manager_tabs = lambda self, *a, **k: None
    out = {"A_before": tree(port)}
    work = PageTab.open(url)
    capture = None
    try:
        assert light_probe.open_light_document(work)["ok"]
        capture = PageTab.open(url)
        ig_c, ig_w = InPageGraph(capture), InPageGraph(work)
        ig_c.navigate(act)
        st = ig_c.wait_ready(timeout=90.0)
        assert st.get("has_token") and st.get("kind") == "ads_manager", st.get("kind")
        time.sleep(5)
        out["B_capture_alive"] = {**tree(port), "capture_heap_mb": heap(capture), "work_heap_mb": heap(work)}
        import secrets, json as j
        n = [j.dumps(secrets.token_urlsafe(16)) for _ in range(3)]
        assert ig_w._eval_helper(f".listen({','.join(n)})") is True
        assert ig_c._eval_helper(f".handOver({','.join(n)},8000)", timeout=20) is True
        capture.close()
        capture = None
        time.sleep(20)
        out["C_capture_closed_20s"] = {**tree(port), "work_heap_mb": heap(work)}
        time.sleep(600)
        out["D_work_tab_10min"] = {**tree(port), "work_heap_mb": heap(work),
                                   "work_has_token": bool(ig_w.state().get("has_token"))}
    finally:
        for t in (capture, work):
            if t is not None:
                try:
                    t.close()
                except Exception:  # noqa: BLE001
                    pass
    out["E_after_close"] = tree(port)
    print(json.dumps(out, indent=1))
