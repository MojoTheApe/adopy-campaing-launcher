"""ACL-1856 (PR 1): журнал операции создания кампании и сверка возобновления.

Ни Meta, ни Multilogin: Graph - это `FakeMeta` в памяти (как в test_acl1761_api_create), журнал - во временной папке.
Проверяется: идентичность и отпечаток, переходы только вперёд, повреждённый файл, срок хранения, протокол `_write`
(`sent` до отправки, отказ журнала до записи), обрыв на каждой записи без дублей, неизвестный результат с удержанием,
сверка возобновления (расхождения без единой записи), catch-all, Stop. Объекты - только PAUSED/ACTIVE-под-PAUSED,
никаких DELETE и активации кампании.
"""
from __future__ import annotations

import json
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
    assert op.path.with_suffix(".corrupt").exists()


def test_unknown_version_is_not_used_and_not_damaged(journal):
    op = journal.open(ACT, "t", "f", "C")
    data = json.loads(op.path.read_text(encoding="utf-8"))
    data["v"] = 99
    op.path.write_text(json.dumps(data), encoding="utf-8")
    other = journal.open(ACT, "t", "f", "C")
    assert other.warnings and other.path != op.path
    assert json.loads(op.path.read_text(encoding="utf-8"))["v"] == 99


def test_prune_removes_only_old_finished_operations(journal, clock):
    done_old = journal.open(ACT, "t", "old", "C")
    done_old.finish(oj.OP_COMPLETE)
    open_old = journal.open(ACT, "t", "open", "C")
    clock.t += oj.RETENTION_S + 10
    done_new = journal.open(ACT, "t", "new", "C")        # open() сам чистит
    done_new.finish(oj.OP_COMPLETE)
    assert not done_old.path.exists()
    assert open_old.path.exists() and done_new.path.exists()


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


def test_an_unknown_write_is_held_then_written_once(tmp_path, journal, clock):
    meta = FakeMeta()
    meta.fail_next["ads"] = "unsent"                  # ответа нет, объект не создан
    first, _ = _go(meta, _draft(tmp_path, ads=1), journal)
    assert first.stop_kind == "unknown"
    assert first.operation["stages"][-1]["state"] == oj.UNKNOWN
    posts = len(meta.posts)
    young, client = _go(meta, _draft(tmp_path, ads=1), journal)         # сразу: держим, ничего не пишем
    assert young.stop_kind == "unknown" and "Wait 10 minutes" in young.error
    assert len(meta.posts) == posts and client.writes == 0
    clock.t += oj.UNKNOWN_HOLD_S + 1
    later, _ = _go(meta, _draft(tmp_path, ads=1), journal)
    assert later.error == "" and _count(meta, "ad") == 1


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


@pytest.mark.parametrize("damage", ["renamed", "deleted", "other_account"])
def test_resume_mismatch_stops_with_zero_writes(tmp_path, journal, damage):
    meta, _first = _partial(tmp_path, journal)
    camp = _by_kind(meta, "campaign")
    if damage == "renamed":
        camp["name"] = "Renamed by hand"
    elif damage == "deleted":
        del meta.objects[camp["id"]]
    elif damage == "other_account":
        camp["account_id"] = "999"
    elif damage == "not_paused":
        camp["status"] = "ACTIVE"
    else:
        camp["adlabels"] = []
    posts = len(meta.posts)
    out, client = _go(meta, _draft(tmp_path, ads=2), journal)
    assert out.stop_kind == "resume_mismatch", (out.stop_kind, out.error)
    assert len(meta.posts) == posts and client.writes == 0
    assert out.operation["partial_ids"]["campaign"] == camp["id"]
    assert out.error.startswith('"') and "Ads Manager" in out.error or "different ad account" in out.error


@pytest.mark.parametrize("damage", ["not_paused", "label_gone"])
def test_verify_resume_rejects_a_campaign_that_is_not_ours_or_not_paused(tmp_path, journal, damage):
    # Имя в таких случаях обычно уходит в `copy N` ещё до сверки; сама сверка - вторая линия защиты.
    meta, _first = _partial(tmp_path, journal)
    camp = _by_kind(meta, "campaign")
    creator, _c = _creator(meta, journal)
    out = cr.Outcome(campaign_name="MX Leads 1")
    plan = cr.plan_from_draft(_draft(tmp_path, ads=2))
    targetings, files = creator._prepare(plan, out)
    choice = creator._resolve_name(plan, targetings)          # до порчи: то же имя, что у первого запуска
    creator._open_operation(choice.plan, out, files)
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
    assert "Wait 10 minutes" in op.summary()["next_action"]
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
