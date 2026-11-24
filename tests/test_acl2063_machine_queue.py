"""Machine admission is shared; each dashboard still gets its real own list."""
from datetime import datetime
from pathlib import Path

import pytest

from src.analysis_agent import account_refresh as ar, account_refresh_state as ars
from src.analysis_agent import campaign_inventory as ci
from src.analysis_agent.account_refresh_machine import MachineStore
from config.paths import get_local_config_dir
from tests.profile_lease_fakes import fake_profile


NOW = datetime(2026, 10, 24, 8, 0).astimezone()
PROFILE = fake_profile("machine-shared")


@pytest.fixture
def dashboards(tmp_path, monkeypatch):
    monkeypatch.setenv("ACL_AUTO_REFRESH", "1")
    monkeypatch.setattr(ars, "machine_root", lambda: tmp_path / "machine", raising=False)
    roots = {name: tmp_path / name for name in ("one", "two")}
    reads = []
    maps = {"one": ["A"], "two": ["A"]}
    clock = [NOW.timestamp()]

    def select(name, members=None):
        if members is not None:
            maps[name] = list(members)
        root = roots[name]
        root.mkdir(exist_ok=True)
        monkeypatch.setenv("ACL_CONFIG_DIR", str(root))
        rows = ["account_name,ad_account_id,profile_id"]
        for account in maps[name]:
            base = account.removeprefix("Alias")
            act = {"A": "12345678", "B": "22345678", "C": "32345678"}[base]
            rows.append(f"{account},act_{act},{fake_profile('machine-' + base)}")
        (root / "ad_account_map.csv").write_text("\n".join(rows) + "\n")
        return root

    def reader(account, trigger):
        reads.append((str(Path(get_local_config_dir())), account))
        stamp = datetime.fromtimestamp(clock[0]).astimezone()
        snap = ci.Snapshot(account=account, state="empty", finished_at=stamp.strftime("%Y-%m-%d %H:%M:%S"),
                           finished_at_utc=stamp.isoformat(),
                           steps={step: "ok" for step in ci.STRUCTURE_STEPS})
        ci.put_result(snap)
        return snap

    def new(owner, members=None):
        root = select(owner, members)
        return ar.RefreshQueue(reader=reader, store=ars.RefreshStore(root),
                               accounts=lambda: list(maps[owner]), clock=lambda: clock[0],
                               machine=MachineStore(tmp_path / "machine"))
    new.clock = clock
    new.maps = maps
    new.roots = roots
    return select, new, reads


def test_two_configs_join_one_window_instead_of_starting_two_sweeps(dashboards):
    select, new, reads = dashboards
    first, second = new("one"), new("two")
    select("one")
    admitted = first.page_open(now=NOW, port=9090)
    select("two")
    joined = second.page_open(now=NOW, port=9095)
    assert admitted["sweep"] == "started"
    assert joined["sweep"] == "joined"
    assert reads == []


def test_duplicate_map_gets_the_actual_result_without_another_read(dashboards):
    select, new, reads = dashboards
    first, second = new("one"), new("two")
    select("one")
    first.page_open(now=NOW, port=9090)
    assert first._drain_once() == 1
    original = ci.get_snapshot("A")
    select("two")
    second.page_open(now=NOW, port=9095)
    second._drain_once()
    received = ci.get_snapshot("A")
    assert len(reads) == 1, "A machine dedup marker must relay the real result, not reread it"
    assert received is not None and received.finished_at_utc == original.finished_at_utc
    assert received.steps == original.steps and received.state == original.state
    assert second.state()["rows"]["A"]["list_at"] == original.finished_at


def test_multilogin_stop_in_another_config_is_visible_before_dispatch(dashboards):
    select, new, reads = dashboards
    first, second = new("one"), new("two")
    select("one")
    snap = ci.Snapshot(account="A", state="failed", error_code="mlx_session_lost", error="Synthetic MLX")
    ci.put_result(snap)
    first.record_result(snap, trigger="action")
    select("two")
    second.page_open(now=NOW, port=9095)
    assert second.state()["halt"]["kind"] == "global"
    assert second._drain_once() == 0
    assert reads == []


def test_checkpoint_in_another_config_does_not_stop_its_reads(dashboards):
    # ACL-2668 (решение 1): чекпоинт A, записанный первым дешбордом, не держит машину.
    select, new, reads = dashboards
    first = new("one")
    first.record_result(ci.Snapshot(account="A", state="failed", stop="checkpoint"), trigger="action")
    assert first._machine.halt(now=NOW.timestamp()) is None
    second = new("two", ["A", "B"])
    second.page_open(now=NOW, port=9095)
    assert second.state()["halt"] is None
    assert second._drain_once() >= 1
    assert "B" in [a for _, a in reads]   # общей паузы нет: B этого дешборда читается


def test_partly_different_maps_join_once_and_both_get_their_whole_list(dashboards):
    select, new, reads = dashboards
    first, second = new("one", ["A", "B"]), new("two", ["B", "C"])
    select("one")
    first.page_open(now=NOW, port=9090)
    assert first._drain_once() == 2
    select("two")
    assert second.page_open(now=NOW, port=9095)["sweep"] == "joined"
    assert second._drain_once() == 1
    # ACL-2668: A and B are read together in the first drain (order free); C comes in the second.
    order = [account for _, account in reads]
    assert sorted(order[:2]) == ["A", "B"] and order[2:] == ["C"], order
    assert all(ci.get_snapshot(account) is not None for account in ("B", "C"))
    assert second.state()["sweep"] == {"window": "2026-10-24/08:00", "done": 2, "total": 2}


def test_alias_gets_its_local_label_without_another_profile_read(dashboards):
    select, new, reads = dashboards
    first, second = new("one"), new("two", ["AliasA"])
    select("one")
    first.page_open(now=NOW, port=9090)
    first._drain_once()
    select("two")
    second.page_open(now=NOW, port=9095)
    assert second._drain_once() == 0
    assert len(reads) == 1
    assert ci.get_snapshot("AliasA").account == "AliasA"
    assert ci.get_snapshot("A") is None


def test_late_unique_member_extends_same_window_without_restamping_old_result(dashboards):
    select, new, reads = dashboards
    first = new("one")
    first.page_open(now=NOW, port=9090)
    first._drain_once()
    old = ci.get_snapshot("A").finished_at_utc
    later = NOW.replace(hour=10)
    new.clock[0] = later.timestamp()
    select("one", ["A", "C"])
    assert first.page_open(now=later, port=9090)["queued"] == 1
    assert first._drain_once() == 1
    assert [a for _, a in reads] == ["A", "C"]
    assert ci.get_snapshot("A").finished_at_utc == old
    assert first.state()["sweep"]["done"] == 2


def test_consumer_with_newer_local_failure_does_not_hide_it_with_old_shared_success(dashboards):
    select, new, reads = dashboards
    first, second = new("one"), new("two")
    select("one")
    first.page_open(now=NOW, port=9090)
    first._drain_once()
    select("two")
    failed = ci.Snapshot(account="A", state="failed", error="Local new failure",
                         finished_at_utc=NOW.isoformat())
    ci.put_result(failed)
    second.page_open(now=NOW, port=9095)
    assert ci.get_attempt("A")["error"] == "Local new failure"
    assert ci.get_snapshot("A") is None
    assert len(reads) == 1


def test_interrupted_read_is_not_replayed_by_restart_or_another_config(dashboards):
    select, new, reads = dashboards
    first = new("one")
    first.page_open(now=NOW, port=9090)
    from src.analysis_agent.account_refresh_machine import binding
    with first._machine.fence(binding("A")).holding():
        first._machine.begin(binding("A"), first._owner, "schedule")
        # Simulate process death after admission/open, before any durable outcome.
    select("two")
    restarted = new("two")
    restarted.page_open(now=NOW, port=9095)
    assert restarted._drain_once() == 0
    assert reads == []
    assert "interrupted" in ci.get_attempt("A")["error"]
    assert restarted.request("A", "retry", force=True)["queued"]
    assert restarted._drain_once() == 1
    assert len(reads) == 1 and ci.get_attempt("A") is None


def test_status_during_own_live_read_does_not_recover_it(dashboards):
    select, new, reads = dashboards
    first = new("one")
    real = first._reader
    def read(account, trigger):
        from src.analysis_agent.account_refresh_machine import binding
        assert first.state()["running"] == account and first.state()["reading"] == [account]
        assert first._machine.running_keys() == {binding(account)}
        return real(account, trigger)
    first._reader = read
    first.page_open(now=NOW, port=9090)
    assert first._drain_once() == 1
    assert len(reads) == 1 and ci.get_snapshot("A").state == "empty"


def test_legacy_admitted_window_is_not_replayed_or_promoted_to_a_success(dashboards):
    select, new, reads = dashboards
    first = new("one")
    with first._store.transaction() as state:
        state["windows_done"] = {"2026-10-24": ["08:00"]}
    assert first.page_open(now=NOW, port=9090)["queued"] == 0
    select("two")
    second = new("two", ["A", "C"])
    assert second.page_open(now=NOW, port=9095)["queued"] == 1
    assert second._drain_once() == 1
    assert [a for _, a in reads] == ["C"]
    assert ci.get_snapshot("A") is None and "interrupted" in ci.get_attempt("A")["error"]
    assert second.state()["rows"]["A"]["state"] == "failed"


def test_bad_map_row_does_not_block_another_accounts_actual_read(dashboards, monkeypatch):
    select, new, reads = dashboards
    first = new("one", ["A", "B"])
    from src.analysis_agent import account_spend
    original = account_spend._entry_for
    def entry(account):
        value = original(account)
        if account == "A":
            from dataclasses import replace
            value = replace(value, profile_id="")
        return value
    monkeypatch.setattr(account_spend, "_entry_for", entry)
    assert first.page_open(now=NOW, port=9090)["queued"] == 1
    assert first._drain_once() == 1
    assert [a for _, a in reads] == ["B"]
    assert first.state()["rows"]["A"]["state"] == "failed"
    assert first.request("A", "retry", force=True) == {"queued": False, "reason": "invalid_account_map"}


def test_checkpoint_after_restart_marks_only_its_account(dashboards):
    select, new, reads = dashboards
    first = new("one")
    first.record_result(ci.Snapshot(account="A", state="failed", stop="checkpoint"), trigger="action")
    second = new("two", ["A", "B"])
    second.page_open(now=NOW, port=9095)
    assert second.state()["halt"] is None          # ACL-2653: no machine-wide stop for a checkpoint
    second.request("B", "retry", force=True)
    # Cross-config (the owner's dev setup only) is accepted: the second dashboard has no hold file of the first,
    # so its own admitted sweep reads A as well. Nothing halts, and the reads are exactly these two.
    # ACL-2668 (main): the retry for B outranks the sweep entry for A, so A yields to the next drain round.
    assert second._drain_once() + second._drain_once() == 2
    assert second.state()["halt"] is None
    assert [a for _, a in reads] == ["B", "A"]
    # Within the first config the hold keeps automatic reads off A's profile.
    select("one")
    assert first.request("A", "attention") == {"queued": False, "reason": "profile_held"}
    assert first.request("A", "schedule", window="08:00") == {"queued": False, "reason": "profile_held"}


def test_old_account_stop_in_machine_state_is_dropped(dashboards):
    # ACL-2668: стоп чекпоинта, записанный прежней сборкой в общий state.json, машину больше не держит.
    select, new, reads = dashboards
    first = new("one", ["A", "B"])
    from src.analysis_agent.account_refresh_machine import binding
    key = binding("A")
    with first._machine.transaction() as state:
        state["revision"] += 1
        state["stops"][key] = {"key": key, "kind": "checkpoint", "reason": "legacy", "until": None,
                               "generation": state["revision"]}
        state["halt"] = dict(state["stops"][key])
    assert first._machine.halt(now=NOW.timestamp()) is None
    first.request("B", "attention")
    assert first._drain_once() == 1 and [a for _, a in reads] == ["B"]


def test_forced_requests_queued_before_peer_result_join_it_but_later_retry_reads(dashboards):
    select, new, reads = dashboards
    first, second = new("one"), new("two")
    select("one")
    first.request("A", "retry", force=True)
    select("two")
    second.request("A", "retry", force=True)
    select("one")
    assert first._drain_once() == 1
    select("two")
    assert second._drain_once() == 0
    assert len(reads) == 1 and ci.get_snapshot("A") is not None
    second.request("A", "retry", force=True)
    assert second._drain_once() == 1
    assert len(reads) == 2


def test_private_projection_failure_reuses_durable_result_without_another_read(dashboards, monkeypatch):
    select, new, reads = dashboards
    first = new('one')
    def reader(account, trigger):
        reads.append(('read', account))
        return ci.Snapshot(account=account, state='empty', finished_at='2026-10-24 08:00:00',
                           finished_at_utc=NOW.isoformat(), steps={s: 'ok' for s in ci.STRUCTURE_STEPS})
    first._reader = reader
    def unavailable(snap):
        raise OSError('synthetic private disk failure')
    monkeypatch.setattr(ci, 'put_result', unavailable)
    first.page_open(now=NOW, port=9090)
    assert first._drain_once() == 1
    assert len(reads) == 1
    assert first.state()['rows']['A']['state'] == 'empty'
    assert ci.get_snapshot('A') is not None
    assert first._drain_once() == 0 and len(reads) == 1


def test_legacy_stop_migrates_before_first_schedule_window(dashboards):
    select, new, reads = dashboards
    new.clock[0] = NOW.replace(hour=7).timestamp()
    first = new('one')
    with first._store.transaction() as state:
        state['halt'] = {'kind': 'global', 'reason': 'legacy Multilogin stop', 'until': None, 'account': 'A'}
    assert first.state()['halt']['kind'] == 'global'
    assert first._store.read()['machine_migrated'] is True
    new.clock[0] = NOW.timestamp()
    select('two'); second = new('two')
    second.page_open(now=NOW, port=9095)
    assert second.state()['halt']['kind'] == 'global'
    assert second._drain_once() == 0 and reads == []


def test_legacy_checkpoint_halt_is_not_migrated(dashboards):
    # ACL-2668: старая локальная пауза чекпоинта не переносится в общий state.json машины.
    select, new, reads = dashboards
    new.clock[0] = NOW.replace(hour=7).timestamp()
    first = new('one')
    with first._store.transaction() as state:
        state['halt'] = {'kind': 'checkpoint', 'reason': 'legacy checkpoint', 'until': None, 'account': 'A'}
    assert first.state()['halt'] is None
    assert first._machine.halt(now=new.clock[0]) is None


@pytest.mark.parametrize('code,raw,expected', [
    ('proxy_auth', 'https://synthetic:secret@proxy.invalid/path', 'proxy login'),
    ('', 'the profile is logged out: synthetic-private', 'logged out'),
    ('', 'profile not found: synthetic-private', 'profile is missing'),
    ('', 'ConnectionError with synthetic-private', 'connection failed'),
])
def test_peer_failure_keeps_a_safe_reason_without_private_details(dashboards, code, raw, expected):
    select, new, reads = dashboards
    first = new('one')
    first.record_result(ci.Snapshot(account='A', state='failed', error=raw, error_code=code,
                                   finished_at_utc=NOW.isoformat()), trigger='action')
    select('two'); second = new('two')
    second.state()
    error = second.state()['rows']['A']['error']
    assert expected in error.lower() and 'Retry' in error
    assert 'synthetic' not in error and 'secret' not in error and 'https://' not in error
    assert ci.get_attempt('A')['error'] == error


@pytest.mark.parametrize("ceiling", [1, 2])
def test_queue_reads_up_to_profiles_at_once_accounts_together(dashboards, monkeypatch, ceiling):
    """ACL-2668: no machine-wide one-read lock; the only count limit is Profiles at once."""
    import threading
    monkeypatch.setenv("ACL_MAX_PROFILES", str(ceiling))
    select, new, reads = dashboards
    first = new("one", ["A", "B", "C"])
    real = first._reader
    guard, live, peak, both = threading.Lock(), [0], [0], threading.Event()
    def read(account, trigger):
        with guard:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
            if live[0] >= 2:
                both.set()
        try:
            # Two reads overlap only if the queue really runs them together.
            both.wait(3 if ceiling > 1 and account != "C" else .2)
            assert first.state()["reading"].count(account) == 1
            return real(account, trigger)
        finally:
            with guard:
                live[0] -= 1
    first._reader = read
    first.page_open(now=NOW, port=9090)
    assert first._drain_once() == 3
    assert sorted(a for _, a in reads) == ["A", "B", "C"]
    assert peak[0] == ceiling
    assert first.state()["reading"] == [] and first.state()["running"] is None
