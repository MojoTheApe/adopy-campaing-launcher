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
from src.fb_api.cdp import CdpTimeout, PageTab  # noqa: E402

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


@pytest.fixture(autouse=True)
def _restore_log_handler_filters(caplog):
    """Pilot CLI tests must not leave redaction on pytest's shared handlers."""
    handlers = set(logging.getLogger().handlers) | {caplog.handler}
    before = {handler: list(handler.filters) for handler in handlers}
    try:
        yield
    finally:
        # Restore the existing filters and their order, including on handlers
        # detached during a test. Production pilot redaction stays unchanged.
        for handler, filters in before.items():
            handler.filters[:] = filters


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
        self.installs = 0
        self.state_value = state or {"kind": "ads_manager", "has_token": True, "source": "network"}
        self.after_reload = after_reload

    def evaluate(self, expression: str):
        # Установка помощника в отдельном мире — большой IIFE; принимаем молча.
        if expression.lstrip().startswith("(() =>"):
            self.installs += 1
            return None
        assert ".ready(" not in expression, "wait must never await an in-page promise"
        if ".call(" in expression:
            spec = json.loads(expression.split(".call(", 1)[1].rsplit(")", 1)[0])
            self.specs.append(spec)
            reply = self.replies.pop(0) if self.replies else {"status": 200, "body": {"data": []}}
            return json.loads(json.dumps(reply).replace("__TOKEN__", self._token))
        if ".retire()" in expression:
            self.retired = True
            return True
        if ".state()" in expression or ".probe()" in expression:
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
        self.worlds = 0

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
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": "F"}}}
        if method == "Page.createIsolatedWorld":
            self.worlds += 1
            return {"executionContextId": 100 + self.worlds}
        if method == "Runtime.evaluate":
            assert session_id == "S-mine"
            assert "contextId" in params, "helper must run in the isolated world"
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
    # Помощник ставится в отдельном мире, не главном: без addScriptToEvaluateOnNewDocument.
    assert not any(m == "Page.addScriptToEvaluateOnNewDocument" for m, _, _ in cdp.sent)
    assert any(m == "Page.createIsolatedWorld" for m, _, _ in cdp.sent)
    navs = [p["url"] for m, p, _ in cdp.sent if m == "Page.navigate"]
    assert navs == ["https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=1234567890"]
    # CDP-события сети не включаются: токен из трафика в Python не читается.
    assert not any(m.startswith("Network.") for m, _, _ in cdp.sent)
    # Python видит только описания запросов, без токена.
    assert gr.name  # имя мира/помощника случайное, в главный мир не попадает


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


def test_graph_call_is_evaluated_once_and_errors_become_a_stop(monkeypatch):
    # Ошибка CDP во время .call → ровно одна попытка .call и стоп, без повтора.
    page = FakePage([OK])
    call_evals = {"n": 0}

    class CallErrsOnceCdp(FakeCdp):
        def call(self, method, params=None, *, session_id="", timeout=5.0):
            if method == "Runtime.evaluate" and ".call(" in (params or {}).get("expression", ""):
                call_evals["n"] += 1
                from src.fb_api.cdp import CdpError
                raise CdpError("Runtime.evaluate: Inspected target navigated or closed")
            return super().call(method, params, session_id=session_id, timeout=timeout)

    cdp = CallErrsOnceCdp(page)
    tab = PageTab.open("http://127.0.0.1:1", connect=lambda u, timeout: cdp)
    gr = tc.InPageGraph(tab, sleep=lambda s: None)
    reply = gr.request("GET", "/me/adaccounts", {"fields": "id"})
    assert reply == {"error": "call_unknown"}
    assert call_evals["n"] == 1                       # отправлено не больше раза
    # GraphClient превращает это в стоп и больше не зовёт.
    client, _ = _client(_ReplayTransport([reply]))
    with pytest.raises(g.PageFailed):
        client.get("/me/adaccounts")


class _ReplayTransport:
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    def request(self, method, path, params=None, **kw):
        self.requests.append((method, path, dict(params or {})))
        return self.replies.pop(0)

    def state(self):
        return {"kind": "ads_manager", "has_token": True}

    def reacquire(self, *, timeout=60.0):
        return {"kind": "ads_manager", "has_token": True}


def test_stale_context_on_a_helper_call_is_retried_once_in_a_new_world():
    # Не-Meta вызов (.state) при исчезнувшем мире пересоздаётся и проходит.
    page = FakePage([], state={"kind": "ads_manager", "has_token": False})

    class StaleOnceCdp(FakeCdp):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self._stale_used = False

        def call(self, method, params=None, *, session_id="", timeout=5.0):
            if (method == "Runtime.evaluate" and ".state()" in (params or {}).get("expression", "")
                    and not self._stale_used):
                self._stale_used = True
                from src.fb_api.cdp import CdpError
                raise CdpError("Cannot find context with specified id")
            return super().call(method, params, session_id=session_id, timeout=timeout)

    cdp = StaleOnceCdp(page)
    tab = PageTab.open("http://127.0.0.1:1", connect=lambda u, timeout: cdp)
    gr = tc.InPageGraph(tab, sleep=lambda s: None)
    out = gr.state()                                   # пересоздал мир и вернул state
    assert out.get("kind") == "ads_manager"
    assert cdp.worlds >= 2                              # мир создан заново


def test_request_strips_tokens_from_the_graph_reply():
    # Вторая линия в Python: даже если помощник не вычистил, токена в ответе нет.
    page = FakePage([{"status": 200, "body": {"data": [{"id": ACT, "leak": "__TOKEN__"}],
                      "paging": {"next": "https://g/x?access_token=__TOKEN__"}}}])
    gr, _ = _graph(page)
    reply = gr.request("GET", "/me/adaccounts", {"fields": "id"})
    blob = json.dumps(reply)
    assert TOKEN not in blob and not TOKEN_LIKE.search(blob)


def test_helper_eval_strips_tokens_from_state():
    # Не-Meta путь (_eval_helper) тоже вычищает токены на стороне Python.
    page = FakePage([], state={"kind": "ads_manager", "has_token": True, "leak": TOKEN})
    gr, _ = _graph(page)
    blob = json.dumps(gr.state())
    assert TOKEN not in blob and not TOKEN_LIKE.search(blob)


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


def test_no_fetch_or_xhr_wrapping_in_sources():
    # Ревью r8: страницу не оборачиваем; помощник живёт в отдельном мире.
    helper = (ROOT / "src" / "fb_api" / "token_capture.py").read_text(encoding="utf-8")
    assert "window.fetch =" not in helper and "XHR.open =" not in helper, "no fetch/XHR wrapping"
    assert "addScriptToEvaluateOnNewDocument" not in helper, "no main-world init script"
    assert "createIsolatedWorld" in (ROOT / "src" / "fb_api" / "cdp.py").read_text(encoding="utf-8")


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
    scrub = tm.ScrubFilter()
    caplog.handler.addFilter(scrub)
    try:
        log.info("token %s act %s", TOKEN, ACT)
        assert TOKEN not in caplog.text and ACT not in caplog.text
    finally:
        # Обработчик caplog общий для файлов сессии: фильтр, оставшийся на нём,
        # прятал бы id профиля в чужих тестах (test_the_api_stop_is_asked_first).
        caplog.handler.removeFilter(scrub)


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

        return StartedProfile(profile_id, folder_id or "F", port=self.start_port,
                              newly_started=True)

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
    opened = prof.open_profile("P1", "F1", "A", launcher_factory=lambda i: launcher,
                               local_probe=lambda pid: ("none", None))
    assert opened.started_by_us is started_by_us
    assert launcher.started == ([] if state == "running" else ["P1"])
    assert prof.release_profile(opened) == release
    assert launcher.stopped == []


def test_hand_opened_profile_is_refused_and_left_alone(monkeypatch):
    launcher = FakeLauncher()
    monkeypatch.setattr(prof, "running_state", lambda l, p: ("running", None))
    with pytest.raises(prof.ProfileError):
        prof.open_profile("P1", "F1", "A", launcher_factory=lambda i: launcher,
                          local_probe=lambda pid: ("none", None))
    assert launcher.started == [] and launcher.stopped == []


def _no_launcher(instance):
    raise AssertionError("MLX must not be asked when the local process is found")


def test_local_process_with_port_is_attached_without_mlx():
    # Живой 5414: MLX /profile/running его не перечислял, start давал 400.
    opened = prof.open_profile("P1", "F1", "A", launcher_factory=_no_launcher,
                               local_probe=lambda pid: ("port", 63405))
    assert opened.port == "63405" and opened.started_by_us is False
    assert opened.cdp_url == "http://127.0.0.1:63405"
    assert prof.release_profile(opened) == "left_running"


def test_local_process_without_port_is_a_hand_opened_window():
    with pytest.raises(prof.ProfileError):
        prof.open_profile("P1", "F1", "A", launcher_factory=_no_launcher,
                          local_probe=lambda pid: ("no_port", None))


def test_no_local_process_falls_back_to_mlx(monkeypatch):
    launcher = FakeLauncher()
    monkeypatch.setattr(prof, "running_state", lambda l, p: ("not_running", None))
    opened = prof.open_profile("P1", "F1", "A", launcher_factory=lambda i: launcher,
                               local_probe=lambda pid: ("none", None))
    assert launcher.started == ["P1"] and opened.port == "7777"


class _Proc:
    def __init__(self, exe, cmdline):
        self.info = {"name": "chrome.exe", "exe": exe, "cmdline": cmdline}


MIMIC = "C:/Users/u/mlx/deps/mimic_154/chrome.exe"
PID = "550e8400-e29b-41d4-a716-446655440000"
OTHER = "another-profile"  # не UUID: сканер секретов разрешает только одну заглушку


def _udd(pid):
    return f"C:/Users/u/mlx/profiles/ws/folder/{pid}"


def test_local_debug_port_matches_only_this_profile():
    procs = [
        _Proc(MIMIC, ["chrome.exe", f"--user-data-dir={_udd(OTHER)}", "--remote-debugging-port=65442"]),
        _Proc(MIMIC, ["chrome.exe", f"--user-data-dir={_udd(PID)}", "--remote-debugging-port=63405"]),
        _Proc("C:/Program Files/Google/Chrome/chrome.exe",
              ["chrome.exe", f"--user-data-dir={_udd(PID)}", "--remote-debugging-port=9999"]),
    ]
    got = prof.local_debug_port(PID, process_iter=lambda: procs, accepting=lambda p: True,
                                listens=lambda proc, port: True)
    assert got == ("port", 63405)                  # не чужой 65442 и не обычный Chrome


def test_local_debug_port_requires_the_process_to_listen_on_the_port():
    procs = [_Proc(MIMIC, ["chrome.exe", f"--user-data-dir={_udd(PID)}", "--remote-debugging-port=63405"])]
    got = prof.local_debug_port(PID, process_iter=lambda: procs, accepting=lambda p: True,
                                listens=lambda proc, port: False)
    assert got == ("closing", 63405)              # порт не его — не цепляемся (профиль закрывается)


def test_local_debug_port_hand_opened_has_no_port():
    procs = [_Proc(MIMIC, ["chrome.exe", f"--user-data-dir={_udd(PID)}"])]
    got = prof.local_debug_port(PID, process_iter=lambda: procs, accepting=lambda p: True,
                                listens=lambda proc, port: True)
    assert got == ("no_port", None)


def test_local_debug_port_none_when_profile_not_running():
    procs = [_Proc(MIMIC, ["chrome.exe", f"--user-data-dir={_udd(OTHER)}", "--remote-debugging-port=65442"])]
    got = prof.local_debug_port(PID, process_iter=lambda: procs, accepting=lambda p: True,
                                listens=lambda proc, port: True)
    assert got == ("none", None)


def test_local_debug_port_does_not_match_a_prefix_of_the_profile_id():
    # Совпадение по ЦЕЛОМУ компоненту пути, не по подстроке.
    procs = [_Proc(MIMIC, ["chrome.exe", f"--user-data-dir={_udd(PID + 'x')}",
                           "--remote-debugging-port=63405"])]
    got = prof.local_debug_port(PID, process_iter=lambda: procs, accepting=lambda p: True,
                                listens=lambda proc, port: True)
    assert got == ("none", None)


FOCUS_STEALERS = ("Target.activateTarget", "Page.bringToFront")


def test_tab_open_never_activates_or_brings_to_front():
    # Ни фокуса, ни окна на экран (правило владельца): только наша вкладка.
    cdp = FakeCdp(FakePage([]))
    tab = PageTab.open("http://127.0.0.1:1", connect=lambda u, timeout: cdp)
    tab.close()
    assert not any(m in FOCUS_STEALERS for m, _, _ in cdp.sent)


def test_pilot_flow_never_activates_or_brings_to_front():
    from scripts import fb_api_pilot as pilot

    page = FakePage(_pilot_replies())
    cdp = FakeCdp(page)
    target = pilot.Target("KLD_TEST", ACT, "prof-uuid-1234", "fold", "A")
    pilot.run_pilot(
        target, emit=lambda s: None,
        open_profile=lambda *a: prof.OpenedProfile("p", "f", "A", "5555", None, False),
        release_profile=lambda o: "left_running",
        open_tab=lambda url: PageTab.open(url, connect=lambda u, timeout: cdp),
        make_client=lambda tr: g.GraphClient(tr, sleep=lambda s: None),
    )
    methods = [m for m, _, _ in cdp.sent]
    assert "Target.createTarget" in methods and "Target.closeTarget" in methods
    assert not any(m in FOCUS_STEALERS for m in methods)
    assert "Network.enable" not in methods and "Runtime.enable" not in methods


def test_local_debug_port_ignores_a_plain_chrome_with_the_right_profile_dir():
    # Обычный Chrome (не Mimic/mlx) с той же папкой и слушающим портом — не наш.
    procs = [_Proc("C:/Program Files/Google/Chrome/Application/chrome.exe",
                   ["chrome.exe", f"--user-data-dir={_udd(PID)}", "--remote-debugging-port=9222"])]
    got = prof.local_debug_port(PID, process_iter=lambda: procs, accepting=lambda p: True,
                                listens=lambda proc, port: True)
    assert got == ("none", None)


def test_loopback_accept_check():
    import socket

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        assert prof._accepting(port) is True
    finally:
        srv.close()
    assert prof._accepting(port) is False


def test_cdp_url_from_mlx_answer():
    assert prof.OpenedProfile("p", "f", "A", "5000", None, False).cdp_url == "http://127.0.0.1:5000"
    assert (prof.OpenedProfile("p", "f", "A", "5000", "devtools/browser/x", False).cdp_url
            == "http://127.0.0.1:5000/devtools/browser/x")


# ───────────────────────── пилот: сухой прогон, разрешение аккаунта ─────────────────────────

class _FailingGraph:
    """Граф, который валится на ожидании токена (как таймаут загрузки)."""

    def __init__(self, tab):
        self.tab = tab

    def install(self):
        pass

    def navigate(self, act_id, *, timeout=60.0):
        pass

    def wait_ready(self, *, timeout=90.0):
        raise CdpTimeout("wait_for_token: the browser did not answer within 90 s")


def test_any_failure_closes_our_own_tab():
    from scripts import fb_api_pilot as pilot

    page = FakePage([])
    cdp = FakeCdp(page, other_tabs=2)   # у браузера есть и другие вкладки
    target = pilot.Target("KLD_TEST", ACT, "prof-uuid-1234", "fold", "A")
    with pytest.raises(CdpTimeout) as ei:
        pilot.run_pilot(
            target, emit=lambda s: None,
            open_profile=lambda *a: prof.OpenedProfile("p", "f", "A", "5555", None, False),
            release_profile=lambda o: "left_running",
            open_tab=lambda url: PageTab.open(url, connect=lambda u, timeout: cdp),
            make_graph=lambda tab: _FailingGraph(tab),
        )
    # Наша вкладка закрыта, и только наша; шаг назван на исключении.
    assert cdp.closed_targets == ["T-mine"] and cdp.closed
    assert getattr(ei.value, "pilot_step", "").startswith("wait_for_token")


def test_last_browser_tab_is_not_closed_on_failure():
    from scripts import fb_api_pilot as pilot

    page = FakePage([])
    cdp = FakeCdp(page, other_tabs=0)   # наша вкладка — единственная
    target = pilot.Target("KLD_TEST", ACT, "p", "f", "A")
    with pytest.raises(CdpTimeout):
        pilot.run_pilot(
            target, emit=lambda s: None,
            open_profile=lambda *a: prof.OpenedProfile("p", "f", "A", "5555", None, False),
            release_profile=lambda o: "left_running",
            open_tab=lambda url: PageTab.open(url, connect=lambda u, timeout: cdp),
            make_graph=lambda tab: _FailingGraph(tab),
        )
    # Последнюю вкладку не закрываем: Chrome (и профиль MLX) закрылся бы.
    assert cdp.closed_targets == [] and cdp.closed


def test_tab_open_disables_background_throttling_on_our_session():
    page = FakePage([])
    cdp = FakeCdp(page)
    tab = PageTab.open("http://127.0.0.1:1", connect=lambda u, timeout: cdp)
    anti = [(m, sid) for m, _, sid in cdp.sent
            if m in ("Emulation.setFocusEmulationEnabled", "Page.setWebLifecycleState")]
    assert {m for m, _ in anti} == {"Emulation.setFocusEmulationEnabled", "Page.setWebLifecycleState"}
    # На НАШЕЙ сессии, не на сессии браузера (ревью r8).
    assert all(sid == "S-mine" for _, sid in anti) and anti
    assert tab.throttle == {"focus": True, "lifecycle": True}
    tab.close()


def test_wait_ready_polls_state_and_never_awaits_a_page_promise():
    # Опрос — это state(); обещание внутри страницы (.ready()) запрещено
    # (FakePage.evaluate упадёт на .ready(). Ревью r8, инцидент 5265).
    page = FakePage([], state={"kind": "ads_manager", "has_token": False})
    gr, _ = _graph(page)
    now = [0.0]
    gr._clock = lambda: now[0]
    gr._sleep = lambda s: now.__setitem__(0, now[0] + s)
    out = gr.wait_ready(timeout=3.0, poll_s=1.0)
    assert out.get("has_token") is False


def test_wait_ready_runs_final_probe_once_when_no_token():
    calls = {"state": 0, "probe": 0}

    class CountingGraph(tc.InPageGraph):
        def _eval_helper(self, tail, *, timeout=60.0):
            if ".probe()" in tail:
                calls["probe"] += 1
            elif ".state()" in tail:
                calls["state"] += 1
            return {"kind": "ads_manager", "has_token": False}

    tab, _ = _tab(FakePage([]))
    gr = CountingGraph(tab)
    now = [0.0]
    gr._clock = lambda: now[0]
    gr._sleep = lambda s: now.__setitem__(0, now[0] + s)
    gr.wait_ready(timeout=2.0, poll_s=1.0)
    assert calls["probe"] == 1 and calls["state"] >= 1


def test_wait_ready_propagates_cdp_timeout_with_the_method():
    class TimingOutGraph(tc.InPageGraph):
        def _eval_helper(self, tail, *, timeout=60.0):
            raise CdpTimeout("Runtime.evaluate: the browser did not answer within 10 s")

    tab, _ = _tab(FakePage([]))
    gr = TimingOutGraph(tab)
    with pytest.raises(CdpTimeout) as ei:
        gr.wait_ready(timeout=5.0)
    assert "Runtime.evaluate" in str(ei.value)


def _tab_with_stuck_socket(other_tabs, list_pages):
    page = FakePage([])

    class StuckCdp(FakeCdp):
        def call(self, method, params=None, *, session_id="", timeout=5.0):
            if method == "Target.getTargets":
                raise CdpTimeout("Target.getTargets: the browser did not answer within 10 s")
            return super().call(method, params, session_id=session_id, timeout=timeout)

    cdp = StuckCdp(page, other_tabs=other_tabs)
    tab = PageTab.open("http://127.0.0.1:9321", connect=lambda u, timeout: cdp)
    closed = []
    tab._http = staticmethod(lambda host, port, path, *, timeout=10.0:
                             list_pages if path == "/json/list"
                             else closed.append(path.rsplit("/", 1)[1]))
    return tab, closed


def test_http_close_does_nothing_when_our_tab_is_the_only_page():
    tab, closed = _tab_with_stuck_socket(0, [{"type": "page", "id": "T-mine"}])
    tab.close()
    assert closed == []                                # последнюю вкладку не трогаем


def test_http_close_does_nothing_when_our_tab_is_absent():
    tab, closed = _tab_with_stuck_socket(1, [{"type": "page", "id": "T-0"},
                                             {"type": "page", "id": "T-1"}])
    tab.close()
    assert closed == []                                # нашей цели нет — ничего не закрываем


def test_http_close_closes_only_our_id_even_when_not_first():
    tab, closed = _tab_with_stuck_socket(1, [{"type": "page", "id": "T-0"},
                                             {"type": "page", "id": "T-mine"}])
    tab.close()
    assert closed == ["T-mine"]                        # не pages[0], а именно наша


def test_isolated_world_does_not_grant_universal_access():
    page = FakePage([], state={"kind": "ads_manager", "has_token": False})
    tab, cdp = _tab(page)
    gr = tc.InPageGraph(tab, sleep=lambda s: None)
    gr._ensure()
    worlds = [p for m, p, _ in cdp.sent if m == "Page.createIsolatedWorld"]
    assert worlds and all(w.get("grantUniveralAccess") is False for w in worlds)


class _PausedTargetCdp(FakeCdp):
    """Новая цель стартует НА ПАУЗЕ (waitForDebuggerOnStart): рендерер молчит
    на Page.enable, пока на нашей сессии не придёт runIfWaitingForDebugger."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.released = False

    def call(self, method, params=None, *, session_id="", timeout=5.0):
        if method == "Runtime.runIfWaitingForDebugger" and session_id == "S-mine":
            self.released = True
        if method == "Page.enable" and not self.released:
            raise CdpTimeout("Page.enable: the browser did not answer within 10 s")
        return super().call(method, params, session_id=session_id, timeout=timeout)


def test_open_releases_a_paused_target_before_page_enable():
    # Живой пилот 5414: Page.enable висел. Отпускаем паузу, как Playwright.
    cdp = _PausedTargetCdp(FakePage([]))
    tab = PageTab.open("http://127.0.0.1:1", connect=lambda u, timeout: cdp)
    methods = [(m, sid) for m, _, sid in cdp.sent]
    run = methods.index(("Runtime.runIfWaitingForDebugger", "S-mine"))
    enable = methods.index(("Page.enable", "S-mine"))
    assert run < enable                                 # сначала отпустить, потом Page.enable
    assert ("Runtime.enable", "S-mine") not in methods  # домен Runtime НЕ включаем
    tab.close()


def test_create_target_is_not_background_like_playwright():
    # Ревьюер на настоящем Chrome: background:true → Page.enable не отвечает
    # никогда. Шлём то же, что Playwright: {url:"about:blank"}, без background.
    cdp = FakeCdp(FakePage([]))
    tab = PageTab.open("http://127.0.0.1:1", connect=lambda u, timeout: cdp)
    created = [p for m, p, _ in cdp.sent if m == "Target.createTarget"]
    assert created and not created[0].get("background", False)
    assert created[0].get("url") == "about:blank"
    methods = [(m, sid) for m, _, sid in cdp.sent]
    assert (methods.index(("Runtime.runIfWaitingForDebugger", "S-mine"))
            < methods.index(("Page.enable", "S-mine")))
    tab.close()


def test_open_uses_one_browser_websocket_and_a_flatten_session():
    # Один websocket браузера; отдельный websocket страницы (/devtools/page/…)
    # не открываем — все команды вкладки идут с sessionId нашей flatten-сессии.
    connects = []

    def connect(url, timeout):
        connects.append(url)
        if "/devtools/page/" in url:
            raise TimeoutError("page websocket never answers")
        return FakeCdp(FakePage([]))

    tab = PageTab.open("http://127.0.0.1:9321", connect=connect)
    assert connects == ["http://127.0.0.1:9321"]        # ровно одно подключение — к браузеру
    attach = [p for m, p, _ in tab._cdp.sent if m == "Target.attachToTarget"]
    assert attach and attach[0].get("flatten") is True
    tab_cmds = [(m, sid) for m, _, sid in tab._cdp.sent
                if m in ("Runtime.runIfWaitingForDebugger", "Page.enable",
                         "Emulation.setFocusEmulationEnabled", "Page.setWebLifecycleState")]
    assert tab_cmds and all(sid == "S-mine" for _, sid in tab_cmds)
    tab.close()


def test_page_tab_call_wraps_raw_timeout_with_the_method_name():
    page = FakePage([])

    class SlowCdp(FakeCdp):
        def call(self, method, params=None, *, session_id="", timeout=5.0):
            if method == "Page.getFrameTree":
                raise TimeoutError("CDP read timed out")  # сырой клиент: без имени метода
            return super().call(method, params, session_id=session_id, timeout=timeout)

    tab = PageTab.open("http://127.0.0.1:1", connect=lambda u, timeout: SlowCdp(page))
    with pytest.raises(CdpTimeout) as ei:
        tab.main_frame_id()
    assert "Page.getFrameTree" in str(ei.value)
    tab.close()


def test_close_falls_back_to_http_when_the_socket_times_out():
    page = FakePage([])

    class StuckCdp(FakeCdp):
        def call(self, method, params=None, *, session_id="", timeout=5.0):
            if method == "Target.getTargets":  # сокет завис на закрытии
                raise CdpTimeout("Target.getTargets: the browser did not answer within 10 s")
            return super().call(method, params, session_id=session_id, timeout=timeout)

    cdp = StuckCdp(page, other_tabs=1)
    tab = PageTab.open("http://127.0.0.1:9321", connect=lambda u, timeout: cdp)
    http = {"listed": False, "closed": None}

    def fake_http(host, port, path, *, timeout=10.0):
        if path == "/json/list":
            http["listed"] = True
            return [{"type": "page", "id": "T-mine"}, {"type": "page", "id": "T-0"}]
        if path.startswith("/json/close/"):
            http["closed"] = path.rsplit("/", 1)[1]
        return ""

    tab._http = staticmethod(fake_http)
    tab.close()
    assert http["listed"] is True and http["closed"] == "T-mine"


def test_close_catches_keyboard_interrupt_and_still_tries_http():
    page = FakePage([])

    class InterruptingCdp(FakeCdp):
        def call(self, method, params=None, *, session_id="", timeout=5.0):
            if method == "Target.getTargets":
                raise KeyboardInterrupt()
            return super().call(method, params, session_id=session_id, timeout=timeout)

    cdp = InterruptingCdp(page, other_tabs=1)
    tab = PageTab.open("http://127.0.0.1:9321", connect=lambda u, timeout: cdp)
    tried = {"closed": None}
    tab._http = staticmethod(lambda host, port, path, *, timeout=10.0:
                             [{"type": "page", "id": "T-mine"}, {"type": "page", "id": "T-0"}]
                             if path == "/json/list"
                             else tried.__setitem__("closed", path.rsplit("/", 1)[1]))
    with pytest.raises(KeyboardInterrupt):
        tab.close()
    assert tried["closed"] == "T-mine"


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
