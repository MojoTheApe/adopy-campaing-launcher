"""ACL-1902: имя кампании занято чужой/отличающейся - робот сам берёт NAME_2, NAME_3 ... до NAME_20.

Meta здесь - `FakeMeta`. Имя выбирается одной функцией до первой записи и в боевом запуске,
и в Check plan; повторный запуск после обрыва находит свою кампанию (в том числе NAME_2)
и не плодит дубль.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_api import preflight_rules as pf  # noqa: E402
from tests.test_acl1761_api_create import FakeMeta, _draft, _publisher, _run  # noqa: E402

BASE = "MX Leads 1"


def _names(meta):
    return sorted(o["name"] for o in meta.objects.values() if o["_kind"] == "campaign")


def _campaign(meta, name):
    return next(o for o in meta.objects.values() if o["_kind"] == "campaign" and o["name"] == name)


def _writes(meta):
    return len(meta.posts)


def _draft_named(tmp_path, name):
    d = _draft(tmp_path, ads=1)
    d.name = name
    return d


def _foreign(meta, name, status="PAUSED"):
    """Кампания с таким именем, но без нашей метки (не ACL)."""
    return meta._new("campaign", name=name, status=status, adlabels=[], objective="OUTCOME_LEADS",
                     daily_budget="2000", bid_strategy="LOWEST_COST_WITHOUT_CAP")


def _verdict(res):
    return next(v for v in res["audit"] if v["rule"] == "existing_diffs")


def test_free_name_stays_as_is(tmp_path):
    meta = FakeMeta()
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert out.error == "" and out.published_as == BASE
    assert _names(meta) == [BASE]
    assert not any("ublished as" in w for w in out.warnings)


def test_ours_and_identical_is_reused_with_no_new_write(tmp_path):
    meta = FakeMeta()
    first, _ = _run(meta, _draft(tmp_path, ads=1))
    assert first.error == ""
    n = _writes(meta)
    again, _ = _run(meta, _draft(tmp_path, ads=1))
    assert again.error == "" and again.published_as == BASE
    assert again.reused["campaign"] == first.campaign_id and _names(meta) == [BASE]
    assert _writes(meta) == n, "повторный запуск ничего не пишет"


def test_ours_but_different_goes_to_suffix_2_then_3(tmp_path):
    meta = FakeMeta()
    assert _run(meta, _draft(tmp_path, ads=1))[0].error == ""
    _campaign(meta, BASE)["daily_budget"] = "999"
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert out.error == "" and out.published_as == BASE + "_2"
    assert _names(meta) == [BASE, BASE + "_2"]
    assert out.created["campaign"] and "campaign" not in out.reused
    assert any("ublished as 'MX Leads 1_2'" in w for w in out.warnings)
    # _2 тоже разошлась - берём _3.
    _campaign(meta, BASE + "_2")["daily_budget"] = "777"
    out3, _ = _run(meta, _draft(tmp_path, ads=1))
    assert out3.error == "" and out3.published_as == BASE + "_3"
    assert _names(meta) == [BASE, BASE + "_2", BASE + "_3"]


def test_suffixed_campaign_has_its_own_tag_and_children_keep_builder_names(tmp_path):
    meta = FakeMeta()
    assert _run(meta, _draft(tmp_path, ads=1))[0].error == ""
    _campaign(meta, BASE)["daily_budget"] = "999"
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert out.error == "" and out.readback["ok"] is True
    assert _campaign(meta, BASE)["adlabels"] != _campaign(meta, BASE + "_2")["adlabels"]
    sets = [o for o in meta.objects.values() if o["_kind"] == "adset"]
    assert sorted(o["name"] for o in sets) == [BASE, BASE]
    assert {o["campaign_id"] for o in sets} == {c["id"] for c in meta.objects.values()
                                                if c["_kind"] == "campaign"}
    # Креативы второго запуска не берутся у первого: имя креатива содержит метку.
    assert sum(1 for o in meta.objects.values() if o["_kind"] == "creative") == 2


def test_same_name_without_our_tag_goes_to_suffix_2(tmp_path):
    meta = FakeMeta()
    _foreign(meta, BASE)
    n = _writes(meta)
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert out.error == "" and out.published_as == BASE + "_2"
    assert _names(meta) == [BASE, BASE + "_2"] and _writes(meta) > n
    assert any("another campaign" in w for w in out.warnings)


def test_identical_suffixed_campaign_from_an_earlier_run_is_reused(tmp_path):
    meta = FakeMeta()
    assert _run(meta, _draft(tmp_path, ads=1))[0].error == ""
    _campaign(meta, BASE)["daily_budget"] = "999"
    first, _ = _run(meta, _draft(tmp_path, ads=1))
    assert first.published_as == BASE + "_2"
    n = _writes(meta)
    again, _ = _run(meta, _draft(tmp_path, ads=1))
    assert again.error == "" and again.published_as == BASE + "_2"
    assert again.reused["campaign"] == first.campaign_id
    assert _writes(meta) == n and len(_names(meta)) == 2, "дубля нет, записей нет"


def test_resume_of_a_partial_run_reuses_the_suffixed_campaign(tmp_path):
    meta = FakeMeta()
    assert _run(meta, _draft(tmp_path, ads=1))[0].error == ""
    _campaign(meta, BASE)["daily_budget"] = "999"
    meta.fail_next["ads"] = "unsent"          # обрыв на объявлении: кампания _2 и группа уже есть
    broken, _ = _run(meta, _draft(tmp_path, ads=1))
    assert broken.stop_kind == "unknown" and broken.published_as == BASE + "_2"
    meta.fail_next.clear()
    resumed, _ = _run(meta, _draft(tmp_path, ads=1))
    assert resumed.error == "" and resumed.published_as == BASE + "_2"
    assert resumed.reused["campaign"] and _names(meta) == [BASE, BASE + "_2"]


def test_all_twenty_names_taken_stops_before_any_write(tmp_path):
    meta = FakeMeta()
    for i in range(1, 21):
        _foreign(meta, BASE if i == 1 else f"{BASE}_{i}")
    n = _writes(meta)
    out, client = _run(meta, _draft(tmp_path, ads=1))
    assert out.stop_kind == "name_taken" and "20" in out.error and "MX Leads 1_20" in out.error
    assert _writes(meta) == n and client.writes == 0 and out.created == {}


def test_unknown_write_readback_uses_the_resolved_name(tmp_path):
    meta = FakeMeta()
    _foreign(meta, BASE)
    meta.fail_next["campaigns"] = "lost"      # создано, ответ потерян
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert out.error == "" and out.published_as == BASE + "_2"
    assert _names(meta) == [BASE, BASE + "_2"], "ровно одна новая кампания, без дубля"
    assert out.created["campaign"] == _campaign(meta, BASE + "_2")["id"]


def test_deleted_and_archived_campaigns_do_not_count(tmp_path):
    meta = FakeMeta()
    _foreign(meta, BASE, status="DELETED")
    _foreign(meta, BASE, status="ARCHIVED")
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert out.error == "" and out.published_as == BASE
    assert BASE + "_2" not in _names(meta)


def test_a_name_already_ending_in_a_number_gets_the_suffix_appended(tmp_path):
    meta = FakeMeta()
    _foreign(meta, "MX Leads_2")
    out, _ = _run(meta, _draft_named(tmp_path, "MX Leads_2"))
    assert out.error == "" and out.published_as == "MX Leads_2_2"


def test_one_search_covers_all_candidates(tmp_path):
    meta = FakeMeta()
    for i in range(1, 5):
        _foreign(meta, BASE if i == 1 else f"{BASE}_{i}")
    meta.gets.clear()
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert out.published_as == BASE + "_5"
    searches = [kw for p, kw in meta.gets if p.endswith("/campaigns")]
    assert len(searches) == 1, "один поиск на все кандидаты (NAME_2 ... уже в его выдаче)"


def test_check_plan_warns_with_the_resolved_name_and_has_no_error(tmp_path):
    meta = FakeMeta()
    assert _run(meta, _draft(tmp_path, ads=1))[0].error == ""
    _campaign(meta, BASE)["daily_budget"] = "999"
    n = _writes(meta)
    pub = _publisher(meta, dry_run=True)
    res = pub.publish_draft(_draft(tmp_path, ads=1))
    assert res["success"] is True and _writes(meta) == n and pub._client.writes == 0
    assert res["campaign_name"] == BASE and res["published_as"] == BASE + "_2"
    assert res["plan"]["campaign"]["name"] == BASE + "_2"
    summary = res["plan"]["summary"]
    assert summary["campaign"] == BASE + "_2" and summary["requested_name"] == BASE
    assert summary["existing"] is False
    verdict = _verdict(res)
    assert verdict["status"] == "warning"
    assert "'MX Leads 1_2'" in verdict["detail"] and "different settings" in verdict["detail"]
    assert "daily budget" in verdict["detail"]
    assert not pf.blocks(res["audit"])


def test_check_plan_of_a_free_name_is_ok_and_all_taken_is_an_error(tmp_path):
    res = _publisher(FakeMeta(), dry_run=True).publish_draft(_draft(tmp_path, ads=1))
    assert _verdict(res)["status"] == "ok"
    meta = FakeMeta()
    for i in range(1, 21):
        _foreign(meta, BASE if i == 1 else f"{BASE}_{i}")
    res = _publisher(meta, dry_run=True).publish_draft(_draft(tmp_path, ads=1))
    verdict = _verdict(res)
    assert verdict["status"] == "error" and "20" in verdict["detail"]
    assert pf.blocks(res["audit"])


# ───────────────────────── ревью: имя из билдера, S1-S3, N3, N5 ─────────────────────────

def test_results_keep_the_builder_name_and_report_published_as(tmp_path):
    meta = FakeMeta()
    assert _run(meta, _draft(tmp_path, ads=1))[0].error == ""
    _campaign(meta, BASE)["daily_budget"] = "999"
    res = _publisher(meta, dry_run=True).publish_draft(_draft(tmp_path, ads=1))
    assert res["campaign_name"] == BASE and res["published_as"] == BASE + "_2"
    live = _publisher(meta).publish_draft(_draft(tmp_path, ads=1))
    assert live["campaign_name"] == BASE and live["published_as"] == BASE + "_2" and live["success"] is True
    # Без переименования поле пустое.
    plain = _publisher(FakeMeta(), dry_run=True).publish_draft(_draft(tmp_path, ads=1))
    assert plain["campaign_name"] == BASE and plain["published_as"] == ""


def test_job_progress_and_draft_status_have_no_orphan_for_a_renamed_campaign():
    from src.web import publish_jobs as pj
    from src.web import publish_progress as pp

    result = {"campaign_name": BASE, "published_as": BASE + "_2", "success": True, "publishable": True,
              "blocking_failures": [], "warnings": []}
    job = {"status": "done", "campaign_names": [BASE], "results": [result], "job_id": "j1"}
    prog = pp.job_progress(job, path=Path("nonexistent.jsonl"))
    assert [c["name"] for c in prog["campaigns"]] == [BASE]
    assert prog["campaigns"][0]["result"]["publishable"] is True
    assert prog["campaigns"][0]["result"]["published_as"] == BASE + "_2"
    status = pj._summarize_results([result], expected_total=1)
    assert status["campaign_names"] == [BASE] and status["published_as"] == {BASE: BASE + "_2"}


def test_truncated_search_is_checked_by_exact_name(tmp_path):
    meta = FakeMeta()
    for i in range(205):
        _foreign(meta, f"{BASE} decoy {i}")
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert out.error == "" and out.published_as == BASE
    ops = [kw["filtering"][0]["operator"] for p, kw in meta.gets if p.endswith("/campaigns")]
    assert "EQUAL" in ops


def test_truncated_search_that_stays_unsure_stops_before_any_write(tmp_path):
    meta = FakeMeta()
    for _ in range(205):
        _foreign(meta, BASE)
    out, client = _run(meta, _draft(tmp_path, ads=1))
    assert out.stop_kind == "name_unsure" and meta.posts == [] and client.writes == 0


def test_adset_that_lost_its_tag_is_resumed_and_with_a_real_diff_it_suffixes(tmp_path):
    meta = FakeMeta()
    assert _run(meta, _draft(tmp_path, ads=1))[0].error == ""
    adset = next(o for o in meta.objects.values() if o["_kind"] == "adset")
    adset["adlabels"] = []
    n = _writes(meta)
    out, client = _run(meta, _draft(tmp_path, ads=1))
    assert out.error == "" and out.published_as == BASE and out.reused["campaign"]
    assert _writes(meta) == n and client.writes == 0 and _names(meta) == [BASE]
    _campaign(meta, BASE)["daily_budget"] = "999"
    out2, _ = _run(meta, _draft(tmp_path, ads=1))
    assert out2.error == "" and out2.published_as == BASE + "_2"
    assert any("(earlier campaign) is left as is" in w and "daily budget" in w for w in out2.warnings)


def test_a_builder_campaign_literally_named_x_2_is_not_taken_for_our_suffix(tmp_path):
    meta = FakeMeta()
    # Кампания, построенная билдером под именем «MX Leads 1_2», с другим объявлением.
    other = _draft(tmp_path, ads=1)
    other.name = BASE + "_2"
    assert _run(meta, other)[0].error == ""
    for o in meta.objects.values():
        if o["_kind"] == "ad":
            o["name"] = "Somebody else's ad"
    base = _foreign(meta, BASE)
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert out.error == "" and out.published_as == BASE + "_3"
    assert meta.objects[base]["name"] == BASE
    assert any("'MX Leads 1_2' is a campaign with other ads" in w for w in out.warnings)


def test_scan_stops_when_too_many_of_our_campaigns_would_be_diffed(tmp_path):
    from src.fb_api import creator as cr

    meta = FakeMeta()
    for i in range(1, 9):
        name = BASE if i == 1 else f"{BASE}_{i}"
        meta._new("campaign", name=name, status="PAUSED", daily_budget="1",
                  adlabels=[{"name": f"acl-{cr.campaign_tag('act_1234567890', name)}"}])
    out, client = _run(meta, _draft(tmp_path, ads=1))
    assert out.stop_kind == "name_scan_limit" and meta.posts == [] and client.writes == 0


def test_too_long_suffixed_name_stops_before_any_write(tmp_path):
    meta = FakeMeta()
    long_name = "L" * 399
    _foreign(meta, long_name)
    out, client = _run(meta, _draft_named(tmp_path, long_name))
    assert out.stop_kind == "name_too_long" and meta.posts == [] and client.writes == 0
