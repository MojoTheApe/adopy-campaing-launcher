"""ACL-1756: Graph API изнутри Multilogin-профиля, токен остаётся во вкладке.

Ни Meta, ни Multilogin здесь не трогаются: браузер — поддельное CDP-соединение,
страница — поддельный помощник, который «держит» токен у себя. Главная
проверка — у Python токена нет нигде: ни в ответах, ни в выводе, ни в логах,
ни в журнале, ни в Sentry. Сам помощник страницы (JS) прогоняется в Node —
см. tests/test_acl1756_page_helper_node.py.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_api import graph as g  # noqa: E402
from src.fb_api import profile as prof  # noqa: E402
from src.fb_api import telemetry as tm  # noqa: E402
from src.fb_api import token_capture as tc  # noqa: E402
from src.fb_api.cdp import PageTab  # noqa: E402

TOKEN = "EAAB" + "Zx9" * 20          # «токен» страницы — живёт только в FakePage
ACT = "act_1234567890"          # заглушки из acl_scan_secrets._PLACEHOLDERS
ACT2 = "act_1234567890123456"
PROFILE_UUID = "550e8400-e29b-41d4-a716-446655440000"
BIZ = "987654321012345"
TOKEN_LIKE = re.compile(r"EAA[A-Za-z0-9]{10,}")
#: Номер кабинета и длинный номер (бизнес, кабинет) в вычищенном тексте.
#: Без обратной косой: проверка платформ (test_acl_test_platform) читает её как путь Windows.
ACT_ID_RE = re.compile(r"act_[0-9]")
LONG_ID_RE = re.compile(r"[0-9]{8,}")


# ───────────────────────── поддельный браузер ─────────────────────────

class FakePage:
    """Страница с помощником: токен у неё, Python получает только ответы.

    Ответы нарочно содержат токен там, где его кладёт Graph (`paging.next`),
    и в «случайном» поле — как будто вычистка помощника не сработала: вторая
    линия в Python должна его убрать.
    """

    def __init__(self, replies=None, *, state=None, after_reload=None):
        self._token = TOKEN
        self.replies = list(replies or [])
        self.specs: list[dict] = []
        self.loaded = False
        self.retired = False
        self.reloads = 0
        self.state_value = state or {"kind": "ads_manager", "has_token": True, "source": "network"}
        self.after_reload = after_reload

    def evaluate(self, expression: str):
        if ".call(" in expression:
            spec = json.loads(expression.split(".call(", 1)[1].rsplit(")", 1)[0])
            self.specs.append(spec)
            reply = self.replies.pop(0) if self.replies else {"status": 200, "body": {"data": []}}
            return json.loads(json.dumps(reply).replace("__TOKEN__", self._token))
        if ".ready(" in expression:
            return dict(self.state_value)
        if ".retire()" in expression:
            self.retired = True
            return True
        if "!h.state().retired" in expression:
            return self.loaded
        if "h.state()" in expression:
            return dict(self.state_value)
        raise AssertionError(f"unexpected expression {expression[:80]}")

    def navigate(self):
        self.loaded = True

    def reload(self):
        self.reloads += 1
        self.retired = False
        if self.after_reload is not None:
            self.state_value = dict(self.after_reload)


class FakeCdp:
    """Соединение к браузеру профиля (вместо `tab_watchdog.RawCdp`)."""

    def __init__(self, page: FakePage, *, other_tabs: int = 1):
        self.page = page
        self.sent: list[tuple[str, dict, str]] = []
        self.other_tabs = other_tabs
        self.closed = False
        self.closed_targets: list[str] = []

    def call(self, method, params=None, *, session_id="", timeout=5.0):
        params = params or {}
        self.sent.append((method, params, session_id))
        if method == "Target.createTarget":
            return {"targetId": "T-mine"}
        if method == "Target.attachToTarget":
            return {"sessionId": "S-mine"}
        if method == "Target.getTargets":
            others = [{"targetId": f"T-{i}", "type": "page"} for i in range(self.other_tabs)]
            return {"targetInfos": others + [{"targetId": "T-mine", "type": "page"}]}
        if method == "Target.closeTarget":
            self.closed_targets.append(params["targetId"])
            return {}
        if method == "Page.navigate":
            self.page.navigate()
            return {"frameId": "F"}
        if method == "Page.reload":
            self.page.reload()
            return {}
        if method == "Runtime.evaluate":
            assert session_id == "S-mine"
            return {"result": {"value": self.page.evaluate(params["expression"])}}
        return {}

    def close(self):
        self.closed = True


def _tab(page, **kw):
    cdp = FakeCdp(page, **kw)
    return PageTab.open("http://127.0.0.1:1", connect=lambda url, timeout: cdp), cdp


def _graph(page):
    tab, cdp = _tab(page)
    return tc.InPageGraph(tab, sleep=lambda s: None), cdp


def _client(transport, **kw):
    sleeps: list[float] = []
    now = [0.0]
    c = g.GraphClient(transport, sleep=lambda s: (sleeps.append(s), now.__setitem__(0, now[0] + s)),
                      clock=lambda: now[0], **kw)
    return c, sleeps


def _err(code, subcode=None):
    e = {"message": f"err {code} access_token=__TOKEN__", "code": code}
    if subcode is not None:
        e["error_subcode"] = subcode
    return {"status": 400, "body": {"error": e}, "headers": {}}


def _walk_strings(value):
    if isinstance(value, dict):
        for k, v in value.items():
            yield str(k)
            yield from _walk_strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _walk_strings(v)
    elif value is not None:
        yield str(value)


# ───────────────────────── токен — только во вкладке ─────────────────────────

def _pilot_replies():
    return [
        {"status": 200, "headers": {"x-business-use-case-usage": json.dumps(
            {BIZ: [{"type": "ads_management", "call_count": 3, "total_cputime": 1, "total_time": 1,
                    "estimated_time_to_regain_access": 0}]})},
         "body": {"data": [{"id": ACT, "account_id": ACT[4:], "account_status": 1,
                            "leak": "__TOKEN__"},
                           {"id": ACT2, "account_id": ACT2[4:]}],
                  "paging": {"cursors": {"after": "x"}}}},
        {"status": 200, "headers": {},
         "body": {"data": [{"id": "1", "effective_status": "ACTIVE"},
                           {"id": "2", "effective_status": "PAUSED"}],
                  "paging": {"cursors": {"after": "c1"},
                             "next": f"https://graph.facebook.com/v22.0/{ACT}/campaigns?access_token=__TOKEN__&after=c1"}}},
        {"status": 200, "headers": {}, "body": {"data": [{"id": "3", "effective_status": "ACTIVE"}],
                                                "paging": {"cursors": {"after": "c2"}}}},
        {"status": 200, "headers": {}, "body": {"data": [{"spend": "12.34", "impressions": "1000",
                                                          "clicks": "20", "account_currency": "USD",
                                                          "actions": [{"action_type": "lead", "value": "3"}]}]}},
        {"status": 200, "headers": {}, "body": {"data": [{"spend": "80.5", "impressions": "9000",
                                                          "clicks": "150", "account_currency": "USD"}]}},
    ]


def test_pilot_end_to_end_python_never_holds_the_token(tmp_path, caplog, capsys, monkeypatch):
    from scripts import fb_api_pilot as pilot

    page = FakePage(_pilot_replies())
    cdp = FakeCdp(page)
    held: list = []           # всё, что Python получил от страницы

    class Spy(tc.InPageGraph):
        def _eval(self, expression, *, timeout=60.0):
            got = super()._eval(expression, timeout=timeout)
            held.append(got)
            return got

    released = []
    opened = prof.OpenedProfile("prof-uuid-1234", "fold", "A", "5555", None, True)
    target = pilot.Target("KLD_TEST", ACT, "prof-uuid-1234", "fold", "A")
    lines: list[str] = []
    caplog.set_level(logging.DEBUG)
    summary = pilot.run_pilot(
        target, emit=lambda s: (lines.append(tm.scrub(s)), print(tm.scrub(s))),
        open_profile=lambda *a: opened,
        release_profile=lambda o: released.append(o) or "left_open_started_by_pilot",
        open_tab=lambda url: PageTab.open(url, connect=lambda u, timeout: cdp),
        make_graph=lambda tab: Spy(tab, sleep=lambda s: None),
        make_client=lambda tr: g.GraphClient(tr, sleep=lambda s: None),
    )
    journal = tmp_path / "pilot.jsonl"
    tm.record({"event": "fb_api_pilot", **summary}, journal)

    # Помощник получил описания запросов, токена в них нет — он у страницы.
    assert [s["path"] for s in page.specs] == [
        "/me/adaccounts", f"/{ACT}/campaigns", f"/{ACT}/campaigns", f"/{ACT}/insights", f"/{ACT}/insights"]
    assert all(s["method"] == "GET" for s in page.specs)
    assert summary["ad_accounts"] == 2 and summary["act_visible"] is True
    assert summary["campaigns"] == 3
    assert summary["insights_yesterday"]["spend"] == 12.34
    assert summary["insights_yesterday"]["leads"] == {"lead": 3}
    assert summary["insights_last_7d"]["spend"] == 80.5
    assert released == [opened]
    assert "T-mine" in cdp.closed_targets and cdp.closed

    everything = [
        *list(_walk_strings(held)), *list(_walk_strings(summary)), *lines,
        capsys.readouterr().out, caplog.text, journal.read_text(encoding="utf-8"),
        *[json.dumps(m) for m in cdp.sent],
    ]
    leaks = [s for s in everything if TOKEN_LIKE.search(s) or TOKEN in s]
    assert not leaks, leaks[:3]
    # Номера в выводе — только последние 4 цифры.
    out_text = "\n".join(lines) + journal.read_text(encoding="utf-8")
    assert ACT not in out_text and ACT[4:] not in out_text and BIZ not in out_text
    assert "act_…7890" in "\n".join(lines)
    assert "Profile left open — close it in Multilogin when done." in lines
    assert any("usage_headers_seen: True" in line for line in lines)


def test_python_sends_only_request_specs_to_the_page():
    page = FakePage([{"status": 200, "body": {"data": []}}])
    gr, cdp = _graph(page)
    gr.start(ACT)
    gr.request("GET", "/me/adaccounts", {"fields": "id"})
    init = [p for m, p, _ in cdp.sent if m == "Page.addScriptToEvaluateOnNewDocument"]
    assert len(init) == 1 and gr.name in init[0]["source"]
    navs = [p["url"] for m, p, _ in cdp.sent if m == "Page.navigate"]
    assert navs == ["https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=1234567890"]
    # CDP-события сети не включаются: токен из трафика в Python не читается.
    assert not any(m.startswith("Network.") for m, _, _ in cdp.sent)


@pytest.mark.parametrize("method,params", [
    ("POST", {}), ("DELETE", {}), ("get", {}),
    ("GET", {"method": "post"}), ("GET", {"_method": "delete"}), ("GET", {"batch": "[]"}),
    ("GET", {"suppress_http_code": "1"}), ("GET", {"access_token": "x"}), ("GET", {"METHOD": "post"}),
    # Имена, которые сервер мог бы нормализовать в `method` (ревью, раунд 2).
    ("GET", {" method": "post"}), ("GET", {"method ": "post"}), ("GET", {"\tmethod": "post"}),
    ("GET", {"method\u0000": "post"}), ("GET", {"method[]": "post"}), ("GET", {"\uff4dethod": "post"}),
    ("GET", {"fields.x": "id"}), ("GET", {"": "x"}),
])
def test_in_page_graph_refuses_writes_before_touching_the_page(method, params):
    page = FakePage([OK])
    gr, cdp = _graph(page)
    sent = len(cdp.sent)
    with pytest.raises(ValueError):
        gr.request(method, "/me", params)
    assert page.specs == [] and len(cdp.sent) == sent


def test_in_page_graph_accepts_plain_read_params():
    page = FakePage([OK])
    gr, _ = _graph(page)
    gr.request("GET", "/act_1/insights", {"fields": "spend", "date_preset": "last_7d", "limit": 5})
    assert page.specs[0]["params"]["date_preset"] == "last_7d"


def test_no_profile_stop_anywhere_in_the_pilot():
    for path in [*(ROOT / "src" / "fb_api").glob("*.py"), ROOT / "scripts" / "fb_api_pilot.py"]:
        src = path.read_text(encoding="utf-8")
        assert "stop_profile" not in src and "terminate" not in src.lower(), path.name


def test_no_network_capture_or_token_api_in_python_sources():
    for path in (ROOT / "src" / "fb_api").glob("*.py"):
        src = path.read_text(encoding="utf-8")
        assert "Network.enable" not in src and "requestWillBeSent" not in src, path.name
        assert "getRequestPostData" not in src, path.name


# ───────────────────────── разбор ошибок ─────────────────────────

class FakeTransport:
    def __init__(self, replies, *, state=None, reacquire_state=None):
        self.replies = list(replies)
        self.requests: list = []
        self.reacquires = 0
        self._state = state or {"kind": "ads_manager", "has_token": True}
        self._reacquire_state = reacquire_state or {"kind": "ads_manager", "has_token": True}

    def request(self, method, path, params=None, **kw):
        self.requests.append((method, path, dict(params or {})))
        # Как будто вычистка страницы не сработала: токен в тексте ошибки.
        return json.loads(json.dumps(self.replies.pop(0)).replace("__TOKEN__", TOKEN))

    def state(self):
        return dict(self._state)

    def reacquire(self, *, timeout=60.0):
        self.reacquires += 1
        return dict(self._reacquire_state)


OK = {"status": 200, "body": {"data": [{"id": "1"}]}, "headers": {}}


def test_190_reacquires_once_in_page_then_succeeds():
    tr = FakeTransport([_err(190), OK])
    c, _ = _client(tr)
    assert c.get("/me/adaccounts")["data"] == [{"id": "1"}]
    assert tr.reacquires == 1 and len(tr.requests) == 2


def test_190_twice_stops_as_logged_out():
    tr = FakeTransport([_err(190, 463), _err(190, 463)])
    c, _ = _client(tr)
    with pytest.raises(g.LoggedOut) as ei:
        c.get("/me/adaccounts")
    assert tr.reacquires == 1 and len(tr.requests) == 2
    assert TOKEN not in str(ei.value)
    with pytest.raises(g.LoggedOut):
        c.get("/me/adaccounts")
    assert len(tr.requests) == 2           # после стопа вызовов нет


@pytest.mark.parametrize("subcode", [459, 464])
def test_190_with_checkpoint_subcode_stops_without_reload(subcode):
    tr = FakeTransport([_err(190, subcode), OK])
    c, _ = _client(tr)
    with pytest.raises(g.Checkpoint):
        c.get("/me")
    assert tr.reacquires == 0 and len(tr.requests) == 1


def test_network_failure_stops_without_a_second_call():
    tr = FakeTransport([{"error": "network"}, OK])
    c, _ = _client(tr)
    with pytest.raises(g.PageFailed):
        c.get("/me")
    with pytest.raises(g.PageFailed):
        c.get("/me")
    assert len(tr.requests) == 1


def test_190_when_reload_lands_on_checkpoint_stops():
    tr = FakeTransport([_err(190)], reacquire_state={"kind": "checkpoint", "has_token": False})
    c, _ = _client(tr)
    with pytest.raises(g.Checkpoint):
        c.get("/me")
    assert len(tr.requests) == 1


def test_checkpoint_page_stops_before_reacquire():
    tr = FakeTransport([_err(190)], state={"kind": "checkpoint", "has_token": False})
    c, _ = _client(tr)
    with pytest.raises(g.Checkpoint):
        c.get("/me")
    assert tr.reacquires == 0


@pytest.mark.parametrize("code", [4, 17, 613, 80004, 32])
def test_rate_limit_codes_stop_without_retry(code):
    tr = FakeTransport([_err(code), OK])
    c, sleeps = _client(tr)
    with pytest.raises(g.RateLimited):
        c.get("/act_1/insights")
    with pytest.raises(g.RateLimited):
        c.get("/act_1/insights")
    assert len(tr.requests) == 1 and tr.reacquires == 0 and sleeps == []


def test_368_stops_immediately_and_never_retries():
    tr = FakeTransport([_err(368), OK, OK])
    c, _ = _client(tr)
    with pytest.raises(g.PolicyBlocked):
        c.get("/me/adaccounts")
    with pytest.raises(g.PolicyBlocked):
        c.get("/me/adaccounts")
    assert len(tr.requests) == 1 and tr.reacquires == 0


def test_html_instead_of_json_checks_the_page():
    tr = FakeTransport([{"status": 200, "body": {"non_json": "<html>"}, "headers": {}}],
                       state={"kind": "login"})
    c, _ = _client(tr)
    with pytest.raises(g.LoggedOut):
        c.get("/me")


def test_graph_error_message_carries_meta_details_scrubbed():
    reply = {"status": 400, "headers": {},
             "endpoint": {"host": "adsmanager-graph.facebook.com", "version": "v23.0", "credentials": "include"},
             "body": {"error": {"message": "Invalid request.", "code": 1, "type": "OAuthException",
                                "error_subcode": 33, "fbtrace_id": "AbC123xyz", "is_transient": False,
                                "error_user_msg": f"see {ACT} token __TOKEN__"}}}
    tr = FakeTransport([reply, OK])
    c, _ = _client(tr)
    with pytest.raises(g.GraphError) as ei:
        c.get("/me/adaccounts")
    text = str(ei.value)
    for part in ("Graph API error 1: Invalid request.", "type=OAuthException", "error_subcode=33",
                 "fbtrace_id=AbC123xyz", "is_transient=False", "host=adsmanager-graph.facebook.com",
                 "version=v23.0", "credentials=include", "http_status=400"):
        assert part in text, (part, text)
    assert TOKEN not in text and ACT not in text
    assert ei.value.details["fbtrace_id"] == "AbC123xyz"
    assert not c.stopped


@pytest.mark.parametrize("kw", [{"graph_host": "evil"}, {"credentials": "same-origin"}])
def test_in_page_graph_options_are_a_closed_list(kw):
    page = FakePage([OK])
    tab, _ = _tab(page)
    with pytest.raises(ValueError):
        tc.InPageGraph(tab, **kw)


def test_in_page_graph_passes_host_and_cookie_mode_to_the_page():
    page = FakePage([OK])
    tab, _ = _tab(page)
    gr = tc.InPageGraph(tab, graph_host="public", credentials="include", sleep=lambda s: None)
    gr.request("GET", "/me", {"fields": "id"})
    assert page.specs[0]["host"] == "public" and page.specs[0]["credentials"] == "include"


def test_graph_error_details_are_scrubbed_at_build():
    reply = {"status": 400, "headers": {},
             "endpoint": {"host": "graph.facebook.com", "version": "v22.0", "credentials": f"x {TOKEN}"},
             "body": {"error": {"message": "x", "code": 100, "type": f"T {ACT}",
                                "fbtrace_id": f"{BIZ}", "error_user_title": f"act={ACT[4:]} {TOKEN}",
                                "error_user_msg": f"see {ACT2} token {TOKEN} biz {BIZ}"}}}
    c, _ = _client(FakeTransport([reply]))
    with pytest.raises(g.GraphError) as ei:
        c.get("/me")
    blob = json.dumps(ei.value.details)
    assert TOKEN not in blob and not TOKEN_LIKE.search(blob)
    assert not ACT_ID_RE.search(blob) and not LONG_ID_RE.search(blob), blob


def test_pilot_flags_reach_in_page_graph(monkeypatch, tmp_path):
    from scripts import fb_api_pilot as pilot

    made: list = []

    class Spy(tc.InPageGraph):
        def __init__(self, tab, **kw):
            super().__init__(tab, **kw)
            made.append((self.graph_host, self.credentials))

    monkeypatch.setattr(tc, "InPageGraph", Spy)
    page = FakePage(_pilot_replies())
    cdp = FakeCdp(page)
    target = pilot.Target("KLD_TEST", ACT, "prof-uuid-1234", "fold", "A")
    pilot.run_pilot(
        target, emit=lambda s: None,
        open_profile=lambda *a: prof.OpenedProfile("p", "f", "A", "5555", None, False),
        release_profile=lambda o: "left_running",
        open_tab=lambda url: PageTab.open(url, connect=lambda u, timeout: cdp),
        make_client=lambda tr: g.GraphClient(tr, sleep=lambda s: None),
        graph_host="public", credentials="omit",
    )
    assert made == [("public", "omit")]
    sent = [s for s in page.specs]
    assert sent and all(s["host"] == "public" and s["credentials"] == "omit" for s in sent)

    # И из командной строки до run_pilot.
    seen = {}
    monkeypatch.setattr("config.paths.load_env", lambda: [])
    monkeypatch.setattr(tm, "journal_path", lambda: tmp_path / "j.jsonl")
    monkeypatch.setattr(pilot, "run_pilot", lambda target, **kw: seen.update(kw) or {})
    assert pilot.main(["--profile-id", "p-1", "--act", ACT, "--run",
                       "--graph-host", "public", "--credentials", "include"]) == 0
    assert (seen["graph_host"], seen["credentials"]) == ("public", "include")


def test_pilot_options_reach_the_transport(monkeypatch):
    from scripts import fb_api_pilot as pilot

    args = pilot._parse(["--profile-id", "p", "--act", ACT, "--graph-host", "public", "--credentials", "omit"])
    assert (args.graph_host, args.credentials) == ("public", "omit")
    assert pilot._parse(["--profile-id", "p", "--act", ACT]).graph_host == "captured"
    with pytest.raises(SystemExit):
        pilot._parse(["--profile-id", "p", "--act", ACT, "--credentials", "same-origin"])


def test_other_graph_errors_do_not_stop_the_client():
    tr = FakeTransport([_err(100), OK])
    c, _ = _client(tr)
    with pytest.raises(g.GraphError) as ei:
        c.get("/act_1/campaigns")
    # Текст ошибки Graph приходит с токеном — в исключение он не попадает.
    assert TOKEN not in str(ei.value) and "access_token=<token>" in str(ei.value)
    assert c.get("/act_1/campaigns")["data"]


# ───────────────────────── лимиты и темп ─────────────────────────

def _usage(pct=0.0, wait_min=0):
    return {"x-business-use-case-usage": json.dumps(
        {BIZ: [{"type": "ads_insights", "call_count": pct, "total_cputime": 1, "total_time": 1,
                "estimated_time_to_regain_access": wait_min}]})}


def test_calls_are_spaced_and_never_polled():
    tr = FakeTransport([OK, OK])
    c, sleeps = _client(tr, min_interval_s=2.0)
    c.get("/a")
    c.get("/b")
    assert sleeps == [2.0]


def test_usage_over_half_slows_down():
    tr = FakeTransport([{**OK, "headers": _usage(60)}, OK])
    c, sleeps = _client(tr, min_interval_s=2.0)
    c.get("/a")
    c.get("/b")
    assert sleeps == [6.0]


def test_usage_near_limit_returns_answer_then_stops():
    tr = FakeTransport([{**OK, "headers": {"x-ad-account-usage": '{"acc_id_util_pct": 85}'}}, OK])
    c, _ = _client(tr)
    assert c.get("/a")["data"]
    with pytest.raises(g.RateLimited):
        c.get("/b")
    assert len(tr.requests) == 1


def test_regain_access_time_stops_with_retry_hint():
    tr = FakeTransport([{**OK, "headers": _usage(10, wait_min=5)}])
    c, _ = _client(tr)
    with pytest.raises(g.RateLimited) as ei:
        c.get("/a")
    assert ei.value.retry_after_s == 300.0


def test_call_budget_is_a_hard_stop():
    tr = FakeTransport([OK, OK, OK])
    c, _ = _client(tr, max_calls=2)
    c.get("/a")
    c.get("/b")
    with pytest.raises(g.RateLimited):
        c.get("/c")
    assert len(tr.requests) == 2


def test_usage_parser_hides_business_ids():
    u = g.usage_from_headers(_usage(42))
    assert u["max_pct"] == 42 and BIZ not in json.dumps(u)


def test_campaigns_paging_uses_cursor_not_next_url():
    tr = FakeTransport([
        {"status": 200, "headers": {}, "body": {"data": [{"id": "1"}], "paging": {
            "cursors": {"after": "A1"}, "next": "https://graph.facebook.com/x?access_token=<hidden>"}}},
        {"status": 200, "headers": {}, "body": {"data": [{"id": "2"}], "paging": {"cursors": {"after": "A2"}}}},
    ])
    c, _ = _client(tr)
    assert [r["id"] for r in c.campaigns(ACT)] == ["1", "2"]
    assert tr.requests[1][2]["after"] == "A1" and tr.requests[1][1] == f"/{ACT}/campaigns"


def test_bad_act_is_refused_before_any_call():
    c, _ = _client(FakeTransport([]))
    with pytest.raises(ValueError):
        c.campaigns("act_12/../me")
    with pytest.raises(ValueError):
        c.insights(ACT, "lifetime")


# ───────────────────────── вычистка ─────────────────────────

def test_scrub_masks_token_acts_and_business_ids():
    text = (f"token {TOKEN} url https://graph.facebook.com/v22.0/{ACT}/x?access_token={TOKEN}&a=1 "
            f"biz {BIZ} act=1234567890")
    out = tm.scrub(text)
    assert TOKEN not in out and "EAA" not in out
    assert "1234567890" not in out and BIZ not in out
    assert "act_<id>" in out


def test_scrub_value_hides_keys_and_nested_values():
    out = tm.scrub_value({"access_token": TOKEN, BIZ: [{"id": ACT}], "note": f"x {TOKEN}"})
    blob = json.dumps(out)
    assert TOKEN not in blob and BIZ not in blob and ACT not in blob


def test_strip_tokens_keeps_ids_but_drops_tokens():
    out = tm.strip_tokens({"id": ACT, "next": f"https://g/x?access_token={TOKEN}&after=1",
                           "access_token": TOKEN, "n": [TOKEN]})
    assert out["id"] == ACT and TOKEN not in json.dumps(out)
    assert out["next"] == "https://g/x?access_token=<hidden>&after=1"


def test_mask_id_keeps_last_four():
    assert tm.mask_id(ACT) == "act_…7890"
    assert tm.mask_id("prof-uuid-abcd") == "…abcd"


def test_scrub_filter_cleans_log_records(caplog):
    log = logging.getLogger("acl1756.test")
    caplog.set_level(logging.INFO)
    caplog.handler.addFilter(tm.ScrubFilter())
    log.info("token %s act %s", TOKEN, ACT)
    assert TOKEN not in caplog.text and ACT not in caplog.text


def test_journal_line_is_scrubbed(tmp_path):
    path = tm.record({"event": "x", "message": f"{TOKEN} {ACT} {BIZ}"}, tmp_path / "j.jsonl")
    text = path.read_text(encoding="utf-8")
    assert TOKEN not in text and ACT not in text and BIZ not in text


def test_sentry_stop_event_is_scrubbed(monkeypatch):
    import types

    from src import acl_sentry

    sent: dict = {}

    class Scope:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def set_tag(self, k, v):
            sent.setdefault("tags", {})[k] = v

        def set_context(self, k, v):
            sent.setdefault("ctx", {})[k] = v

    fake = types.SimpleNamespace(
        new_scope=lambda: Scope(),
        capture_message=lambda msg, level=None: sent.update(msg=msg) or "evt1",
    )
    monkeypatch.setitem(sys.modules, "sentry_sdk", fake)
    monkeypatch.setattr(acl_sentry, "init", lambda: True)
    assert tm.report_stop("logged_out", f"bad token {TOKEN} for {ACT}",
                          {"account": "X", "act": ACT, "biz": BIZ}) == "evt1"
    blob = json.dumps(sent)
    assert TOKEN not in blob and ACT not in blob and BIZ not in blob
    assert sent["tags"]["fb_api_stop"] == "logged_out"


# ───────────────────────── вкладка и профиль ─────────────────────────

def test_tab_closes_only_itself():
    tab, cdp = _tab(FakePage(), other_tabs=2)
    tab.close()
    assert cdp.closed_targets == ["T-mine"] and cdp.closed


def test_last_tab_is_not_closed():
    tab, cdp = _tab(FakePage(), other_tabs=0)
    tab.close()
    assert cdp.closed_targets == [] and cdp.closed


class FakeLauncher:
    def __init__(self, start_port="7777"):
        self.started = []
        self.stopped = []
        self.start_port = start_port

    def start_profile(self, folder_id, profile_id, *, headless=False):
        self.started.append(profile_id)
        from src.meta_campaign_agent.multilogin_launcher import StartedProfile

        return StartedProfile(profile_id, folder_id or "F", port=self.start_port)

    def stop_profile(self, profile_id, folder_id=None):
        self.stopped.append(profile_id)
        raise AssertionError("the pilot must never stop a profile")


@pytest.mark.parametrize("state,port,started_by_us,release", [
    ("running", "6000", False, "left_running"),
    ("not_running", None, True, "left_open_started_by_pilot"),
    ("unknown", None, False, "left_running"),
])
def test_profile_is_never_stopped(monkeypatch, state, port, started_by_us, release):
    launcher = FakeLauncher()
    monkeypatch.setattr(prof, "running_state", lambda l, p: (state, port))
    opened = prof.open_profile("P1", "F1", "A", launcher_factory=lambda i: launcher)
    assert opened.started_by_us is started_by_us
    assert launcher.started == ([] if state == "running" else ["P1"])
    assert prof.release_profile(opened) == release
    assert launcher.stopped == []


def test_hand_opened_profile_is_refused_and_left_alone(monkeypatch):
    launcher = FakeLauncher()
    monkeypatch.setattr(prof, "running_state", lambda l, p: ("running", None))
    with pytest.raises(prof.ProfileError):
        prof.open_profile("P1", "F1", "A", launcher_factory=lambda i: launcher)
    assert launcher.started == [] and launcher.stopped == []


def test_cdp_url_from_mlx_answer():
    assert prof.OpenedProfile("p", "f", "A", "5000", None, False).cdp_url == "http://127.0.0.1:5000"
    assert (prof.OpenedProfile("p", "f", "A", "5000", "devtools/browser/x", False).cdp_url
            == "http://127.0.0.1:5000/devtools/browser/x")


# ───────────────────────── пилот: сухой прогон, разрешение аккаунта ─────────────────────────

def test_pilot_default_is_a_dry_run(monkeypatch, capsys, tmp_path):
    from scripts import fb_api_pilot as pilot

    monkeypatch.setattr("config.paths.load_env", lambda: [])
    monkeypatch.setattr(pilot, "run_pilot", lambda *a, **k: (_ for _ in ()).throw(AssertionError("ran")))
    monkeypatch.setattr(tm, "journal_path", lambda: tmp_path / "j.jsonl")
    assert pilot.main(["--profile-id", PROFILE_UUID, "--act", ACT]) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out and ACT not in out and "act_…7890" in out
    assert PROFILE_UUID not in out and "…0000" in out
    assert not (tmp_path / "j.jsonl").exists()


def test_pilot_resolves_account_from_the_map(tmp_path, monkeypatch, capsys):
    from scripts import fb_api_pilot as pilot

    csv_path = tmp_path / "map.csv"
    csv_path.write_text("account_name,ad_account_id,profile_id,folder_id,multilogin_instance\n"
                        "KLD_TEST_B1,1234567890,prof-1-abcd,fold-1,B\n", encoding="utf-8")
    monkeypatch.setenv("META_AD_ACCOUNT_MAP_PATH", str(csv_path))
    t = pilot.resolve_target(pilot._parse(["--account", "kld_test_b1"]))
    assert (t.act, t.profile_id, t.folder_id, t.instance) == (ACT, "prof-1-abcd", "fold-1", "B")


def test_pilot_stop_is_reported_scrubbed(monkeypatch, capsys, tmp_path):
    from scripts import fb_api_pilot as pilot

    monkeypatch.setattr("config.paths.load_env", lambda: [])
    reported = []
    monkeypatch.setattr(pilot, "run_pilot",
                        lambda *a, **k: (_ for _ in ()).throw(g.PolicyBlocked(f"368 for {ACT} {TOKEN}", code=368)))
    monkeypatch.setattr(tm, "journal_path", lambda: tmp_path / "j.jsonl")
    monkeypatch.setattr(tm, "report_stop", lambda *a: reported.append(a) or "")
    assert pilot.main(["--profile-id", "p-1", "--act", ACT, "--run"]) == 2
    out = capsys.readouterr().out
    assert "STOP (policy_block)" in out and TOKEN not in out and ACT not in out
    assert TOKEN not in (tmp_path / "j.jsonl").read_text(encoding="utf-8")
    assert reported and reported[0][0] == "policy_block"


# ───────────────────────── без Playwright ─────────────────────────

def test_fb_api_does_not_import_playwright():
    code = textwrap.dedent("""
        import sys
        import src.fb_api, src.fb_api.cdp, src.fb_api.graph, src.fb_api.token_capture
        import src.fb_api.profile, src.fb_api.telemetry
        import scripts.fb_api_pilot
        bad = sorted(m for m in sys.modules if m.split('.')[0] == 'playwright')
        print(bad)
        assert not bad, bad
    """)
    done = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), capture_output=True, text=True,
                          timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr
    for path in (ROOT / "src" / "fb_api").glob("*.py"):
        assert not re.search(r"^\s*(from|import)\s+playwright", path.read_text(encoding="utf-8"), re.M), path
