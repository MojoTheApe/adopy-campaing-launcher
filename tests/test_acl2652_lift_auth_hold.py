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


class _Meta:
    """Transport that counts calls; `code31` answers every call with 31/3858385."""

    def __init__(self, code31=False):
        self.calls, self.code31 = 0, code31

    def request(self, method, path, params=None):
        self.calls += 1
        if self.code31:
            return {"status": 400, "body": {"error": {"code": 31, "error_subcode": 3858385, "message": "m",
                                                      "error_user_title": "Please authenticate your account"}}}
        return {"status": 200, "body": {"data": []}}

    def state(self):
        return {"kind": "ads_manager", "has_token": True}


@contextmanager
def _null():
    yield


def _rig_commands(monkeypatch, meta, seen):
    """campaign_commands.open_session over a fake `_session`: it records the hold at entry, hands out a real
    GraphClient and, like `_open_session`, notes the client's stop afterwards."""
    from src.web import campaign_commands

    monkeypatch.setattr("src.fb_ads_rpa.operations.profile_lock", lambda key: _null())
    monkeypatch.setattr("src.fb_ads_rpa.operations.browser_operation.reading", lambda: _null())

    @contextmanager
    def session(account, *, commands=False, photo=False):
        seen.append(blocks.is_held(KEY))
        client = g.GraphClient(meta, sleep=lambda s: None, min_interval_s=0.0)
        try:
            yield client
        finally:
            if client.stopped is not None:
                api._start_cooldown(KEY, client.stopped, account)

    monkeypatch.setattr(api, "_session", session)
    return campaign_commands


# ───────────────────────── 1. a person's edit lifts the hold and makes one attempt ─────────────────────────

@pytest.mark.parametrize("commands", [True, False])
def test_an_edit_lifts_the_auth_hold_and_makes_one_attempt(monkeypatch, keyed, commands):
    meta, seen = _Meta(), []
    cc = _rig_commands(monkeypatch, meta, seen)
    _hold()
    with cc.open_session(LABEL, commands) as client:
        client.get("/act_1/campaigns", {})
    assert seen == [False], "the hold is gone before the session opens, so nothing refuses the edit"
    assert meta.calls == 1 and not blocks.is_held(KEY)


def test_when_meta_still_asks_the_hold_returns_with_the_same_message(monkeypatch, keyed):
    meta, seen = _Meta(code31=True), []
    cc = _rig_commands(monkeypatch, meta, seen)
    _hold()
    before = blocks.refusal(KEY, LABEL, "read")[0]
    with pytest.raises(g.AuthRequired):
        with cc.open_session(LABEL, True) as client:
            for _ in range(2):
                try:
                    client.get("/act_1/campaigns", {})
                except g.AuthRequired:
                    pass
            client.get("/act_1/campaigns", {})
    assert meta.calls == 1, "after Meta's answer no further call is made"
    held = blocks.hold_of(KEY)
    assert held and held["kind"] == "checkpoint" and held["code"] == 31
    after = blocks.refusal(KEY, LABEL, "read")[0]
    assert after.startswith("Meta asks to confirm this account (security check).")
    assert after.split(". ")[0] == before.split(". ")[0] and after.startswith(before.split(" Nothing")[0])
    assert "press Retry on this profile" in after


@pytest.mark.parametrize("stop", [g.Checkpoint("c"), g.PolicyBlocked("p", code=368),
                                  g.LoggedOut("l", page_seen=True)])
def test_other_holds_are_never_lifted_by_an_edit(monkeypatch, keyed, stop):
    meta, seen = _Meta(), []
    cc = _rig_commands(monkeypatch, meta, seen)
    _hold(stop)
    with cc.open_session(LABEL, True):
        pass
    assert seen == [True] and blocks.is_held(KEY)


def test_the_shut_off_button_lifts_the_auth_hold_too(monkeypatch, keyed):
    _hold()
    seen = []
    monkeypatch.setattr(api, "_turn_off_locked", lambda *a, **k: seen.append(blocks.is_held(KEY)) or "done")
    api.turn_off(ACCOUNT, "campaign", "123456789012345", expected_name="x", requester="me")
    assert seen == [False]


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

def test_the_state_carries_the_hold_with_reason_and_since(keyed):
    from src.web import app

    _hold()
    shown = app._account_holds([LABEL, "OTHER"])
    assert set(shown) == {LABEL} or set(shown) == {LABEL, "OTHER"}   # both labels share the one profile here
    assert shown[LABEL]["kind"] == "checkpoint" and shown[LABEL]["at"]
    assert shown[LABEL]["reason"] == "Meta asks to confirm this account (security check)."
    assert "press Retry on this profile" in shown[LABEL]["next"]


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
