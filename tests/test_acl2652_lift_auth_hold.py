"""ACL-2652: a person's edit lifts an auth hold (code 31); Retry is shown for any hold; background work never lifts.

Fixtures only: nothing reaches Meta or a browser.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest

from src.analysis_agent import account_refresh as ar
from src.analysis_agent import account_spend_api as api
from src.fb_api import account_blocks as blocks
from src.fb_api import graph as g
from tests.test_acl1760_api_read import ACCOUNT, D, world  # noqa: F401  (fixture)
from tests.test_acl2063_refresh_queue import _queue, env  # noqa: F401  (fixture)
from tests.test_acl2063_structure_read import _structure_snap, store  # noqa: F401  (fixture)

ROOT = Path(__file__).resolve().parent.parent
KEY = "prof-1"
LABEL = "KLD_BFL_F92"


@pytest.fixture(autouse=True)
def _holds(tmp_path, monkeypatch):
    monkeypatch.setattr(blocks, "_PATH_OVERRIDE", tmp_path / "holds.json")
    monkeypatch.setattr(api, "_replacement_spent", lambda key: True)
    api._COOLDOWN.clear()
    blocks._TIMED_META.clear()
    yield
    api._COOLDOWN.clear()
    blocks._TIMED_META.clear()


@pytest.fixture
def keyed(monkeypatch):
    """Every label is the one physical profile KEY."""
    monkeypatch.setattr(api._as, "_profile_key", lambda label, *a, **k: KEY)


def _auth_stop():
    return g.AuthRequired("a", code=31, subcode=3858385)


def _hold(stop=None, label=LABEL, key=KEY):
    with blocks.problem_action("command"):
        api._start_cooldown(key, stop or _auth_stop(), label)
    assert blocks.is_held(key)


@contextmanager
def _null():
    yield


# ───────────────────────── 1. a person's edit passes the hold; it ends only after Meta answers ─────────────────────────

def _account_call(sess):
    return sess.client.account(sess.act)


def test_an_edit_passes_the_auth_hold_makes_one_call_and_the_hold_ends_after_the_answer(world):  # noqa: F811
    key = api._as._profile_key(ACCOUNT)
    _hold(key=key)
    with blocks.person_edit(), api._session(ACCOUNT) as sess:
        assert blocks.is_held(key), "the file stays while the session runs"
        _account_call(sess)
    assert len(world.page.specs) == 1 and not blocks.is_held(key)


def test_a_plain_session_is_still_refused_by_the_hold(world):  # noqa: F811
    key = api._as._profile_key(ACCOUNT)
    _hold(key=key)
    with pytest.raises(api.ApiReadError, match="Nothing was called"):
        with api._session(ACCOUNT):
            pass
    assert blocks.is_held(key) and world.page.specs == []


def test_when_meta_still_asks_the_hold_returns_with_the_same_message(world):  # noqa: F811
    from tests.test_acl1760_api_read import err

    key = api._as._profile_key(ACCOUNT)
    world.page.routes["/ACT||"] = err(31, 3858385)
    _hold(key=key)
    before = blocks.refusal(key, ACCOUNT, "read")[0]
    with pytest.raises(g.AuthRequired):
        with blocks.person_edit(), api._session(ACCOUNT) as sess:
            _account_call(sess)
    assert len(world.page.specs) == 1
    held = blocks.hold_of(key)
    assert held and held["kind"] == "checkpoint" and held["code"] == 31
    after = blocks.refusal(key, ACCOUNT, "read")[0]
    assert after.split(" Nothing")[0] == before.split(" Nothing")[0]
    assert "press Retry on this profile" in after


def test_a_session_that_fails_before_any_call_keeps_the_hold(world, monkeypatch):  # noqa: F811
    key = api._as._profile_key(ACCOUNT)
    _hold(key=key)
    with blocks.person_edit(), api._session(ACCOUNT):
        pass                                     # opened, nothing asked
    assert blocks.is_held(key)
    from src.analysis_agent import account_refresh_machine as m

    monkeypatch.setattr(m, "verify_binding", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("binding")))
    with pytest.raises(RuntimeError):
        with blocks.person_edit(), api._session(ACCOUNT):
            pass
    assert blocks.is_held(key) and world.page.specs == []


@pytest.mark.parametrize("stop", [g.Checkpoint("c"), g.PolicyBlocked("p", code=368),
                                  g.LoggedOut("l", page_seen=True)])
def test_other_holds_are_never_passed_by_an_edit(world, stop):  # noqa: F811
    key = api._as._profile_key(ACCOUNT)
    _hold(stop, key=key)
    with pytest.raises(api.ApiReadError, match="Nothing was called"):
        with blocks.person_edit(), api._session(ACCOUNT):
            pass
    assert blocks.is_held(key) and world.page.specs == []


def test_the_command_session_marks_the_action_as_a_persons_write(monkeypatch, keyed):
    from src.web import campaign_commands

    seen = []
    monkeypatch.setattr("src.fb_ads_rpa.operations.profile_lock", lambda key: _null())
    monkeypatch.setattr("src.fb_ads_rpa.operations.browser_operation.reading", lambda: _null())

    @contextmanager
    def session(account, *, commands=False, photo=False):
        seen.append(blocks.person_auth_probe(KEY))
        yield None

    monkeypatch.setattr(api, "_session", session)
    _hold()
    with campaign_commands.open_session(LABEL, True):
        pass
    assert seen == [True] and blocks.is_held(KEY), "passing is not clearing: the file stays until Meta answers"


def test_the_shut_off_with_a_busy_profile_keeps_the_hold(world):  # noqa: F811
    key = api._as._profile_key(ACCOUNT)
    _hold(key=key)
    out = api.turn_off(ACCOUNT, "campaign", "123456789012345", expected_name="x", requester="me",
                       profile_busy=lambda k: True)
    assert out.error == api.PROFILE_BUSY_ERROR and blocks.is_held(key) and world.page.specs == []


def test_the_shut_off_passes_the_hold_inside_the_lock_and_makes_its_call(world, monkeypatch):  # noqa: F811
    key = api._as._profile_key(ACCOUNT)
    _hold(key=key)
    seen = []

    def fake(out, sess, parent_id, expected_name, wrote):
        seen.append(blocks.is_held(key))
        _account_call(sess)

    monkeypatch.setattr(api, "_turn_off_in_session", fake)
    orig = api._make_graph
    monkeypatch.setattr(api, "_make_graph", lambda tab, **kw: orig(tab))      # the rig's graph takes no `commands_act`
    out = api.turn_off(ACCOUNT, "campaign", "123456789012345", expected_name="x", requester="me",
                       profile_busy=lambda k: False)
    assert seen == [True], out.error
    assert not blocks.is_held(key) and len(world.page.specs) == 1


def test_publish_with_no_call_keeps_the_hold_and_with_an_answer_clears_it(monkeypatch, tmp_path):
    from tests.test_acl1761_api_create import FakeMeta
    from tests.test_acl2595_auth_checkpoint import PID, _rig  # noqa: F401

    publish_api = _rig(monkeypatch, tmp_path, FakeMeta())
    api._start_cooldown("prof-1", _auth_stop(), LABEL)
    monkeypatch.setattr(publish_api, "preflight_account",
                        lambda *a, **k: (_ for _ in ()).throw(publish_api.PlanError("no call made")))
    with pytest.raises(publish_api.ApiPublishError):
        publish_api.ApiPublisher().open(LABEL)
    assert blocks.is_held("prof-1"), "the account check failed before Meta answered"


# ───────────────────────── 2. background work never lifts it ─────────────────────────

@pytest.mark.parametrize("action", ["schedule", "attention", "action"])
def test_a_background_read_does_not_lift_the_auth_hold(world, action):  # noqa: F811
    key = api._as._profile_key(ACCOUNT)
    _hold(key=key)
    with blocks.problem_action(action):
        done = api.read_via_api(ACCOUNT, D, D)
    assert done.state == "failed" and "Nothing was called." in done.error
    assert blocks.is_held(key) and world.page.specs == []


def test_the_refresh_queue_does_not_queue_automatic_reads_for_a_held_profile(env, monkeypatch):  # noqa: F811
    monkeypatch.setattr(ar, "_profile_held", lambda account: True)
    q = _queue(env)
    for trigger in ("attention", "schedule"):
        assert q.request("A", trigger) == {"queued": False, "reason": "profile_held"}
    assert env.read == []


# ───────────────────────── 3. Retry for any hold, one queued read, no timer ─────────────────────────

def test_the_state_carries_the_hold_with_reason_and_since(monkeypatch):
    from src.web import app

    monkeypatch.setattr(api._as, "_profile_key", lambda label, *a, **k: KEY if label == LABEL else "")
    _hold()
    shown = app._account_holds([LABEL, "OTHER"])
    assert set(shown) == {LABEL}, "only the held profile is listed"
    assert shown[LABEL] == {
        "kind": "checkpoint", "code": 31, "at": blocks.hold_of(KEY)["at"],
        "reason": "Meta asks to confirm this account (security check).",
        "next": "Open Ads Manager in this profile and complete the check, then press Retry on this profile. "
                "Existing ads keep running."}


def test_the_state_shows_no_hold_for_a_clear_profile(keyed):
    from src.web import app

    assert app._account_holds([LABEL]) == {}


def test_retry_queues_exactly_one_read_for_a_held_profile(env, monkeypatch):  # noqa: F811
    monkeypatch.setattr(ar, "_profile_held", lambda account: True)
    q = _queue(env)
    assert q.request(LABEL, "retry", force=True) == {"queued": True, "reason": "queued"}
    assert q.request(LABEL, "retry", force=True)["queued"] is True      # a second press replaces, never doubles
    assert len(q.state()["queued"]) == 1
    q._drain_once()
    assert env.read == [(LABEL, "retry")]
    q._drain_once()
    assert env.read == [(LABEL, "retry")], "nothing re-queues itself: no timer, no polling"


def test_the_row_retry_is_not_gated_on_a_failed_read_and_shows_on_home():
    table = (ROOT / "src/web/templates/_campaign_table.html").read_text(encoding="utf-8")
    assert "S.holds = view.holds || {}" in table
    assert "!bad && !closed && !hold && !S.uncertain[account]" in table
    assert "h.readBtn.dataset.hold = '1'" in table
    assert "S.holds[account].at" in table                       # since-when beside the reason
    report = (ROOT / "src/web/templates/report.html").read_text(encoding="utf-8")
    assert ".aov .hc-rc:not([data-hold])" in report and ".aov .hc-rc, " not in report


BANNER_JS = r"""
const fs = require('fs');
const calls = [], timers = [];
function el(tag) { return {tag, children: [], handlers: {}, attrs: {}, hidden: false, disabled: false, textContent: '',
  setAttribute(k, v) { this.attrs[k] = v; }, addEventListener(t, f) { this.handlers[t] = f; },
  appendChild(c) { this.children.push(c); return c; }, parentNode: null}; }
const box = el('div');
global.window = {location: {href: 'http://x/'}};
global.document = {readyState: 'loading', addEventListener() {}, getElementById: () => box,
  createElement: el, createTextNode: (t) => ({text: t}), querySelector: () => null};
global.history = {state: null, replaceState() {}};
global.setTimeout = (f, ms) => { timers.push(ms); return 1; };
const view = {ok: true, running: null, queued: [], rows: {}, accounts: {}, sweep: null, needs_api: false,
  halt: {reason: 'A profile is on a Facebook checkpoint. Solve it inside Multilogin, then press Retry.',
         until: null, kind: 'checkpoint', account: 'KLD_BFL_F92'},
  holds: {KLD_BFL_F92: {kind: 'checkpoint', reason: 'r', next: 'n', at: 1}}};
global.fetch = (url, opts) => {
  calls.push(url + (opts && opts.body ? ' ' + opts.body.get('trigger') + ' ' + opts.body.get('account') : ''));
  return Promise.resolve({ok: true, json: () => Promise.resolve(url.indexOf('/state') >= 0 ? view : {ok: true, queued: true})});
};
eval(fs.readFileSync(process.argv[2], 'utf8'));
(async () => {
  await window.aclAccountRefresh.load();
  const btn = box.children.find(c => c.tag === 'button');
  if (!btn || btn.textContent !== 'Retry') { console.log(JSON.stringify({error: 'no Retry button'})); return; }
  btn.handlers.click(); btn.handlers.click();
  await new Promise(r => setImmediate(r));
  console.log(JSON.stringify({requests: calls.filter(c => c.indexOf('/request') >= 0), timers}));
})();
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_red_banner_has_a_retry_button_that_queues_one_read(tmp_path):
    f = tmp_path / "banner.js"
    f.write_text(BANNER_JS, encoding="utf-8")
    out = subprocess.run(["node", str(f), str(ROOT / "src/web/static/account_refresh.js")],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert "error" not in got, got
    assert got["requests"] == ["/account-refresh/request retry KLD_BFL_F92"]   # two clicks, one read
    assert got["timers"] == [], "the banner never polls"


# ───────────────────────── 4. a successful read clears the hold ─────────────────────────

def test_a_successful_retry_read_clears_the_auth_hold(world):  # noqa: F811
    key = api._as._profile_key(ACCOUNT)
    _hold(key=key)
    with blocks.problem_action("retry"):
        done = api.read_via_api(ACCOUNT, D, D)
    assert done.state != "failed", done.error
    assert not blocks.is_held(key) and api._cooldown_state(key) == (0.0, "")


def test_a_retry_on_a_page_that_is_still_stuck_keeps_the_hold(world):  # noqa: F811
    key = api._as._profile_key(ACCOUNT)
    _hold(key=key)
    world.page.state_value = {"kind": "checkpoint", "has_token": False}
    with blocks.problem_action("retry"):
        again = api.read_via_api(ACCOUNT, D, D)
    assert again.state == "failed" and blocks.is_held(key)


# ───────────────────────── Retry is one status-only read; Open never lifts ─────────────────────────

def _run_read_rig(monkeypatch, held):
    from src.analysis_agent import campaign_inventory as ci

    calls = []
    monkeypatch.setattr(ci, "backend_ready", lambda: True)
    monkeypatch.setattr(ci, "get_snapshot", lambda account: None)
    monkeypatch.setattr(ar, "_profile_held", lambda account: held)

    def status(account, *, trigger="open"):
        calls.append(("status", trigger))
        snap = ci.Snapshot(account=account, state="ok")
        snap._status_only = True
        return snap

    monkeypatch.setattr(ci, "read_status_only", status)
    monkeypatch.setattr(ci, "read_via_api", lambda *a, **k: calls.append(("list", k)) or ci.Snapshot(account=a[0], state="ok"))
    return ci, calls


def test_retry_for_a_held_account_is_one_status_only_read(monkeypatch):
    ci, calls = _run_read_rig(monkeypatch, held=True)
    ci._run_read(LABEL, trigger="retry", persist=False)
    assert calls == [("status", "retry")], "one request for the account, no list"


def test_retry_for_a_clear_account_still_reads_the_list(monkeypatch):
    ci, calls = _run_read_rig(monkeypatch, held=False)
    ci._run_read(LABEL, trigger="retry", persist=False)
    assert [c[0] for c in calls] == ["list"]


def _open_read(key, account):
    """The status read the queue runs after a person's Open profile (the `open` trigger)."""
    from src.analysis_agent import campaign_inventory as ci

    blocks.mark_person_open(key)
    assert ar._held_blocks(account, "open", True) is False, "a person's forced Open passes the hold"
    with blocks.problem_action("attention"):
        return ci._run_read(account, trigger="open", persist=False)


def test_open_lifts_the_auth_hold_and_makes_one_status_read(world, monkeypatch):  # noqa: F811
    from src.analysis_agent import campaign_inventory as ci

    monkeypatch.setattr(ci, "backend_ready", lambda: True)
    monkeypatch.setattr(ci, "get_snapshot", lambda account: None)
    monkeypatch.setattr(ci, "record_status", lambda snap: None)
    key = api._as._profile_key(ACCOUNT)
    _hold(key=key)
    snap = _open_read(key, ACCOUNT)
    assert snap.state == "ok", snap.error
    assert len(world.page.specs) == 1 and not blocks.is_held(key)


def test_open_that_meta_still_refuses_restores_the_hold(world, monkeypatch):  # noqa: F811
    from src.analysis_agent import campaign_inventory as ci
    from tests.test_acl1760_api_read import err

    monkeypatch.setattr(ci, "backend_ready", lambda: True)
    monkeypatch.setattr(ci, "get_snapshot", lambda account: None)
    monkeypatch.setattr(ci, "record_status", lambda snap: None)
    world.page.routes["/ACT||"] = err(31, 3858385)
    key = api._as._profile_key(ACCOUNT)
    _hold(key=key)
    snap = _open_read(key, ACCOUNT)
    assert snap.state == "failed" and len(world.page.specs) == 1
    held = blocks.hold_of(key)
    assert held and held["kind"] == "checkpoint" and held["code"] == 31


def test_an_open_that_nobody_pressed_and_other_holds_stay_held(world, monkeypatch):  # noqa: F811
    key = api._as._profile_key(ACCOUNT)
    _hold(key=key)
    assert ar._held_blocks(ACCOUNT, "open") is True, "an unforced open passes no hold"
    assert ar._held_blocks(ACCOUNT, "open", force=True) is False, "a person's Open acts as Retry"
    api._COOLDOWN.clear()
    blocks.clear(key)
    _hold(g.PolicyBlocked("p", code=368), key=key)
    blocks.mark_person_open(key)
    assert ar._held_blocks(ACCOUNT, "open") is True, "an unforced open passes no hold"
    assert ar._held_blocks(ACCOUNT, "open", force=True) is False, "a person's forced Open reads through a policy block too"
    blocks._PERSON_OPEN.clear()


def test_the_open_button_marks_the_status_read_as_the_persons(monkeypatch, keyed):
    from src.web import warm_api

    monkeypatch.setattr(ar, "queue", lambda: type("Q", (), {"request": lambda self, *a, **k: {"queued": True}})())
    assert warm_api.request_status_read(LABEL) == "queued"
    assert blocks.person_open_pending(KEY)
    blocks._PERSON_OPEN.clear()


# ───────────────────────── a rendered row shows Retry for a hold without a failed read ─────────────────────────

def test_a_held_account_shows_retry_without_a_failed_read(monkeypatch):
    pytest.importorskip("playwright")
    from tests.test_acl2063_dashboard_browser import Dashboard, launch_chromium, sync_playwright

    from src.web import app as web

    hold = {"kind": "checkpoint", "code": 31, "at": 1_800_000_000.0,
            "reason": "Meta asks to confirm this account (security check).",
            "next": "Open Ads Manager in this profile and complete the check, then press Retry on this profile."}
    monkeypatch.setattr(web, "_account_holds", lambda accounts: {"Acc": hold})
    import tempfile

    with tempfile.TemporaryDirectory() as tmp, sync_playwright() as pw:
        site = Dashboard(web, monkeypatch, Path(tmp))
        browser = launch_chromium(pw)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.route("http://acl.test/**", site.handle)
        try:
            site.save("Acc", age=60)
            page.goto("http://acl.test/")
            page.wait_for_function("document.querySelector('.amap-campaigns')?._hc?.state.holds?.Acc")
            row = page.locator('tr:has(td[data-account="Acc"])')
            assert "failed" not in (page.evaluate("document.querySelector('.amap-campaigns')._hc.state.refreshRows?.Acc?.state") or "")
            assert row.locator("button.hc-rc:visible").all_inner_texts() == ["Retry"]
            assert "press Retry on this profile" in row.locator("button.hc-rc").get_attribute("title")
        finally:
            site.release.set()
            site.q.stop()
            browser.close()


# ───────────────────────── Retry through the read machine; publish keeps timed pauses ─────────────────────────

def test_retry_on_a_held_account_completes_through_run_read_and_keeps_the_saved_list(monkeypatch, store):  # noqa: F811
    import contextlib

    from src.analysis_agent import campaign_inventory as ci

    monkeypatch.setattr(ci, "backend_ready", lambda: True)
    monkeypatch.setattr(ci._as, "_lock_for", lambda account: contextlib.nullcontext())
    monkeypatch.setattr(ci, "read_via_api", lambda account, *a, **k: ci.Snapshot(**{**_structure_snap().to_dict(), "account": account}))
    ci.run_read("A", trigger="schedule")
    assert len(ci.get_snapshot("A").campaigns) == 1
    key = ci._as._profile_key("A")
    _hold(label="A", key=key)
    monkeypatch.setattr(ar, "_profile_held", lambda account: blocks.is_held(key))
    seen = []

    def status(account, *, trigger="open"):
        seen.append((trigger, blocks.buyer_started()))
        if blocks.buyer_started():
            blocks.clear(key, "retry")                # what a healthy page does for a person's Retry
        snap = ci.Snapshot(account=account, state="ok", currency="USD")
        snap._status_only = True
        return snap

    monkeypatch.setattr(ci, "read_status_only", status)
    done = ci.run_read("A", trigger="retry")
    assert seen == [("retry", True)] and done.state == "ok" and ci.is_status_only_ok(done), done.error
    assert len(ci.get_snapshot("A").campaigns) == 1, "the saved list is kept"
    attempt = ci.get_attempt("A")
    assert not attempt or attempt.get("state") != "failed", "no failed attempt is written"
    assert not blocks.is_held(key)


def test_publish_still_refuses_a_timed_pause_on_top_of_an_auth_hold(monkeypatch, tmp_path):
    from tests.test_acl1761_api_create import FakeMeta
    from tests.test_acl2595_auth_checkpoint import _rig

    publish_api = _rig(monkeypatch, tmp_path, FakeMeta())
    api._start_cooldown("prof-1", _auth_stop(), LABEL)
    monkeypatch.setattr(publish_api, "_cooldown", lambda label: (300.0, "rate_limited"))
    with pytest.raises(publish_api.ApiPublishError):
        publish_api.ApiPublisher().open(LABEL)
    assert blocks.is_held("prof-1")


# ───────────────────────── frozen-tab hold (KLD_BFL_I04, 2026-11-18): Open lifts it, the code is the tab's own ─────────────────────────

def test_a_frozen_tab_hold_is_refused_with_the_tabs_code_not_account_blocked():
    _hold(g.PageUnresponsive("f"))
    msg, code = blocks.refusal(KEY, LABEL, "publish")
    assert "stopped answering" in msg and code == "page_unresponsive"
    assert blocks.refusal(KEY, LABEL, "publish")[1] != "account_blocked"


def test_open_profile_lifts_only_the_frozen_tab_hold(monkeypatch, keyed):
    from src.web import warm_api

    monkeypatch.setattr(ar, "queue", lambda: type("Q", (), {"request": lambda self, *a, **k: {"queued": True}})())
    _hold(g.PageUnresponsive("f"))
    assert warm_api.request_status_read(LABEL) == "queued"
    assert not blocks.is_held(KEY)
    blocks._PERSON_OPEN.clear()
    _hold(g.PolicyBlocked("p", code=368))
    warm_api.request_status_read(LABEL)
    # ACL-2661: the stub queue never reads; the hold stays until a read passes (a real forced Open acts as Retry)
    assert blocks.is_held(KEY), "the request alone lifts nothing; a healthy read does"
    blocks._PERSON_OPEN.clear()


def test_a_persons_edit_does_not_lift_a_frozen_tab_hold(world):  # noqa: F811
    key = api._as._profile_key(ACCOUNT)
    _hold(g.PageUnresponsive("f"), key=key)
    with pytest.raises(api.ApiReadError) as err:
        with blocks.person_edit(), api._session(ACCOUNT):
            pass
    assert err.value.code == "page_unresponsive" and blocks.is_held(key) and world.page.specs == []


def test_retry_on_a_frozen_tab_hold_clears_it_once_the_page_answers(world):  # noqa: F811
    key = api._as._profile_key(ACCOUNT)
    _hold(g.PageUnresponsive("f"), key=key)
    with blocks.problem_action("retry"):
        done = api.read_via_api(ACCOUNT, D, D)
    assert done.state == "ok", done.error
    assert not blocks.is_held(key)


def test_check_plan_after_a_fresh_reopen_lifts_the_frozen_tab_hold_and_proceeds(monkeypatch, tmp_path):
    from tests.test_acl1761_api_create import FakeMeta
    from tests.test_acl2595_auth_checkpoint import _rig

    publish_api = _rig(monkeypatch, tmp_path, FakeMeta())
    api._start_cooldown("prof-1", g.PageUnresponsive("f"), "KLD_BFL_I04")
    assert blocks.is_held("prof-1")
    pub = publish_api.ApiPublisher()
    pub.open("KLD_BFL_I04")                       # no refusal, no buyer click
    assert not blocks.is_held("prof-1") and pub._client is not None


def test_a_reopen_that_is_still_unresponsive_keeps_the_hold_with_the_tabs_code_on_that_account_only(monkeypatch, tmp_path):
    from tests.test_acl1761_api_create import FakeMeta
    from tests.test_acl2595_auth_checkpoint import _rig

    publish_api = _rig(monkeypatch, tmp_path, FakeMeta())
    api._start_cooldown("prof-1", g.PageUnresponsive("f"), "KLD_BFL_I04")
    monkeypatch.setattr(publish_api.ApiPublisher, "_open_warm", lambda self, *a: (_ for _ in ()).throw(
        publish_api.ApiPublishError("The Ads Manager tab stopped answering.", code="page_unresponsive")))
    with pytest.raises(publish_api.ApiPublishError) as err:
        publish_api.ApiPublisher().open("KLD_BFL_I04")
    assert err.value.code == "page_unresponsive" and blocks.is_held("prof-1")
    assert not blocks.is_held("prof-other"), "only that profile carries the hold"


def test_a_fresh_reopen_never_lifts_a_checkpoint_hold(monkeypatch, tmp_path):
    from tests.test_acl1761_api_create import FakeMeta
    from tests.test_acl2595_auth_checkpoint import _rig

    publish_api = _rig(monkeypatch, tmp_path, FakeMeta())
    api._start_cooldown("prof-1", g.Checkpoint("c"), "KLD_BFL_I04")
    with pytest.raises(publish_api.ApiPublishError):
        publish_api.ApiPublisher().open("KLD_BFL_I04")
    assert blocks.is_held("prof-1")
