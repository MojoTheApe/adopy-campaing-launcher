"""ACL-2338: an in-page write that hits a CDP timeout is reconciled like a network timeout.

Fakes only: no Meta, no Multilogin, no real waits.
"""
import pytest

from src.fb_ads_rpa import run_telemetry as rt
from src.fb_api import graph as g
from tests.test_acl1761_api_create import ACT, FakeMeta, _draft, _run

CDP = {"error": "call_unknown", "reason": "cdp_timeout"}


class CdpMeta(FakeMeta):
    """`cdp_*`: the in-page call never returned (tab frozen). `page_*`: the page aborted its own POST at the
    deadline and answered network_timeout. `*_lost`: the ad was created; `*_unsent`: it was not."""

    REPLIES = {"cdp": dict(CDP), "page": {"error": "network_timeout"}}

    def write(self, path, params, files=None, filename="", **kw):
        edge = path.rsplit("/", 1)[1]
        mode = self.fail_next.get(edge)
        if mode in ("cdp_lost", "cdp_unsent", "page_lost", "page_unsent"):
            who, what = mode.split("_")
            self.fail_next[edge] = what
            super().write(path, params, files=files, filename=filename, **kw)
            return dict(self.REPLIES[who])
        return super().write(path, params, files=files, filename=filename, **kw)


def _edges(meta, edge):
    return [p for p, _ in meta.posts if p.endswith("/" + edge)]


def test_page_timeout_with_both_readbacks_empty_resends_exactly_once(tmp_path):
    meta = CdpMeta()
    meta.fail_next["ads"] = "page_unsent"
    out, _ = _run(meta, _draft(tmp_path, ads=1), reconcile_wait_s=0)
    assert not out.error, out.error
    assert len(_edges(meta, "ads")) == 2
    assert len([o for o in meta.objects.values() if o["_kind"] == "ad"]) == 1


@pytest.mark.parametrize("mode", ["cdp_lost", "page_lost"])
def test_timeout_where_the_ad_exists_is_not_resent(tmp_path, mode):
    meta = CdpMeta()
    meta.fail_next["ads"] = mode
    out, _ = _run(meta, _draft(tmp_path, ads=1), reconcile_wait_s=0)
    assert not out.error, out.error
    assert len(_edges(meta, "ads")) == 1


def test_ad_appearing_only_for_the_second_readback_is_not_resent(tmp_path):
    meta = CdpMeta()
    meta.fail_next["ads"] = "page_lost"
    hidden = {"n": 0}
    real_get = meta.request

    def request(method, path, params=None, **kw):
        reply = real_get(method, path, params, **kw)
        if path.endswith("/ads") and hidden["n"] < 1:
            hidden["n"] += 1          # the first readback of the ad list sees nothing yet
            return {"status": 200, "body": {"data": []}}
        return reply

    meta.request = request
    out, _ = _run(meta, _draft(tmp_path, ads=1), reconcile_wait_s=0)
    assert not out.error, out.error
    assert len(_edges(meta, "ads")) == 1


@pytest.mark.parametrize("video", [False, True])
def test_upload_with_a_cdp_timeout_is_never_resent(tmp_path, video):
    meta = CdpMeta()
    edge = "advideos" if video else "adimages"
    meta.fail_next[edge] = "cdp_unsent"
    out, _ = _run(meta, _draft(tmp_path, ads=1, video=video), reconcile_wait_s=0)
    assert out.error and out.stop_kind == "unknown"
    assert len(_edges(meta, edge)) == 1


@pytest.mark.parametrize("reason", ["context_stale", "page_closed"])
def test_stale_context_and_closed_page_are_never_resent(tmp_path, reason):
    class Gone(FakeMeta):
        def write(self, path, params, files=None, filename="", **kw):
            if path.endswith("/ads"):
                self.ad_posts = getattr(self, "ad_posts", 0) + 1
                return {"error": "call_unknown", "reason": reason}      # nothing was created
            return super().write(path, params, files=files, filename=filename, **kw)

    meta = Gone()
    out, _ = _run(meta, _draft(tmp_path, ads=1), reconcile_wait_s=0)
    assert out.error and out.stop_kind == "unknown"
    assert meta.ad_posts == 1
    from src.fb_api import creator as cr
    assert not {"context_stale", "page_closed"} & set(cr.RESEND_REASONS)


def test_a_failing_readback_leaves_a_stop_row(tmp_path, monkeypatch):
    rows = []
    monkeypatch.setattr(rt, "record", lambda step, ok=None, **kw: rows.append((step, kw.get("diag") or {})))
    meta = CdpMeta()
    meta.fail_next["ads"] = "cdp_unsent"
    real = meta.request

    def request(method, path, params=None, **kw):
        if path.endswith("/ads") and _edges(meta, "ads"):      # only after the write was sent
            raise g.PageFailed("tab gone")
        return real(method, path, params, **kw)

    meta.request = request
    out, _ = _run(meta, _draft(tmp_path, ads=1), reconcile_wait_s=0)
    assert out.error
    row = [d for s, d in rows if s == "api_create_step" and d.get("reason") == "cdp_timeout"]
    assert row and row[0]["outcome"].startswith("stop_readback_failed")


def test_no_file_post_is_aborted_in_the_page_before_the_cdp_wait_ends():
    import json
    from tests.test_acl1761_api_create import StubTab, wp
    from src.fb_api import token_capture as tc

    tab = StubTab()
    t = tc.InPageGraph(tab, allow_writes=True, write_act=ACT)
    t.write(f"/{ACT}/campaigns", {"name": "c", "objective": "OUTCOME_LEADS", "status": "PAUSED",
                                  "special_ad_categories": "[]", "buying_type": "AUCTION"}, timeout=120.0)
    spec = json.loads(tab.calls()[0].split(".call(", 1)[1].rsplit(")", 1)[0])
    assert 0 < spec["post_timeout_ms"] < 120_000      # the page gives up (and aborts) first


@pytest.mark.parametrize("body_hangs", [False, True])
def test_first_post_that_lands_late_is_aborted_in_the_page_before_any_readback(body_hangs):
    """The page helper itself: a hanging POST fetch is aborted at its deadline and answers network_timeout."""
    import json, shutil, subprocess, tempfile
    from pathlib import Path
    from src.fb_api import token_capture as tc, write_policy as wp
    from tests.test_acl1761_api_create import TOKEN

    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    script = r"""
const calls = [];
const fakeFetch = (url, init) => { calls.push({ aborted: () => init.signal && init.signal.aborted, init });
  if (BODY_HANGS) return Promise.resolve({ status: 200, headers: { get: () => null },
    text: () => new Promise(() => {}) });
  return new Promise((res, rej) => { if (init.signal) init.signal.addEventListener('abort', () => rej(new Error('aborted'))); }); };
const loc = { href: 'https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=1',
              hostname: 'adsmanager.facebook.com', pathname: '/adsmanager/manage/campaigns' };
const doc = { readyState: 'complete', querySelector: () => null, scripts: [], documentElement: { innerHTML: '' } };
const perf = { getEntriesByType: (t) => t === 'resource' ? [RES].map((n) => ({ name: n })) : [] };
const G = {};
new Function('globalThis','location','document','performance','fetch','URL','URLSearchParams', HELPER)(G, loc, doc, perf, fakeFetch, URL, URLSearchParams);
(async () => {
  const h = G[NAME];
  const got = await h.call({ method: 'POST', path: '/' + ACT + '/campaigns', post_timeout_ms: 30,
    params: { name: 'C', objective: 'OUTCOME_LEADS', status: 'PAUSED', special_ad_categories: '[]', buying_type: 'AUCTION' } });
  process.stdout.write(JSON.stringify({ got: got, n: calls.length, aborted: calls[0].aborted() }));
})();
"""
    act = "act_1234567890"
    res = f"https://graph.facebook.com/v22.0/{act}/campaigns?fields=name&access_token={TOKEN}"
    src = (script.replace("BODY_HANGS", "true" if body_hangs else "false").replace("RES", json.dumps(res)).replace("ACT", json.dumps(act)).replace("NAME", json.dumps("__n"))
           .replace("HELPER", json.dumps(tc.helper_source("__n", wp.policy_for_page(act)))))
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(src)
        path = fh.name
    try:
        done = subprocess.run([node, path], capture_output=True, text=True, timeout=60)
    finally:
        Path(path).unlink(missing_ok=True)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    assert out["got"]["error"] == "network_timeout" and out["n"] == 1 and out["aborted"] is True


def test_write_unknown_carries_the_in_page_reason(tmp_path):
    class Gone(FakeMeta):
        def write(self, path, params, files=None, filename="", **kw):
            return {"error": "call_unknown", "reason": "context_stale"}

    client = g.GraphClient(Gone(), allow_writes=True, sleep=lambda s: None, min_interval_s=0.0, max_calls=200)
    client.write_act = ACT
    with pytest.raises(g.WriteUnknown) as caught:
        client.post(f"/{ACT}/ads", {"name": "x"})
    assert caught.value.reason == "context_stale"


def test_cdp_timeout_stops_after_exactly_one_post_and_leaves_a_row(tmp_path, monkeypatch):
    rows = []
    monkeypatch.setattr(rt, "record", lambda step, ok=None, **kw: rows.append((step, kw.get("diag") or {})))
    meta = CdpMeta()
    meta.fail_next["ads"] = "cdp_unsent"
    out, _ = _run(meta, _draft(tmp_path, ads=1), reconcile_wait_s=0)
    assert out.error and out.stop_kind == "unknown"
    assert len(_edges(meta, "ads")) == 1
    row = [d for s, d in rows if s == "api_create_step" and d.get("reason") == "cdp_timeout"]
    assert len(row) == 1 and row[0]["outcome"] == "stop_not_found"
    assert {"waited_s", "readback_1", "cpu_pct", "page_count"} <= set(row[0])


def test_telemetry_row_carries_reason_waits_and_both_readbacks(tmp_path, monkeypatch):
    rows = []
    monkeypatch.setattr(rt, "record", lambda step, ok=None, **kw: rows.append((step, kw.get("diag") or {})))
    meta = CdpMeta()
    meta.fail_next["ads"] = "page_unsent"
    out, _ = _run(meta, _draft(tmp_path, ads=1), reconcile_wait_s=0)
    assert not out.error, out.error
    assert len(_edges(meta, "ads")) == 2
    row = [d for s, d in rows if s == "api_create_step" and d.get("reason") == "network_timeout"]
    assert len(row) == 1
    assert row[0]["outcome"] == "resend" and row[0]["readback_1"] is False and row[0]["readback_2"] is False
    assert {"waited_s", "cpu_pct", "page_count"} <= set(row[0])


def test_post_deadline_has_a_floor_and_stays_under_the_cdp_wait():
    from src.fb_api import token_capture as tc

    assert tc.InPageGraph.post_deadline_ms(120.0) == 102_000
    assert tc.InPageGraph.post_deadline_ms(30.0) == 20_000
    assert tc.InPageGraph.post_deadline_ms(1.0) == 5_000          # floor: a short wait never aborts a healthy write
