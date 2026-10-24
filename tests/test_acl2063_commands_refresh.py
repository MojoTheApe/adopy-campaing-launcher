"""Actual command engine refreshes with its same leased fake Graph session."""
from contextlib import contextmanager
from types import SimpleNamespace
import pytest

from tests.profile_lease_fakes import fake_profile
from src.analysis_agent import account_refresh as ar, campaign_inventory as ci
from src.analysis_agent.account_refresh_state import read_session
from src.fb_ads_rpa.profile_lease import profile_lock
from src.fb_api import commands as cmd
from src.web import campaign_commands as webcmd
from tests.test_acl1784_api_review_window import api_job  # noqa: F401
from tests.test_acl1800_campaign_commands import ACT, ACCOUNT, CAMP_A, FakeMeta, World, t


class InventoryMeta(FakeMeta):
    def request(self, method, path, params=None):
        kinds = {"campaigns": "campaign", "adsets": "adset", "ads": "ad"}
        for edge, kind in kinds.items():
            if path == "/" + ACT + "/" + edge:
                assert method == "GET"
                self.gets.append(path)
                return {"status": 200, "body": {"data": [
                    self._row(o, (params or {}).get("fields", "")) for o in self.objs.values() if o["_kind"] == kind]}}
        return super().request(method, path, params)


class SessionWorld(World):
    active = False
    @contextmanager
    def open_session(self, account, commands):
        with profile_lock(fake_profile(ACCOUNT)), read_session(priority=commands):
            with super().open_session(account, commands) as sess:
                self.active = True
                sess.profile_id = fake_profile(ACCOUNT)
                self.current = sess
                try:
                    yield sess
                finally:
                    self.active = False


@pytest.fixture
def world(tmp_path, monkeypatch):
    w = SessionWorld(tmp_path, InventoryMeta())
    monkeypatch.setattr(ci, "backend_ready", lambda: True)
    monkeypatch.setattr(ci, "_store_path", lambda: tmp_path / "inventory.json")
    monkeypatch.setattr(ci._as, "_entry_for", lambda account: SimpleNamespace(
        ad_account_id=ACT, profile_id=fake_profile(ACCOUNT)))
    q = ar.RefreshQueue(accounts=lambda: [ACCOUNT])
    monkeypatch.setattr(ar, "_QUEUE", q)
    monkeypatch.setattr(q, "start", lambda: None)
    return w, q


def execute(w, preview, **callbacks):
    return cmd.execute(w.store, w.open_session, revision_id=preview["revision_id"],
                       idempotency_key="refresh-key-0001", spawn=lambda fn: fn(), sleep=lambda _: None, **callbacks)


def test_success_refresh_uses_exact_same_session_without_extra_open_or_write(world):
    w, q = world
    preview = w.preview("set_status", "ACTIVE", [t("campaign", CAMP_A)])
    calls = []
    def refresh(op, sess):
        assert w.active and sess is w.current
        calls.append(sess)
        webcmd._refresh_after_write(op, sess)
    execute(w, preview, after_write=refresh, on_stale=webcmd._refresh_stale)
    assert w.final(preview)["state"] == cmd.SUCCEEDED
    assert len(w.sessions) == 2 and len(calls) == 1
    assert w.meta.posts == [(f"/{CAMP_A}", {"status": "ACTIVE"})]
    snap = ci.get_snapshot(ACCOUNT)
    assert snap.state == "ok" and snap.insights == {} and (snap.date_from, snap.date_to) == ("", "")
    assert next(c for c in snap.campaigns if c["id"] == CAMP_A)["status"] == "ACTIVE"
    assert q.state()["rows"][ACCOUNT]["state"] == "ok" and q.state()["queued"] == []


def test_hand_edit_preserves_no_write_refusal_and_queues_immediate_action(world):
    w, q = world
    preview = w.preview("set_status", "ACTIVE", [t("campaign", CAMP_A)])
    w.meta.objs[CAMP_A]["status"] = "ACTIVE"
    called = []
    execute(w, preview, after_write=lambda *a: called.append(a), on_stale=webcmd._refresh_stale)
    final = w.final(preview)
    assert final["state"] == cmd.FAILED and final["stale"] is True and w.meta.posts == []
    assert called == []
    assert q.state()["queued"] == [{"account": ACCOUNT, "trigger": "action"}]
    assert "Nothing was written" in final["message"]


def test_post_write_call_ceiling_failure_keeps_success_and_does_not_reset_budget(world, monkeypatch):
    w, q = world
    monkeypatch.setenv(cmd.MAX_CALLS_ENV, "4")
    preview = w.preview("set_status", "ACTIVE", [t("campaign", CAMP_A)])
    execute(w, preview, after_write=webcmd._refresh_after_write)
    assert w.final(preview)["state"] == cmd.SUCCEEDED and len(w.meta.posts) == 1
    assert w.clients[-1].calls == 4 and w.clients[-1].stopped.kind == "call_budget"
    assert ci.get_attempt(ACCOUNT)["state"] == "failed"
    assert q.state()["rows"][ACCOUNT]["state"] == "failed" and q.state()["halt"] is not None


def test_unexpected_refresh_exception_cannot_rewrite_a_confirmed_command(world):
    w, _q = world
    preview = w.preview("set_status", "ACTIVE", [t("campaign", CAMP_A)])
    def failure(*args):
        raise RuntimeError("synthetic refresh failure")
    execute(w, preview, after_write=failure)
    assert w.final(preview)["state"] == cmd.SUCCEEDED and len(w.meta.posts) == 1


def test_unknown_write_does_not_run_success_refresh_or_resend(world):
    w, _q = world
    preview = w.preview("set_status", "ACTIVE", [t("campaign", CAMP_A)])
    w.meta.fail_next[CAMP_A] = "lost"
    callbacks = []
    execute(w, preview, after_write=lambda *a: callbacks.append(a))
    assert w.final(preview)["state"] == cmd.UNKNOWN and len(w.meta.posts) == 1 and callbacks == []


def test_removed_mapping_skips_same_session_read_without_erasing_stop_history(world, monkeypatch):
    w, _q = world
    ci.put_result(ci.Snapshot(account=ACCOUNT, state="failed", stop="checkpoint",
                              finished_at_utc="2026-10-24T08:00:00+00:00"))
    before = ci._store_path().read_bytes()
    monkeypatch.setattr(ci._as, "_entry_for", lambda account: None)
    sess = SimpleNamespace(client=SimpleNamespace(calls=0), act=ACT)
    snap = ci.read_in_session(ACCOUNT, sess, trigger="action")
    assert snap.error_code == "account_removed" and sess.client.calls == 0
    assert ci._store_path().read_bytes() == before
    assert ci.latest_stops()[ACCOUNT][0] == "checkpoint"


def test_publisher_refreshes_with_its_existing_session_before_release(world):
    from src.web.publish_api import ApiPublisher
    w, q = world
    publisher = ApiPublisher()
    publisher.account_label = ACCOUNT
    with w.open_session(ACCOUNT, True) as sess:
        publisher._client, publisher.act = sess.client, sess.act
        publisher._session_profile_id = sess.profile_id
        publisher.refresh_structure()
        assert w.active
    assert len(w.sessions) == 1 and ci.get_snapshot(ACCOUNT).state == 'ok'
    assert w.meta.posts == [] and q.state()['queued'] == []


@pytest.mark.parametrize('guard', ['dry_run', 'blocked', 'stop', 'cancel'])
def test_publisher_never_reads_after_unconfirmed_cancelled_or_dry_publish(world, guard):
    from src.web.publish_api import ApiPublisher
    w, _q = world
    publisher = ApiPublisher()
    publisher.account_label, publisher.act = ACCOUNT, ACT
    publisher._client = object()
    if guard == 'cancel':
        publisher.cancel()
    else:
        setattr(publisher, guard, True)
    publisher.refresh_structure()
    assert ci.get_snapshot(ACCOUNT) is None and w.sessions == []


def test_terminal_command_keeps_ui_polling_until_same_session_refresh_finishes(world):
    import threading
    w, _q = world
    preview = w.preview('set_status', 'ACTIVE', [t('campaign', CAMP_A)])
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    def refresh(op, sess):
        assert w.active
        entered.set()
        assert release.wait(3)
        webcmd._refresh_after_write(op, sess)
    def spawn(fn):
        def work():
            try: fn()
            finally: finished.set()
        threading.Thread(target=work).start()
    cmd.execute(w.store, w.open_session, revision_id=preview['revision_id'],
                idempotency_key='refresh-pending-001', spawn=spawn, after_write=refresh)
    try:
        assert entered.wait(3)
        visible = w.final(preview)
        assert visible['state'] == cmd.SUCCEEDED and visible.get('refresh_pending') is True
        assert ci.get_snapshot(ACCOUNT) is None
    finally:
        release.set()
        assert finished.wait(3)
    assert w.final(preview)['refresh_pending'] is False and ci.get_snapshot(ACCOUNT).state == 'ok'
    assert len(w.sessions) == 2 and len(w.meta.posts) == 1


def test_restart_never_replays_a_confirmed_write_or_waits_for_a_dead_refresh(world):
    w, _q = world
    preview=w.preview('set_status','ACTIVE',[t('campaign',CAMP_A)])
    execute(w,preview)
    done=w.final(preview)
    done['refresh_pending']=True
    w.store.save(done)
    restarted=cmd.OperationStore(w.store.path)
    visible=cmd.status(restarted,done['op_id'])
    assert visible['state']==cmd.SUCCEEDED and visible['refresh_pending'] is False
    assert len(w.meta.posts)==1


def test_builder_job_refreshes_before_releasing_its_profile(monkeypatch, api_job):
    from tests.test_acl1784_api_review_window import FakePublisher, _ok_result
    events=[]
    original=FakePublisher.finish_job
    monkeypatch.setattr(FakePublisher,'refresh_structure',lambda self:events.append('read'),raising=False)
    def finish(self,*args,**kwargs):
        events.append('release')
        return original(self,*args,**kwargs)
    monkeypatch.setattr(FakePublisher,'finish_job',finish)
    _slots,job,_pub=api_job({'drafts':[{'name':'C1'}], 'writes':1, 'result':_ok_result, 'started_now':True})
    assert events==['read','release'] and job['status']=='done'


def test_hand_edit_before_preview_refuses_and_refreshes_instead_of_silent_noop(world, monkeypatch):
    from tests.test_acl1798_campaign_inventory import camp
    w, q = world
    monkeypatch.setenv('ACL_FB_BACKEND','api')
    monkeypatch.setenv('ACL_BIND_HOST','127.0.0.1')
    monkeypatch.setattr(webcmd,'_sessions',lambda:w.open_session)
    # Cached list says ACTIVE; a human already paused it before our preview.
    snap=ci.Snapshot(account=ACCOUNT,state='ok')
    snap.campaigns,_=ci.parse_inventory([camp(CAMP_A,'Camp A',status='ACTIVE')],'campaign')
    ci.put_result(snap)
    result,status=webcmd.run_preview({'account':ACCOUNT,'actor':'Dana','command':'set_status',
        'value':'PAUSED','targets':[t('campaign',CAMP_A)]})
    assert status==409 and result['code']=='stale'
    assert w.meta.posts==[] and len(w.sessions)==1
    assert q.state()['queued']==[{'account':ACCOUNT,'trigger':'action'}]
