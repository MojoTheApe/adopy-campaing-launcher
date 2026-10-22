"""No network: the fixed no-creation boundary in Python and the native JS helper."""
import json
import re
import shutil
import subprocess

import pytest

from src.fb_api import graph as g, page_validation as pv, token_capture as tc, write_policy as wp
from tests.test_acl1761_api_create import ACT, ACT2, TOKEN, StubTab


class ValidationTab(StubTab):
    def navigate(self, url, timeout=60):
        self.url = url

    def evaluate_in(self, ctx, expr, timeout=60):
        if ".validatePage(" in expr:
            self.exprs.append(expr)
            if self.call_exc:
                raise self.call_exc
            return self.call_reply
        return super().evaluate_in(ctx, expr, timeout)


def test_read_transport_validation_is_pinned_by_actual_account_navigation():
    tab = ValidationTab({"status": 200, "body": {"success": True}})
    transport = tc.InPageGraph(tab)
    with pytest.raises(ValueError, match="bound"):
        transport.validate_page(ACT, "777", "https://example.com/")
    transport.navigate(ACT)
    with pytest.raises(ValueError, match="bound"):
        transport.validate_page(ACT2, "777", "https://example.com/")
    assert not any(".validatePage(" in e for e in tab.exprs)
    client = g.GraphClient(transport, min_interval_s=0)
    assert client.validate_page(ACT, "777", "https://example.com/") == {"success": True}
    assert client.calls == 1 and client.writes == 0
    assert len([e for e in tab.exprs if ".validatePage(" in e]) == 1
    assert not transport.allow_writes and transport._write_policy is None
    with pytest.raises(ValueError, match="read-only"):
        transport.write(f"/{ACT}/adcreatives", pv.params(ACT, "777"))
    with pytest.raises(ValueError, match="read-only"):
        client.post(f"/{ACT}/adcreatives", pv.params(ACT, "777"))


@pytest.mark.parametrize("act,page,link", [
    (ACT2, "777", "https://example.com"), (ACT, "bad", "https://example.com"),
    (ACT, "777", "javascript:alert(1)"), (ACT, "777", "https://user:pass@example.com"),
    (ACT, "777", "https://example.com/\n?method=DELETE"),
])
def test_unsafe_validation_never_reaches_the_page(act, page, link):
    tab = ValidationTab()
    transport = tc.InPageGraph(tab, allow_writes=True, write_act=ACT)
    with pytest.raises(ValueError):
        transport.validate_page(act, page, link)
    assert tab.exprs == []


def test_unknown_validation_is_not_retried_or_counted_as_a_write():
    tab = ValidationTab(call_exc=tc.CdpTimeout("synthetic timeout"))
    transport = tc.InPageGraph(tab, allow_writes=True, write_act=ACT)
    client = g.GraphClient(transport, min_interval_s=0)
    with pytest.raises(g.GraphError, match="not confirm"):
        client.validate_page(ACT, "777")
    assert client.calls == 1 and client.writes == 0
    assert len([e for e in tab.exprs if ".validatePage(" in e]) == 1


@pytest.mark.parametrize("mode", ["unsupported", "budget", "unreconciled", "stopped"])
def test_existing_stop_budget_and_unknown_write_guards_still_apply(mode):
    class Transport:
        calls = 0
        def validate_page(self, *args):
            self.calls += 1
            return {"status": 200, "body": {"success": True}}
    transport = Transport() if mode != "unsupported" else object()
    client = g.GraphClient(transport, min_interval_s=0)
    if mode == "budget":
        client.calls = client.max_calls
    elif mode == "unreconciled":
        client.unreconciled = f"/{ACT}/campaigns"
    elif mode == "stopped":
        client.stopped = g.PolicyBlocked("stopped")
    with pytest.raises((g.GraphError, g.GraphStop, g.WritesBlocked)):
        client.validate_page(ACT, "777")
    assert getattr(transport, "calls", 0) == 0


@pytest.mark.parametrize("code,error", [(190, g.LoggedOut), (368, g.PolicyBlocked), (4, g.RateLimited)])
def test_meta_validation_stops_keep_their_existing_meaning(code, error):
    class Transport:
        def validate_page(self, *args):
            return {"status": 400, "body": {"error": {"code": code, "message": "synthetic refusal"}}}
    client = g.GraphClient(Transport(), min_interval_s=0)
    with pytest.raises(error):
        client.validate_page(ACT, "777")
    assert isinstance(client.stopped, error) and client.writes == 0


def test_helper_only_sends_the_fixed_validate_only_creative(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    helper = tc.helper_source("n", None, ACT)
    script = r'''
const calls = [], G = {};
const loc = {href: 'https://adsmanager.facebook.com/adsmanager/manage/campaigns', hostname: 'adsmanager.facebook.com', pathname: '/adsmanager/manage/campaigns'};
const doc = {readyState: 'complete', querySelector: () => null, scripts: [], documentElement: {innerHTML: '"accessToken":"' + TOKEN + '"'}};
const fetcher = async (url, opts) => { calls.push({url, method:opts.method, params:Object.fromEntries(opts.body)}); return {status:200,text:async()=>'{"success":true}'}; };
new Function('globalThis','location','document','performance','fetch','URL','URLSearchParams',HELPER)(G,loc,doc,{getEntriesByType:()=>[]},fetcher,URL,URLSearchParams);
(async()=>{
 const h=G.n, out={};
 out.generic=await h.call({method:'POST',path:'/'+ACT+'/adcreatives',params:{execution_options:['validate_only']}});
 out.allowed=await h.validatePage({page_id:'777',link:'https://example.com/'});
 out.rejected=[];
 for (const extra of [{method:'POST'}, {path:'/'+ACT2+'/adcreatives'}, {params:{execution_options:[]}}, {files:{filename:'x'}}, {execution_options:['include_recommendations']}]) {
   out.rejected.push(await h.validatePage({page_id:'777',link:'https://example.com/',...extra}));
 }
 out.calls=calls; process.stdout.write(JSON.stringify(out));
})();
'''
    values = {"TOKEN": TOKEN, "HELPER": helper, "ACT2": ACT2, "ACT": ACT}
    script = re.sub(r"\b(TOKEN|HELPER|ACT2|ACT)\b", lambda m: json.dumps(values[m[0]]), script)
    path = tmp_path / "validation.js"
    path.write_text(script)
    result = subprocess.run([node, str(path)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["generic"]["error"] == "read_only"
    assert out["allowed"]["body"] == {"success": True}
    assert all(r["error"] == "validation_refused" for r in out["rejected"])
    assert len(out["calls"]) == 1
    call = out["calls"][0]
    assert call["url"].endswith(f"/{ACT}/adcreatives") and call["method"] == "POST"
    assert "access_token" not in call["url"]
    payload = call["params"]
    assert set(payload) == {"name", "object_story_spec", "execution_options", "access_token"}
    assert json.loads(payload["execution_options"]) == ["validate_only"]
    assert json.loads(payload["object_story_spec"]) == {"page_id": "777", "link_data": {"link": "https://example.com/"}}
    assert TOKEN not in json.dumps({k:v for k,v in out.items() if k != "calls"})
    # The generic creation policy still rejects the same option.
    with pytest.raises(wp.WriteRefused):
        wp.check(ACT, "POST", f"/{ACT}/adcreatives", pv.params(ACT, "777"))


def test_validation_honors_usage_limit_headers_before_creation():
    class Transport:
        def validate_page(self, *args):
            return {"status": 200, "body": {"success": True},
                    "headers": {"x-app-usage": json.dumps({"call_count": 99})}}
    client = g.GraphClient(Transport(), min_interval_s=0)
    with pytest.raises(g.RateLimited):
        client.validate_page(ACT, "777")
    assert client.usage_headers_seen and isinstance(client.stopped, g.RateLimited) and client.writes == 0
