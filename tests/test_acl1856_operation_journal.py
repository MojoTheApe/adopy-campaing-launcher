"""ACL-1856 (PR 1): журнал операции создания кампании и сверка возобновления.

Ни Meta, ни Multilogin: Graph - это `FakeMeta` в памяти (как в test_acl1761_api_create), журнал - во временной папке.
Проверяется: идентичность и отпечаток, переходы только вперёд, повреждённый файл, срок хранения, протокол `_write`
(`sent` до отправки, отказ журнала до записи), обрыв на каждой записи без дублей, неизвестный результат с удержанием,
сверка возобновления (расхождения без единой записи), catch-all, Stop. Объекты - только PAUSED/ACTIVE-под-PAUSED,
никаких DELETE и активации кампании.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_api import creator as cr  # noqa: E402
from src.fb_api import graph as g  # noqa: E402
from src.fb_api import operation_journal as oj  # noqa: E402
from tests.test_acl1761_api_create import ACT, FakeMeta, _draft  # noqa: E402


# ───────────────────────── журнал сам по себе ─────────────────────────

class Clock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def journal(tmp_path, clock):
    return oj.OperationJournal(tmp_path / "ops", clock=clock)


def test_same_fingerprint_is_the_same_file_and_another_is_a_new_operation(journal):
    a = journal.open(ACT, "tag", "fp1", "C")
    again = journal.open(ACT, "tag", "fp1", "C")
    other = journal.open(ACT, "tag", "fp2", "C")
    acct = journal.open("act_999", "tag", "fp1", "C")
    assert a.id == again.id and a.path == again.path
    assert len({a.id, other.id, acct.id}) == 3


def test_fingerprint_ignores_warnings_but_not_plan_or_file_content():
    plan = {"name": "C", "ad_sets": [{"name": "S"}], "warnings": ["x"]}
    same = {"name": "C", "ad_sets": [{"name": "S"}], "warnings": ["other"]}
    changed = {"name": "C", "ad_sets": [{"name": "S2"}], "warnings": []}
    base = oj.plan_fingerprint(plan, {"1": ["aa"]})
    assert oj.plan_fingerprint(same, {"1": ["aa"]}) == base
    assert oj.plan_fingerprint(changed, {"1": ["aa"]}) != base
    assert oj.plan_fingerprint(plan, {"1": ["bb"]}) != base


def test_stage_transitions_only_go_forward(journal):
    op = journal.open(ACT, "t", "f", "C")
    op.begin("campaign", "campaign", name="C")
    op.confirm("campaign", "11")
    with pytest.raises(oj.JournalError):
        op.begin("campaign", "campaign")          # подтверждённое не отправляется второй раз
    with pytest.raises(oj.JournalError):
        op.mark("campaign", oj.SENT)
    with pytest.raises(oj.JournalError):
        op.confirm("campaign", "12")              # другой id у подтверждённого
    op.confirm("campaign", "11")                  # тот же id - повтор безвреден
    assert op.stage("campaign")["state"] == oj.CONFIRMED


def test_a_failed_attempt_can_be_sent_again(journal):
    op = journal.open(ACT, "t", "f", "C")
    op.begin("ad 1", "ad")
    op.mark("ad 1", oj.UNKNOWN)
    op.begin("ad 1", "ad")
    op.mark("ad 1", oj.REJECTED)
    op.begin("ad 1", "ad")
    assert op.stage("ad 1")["attempts"] == 3 and op.stage("ad 1")["state"] == oj.SENT


def test_corrupt_file_is_set_aside_and_the_operation_starts_fresh(journal):
    op = journal.open(ACT, "t", "f", "C")
    op.begin("campaign", "campaign")
    op.path.write_text("{not json", encoding="utf-8")
    fresh = journal.open(ACT, "t", "f", "C")
    assert fresh.stage("campaign") is None and fresh.warnings
    assert len(list(op.path.parent.glob(f"{op.id}.*.corrupt"))) == 1
    # a second corruption of the same operation keeps the first .corrupt file (timestamped names)
    fresh.begin("campaign", "campaign")
    fresh.path.write_text("{again", encoding="utf-8")
    journal.clock.t += 5
    journal.open(ACT, "t", "f", "C")
    assert len(list(op.path.parent.glob(f"{op.id}.*.corrupt"))) == 2


def test_unknown_version_is_not_used_and_not_damaged(journal):
    op = journal.open(ACT, "t", "f", "C")
    data = json.loads(op.path.read_text(encoding="utf-8"))
    data["v"] = 99
    op.path.write_text(json.dumps(data), encoding="utf-8")
    other = journal.open(ACT, "t", "f", "C")
    assert other.warnings and other.path != op.path
    assert json.loads(op.path.read_text(encoding="utf-8"))["v"] == 99


def test_prune_removes_old_operations_of_every_state_and_old_side_files(journal, clock):
    done_old = journal.open(ACT, "t", "old", "C")
    done_old.finish(oj.OP_COMPLETE)
    open_old = journal.open(ACT, "t", "open", "C")
    stopped_old = journal.open(ACT, "t", "stopped", "C")
    stopped_old.finish(oj.OP_STOPPED, "unknown")
    failed_old = journal.open(ACT, "t", "failed", "C")
    failed_old.finish(oj.OP_FAILED, "RuntimeError")
    side = [journal.root / "op-aaaa.1.corrupt", journal.root / "op-aaaa.2.superseded", journal.root / "op-aaaa.v1.json"]
    for f in side:
        f.write_text("x", encoding="utf-8")
        os.utime(f, (1, 1))                                    # very old file time
    clock.t += oj.RETENTION_S + 10
    done_new = journal.open(ACT, "t", "new", "C")        # open() runs the prune (at most once per PRUNE_EVERY_S)
    done_new.finish(oj.OP_COMPLETE)
    for gone in (done_old, open_old, stopped_old, failed_old):
        assert not gone.path.exists()
    assert done_new.path.exists()
    assert not any(f.exists() for f in side)


def test_prune_scans_at_most_once_per_interval(journal, clock, monkeypatch):
    journal.open(ACT, "t", "a", "C")
    reads = []
    real = oj.json.loads
    monkeypatch.setattr(oj.json, "loads", lambda *a, **k: reads.append(1) or real(*a, **k))
    clock.t += 60
    journal.open(ACT, "t", "b", "C")                     # within PRUNE_EVERY_S: no scan of the folder
    assert journal.prune() == 0
    n_open = len(reads)
    clock.t += oj.PRUNE_EVERY_S + 1
    journal.prune()
    assert len(reads) > n_open


def test_the_request_scan_reads_only_recent_files(journal, clock):
    old = journal.open(ACT, "t", "old", "C", request_id="rq-old")
    os.utime(old.path, (1, 1))                           # untouched for ages
    reads = []
    real = journal._read_file
    journal._read_file = lambda p: reads.append(p.name) or real(p)
    assert journal._request_owner("rq-old") == ""        # an old file cannot hold a live submit key
    assert old.path.name not in reads


def test_reads_do_not_write(journal):
    op = journal.open(ACT, "t", "f", "C")
    op.begin("campaign", "campaign")
    before = (op.path.stat().st_mtime_ns, op.path.read_bytes())
    assert journal.read(op.id)["stages"]["campaign"]["state"] == oj.SENT
    op.summary()
    assert journal.read("op-missing") is None and not journal.path_for("op-missing").exists()
    assert (op.path.stat().st_mtime_ns, op.path.read_bytes()) == before


def test_a_failed_write_keeps_the_old_file(journal, monkeypatch):
    op = journal.open(ACT, "t", "f", "C")
    op.begin("campaign", "campaign")
    before = op.path.read_bytes()

    def boom(path, data):
        raise OSError("disk full")
    monkeypatch.setattr(oj, "_atomic_write", boom)
    with pytest.raises(oj.JournalError):
        op.confirm("campaign", "1")
    assert op.path.read_bytes() == before


def test_two_threads_keep_both_stages(journal):
    op = journal.open(ACT, "t", "f", "C")
    other = journal.open(ACT, "t", "f", "C")
    errors = []

    def go(o, key):
        try:
            for i in range(10):
                o.begin(f"{key}{i}", "ad")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
    ts = [threading.Thread(target=go, args=(op, "a")), threading.Thread(target=go, args=(other, "b"))]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors
    assert len(journal.read(op.id)["stages"]) == 20


# ───────────────────────── создатель с журналом ─────────────────────────

class Crashy(FakeMeta):
    """FakeMeta, который «падает» (RuntimeError) на N-й записи: до отправки или после."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.crash_at = None
        self.phase = "after"
        self.count = 0

    def write(self, path, params, files=None, filename="", **kw):
        self.count += 1
        hit = self.crash_at == self.count
        if hit and self.phase == "before":
            raise RuntimeError("injected crash before the POST")
        body = super().write(path, params, files, filename, **kw)
        if hit:
            raise RuntimeError("injected crash after the POST")
        return body


def _creator(meta, journal, **kw):
    client = g.GraphClient(meta, allow_writes=True, sleep=lambda s: None, min_interval_s=0.0, max_calls=400)
    facts = cr.preflight_account(client, ACT)
    return cr.CampaignCreator(client, facts, cr.TargetingResolver(client), sleep=lambda s: None,
                              journal=journal, **kw), client


def _go(meta, draft, journal, **kw):
    creator, client = _creator(meta, journal, **kw)
    return creator.run(cr.plan_from_draft(draft)), client


def _count(meta, kind):
    return len([o for o in meta.objects.values() if o["_kind"] == kind])


def _assert_safe(meta):
    for path, params in meta.posts:
        edge = path.rsplit("/", 1)[1]
        if edge == "campaigns":
            assert params["status"] == "PAUSED"
        assert json.dumps(params).count("ACTIVE") == (1 if edge in ("adsets", "ads") else 0)


def test_a_clean_run_is_journaled_and_a_rerun_writes_nothing(tmp_path, journal):
    meta = FakeMeta(video_states=("ready",))
    first, _ = _go(meta, _draft(tmp_path, video=True, ads=3), journal)
    assert first.error == "", first.error
    assert first.operation["state"] == oj.OP_COMPLETE
    states = {s["label"]: s["state"] for s in first.operation["stages"]}
    assert set(states.values()) == {oj.CONFIRMED} and "campaign" in states
    assert first.operation["partial_ids"]["campaign"] == first.campaign_id
    posts = len(meta.posts)
    second, client = _go(meta, _draft(tmp_path, video=True, ads=3), journal)
    assert second.error == "" and second.created == {} and client.writes == 0
    assert len(meta.posts) == posts
    assert len(list(journal.root.glob("op-*.json"))) == 1       # тот же отпечаток - тот же файл
    _assert_safe(meta)


@pytest.mark.parametrize("phase", ["before", "after"])
@pytest.mark.parametrize("n", range(1, 9))
def test_a_crash_at_any_write_resumes_without_duplicates(tmp_path, journal, clock, n, phase):
    base = FakeMeta()
    _go(base, _draft(tmp_path, ads=2), oj.OperationJournal(tmp_path / "ref", clock=clock))
    want = {k: _count(base, k) for k in ("campaign", "adset", "ad", "creative")}
    want_posts = len(base.posts)

    meta = Crashy()
    meta.crash_at, meta.phase = n, phase
    first, _ = _go(meta, _draft(tmp_path, ads=2), journal)
    assert first.stop_kind == "unexpected" and first.error == cr.MSG_UNEXPECTED
    assert first.operation["state"] == oj.OP_FAILED
    meta.crash_at = None
    clock.t += oj.UNKNOWN_HOLD_S + 1                 # удержание неизвестной записи прошло
    second, _ = _go(meta, _draft(tmp_path, ads=2), journal)
    if n in (4, 7) and phase == "before":
        # Креатив имеет свою прежнюю квитанцию (creative_recovery): оборванная запись креатива требует сверки руками.
        assert "earlier creative write has an unknown result" in second.error
        assert _count(meta, "creative") <= want["creative"] and len(meta.posts) == n - 1
        return
    assert second.error == "", second.error
    assert {k: _count(meta, k) for k in want} == want
    assert len(meta.posts) - (1 if phase == "after" else 0) <= want_posts + (1 if phase == "before" else 0)
    assert second.readback["ok"] is True and second.operation["state"] == oj.OP_COMPLETE
    _assert_safe(meta)


def test_the_catch_all_keeps_the_partial_ids_and_plain_text(tmp_path, journal):
    meta = Crashy()
    meta.crash_at, meta.phase = 3, "after"            # после набора, при первой картинке
    out, _ = _go(meta, _draft(tmp_path, ads=2), journal)
    assert out.error == cr.MSG_UNEXPECTED and "Traceback" not in out.error
    ids = out.operation["partial_ids"]
    assert "campaign" in ids and any(k.startswith("ad set") for k in ids)
    assert out.operation["state"] == oj.OP_FAILED


def test_a_journal_that_cannot_save_blocks_every_write(tmp_path, journal, monkeypatch):
    def boom(self, *a, **k):
        raise oj.JournalError("disk full")
    monkeypatch.setattr(oj.Operation, "begin", boom)
    meta = FakeMeta()
    out, _ = _go(meta, _draft(tmp_path, ads=1), journal)
    assert out.stop_kind == "journal" and out.error == cr.MSG_JOURNAL
    assert meta.posts == []


def test_an_unopenable_journal_blocks_before_any_write(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    meta = FakeMeta()
    out, _ = _go(meta, _draft(tmp_path, ads=1), oj.OperationJournal(blocker / "sub"))
    assert out.stop_kind == "journal" and meta.posts == []


def test_a_lost_confirmation_record_is_only_a_note_and_the_next_run_adopts(tmp_path, journal, monkeypatch):
    real = oj.Operation.confirm

    def flaky(self, key, object_id):
        if key == "campaign":
            raise oj.JournalError("disk full")
        return real(self, key, object_id)
    monkeypatch.setattr(oj.Operation, "confirm", flaky)
    meta = Crashy()
    meta.crash_at, meta.phase = 3, "after"
    first, _ = _go(meta, _draft(tmp_path, ads=1), journal)
    assert first.stop_kind == "unexpected"
    assert any("progress record could not be saved" in s for s in first.steps)
    monkeypatch.setattr(oj.Operation, "confirm", real)
    meta.crash_at = None
    second, _ = _go(meta, _draft(tmp_path, ads=1), journal)
    assert second.error == "" and _count(meta, "campaign") == 1


def test_an_unsent_write_found_nowhere_is_written_again_without_waiting(tmp_path, journal):
    """ACL-2250 contract: the earlier run looked and found nothing; a rerun does not wait 10 minutes."""
    meta = FakeMeta()
    meta.fail_next["ads"] = "unsent"                  # ответа нет, объект не создан
    first, _ = _go(meta, _draft(tmp_path, ads=1), journal)
    assert first.stop_kind == "unknown"
    assert first.operation["stages"][-1]["state"] == oj.UNKNOWN
    assert "Wait 10 minutes" not in first.operation["next_action"]
    again, _c = _go(meta, _draft(tmp_path, ads=1), journal)
    assert again.error == "" and _count(meta, "ad") == 1


def test_a_stage_left_sent_by_a_dead_run_is_read_back_then_written_without_a_time_stop(tmp_path, journal, clock):
    """ACL-2583 owner rule: no "wait 10 minutes". The unknown stage is read back (after the ~30 s pause), then sent."""
    meta = Crashy()
    meta.crash_at, meta.phase = 5, "before"           # the run dies before the first ad leaves: stage stays `sent`
    first, _ = _go(meta, _draft(tmp_path, ads=1), journal)
    assert first.stop_kind == "unexpected"
    assert "Wait 10 minutes" not in first.operation["next_action"]
    meta.crash_at = None
    creator, client = _creator(meta, journal)
    sleeps = []

    def sleep(s_):
        sleeps.append(s_)
        clock.t += s_                                 # the pause is real time for the journal's busy window
    creator.sleep = sleep
    again = creator.run(cr.plan_from_draft(_draft(tmp_path, ads=1)))
    assert again.error == "" and not again.stop_kind, again.error
    assert _count(meta, "ad") == 1 and _count(meta, "campaign") == 1
    assert 25 <= sum(sleeps) <= 35, sleeps            # the same ~30 s pause as a transient read


def test_an_unknown_stage_found_by_the_second_readback_is_adopted_not_written(tmp_path, journal):
    meta = FakeMeta()
    creator, client = _creator(meta, journal)
    sleeps = []
    creator.sleep = sleeps.append
    creator._op = journal.open(ACT, "tag", "fp-late", "C")
    creator._op.begin("ad 1", "ad", name="Ad 1")                      # an earlier run died mid-send: stage `sent`
    reads = []

    def find():
        reads.append(1)
        return None if len(reads) == 1 else "AD-LATE"                 # Meta lists it only on the second read

    def send():
        raise AssertionError("must not be written again")
    out = cr.Outcome("C")
    assert creator._journaled(out, "ad 1", "ad", send, find, name="Ad 1") == "AD-LATE"
    assert len(reads) == 2 and 25 <= sum(sleeps) <= 35, (reads, sleeps)
    assert creator._op.stage("ad 1")["state"] == oj.CONFIRMED


def test_an_unknown_stage_not_found_twice_is_written_again_and_recorded(tmp_path, journal):
    meta = FakeMeta()
    creator, client = _creator(meta, journal)
    creator.sleep = lambda s: None
    creator._op = journal.open(ACT, "tag", "fp-late2", "C")
    creator._op.begin("ad 1", "ad", name="Ad 1")
    sent = []
    out = cr.Outcome("C")
    got = creator._journaled(out, "ad 1", "ad", lambda: sent.append(1) or "AD-NEW", lambda: None, name="Ad 1")
    assert got == "AD-NEW" and sent == [1]
    st = creator._op.stage("ad 1")
    assert st["state"] == oj.CONFIRMED and st["attempts"] == 2        # the journal keeps both sends


def test_an_unknown_write_that_did_land_is_adopted_not_repeated(tmp_path, journal):
    meta = Crashy()
    meta.crash_at, meta.phase = 5, "after"            # объявление создано, ответ потерян
    first, _ = _go(meta, _draft(tmp_path, ads=1), journal)
    assert first.stop_kind == "unexpected"
    meta.crash_at = None
    second, _ = _go(meta, _draft(tmp_path, ads=1), journal)             # не ждём: находим чтением
    assert second.error == "" and _count(meta, "ad") == 1
    assert len([p for p, _ in meta.posts if p.endswith("/ads")]) == 1


def _partial(tmp_path, journal, ads=2):
    """Прогон, оборванный после наборов: кампания и набор подтверждены в журнале."""
    meta = Crashy(video_states=("ready",))
    meta.crash_at, meta.phase = 3, "before"
    out, _ = _go(meta, _draft(tmp_path, ads=ads), journal)
    assert out.stop_kind == "unexpected"
    meta.crash_at = None
    return meta, out


def _by_kind(meta, kind):
    return next(o for o in meta.objects.values() if o["_kind"] == kind)


@pytest.mark.parametrize("damage", ["other_account"])
def test_resume_mismatch_stops_with_zero_writes(tmp_path, journal, damage):
    meta, _first = _partial(tmp_path, journal)
    camp = _by_kind(meta, "campaign")
    camp["account_id"] = "999"
    posts = len(meta.posts)
    out, client = _go(meta, _draft(tmp_path, ads=2), journal)
    assert out.stop_kind == "resume_mismatch", (out.stop_kind, out.error)
    assert len(meta.posts) == posts and client.writes == 0
    assert out.operation["partial_ids"]["campaign"] == camp["id"]
    assert out.error == cr.MSG_OTHER_ACCOUNT.format(name="MX Leads 1")


@pytest.mark.parametrize("damage", ["not_paused", "label_gone"])
def test_verify_resume_rejects_a_campaign_that_is_not_ours_or_not_paused(tmp_path, journal, damage):
    # Имя в таких случаях обычно уходит в `copy N` ещё до сверки; сама сверка - вторая линия защиты.
    meta, _first = _partial(tmp_path, journal)
    camp = _by_kind(meta, "campaign")
    creator, _c = _creator(meta, journal)
    out = cr.Outcome(campaign_name="MX Leads 1")
    plan = cr.plan_from_draft(_draft(tmp_path, ads=2))
    submitted = (creator._plan_view(plan), plan.tag)          # as submitted: before `_prepare` fills Page and pixel
    targetings, files = creator._prepare(plan, out)
    choice = creator._resolve_name(plan, targetings)          # до порчи: то же имя, что у первого запуска
    creator._open_operation(choice.plan, out, files, submitted=submitted, existing_id=camp["id"])
    assert creator._op.confirmed_stages()                      # это та же операция, что оборвалась
    camp["status" if damage == "not_paused" else "adlabels"] = "ACTIVE" if damage == "not_paused" else []
    posts = len(meta.posts)
    with pytest.raises(cr.CreationStopped) as exc:
        creator._verify_resume(choice.plan, out, camp["id"])
    assert exc.value.kind == "resume_mismatch" and len(meta.posts) == posts


def test_a_renamed_ad_set_stops_the_resume(tmp_path, journal):
    meta, _ = _partial(tmp_path, journal)
    _by_kind(meta, "adset")["name"] = "Changed"
    posts = len(meta.posts)
    out, _c = _go(meta, _draft(tmp_path, ads=2), journal)
    assert out.stop_kind == "resume_mismatch" and len(meta.posts) == posts


def test_a_video_that_is_not_ready_stops_the_resume(tmp_path, journal):
    meta = FakeMeta(video_states=("ready",))
    done, _ = _go(meta, _draft(tmp_path, video=True, ads=2), journal)
    assert done.error == ""
    meta.video_states = ["error"]
    for o in meta.objects.values():
        if o["_kind"] == "video":
            o["_polls"] = 0
    posts = len(meta.posts)
    out, _c = _go(meta, _draft(tmp_path, video=True, ads=2), journal)
    assert out.stop_kind == "resume_mismatch" and "still processing or was rejected" in out.error
    assert len(meta.posts) == posts


def test_changed_file_bytes_are_a_new_operation(tmp_path, journal):
    meta = FakeMeta()
    _go(meta, _draft(tmp_path, ads=1), journal)
    assert len(list(journal.root.glob("op-*.json"))) == 1
    draft = _draft(tmp_path, ads=1)
    (tmp_path / "1000_img.png").write_bytes(b"different bytes")     # после сборки черновика: файл изменился
    _go(meta, draft, journal)
    assert len(list(journal.root.glob("op-*.json"))) == 2


def test_a_fingerprint_is_stable_between_runs_of_the_same_draft(tmp_path, journal):
    ids = set()
    for _ in range(2):
        creator, _c = _creator(FakeMeta(), journal)
        plan = cr.plan_from_draft(_draft(tmp_path, ads=2))
        files = {ad.path: ad.path.encode() for s in plan.ad_sets for ad in s.ads}
        creator._open_operation(plan, cr.Outcome(campaign_name=plan.name), files)
        ids.add(creator._op.id)
    assert len(ids) == 1


def test_stop_keeps_the_partial_and_starts_nothing(tmp_path, journal):
    meta = FakeMeta()
    flag = {"stop": False}
    real = meta.write

    def write(path, params, *a, **k):
        body = real(path, params, *a, **k)
        if path.endswith("/campaigns"):
            flag["stop"] = True
        return body
    meta.write = write
    out, _ = _go(meta, _draft(tmp_path, ads=1), journal, cancelled=lambda: flag["stop"])
    assert out.stop_kind == "cancelled"
    assert out.operation["partial_ids"]["campaign"] == out.campaign_id
    assert [p.rsplit("/", 1)[1] for p, _ in meta.posts] == ["campaigns"]


def test_a_refusal_by_meta_is_recorded_as_rejected(tmp_path, journal):
    meta = FakeMeta()
    real = meta.write

    def write(path, params, *a, **k):
        if path.endswith("/adsets"):
            raise g.GraphError("(#100) refused", code=100) if "code" in g.GraphError.__init__.__code__.co_varnames \
                else g.GraphError("(#100) refused")
        return real(path, params, *a, **k)
    meta.write = write
    out, _ = _go(meta, _draft(tmp_path, ads=1), journal)
    assert out.stop_kind in ("graph_error", "unknown", "unexpected")
    if out.stop_kind == "graph_error":
        stages = {s["label"]: s["state"] for s in out.operation["stages"]}
        assert stages[next(k for k in stages if k.startswith("ad set"))] == oj.REJECTED


def test_without_a_journal_nothing_is_written_to_disk(tmp_path):
    meta = FakeMeta()
    out, _ = _go(meta, _draft(tmp_path, ads=1), None)
    assert out.error == "" and out.operation == {}


# ───────────────────────── PR 2: request key, result, publish wiring ─────────────────────────

def test_request_id_is_bound_to_one_plan_revision(journal):
    a = journal.open(ACT, "tag", "fp1", "C", request_id="req#tag")
    again = journal.open(ACT, "tag", "fp1", "C", request_id="req#tag")      # duplicate submit, also after a restart
    assert again.id == a.id and again.data["request_ids"] == ["req#tag"]
    fresh = oj.OperationJournal(journal.root)                              # another process / restart
    assert fresh.open(ACT, "tag", "fp1", "C", request_id="req#tag").id == a.id
    with pytest.raises(oj.RequestConflict):
        fresh.open(ACT, "tag", "fp2", "C", request_id="req#tag")            # same key, other plan revision
    assert len(list(journal.root.glob("op-*.json"))) == 1                   # the refused revision left no file


def test_creator_refuses_a_reused_request_key_before_any_write(tmp_path, journal):
    meta = FakeMeta()
    first, _ = _go(meta, _draft(tmp_path, ads=1), journal, request_key="r1#t")
    assert first.error == ""
    posts = len(meta.posts)
    other, client = _go(meta, _draft(tmp_path, ads=2), journal, request_key="r1#t")
    assert other.stop_kind == "request_conflict" and "different campaign plan" in other.error
    assert client.writes == 0 and len(meta.posts) == posts
    again, client = _go(meta, _draft(tmp_path, ads=1), journal, request_key="r1#t")
    assert again.error == "" and client.writes == 0                         # same plan, same key: same operation


def test_next_action_is_plain_and_reads_do_not_write(tmp_path, journal, clock):
    meta = FakeMeta()
    flag = {"stop": False}
    real = meta.write

    def write(path, params, *a, **k):
        body = real(path, params, *a, **k)
        if path.endswith("/campaigns"):
            flag["stop"] = True
        return body
    meta.write = write
    out, _ = _go(meta, _draft(tmp_path, ads=1), journal, cancelled=lambda: flag["stop"])
    path = next(journal.root.glob("op-*.json"))
    before = path.read_bytes()
    assert "Publish again" in out.operation["next_action"] and "twice" in out.operation["next_action"]
    op = journal.open(ACT, out_tag(path), json.loads(before)["fingerprint"], "C")
    op.summary()
    assert path.read_bytes() == before or json.loads(path.read_bytes())["stages"] == json.loads(before)["stages"]


def out_tag(path):
    return json.loads(path.read_text(encoding="utf-8"))["tag"]


def test_an_unknown_stage_never_reads_as_success_and_says_to_wait(journal):
    op = journal.open(ACT, "tag", "fp", "C")
    op.begin("campaign", "campaign")
    op.mark("campaign", oj.UNKNOWN)
    assert "Wait" not in op.summary()["next_action"] and "minutes" not in op.summary()["next_action"]
    assert op.summary()["partial_ids"] == {}


def test_result_dict_carries_the_operation(tmp_path, journal):
    from src.web import publish_api

    meta = FakeMeta()
    out, _ = _go(meta, _draft(tmp_path, ads=1), journal)
    res = publish_api.result_dict(out)
    assert res["operation"]["state"] == oj.OP_COMPLETE and res["operation"]["partial_ids"]["campaign"]
    assert res["operation"]["next_action"]
    assert publish_api.result_dict(cr.Outcome(campaign_name="X"))["operation"] is None


def test_the_job_passes_its_request_id_to_the_publisher():
    import inspect

    from src.web import publish_api, publish_jobs

    assert "request_id" in inspect.signature(publish_jobs.create_job).parameters
    assert "request_id" in inspect.signature(publish_jobs._start_job).parameters
    assert publish_api.ApiPublisher.request_id == ""


def test_the_journal_can_be_turned_off(monkeypatch):
    from src.web import publish_api

    assert publish_api.journal_enabled()
    monkeypatch.setenv("ACL_OPERATION_JOURNAL", "0")
    assert not publish_api.journal_enabled()


# ───────────────────────── review round 1 (PR 1569): regression tests ─────────────────────────

from src.fb_api import name_receipt as nr  # noqa: E402


def _delete_campaign_tree(meta, camp_id=None):
    """The buyer deletes the campaign (and what hangs under it) in Ads Manager."""
    camp = _by_kind(meta, "campaign") if camp_id is None else meta.objects[camp_id]
    for oid in [o for o, v in meta.objects.items() if v["_kind"] in ("adset", "ad")]:
        del meta.objects[oid]
    del meta.objects[camp["id"]]
    return camp["id"]


def test_republish_after_the_campaign_was_deleted_in_meta_starts_a_new_operation(tmp_path, journal):
    meta = FakeMeta()
    first, _ = _go(meta, _draft(tmp_path, ads=2), journal)
    assert first.error == "" and first.operation["state"] == oj.OP_COMPLETE
    gone = _delete_campaign_tree(meta)
    posts = len(meta.posts)
    second, _c = _go(meta, _draft(tmp_path, ads=2), journal)
    assert second.error == "", second.error
    assert second.campaign_id and second.campaign_id != gone and len(meta.posts) > posts
    assert second.operation["state"] == oj.OP_COMPLETE
    # ACL-2583 (Doc284): "does not exist" (100) for a campaign THIS draft recorded never frees its name - Meta says
    # the same for a live campaign it cannot load. The new campaign continues as "<name> copy 1".
    assert second.published_as.endswith(" copy 1")
    assert any("copy" in w for w in second.warnings)
    _assert_safe(meta)
    third, _c = _go(meta, _draft(tmp_path, ads=2), journal)              # and a third publish is just as unblocked
    assert third.error == ""
    names = [o["name"] for o in meta.objects.values() if o["_kind"] == "campaign"]
    assert len(names) == len(set(names))


def test_republish_after_a_partly_created_campaign_was_deleted(tmp_path, journal):
    meta, first = _partial(tmp_path, journal)
    assert first.operation["partial_ids"]["campaign"]
    _delete_campaign_tree(meta)
    nr.delete(ACT, "MX Leads 1")                                         # no receipt either: a clean slate in Meta
    out, _c = _go(meta, _draft(tmp_path, ads=2), journal)
    assert out.error == "", out.error
    assert out.operation["state"] == oj.OP_COMPLETE
    assert _count(meta, "campaign") == 1 and _count(meta, "ad") == 2


def test_a_campaign_gone_to_the_deleted_status_is_not_a_block_either(tmp_path, journal):
    meta = FakeMeta()
    first, _ = _go(meta, _draft(tmp_path, ads=1), journal)
    camp = _by_kind(meta, "campaign")
    camp["status"] = "DELETED"
    camp["effective_status"] = "DELETED"
    nr.delete(ACT, "MX Leads 1")
    out, _c = _go(meta, _draft(tmp_path, ads=1), journal)
    assert out.error == "" and out.campaign_id != first.campaign_id


def test_a_live_campaign_the_name_search_misses_inside_the_window_is_resumed_not_stopped(tmp_path, journal, clock,
                                                                                         monkeypatch):
    # ACL-2583 owner rule: no lag window; the run stays inside 10 minutes of the earlier one and resumes by id.
    meta, first = _partial(tmp_path, journal)
    nr.delete(ACT, "MX Leads 1")
    monkeypatch.setattr(cr.CampaignCreator, "_family_rows", lambda self, plan: [])      # Meta lag: not listed yet
    creator, _client = _creator(meta, journal)

    def sleep(s_):
        clock.t += s_                                  # a real pause moves the journal's clock (still < 10 minutes)
    creator.sleep = sleep
    out = creator.run(cr.plan_from_draft(_draft(tmp_path, ads=2)))
    assert out.error == "" and not out.stop_kind, (out.stop_kind, out.error)
    assert out.campaign_id == first.operation["partial_ids"]["campaign"]
    assert _count(meta, "campaign") == 1 and _count(meta, "ad") == 2
    assert not list(journal.root.glob("*.superseded"))
    text = " ".join([out.error, *out.warnings])
    assert "few minutes" not in text and "does not list it yet" not in text
    _assert_safe(meta)


def test_a_campaign_renamed_in_ads_manager_is_taken_over_and_never_asks_to_wait(tmp_path, journal, clock):
    meta, _first = _partial(tmp_path, journal)
    renamed = _by_kind(meta, "campaign")
    renamed["name"] = "Renamed by hand"
    for _ in range(3):                                                  # the buyer keeps publishing; the clock moves on
        clock.t += 3600
        writes_into_renamed = lambda: [p for p, _ in meta.posts if p.startswith(f"/{renamed['id']}/")]
        before = len(writes_into_renamed())
        out, _c = _go(meta, _draft(tmp_path, ads=2), journal)
        assert "Wait" not in (out.error or "") and "does not list it yet" not in (out.error or "")
        assert "Wait" not in ((out.operation or {}).get("next_action") or "")
        assert len(writes_into_renamed()) == before                     # nothing is written into the renamed campaign
    assert renamed["name"] == "Renamed by hand"
    assert list(journal.root.glob("*.superseded"))                      # the old record was set aside


def test_a_live_campaign_the_name_search_misses_after_the_lag_window_is_resumed_not_duplicated(
        tmp_path, journal, clock, monkeypatch):
    # Reviewer probe (round 3): name search empty, clock +11 min, publish -> exactly one live campaign.
    meta, first = _partial(tmp_path, journal)
    nr.delete(ACT, "MX Leads 1")
    monkeypatch.setattr(cr.CampaignCreator, "_family_rows", lambda self, plan: [])
    clock.t += 11 * 60
    out, _c = _go(meta, _draft(tmp_path, ads=2), journal)
    assert out.error == "", out.error
    assert "does not list it yet" not in (out.error or "")
    assert out.campaign_id == first.operation["partial_ids"]["campaign"]
    assert _count(meta, "campaign") == 1 and _count(meta, "ad") == 2
    assert not list(journal.root.glob("*.superseded"))
    assert not any("no longer in Ads Manager" in w for w in out.warnings)
    _assert_safe(meta)


@pytest.mark.parametrize("status", ["DELETED", "ARCHIVED"])
def test_a_campaign_really_gone_after_the_lag_window_is_a_new_operation(tmp_path, journal, clock, monkeypatch, status):
    meta, first = _partial(tmp_path, journal)
    camp = _by_kind(meta, "campaign")
    camp["status"] = status
    camp["effective_status"] = status
    nr.delete(ACT, "MX Leads 1")
    monkeypatch.setattr(cr.CampaignCreator, "_family_rows", lambda self, plan: [])
    clock.t += 11 * 60
    out, _c = _go(meta, _draft(tmp_path, ads=2), journal)
    assert out.error == "" and out.campaign_id and out.campaign_id != camp["id"]
    assert list(journal.root.glob("*.superseded"))
    assert any("no longer in Ads Manager" in w for w in out.warnings)


def test_a_campaign_whose_read_by_id_fails_after_the_lag_window_is_a_new_operation(tmp_path, journal, clock,
                                                                                 monkeypatch):
    meta, first = _partial(tmp_path, journal)
    old_id = _by_kind(meta, "campaign")["id"]
    del meta.objects[old_id]
    nr.delete(ACT, "MX Leads 1")
    monkeypatch.setattr(cr.CampaignCreator, "_family_rows", lambda self, plan: [])
    clock.t += 11 * 60
    out, _c = _go(meta, _draft(tmp_path, ads=2), journal)
    assert out.error == "" and out.campaign_id and out.campaign_id != old_id
    assert list(journal.root.glob("*.superseded"))


def test_a_renamed_campaign_warning_says_renamed_not_gone(tmp_path, journal, clock):
    meta, _first = _partial(tmp_path, journal)
    _by_kind(meta, "campaign")["name"] = "Renamed by hand"
    clock.t += 3600
    out, _c = _go(meta, _draft(tmp_path, ads=2), journal)
    assert any("renamed in Ads Manager" in w for w in out.warnings)
    assert not any("no longer in Ads Manager" in w for w in out.warnings)


def test_next_action_never_tells_to_wait_for_a_sent_stage(journal, clock):
    op = journal.open(ACT, "tag", "fp-hold", "C")
    op.begin("campaign", "campaign", name="C")
    assert op.next_action().startswith("Publish again") and "minutes" not in op.next_action()
    clock.t += oj.UNKNOWN_HOLD_S + 1
    assert op.next_action().startswith("Publish again")


def test_a_second_publish_of_a_finished_plan_is_a_new_copy_and_a_new_operation(tmp_path, journal):
    meta = FakeMeta()
    first, _ = _go(meta, _draft(tmp_path, ads=1), journal)
    nr.delete(ACT, "MX Leads 1")                                         # the job ended clean: receipts are cleared
    second, _c = _go(meta, _draft(tmp_path, ads=1), journal)
    assert second.error == "" and second.published_as == "MX Leads 1 copy 1"
    assert second.campaign_id != first.campaign_id and _count(meta, "campaign") == 2
    assert second.operation["state"] == oj.OP_COMPLETE


def test_next_action_never_says_nothing_to_do_next_to_an_error(tmp_path, journal):
    meta = FakeMeta()
    first, _ = _go(meta, _draft(tmp_path, ads=2), journal)
    assert first.error == ""
    creative = next(o for o in meta.objects.values() if o["_kind"] == "creative")
    del meta.objects[creative["id"]]                                     # a confirmed creative is gone
    posts = len(meta.posts)
    out, _c = _go(meta, _draft(tmp_path, ads=2), journal)
    assert out.stop_kind == "resume_mismatch" and len(meta.posts) == posts
    assert out.operation["state"] == oj.OP_STOPPED                       # a COMPLETE record that stops is not "complete"
    assert "Nothing to do" not in out.operation["next_action"] and "Ads Manager" in out.operation["next_action"]
    assert json.loads(next(journal.root.glob("op-*.json")).read_text(encoding="utf-8"))["state"] == oj.OP_STOPPED


@pytest.mark.parametrize("scenario", ["crash", "unknown", "cancel", "mismatch", "journal"])
def test_next_action_agrees_with_every_failed_result(tmp_path, journal, monkeypatch, scenario):
    meta = Crashy() if scenario == "crash" else FakeMeta()
    kw = {}
    if scenario == "crash":
        meta.crash_at, meta.phase = 4, "after"
    elif scenario == "unknown":
        meta.fail_next["ads"] = "unsent"
    elif scenario == "cancel":
        flag = {"stop": False}
        real = meta.write

        def write(path, params, *a, **k):
            body = real(path, params, *a, **k)
            flag["stop"] = flag["stop"] or path.endswith("/campaigns")
            return body
        meta.write = write
        kw["cancelled"] = lambda: flag["stop"]
    elif scenario == "mismatch":
        _go(meta, _draft(tmp_path, ads=1), journal)
        del meta.objects[next(o for o in meta.objects.values() if o["_kind"] == "creative")["id"]]
    elif scenario == "journal":
        def refuse(self, *a, **k):
            raise oj.JournalError("disk full")
        monkeypatch.setattr(oj.Operation, "begin", refuse)
    out, _c = _go(meta, _draft(tmp_path, ads=1), journal, **kw)
    assert out.error, scenario
    text = out.operation.get("next_action", "") if out.operation else ""
    assert "Nothing to do" not in text
    if scenario == "unknown":
        assert "Wait 10 minutes" not in text                              # looked for and not found: sent again
    if scenario == "mismatch":
        assert "Ads Manager" in text


def test_two_identical_drafts_of_one_submit_are_both_created(tmp_path, journal):
    meta = FakeMeta()
    a, _ = _go(meta, _draft(tmp_path, ads=1), journal, request_key="req#0", slot=0)
    nr.delete(ACT, "MX Leads 1")                                         # no receipt: the second one gets a copy name
    b, _c = _go(meta, _draft(tmp_path, ads=1), journal, request_key="req#1", slot=1)
    assert a.error == "" and b.error == "", (a.error, b.error)
    assert (a.published_as, b.published_as) == ("MX Leads 1", "MX Leads 1 copy 1")
    assert _count(meta, "campaign") == 2
    assert len(list(journal.root.glob("op-*.json"))) == 2
    # a restart of the same submit sees the same keys as the same operations, with no conflict
    again, _c = _go(meta, _draft(tmp_path, ads=1), journal, request_key="req#0", slot=0)
    assert again.stop_kind != "request_conflict"


def test_the_publisher_binds_the_key_to_the_draft_position_not_to_the_name(tmp_path, monkeypatch):
    from src.web import publish_api
    from tests.test_acl1761_api_create import _publisher

    seen = []
    real = cr.CampaignCreator.__init__

    def spy(self, *a, **k):
        seen.append((k.get("request_key"), k.get("slot")))
        real(self, *a, **k)
    monkeypatch.setattr(cr.CampaignCreator, "__init__", spy)
    pub = _publisher(FakeMeta())
    pub.request_id = "rid-1"
    d1, d2 = _draft(tmp_path, ads=1), _draft(tmp_path, ads=1)               # the same campaign name
    pub.draft_slots = {id(d1): 0, id(d2): 1}
    pub.publish_draft(d1)
    pub.publish_draft(d2)
    assert seen == [("rid-1#0", 0), ("rid-1#1", 1)]
    assert publish_api.ApiPublisher.draft_slots is None


def _zone_run(meta, draft, now, journal):
    from tests.test_acl1443_lifetime_budget import _plan

    client = g.GraphClient(meta, allow_writes=True, sleep=lambda s: None, min_interval_s=0.0, max_calls=300)
    facts = cr.preflight_account(client, ACT)
    creator = cr.CampaignCreator(client, facts, cr.TargetingResolver(client), sleep=lambda s: None,
                                 now=lambda: now, journal=journal)
    return creator.run(_plan(draft))


def test_a_lifetime_plan_with_a_start_today_keeps_its_operation_when_the_clock_moves(tmp_path, journal,
                                                                                    monkeypatch):
    from tests.test_acl1443_lifetime_budget import ZoneMeta, _at, _life

    monkeypatch.setenv("ACL_TEAM_TIMEZONE", "America/New_York")
    meta = ZoneMeta()
    meta.fail_next["ads"] = "unsent"
    first = _zone_run(meta, _life(tmp_path, start="2026-10-20T09:00"), _at(9, 30), journal)    # start moves to 09:45
    assert first.error and first.operation["stages"]
    ops = list(journal.root.glob("op-*.json"))
    assert len(ops) == 1
    second = _zone_run(meta, _life(tmp_path, start="2026-10-20T09:00"), _at(9, 50), journal)   # start moves to 10:00
    assert second.error == "", second.error
    assert list(journal.root.glob("op-*.json")) == ops                   # the SAME operation, not a second empty one
    assert second.operation["id"] == first.operation["id"]


def test_the_identity_does_not_depend_on_the_chosen_name(tmp_path, journal):
    meta = FakeMeta()
    _go(meta, _draft(tmp_path, ads=1), journal)
    nr.delete(ACT, "MX Leads 1")
    second, _c = _go(meta, _draft(tmp_path, ads=1), journal)             # published as "MX Leads 1 copy 1"
    assert second.published_as.endswith("copy 1")
    rec = json.loads(next(journal.root.glob("op-*.json")).read_text(encoding="utf-8"))
    assert rec["published_name"] == "MX Leads 1 copy 1" and rec["copy_number"] == 1
    assert rec["tag"] == cr.campaign_tag(ACT, "MX Leads 1")              # identity = the submitted name's tag


# -- two runs, one step

def test_a_second_run_cannot_send_a_step_that_another_run_just_sent(journal):
    a = journal.open(ACT, "t", "f", "C")
    b = journal.open(ACT, "t", "f", "C")                                 # opened earlier: knows no stage
    a.begin("ad 1", "ad")
    with pytest.raises(oj.StageBusy):
        b.begin("ad 1", "ad")
    a.begin("ad 1", "ad")                                                # the owner may send again (a resend)
    assert journal.read(a.id)["stages"]["ad 1"]["attempts"] == 2


def test_a_step_sent_long_ago_by_a_dead_run_can_be_taken_over(journal, clock):
    a = journal.open(ACT, "t", "f", "C")
    b = journal.open(ACT, "t", "f", "C")
    a.begin("ad 1", "ad")
    clock.t += oj.UNKNOWN_HOLD_S + 1
    b.begin("ad 1", "ad")
    assert journal.read(a.id)["stages"]["ad 1"]["owner"] == b.owner


_RACER = r"""
import sys, time
sys.path.insert(0, sys.argv[1])
from src.fb_api import operation_journal as oj
j = oj.OperationJournal(sys.argv[2])
op = j.open("act_1", "t", "f", "C")
while time.time() < float(sys.argv[3]):
    pass
try:
    op.begin("campaign", "campaign")
    print("SENT")
except oj.StageBusy:
    print("BUSY")
"""


def test_two_processes_cannot_both_send_the_same_step(tmp_path):
    import time

    script = tmp_path / "racer.py"
    script.write_text(_RACER, encoding="utf-8")
    root = tmp_path / "shared_ops"
    go_at = time.time() + 8
    procs = [subprocess.Popen([sys.executable, str(script), str(ROOT), str(root), str(go_at)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
    outs = []
    for p in procs:
        out, err = p.communicate(timeout=120)
        assert p.returncode == 0, err
        outs.append(out.strip())
    assert sorted(outs) == ["BUSY", "SENT"]
    rec = json.loads(next(root.glob("op-*.json")).read_text(encoding="utf-8"))
    assert rec["stages"]["campaign"]["attempts"] == 1


def test_the_creator_does_not_send_a_step_another_process_just_sent(tmp_path, journal, monkeypatch):
    monkeypatch.setattr(oj.Operation, "refresh", lambda self: None)      # this run's view of the file is stale
    real_begin = oj.Operation.begin
    raced = {"done": False}

    def racing(self, key, *a, **k):
        if key == "campaign" and not raced["done"]:
            raced["done"] = True
            other = journal.open(self.data["act"], self.data["tag"], self.data["fingerprint"], "C")
            real_begin(other, key, "campaign")                           # the other process wins the race
        return real_begin(self, key, *a, **k)
    monkeypatch.setattr(oj.Operation, "begin", racing)
    meta = FakeMeta()
    out, client = _go(meta, _draft(tmp_path, ads=1), journal)
    assert out.stop_kind == "busy" and "right now" in out.error
    assert meta.posts == [] and client.writes == 0


def test_a_stage_the_other_run_already_confirmed_is_adopted_not_sent(tmp_path, journal):
    creator, client = _creator(FakeMeta(), journal)
    plan = cr.plan_from_draft(_draft(tmp_path, ads=1))
    out = cr.Outcome(campaign_name=plan.name)
    creator._open_operation(plan, out, {})
    other = journal.open(plan.act, plan.tag, creator._op.data["fingerprint"], plan.name)
    other.begin("campaign", "campaign")
    other.confirm("campaign", "777")
    sent = []
    got = creator._journaled(out, "campaign", "campaign", lambda: sent.append(1) or "888", lambda: None)
    assert got == "777" and sent == []


# -- one revision cannot touch another's readiness (criterion 5)

def test_a_late_worker_of_an_older_revision_cannot_touch_a_newer_revisions_record(journal):
    rev1 = journal.open(ACT, "tag", "fp-rev1", "C")
    rev1.begin("campaign", "campaign")
    rev2 = journal.open(ACT, "tag", "fp-rev2", "C")                      # the user edited the plan: a newer revision
    rev2.begin("campaign", "campaign")
    rev2.confirm("campaign", "22")
    rev2.finish(oj.OP_COMPLETE)
    assert rev1.path != rev2.path
    before = rev2.path.read_bytes()
    rev1.mark("campaign", oj.CANCELLED, "cancelled")                     # the older run is cancelled / finishes late
    rev1.finish(oj.OP_STOPPED, "cancelled")
    assert rev2.path.read_bytes() == before                              # the newer record is byte for byte unchanged
    now = journal.read(rev2.id)
    assert now["state"] == oj.OP_COMPLETE and now["stages"]["campaign"]["object_id"] == "22"


# -- resume reads come out of the confirmed objects, not a flat number

def _wide_draft(tmp_path, sets=3, ads=5):
    import copy

    d = _draft(tmp_path, ads=ads)
    base = d.ad_sets[0]
    for i in range(2, sets + 1):
        extra = copy.deepcopy(base)
        extra.name = f"MX Leads {i}"
        for j, ad in enumerate(extra.ads):
            ad.copy_name = f"Text{i}-{j}"
            ad.ad_title = f"Head {i}-{j}"
        d.ad_sets.append(extra)
    return d


def test_the_resume_check_allowance_follows_the_confirmed_objects(tmp_path, journal):
    meta = FakeMeta()
    done, _ = _go(meta, _wide_draft(tmp_path), journal)
    assert done.error == "", done.error
    creator, client = _creator(meta, journal)
    plan = cr.plan_from_draft(_wide_draft(tmp_path))
    submitted = (creator._plan_view(plan), plan.tag)
    out = cr.Outcome(campaign_name=plan.name)
    targetings, files = creator._prepare(plan, out)
    choice = creator._resolve_name(plan, targetings)
    creator._open_operation(choice.plan, out, files, submitted=submitted, existing_id=str(choice.existing["id"]))
    stages = creator._op.confirmed_stages()
    assert len(stages) > 30
    allowance = cr.resume_verify_calls(stages)
    assert allowance > 4                                                 # a flat +4 could not cover a 3x5 plan
    client.reset_budget(0)                                               # nothing but the allowance is left
    creator._verify_resume(choice.plan, out, str(choice.existing["id"]))
    assert 0 < client.calls <= allowance and client.max_calls == allowance


def test_resume_verify_calls_counts_each_kind():
    def st(kind):
        return ("k", {"kind": kind})
    assert cr.resume_verify_calls([]) == 0
    assert cr.resume_verify_calls([st("campaign")]) == 1
    assert cr.resume_verify_calls([st("campaign"), st("creative"), st("video"), st("image")]) == 4
    assert cr.resume_verify_calls([st("campaign"), st("adset"), st("adset"), st("ad")]) == 1 + 2 + 2 * 3
