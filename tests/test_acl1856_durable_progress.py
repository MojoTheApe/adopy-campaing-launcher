"""ACL-1856: durable operation progress and verified resume. Offline: fake Meta, synthetic data, no live account."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_api import creator as cr  # noqa: E402
from src.fb_api import graph as g  # noqa: E402
from src.fb_api import operation_journal as oj  # noqa: E402
from tests.test_acl1761_api_create import ACT, FakeMeta, _draft  # noqa: E402


def _plan(tmp_path, **kw):
    return cr.plan_from_draft(_draft(tmp_path, **kw))


def _run(meta, tmp_path, journal, *, ads=2, video=False, **kw):
    client = g.GraphClient(meta, allow_writes=True, sleep=lambda s: None, min_interval_s=0.0, max_calls=300)
    facts = cr.preflight_account(client, ACT)
    creator = cr.CampaignCreator(client, facts, cr.TargetingResolver(client), sleep=lambda s: None,
                                 journal=journal, **kw)
    return creator.run(_plan(tmp_path, ads=ads, video=video)), client


def _journal(tmp_path, root, **kw):
    return oj.OperationJournal.open(_plan(tmp_path, **kw), root=root)


def _count(meta, kind):
    return len([o for o in meta.objects.values() if o["_kind"] == kind])


# ───────────── journal unit ─────────────

def test_same_plan_same_operation_after_reopen(tmp_path):
    a = _journal(tmp_path, tmp_path / "j")
    a.sent("campaign", "campaign")
    a.confirm("campaign", "555", name="MX Leads 1")
    b = _journal(tmp_path, tmp_path / "j")
    assert b.operation_id == a.operation_id
    assert b.confirmed()["campaign"]["object_id"] == "555"


def test_changed_plan_is_a_new_operation(tmp_path):
    a = _journal(tmp_path, tmp_path / "j", ads=2)
    b = _journal(tmp_path, tmp_path / "j", ads=1)
    assert a.operation_id != b.operation_id


def test_request_id_conflict_is_refused(tmp_path):
    root = tmp_path / "j"
    oj.OperationJournal.open(_plan(tmp_path, ads=2), root=root, request_id="r1")
    again = oj.OperationJournal.open(_plan(tmp_path, ads=2), root=root, request_id="r1")
    assert again.doc["request_ids"] == ["r1"]
    with pytest.raises(oj.RequestConflict):
        oj.OperationJournal.open(_plan(tmp_path, ads=1), root=root, request_id="r1")


def test_confirmed_never_goes_back_to_sent(tmp_path):
    j = _journal(tmp_path, tmp_path / "j")
    j.sent("campaign", "campaign")
    j.confirm("campaign", "9")
    j.sent("campaign", "campaign")
    j.unknown("campaign")
    assert j.stage("campaign")["state"] == oj.CONFIRMED


def test_corrupt_file_is_set_aside_with_a_note(tmp_path):
    root = tmp_path / "j"
    a = _journal(tmp_path, root)
    a.path.write_text("{not json", encoding="utf-8")
    b = _journal(tmp_path, root)
    assert b.load_note and not b.stages
    assert a.path.with_name(a.path.name + ".corrupt").exists()


def test_prune_removes_only_old_complete_operations(tmp_path):
    root = tmp_path / "j"
    a = _journal(tmp_path, root, ads=2)
    a.finish("complete")
    b = _journal(tmp_path, root, ads=1)
    b.finish("partial")
    assert oj.OperationJournal.prune(root, now=a.doc["updated_at"] + oj.RETENTION_S + 10) == 1
    assert not a.path.exists() and b.path.exists()


def test_view_gives_partial_ids_and_a_plain_next_action(tmp_path):
    j = _journal(tmp_path, tmp_path / "j")
    j.sent("campaign", "campaign")
    j.confirm("campaign", "77")
    v = j.view()
    assert v["partial_ids"] == ["77"] and "Publish again" in v["next_action"]
    j.sent("ad 1", "ad")
    assert "Wait about" in j.view()["next_action"]


# ───────────── creator with a journal ─────────────

def test_full_run_records_every_stage_and_is_complete(tmp_path):
    meta = FakeMeta()
    j = _journal(tmp_path, tmp_path / "j")
    out, _ = _run(meta, tmp_path, j)
    assert out.error == "", out.error
    kinds = sorted(v["kind"] for v in j.confirmed().values())
    assert kinds.count("campaign") == 1 and kinds.count("adset") == 1
    assert kinds.count("ad") == 2 and kinds.count("creative") == 2 and kinds.count("image") == 2
    assert j.doc["state"] == "complete" and out.operation["state"] == "complete"


def test_restart_after_a_stop_resumes_without_duplicates(tmp_path):
    meta = FakeMeta()
    root = tmp_path / "j"
    meta.fail_next["ads"] = "unsent"
    first, _ = _run(meta, tmp_path, _journal(tmp_path, root))
    assert first.stop_kind == "unknown" and first.operation["partial_ids"]
    # "dashboard restart": a new journal object over the same file; the short write hold has passed
    j2 = _journal(tmp_path, root)
    for st in j2.stages.values():
        if st["state"] in oj.PENDING_STATES:
            st["sent_at"] -= oj.UNKNOWN_HOLD_WRITE_S + 5
    second, _ = _run(meta, tmp_path, j2)
    assert second.error == "", second.error
    assert _count(meta, "campaign") == 1 and _count(meta, "ad") == 2 and _count(meta, "creative") == 2
    assert second.operation["state"] == "complete"


def test_save_failure_before_a_write_blocks_that_write(tmp_path, monkeypatch):
    meta = FakeMeta()
    j = _journal(tmp_path, tmp_path / "j")

    def boom(*_a, **_k):
        raise oj.JournalError("Progress could not be saved on this computer; nothing was written.")
    monkeypatch.setattr(j, "sent", boom)
    out, client = _run(meta, tmp_path, j)
    assert out.stop_kind == "journal" and not meta.posts and client.writes == 0


def test_save_failure_after_the_answer_keeps_the_object_and_warns(tmp_path, monkeypatch):
    meta = FakeMeta()
    j = _journal(tmp_path, tmp_path / "j")
    monkeypatch.setattr(j, "confirm", lambda *a, **k: False)
    out, _ = _run(meta, tmp_path, j, ads=1)
    assert out.error == "" and any("could not be saved" in w for w in out.warnings)


def test_unknown_result_is_saved_and_a_young_one_is_not_sent_again(tmp_path):
    meta = FakeMeta()
    root = tmp_path / "j"
    meta.fail_next["adimages"] = "unsent"        # upload lost, nothing created
    first, _ = _run(meta, tmp_path, _journal(tmp_path, root, ads=1), ads=1)
    assert first.stop_kind == "unknown"
    j2 = _journal(tmp_path, root, ads=1)
    assert any(v["state"] in oj.PENDING_STATES for v in j2.stages.values())
    posts_before = len(meta.posts)
    second, _ = _run(meta, tmp_path, j2, ads=1)
    assert second.stop_kind == "unknown" and "Wait about 10 minute" in second.error
    assert len(meta.posts) == posts_before            # nothing was sent blindly


def test_an_old_unknown_upload_may_be_sent_again(tmp_path):
    meta = FakeMeta()
    root = tmp_path / "j"
    meta.fail_next["adimages"] = "unsent"
    _run(meta, tmp_path, _journal(tmp_path, root, ads=1), ads=1)
    j2 = _journal(tmp_path, root, ads=1)
    for st in j2.stages.values():
        if st["state"] in oj.PENDING_STATES:
            st["sent_at"] -= oj.UNKNOWN_HOLD_S + 5
    out, _ = _run(meta, tmp_path, j2, ads=1)
    assert out.error == "", out.error
    assert _count(meta, "ad") == 1


def test_lost_answer_for_a_created_object_is_adopted_not_duplicated(tmp_path):
    meta = FakeMeta()
    root = tmp_path / "j"
    meta.fail_next["campaigns"] = "lost"
    out, _ = _run(meta, tmp_path, _journal(tmp_path, root, ads=1), ads=1)
    assert out.error == "" and _count(meta, "campaign") == 1


@pytest.mark.parametrize("edit,needle", [
    ("rename_adset", "renamed"),
    ("drop_ad", "no longer lists"),
    ("drop_creative", "no longer lists"),
    ("unpause", "recorded as created"),
])
def test_resume_mismatch_stops_before_any_write(tmp_path, edit, needle):
    meta = FakeMeta()
    root = tmp_path / "j"
    first, _ = _run(meta, tmp_path, _journal(tmp_path, root), ads=2)
    assert first.error == ""
    if edit == "rename_adset":
        next(o for o in meta.objects.values() if o["_kind"] == "adset")["name"] = "Edited by hand"
    elif edit == "drop_ad":
        oid = next(i for i, o in meta.objects.items() if o["_kind"] == "ad")
        del meta.objects[oid]
    elif edit == "drop_creative":
        oid = next(i for i, o in meta.objects.items() if o["_kind"] == "creative")
        del meta.objects[oid]
        meta.objects.setdefault("x", {"_kind": "none"})
        for o in meta.objects.values():       # keep the fake's ad->creative join intact
            if o.get("_kind") == "ad" and o.get("creative", {}).get("creative_id") == oid:
                o["creative"]["creative_id"] = next(i for i, c in meta.objects.items() if c["_kind"] == "creative")
    elif edit == "unpause":
        next(o for o in meta.objects.values() if o["_kind"] == "campaign")["status"] = "ACTIVE"
    posts = len(meta.posts)
    out, _ = _run(meta, tmp_path, _journal(tmp_path, root), ads=2)
    assert out.stop_kind == "resume_mismatch", out.error
    assert len(meta.posts) == posts, "a mismatch must not write anything"
    assert needle in out.error and out.operation["partial_ids"]


def test_deleted_campaign_is_not_silently_recreated(tmp_path):
    meta = FakeMeta()
    root = tmp_path / "j"
    _run(meta, tmp_path, _journal(tmp_path, root, ads=1), ads=1)
    camp = next(i for i, o in meta.objects.items() if o["_kind"] == "campaign")
    meta.objects[camp]["status"] = "DELETED"
    posts = len(meta.posts)
    out, _ = _run(meta, tmp_path, _journal(tmp_path, root, ads=1), ads=1)
    assert out.stop_kind == "resume_mismatch" and len(meta.posts) == posts
    assert camp in out.error


def test_recorded_image_missing_in_the_account_is_not_uploaded_again(tmp_path):
    meta = FakeMeta()
    root = tmp_path / "j"
    meta.fail_next["ads"] = "unsent"
    first, _ = _run(meta, tmp_path, _journal(tmp_path, root, ads=1), ads=1)
    assert first.stop_kind == "unknown"
    meta.images.clear()                                    # the image vanished from the library
    # ad 1 is not yet created, so the run reaches its media lookup
    out, _ = _run(meta, tmp_path, _journal(tmp_path, root, ads=1), ads=1)
    assert out.stop_kind == "resume_mismatch" and "image" in out.error
    assert len([p for p, _ in meta.posts if p.endswith("/adimages")]) == 1


def test_cancel_keeps_partial_output(tmp_path):
    meta = FakeMeta()
    root = tmp_path / "j"
    seen = {"n": 0}

    def cancelled():
        seen["n"] += 1
        return seen["n"] > 4
    out, _ = _run(meta, tmp_path, _journal(tmp_path, root), cancelled=cancelled)
    assert out.stop_kind == "cancelled"
    assert json.loads((next(Path(root).glob("op-*.json"))).read_text(encoding="utf-8"))["state"] == "cancelled"
    assert out.operation["partial_ids"]


def test_no_journal_behaves_as_before(tmp_path):
    meta = FakeMeta()
    out, _ = _run(meta, tmp_path, None, ads=1)
    assert out.error == "" and out.operation == {}


def test_publisher_binds_the_request_per_campaign_and_refuses_a_conflict(tmp_path, monkeypatch):
    from src.web import publish_api

    root = tmp_path / "j"
    monkeypatch.setattr(oj, "default_dir", lambda: root)
    p1, p2 = _plan(tmp_path, ads=2), _plan(tmp_path, ads=1)
    key = f"req-1#{p1.tag}"
    oj.OperationJournal.open(p1, request_id=key)
    assert publish_api.journal_enabled()
    with pytest.raises(oj.RequestConflict):
        oj.OperationJournal.open(p2, request_id=key)         # same submit + campaign, other plan revision
    oj.OperationJournal.open(p1, request_id=key)             # a duplicate submit of the same revision is fine
