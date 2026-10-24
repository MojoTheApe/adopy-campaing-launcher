"""Actual read contexts retain the machine fence through durable settlement."""
from contextlib import contextmanager
from types import SimpleNamespace
import threading

import pytest

from src.analysis_agent import account_refresh as ar, account_refresh_state as ars
from src.analysis_agent import account_spend_api as api, campaign_inventory as ci
from src.analysis_agent.account_refresh_machine import MachineStore
from tests.profile_lease_fakes import fake_profile


@pytest.mark.parametrize("warm", [False, True])
def test_inner_session_exit_cannot_admit_next_reader_before_result_and_stop(monkeypatch, warm):
    profile = fake_profile("outcome-fence")
    monkeypatch.setattr(ci._as, "_entry_for", lambda a: SimpleNamespace(
        ad_account_id="act_12345678", profile_id=profile, folder_id="", multilogin_instance="A"))
    monkeypatch.setattr(ci, "backend_ready", lambda: True)
    client = SimpleNamespace(calls=1, stopped=None)
    sess = SimpleNamespace(client=client, act="act_12345678", notes={})
    @contextmanager
    def opened(*a, **kw):
        yield sess
    if warm:
        monkeypatch.setattr(api, "_warm_registry", lambda: SimpleNamespace(is_warm=lambda p: True))
        monkeypatch.setattr(api, "_warm_session", opened)
        monkeypatch.setattr(api, "_cooldown_state", lambda key: (0, ""))
    else:
        monkeypatch.setattr(api, "_open_session", opened)
    original = api._session
    after_inner = []
    @contextmanager
    def observed(*a, **kw):
        with original(*a, **kw) as active:
            yield active
        def contender():
            try:
                with ars.read_session(timeout=0):
                    after_inner.append("admitted")
            except TimeoutError:
                after_inner.append("blocked")
        thread = threading.Thread(target=contender)
        thread.start(); thread.join(2)
        assert not thread.is_alive()
    monkeypatch.setattr(api, "_session", observed)
    def stop(snap, *a, **kw):
        snap.stop = "checkpoint"
        snap.steps = {"campaigns": "failed", "adsets": "skipped", "ads": "skipped"}
        snap.error = "Synthetic checkpoint"
    monkeypatch.setattr(ci, "read_into", stop)
    result = ci.run_read("A", trigger="schedule")
    assert result.stop == "checkpoint" and result.state == "failed"
    assert after_inner == ["blocked"]
    assert MachineStore().halt()["kind"] == "checkpoint"
    with ars.read_session(timeout=0):
        assert ci.get_attempt("A")["stop"] == "checkpoint"


def test_nested_read_does_not_wait_on_another_threads_priority_intent():
    waiting, acquired = threading.Event(), threading.Event()
    with ars.read_session():
        def priority():
            waiting.set()
            with ars.read_session(priority=True):
                acquired.set()
        thread = threading.Thread(target=priority)
        thread.start()
        assert waiting.wait(1)
        # Wait for real ownership of the intent, not a timing sleep.
        for _ in range(1000):
            intent = ars.machine_lease("account-read-priority.lock")
            hold = intent.holding(timeout=0)
            try:
                hold.__enter__()
            except TimeoutError:
                break
            else:
                hold.__exit__(None, None, None)
                threading.Event().wait(.001)
        else:
            pytest.fail("The competing priority request never acquired its real intent lease")
        with ars.read_session(timeout=0):
            assert not acquired.is_set()
    thread.join(2)
    assert not thread.is_alive() and acquired.is_set()


def test_actual_publish_holds_only_its_target_and_allows_another_publish(monkeypatch, api_job):
    from src.web import publish_jobs as pj
    from tests.test_acl1784_api_review_window import FakePublisher, _ok_result
    opened_a, opened_b, release = threading.Event(), threading.Event(), threading.Event()
    original = FakePublisher.open
    outcomes = []
    def opened(self, account):
        original(self, account)
        # The real publisher owns this exact machine interlock. Its inline
        # refresh can reenter it without taking the queued dispatcher.
        assert ars.target_lease(fake_profile(account)).owned()
        with ars.target_activity(fake_profile(account)), ars.read_session(priority=True):
            pass
        if account == 'machine-publish-A':
            opened_a.set()
            assert release.wait(3)
        else:
            opened_b.set()
    monkeypatch.setattr(FakePublisher, 'open', opened)
    scenario = {'drafts': [{'name': 'C1'}], 'result': _ok_result, 'writes': 0}
    def publish(account):
        outcomes.append(api_job(scenario, label=account)[1])
    first = threading.Thread(target=publish, args=('machine-publish-A',))
    second = threading.Thread(target=publish, args=('machine-publish-B',))
    first.start()
    try:
        assert opened_a.wait(3)
        with pytest.raises(TimeoutError):
            with ars.target_lease(fake_profile('machine-publish-A')).holding(timeout=0):
                pytest.fail('A read interlock entered a live publisher of the same target')
        second.start()
        assert opened_b.wait(3), 'A different target must not wait on a global writer lease'
    finally:
        release.set()
        first.join(3)
        if second.ident:
            second.join(3)
    assert not first.is_alive() and not second.is_alive()
    assert len(outcomes) == 2 and all(job['status'] == 'done' for job in outcomes)


# Reuse its established fake transport/slot setup, not a second publisher model.
from tests.test_acl1784_api_review_window import api_job
