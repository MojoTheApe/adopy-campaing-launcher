"""ACL-2344 P5: compare the request headers of ONE identical Graph read made from an Ads Manager tab
and from the light work tab. Two reads in total. Headers are captured locally (CDP Network events);
cookie/authorization values, query strings and IP addresses are never printed.
Needs ACL_2344_P3_CONFIRMED=owner-go.
"""
import json
import os
import secrets
import sys
import urllib.parse as up

from src.fb_api import light_probe
from src.fb_api.cdp import PageTab
from src.fb_api.token_capture import InPageGraph

SAFE = ("referer", "origin", "sec-fetch-site", "sec-fetch-mode", "sec-fetch-dest", "user-agent",
        "accept", "accept-language", "accept-encoding", "cache-control", "pragma", "sec-ch-ua",
        "sec-ch-ua-mobile", "sec-ch-ua-platform", "priority")


def _no_query(u: str) -> str:
    p = up.urlsplit(u or "")
    return f"{p.scheme}://{p.netloc}{p.path}" + (" +query" if p.query else "")


def watched(tab):
    """Route every event seen during any call of this tab into a list."""
    events: list = []
    orig = tab.call

    def call(method, params=None, *, timeout=10.0, on_event=None):
        return orig(method, params, timeout=timeout, on_event=on_event or events.append)

    tab.call = call
    return events


def summarise(events: list) -> dict:
    ids = {}
    for e in events:
        m, p = e.get("method"), e.get("params") or {}
        if m == "Network.requestWillBeSent" and "graph.facebook.com" in (p.get("request") or {}).get("url", "") \
                and "/me/adaccounts" in p["request"]["url"]:
            ids[p["requestId"]] = {"url": _no_query(p["request"]["url"]), "method": p["request"].get("method"),
                                   "initiator": (p.get("initiator") or {}).get("type"),
                                   "type": p.get("type"),
                                   "referrerPolicy": p["request"].get("referrerPolicy"),
                                   "will_headers": {k.lower(): v for k, v in (p["request"].get("headers") or {}).items()
                                                    if k.lower() in SAFE}}
    for e in events:
        m, p = e.get("method"), e.get("params") or {}
        rid = p.get("requestId")
        if rid not in ids:
            continue
        if m == "Network.requestWillBeSentExtraInfo":
            h = {k.lower(): v for k, v in (p.get("headers") or {}).items()}
            ids[rid]["wire_headers"] = {k: (_no_query(v) if k == "referer" else v) for k, v in h.items() if k in SAFE}
            ids[rid]["wire_header_names"] = sorted(h)
            ids[rid]["cookie_present"] = "cookie" in h
            ids[rid]["cookie_names"] = len([c for c in h.get("cookie", "").split(";") if c.strip()])
        elif m == "Network.responseReceived":
            r = p.get("response") or {}
            ids[rid]["status"] = r.get("status")
            ids[rid]["protocol"] = r.get("protocol")
            ids[rid]["_ip"] = r.get("remoteIPAddress")
        elif m == "Network.responseReceivedExtraInfo":
            ids[rid]["resp_status"] = p.get("statusCode")
    for v in ids.values():
        if v.get("will_headers", {}).get("referer"):
            v["will_headers"]["referer"] = _no_query(v["will_headers"]["referer"])
    return ids


def read(tab, ig, events):
    del events[:]
    tab.call("Network.enable")
    try:
        got = ig.request("GET", "/me/adaccounts", {"fields": "id", "limit": "1"}, timeout=15)
        tab.call("Runtime.evaluate", {"expression": "new Promise(r=>setTimeout(r,1500))", "awaitPromise": True},
                 timeout=10)
    finally:
        try:
            tab.call("Network.disable")
        except Exception:  # noqa: BLE001
            pass
    return got.get("status"), summarise(events)


if __name__ == "__main__":
    if os.environ.get("ACL_2344_P3_CONFIRMED") != "owner-go" or len(sys.argv) != 3:
        sys.exit("usage: ACL_2344_P3_CONFIRMED=owner-go python -m scripts.probe_2344_p5 <cdp_url> <act_id>")
    url, act = sys.argv[1:]
    PageTab.close_restored_ads_manager_tabs = lambda self, *a, **k: None
    work = PageTab.open(url)
    capture = None
    out = {}
    try:
        assert light_probe.open_light_document(work)["ok"]
        capture = PageTab.open(url)
        ig_c, ig_w = InPageGraph(capture), InPageGraph(work)
        ev_c, ev_w = watched(capture), watched(work)
        ig_c.navigate(act)
        st = ig_c.wait_ready(timeout=90.0)
        assert st.get("has_token") and st.get("kind") == "ads_manager"
        status_c, rows_c = read(capture, ig_c, ev_c)          # read 1: Ads Manager tab (today's mode)
        out["ads_manager_tab"] = {"status": status_c, "requests": len(rows_c)}
        if status_c != 200:
            out["stopped"] = "ads_manager_read"
        else:
            n = [json.dumps(secrets.token_urlsafe(16)) for _ in range(3)]
            assert ig_w._eval_helper(f".listen({','.join(n)})") is True
            assert ig_c._eval_helper(f".handOver({','.join(n)},8000)", timeout=20) is True
            capture.close()
            capture = None
            status_w, rows_w = read(work, ig_w, ev_w)         # read 2: light work tab
            out["work_tab"] = {"status": status_w, "requests": len(rows_w)}
            a = next(iter(rows_c.values()), {})
            b = next(iter(rows_w.values()), {})
            out["ads_manager_request"] = {k: v for k, v in a.items() if k != "_ip"}
            out["work_request"] = {k: v for k, v in b.items() if k != "_ip"}
            out["same_remote_ip"] = (a.get("_ip") == b.get("_ip")) if a.get("_ip") and b.get("_ip") else None
            ha, hb = a.get("wire_headers") or a.get("will_headers") or {}, b.get("wire_headers") or b.get("will_headers") or {}
            out["header_diffs"] = {k: [ha.get(k), hb.get(k)] for k in sorted(set(ha) | set(hb)) if ha.get(k) != hb.get(k)}
            out["header_name_diffs"] = sorted(set(a.get("wire_header_names", [])) ^ set(b.get("wire_header_names", [])))
    finally:
        for t in (capture, work):
            if t is not None:
                try:
                    t.close()
                except Exception:  # noqa: BLE001
                    pass
    print(json.dumps(out, indent=1))
