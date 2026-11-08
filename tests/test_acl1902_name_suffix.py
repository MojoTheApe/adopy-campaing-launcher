"""ACL-1902 → ACL-2250: имя кампании занято - робот берёт `NAME copy 1`, `NAME copy 2` ... (раньше NAME_2 ... NAME_20).

Meta здесь - `FakeMeta`. Имя выбирается одной функцией до первой записи и в боевом запуске,
и в Check plan; повторный запуск после обрыва дописывает свою кампанию (в том числе копию)
и не плодит дубль. Счётчик получают кампания и все её наборы (ACL-2250, D1). Остальные сценарии -
в `test_acl2250_copy_counter.py` и `test_acl2250_resume_and_job.py`.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_api import name_receipt as nr  # noqa: E402
from src.fb_api import preflight_rules as pf  # noqa: E402
from tests.test_acl1761_api_create import ACT, FakeMeta, _draft, _publisher, _run  # noqa: E402

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


def _end_job(base=BASE):
    nr.delete(ACT, base)        # конец задания (`finish_name_records`): следующая публикация - повтор


def _verdict(res):
    return next(v for v in res["audit"] if v["rule"] == "existing_diffs")


def test_free_name_stays_as_is(tmp_path):
    meta = FakeMeta()
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert out.error == "" and out.published_as == BASE
    assert _names(meta) == [BASE]
    assert not any("ublished as" in w for w in out.warnings)


def test_identical_campaign_of_an_unfinished_job_is_resumed_with_no_new_write(tmp_path):
    meta = FakeMeta()
    first, _ = _run(meta, _draft(tmp_path, ads=1))
    assert first.error == ""
    n = _writes(meta)
    again, _ = _run(meta, _draft(tmp_path, ads=1))          # квитанция жива: задание не закончено
    assert again.error == "" and again.published_as == BASE
    assert again.reused["campaign"] == first.campaign_id and _names(meta) == [BASE]
    assert _writes(meta) == n, "повторный запуск ничего не пишет"


def test_identical_campaign_of_a_finished_job_gets_copy_1(tmp_path):
    meta = FakeMeta()
    assert _run(meta, _draft(tmp_path, ads=1))[0].error == ""
    _end_job()
    again, _ = _run(meta, _draft(tmp_path, ads=1))
    assert again.error == "" and again.published_as == BASE + " copy 1"
    assert _names(meta) == [BASE, BASE + " copy 1"]


def test_ours_but_different_goes_to_copy_1_then_copy_2(tmp_path):
    meta = FakeMeta()
    assert _run(meta, _draft(tmp_path, ads=1))[0].error == ""
    _campaign(meta, BASE)["daily_budget"] = "999"
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert out.error == "" and out.published_as == BASE + " copy 1"
    assert _names(meta) == [BASE, BASE + " copy 1"]
    assert out.created["campaign"] and "campaign" not in out.reused
    assert any("ublished as 'MX Leads 1 copy 1'" in w for w in out.warnings)
    _end_job()
    _campaign(meta, BASE + " copy 1")["daily_budget"] = "777"
    out2, _ = _run(meta, _draft(tmp_path, ads=1))
    assert out2.error == "" and out2.published_as == BASE + " copy 2"
    assert _names(meta) == [BASE, BASE + " copy 1", BASE + " copy 2"]


def test_copy_has_its_own_tag_and_ad_sets_get_the_counter_but_ads_keep_builder_names(tmp_path):
    meta = FakeMeta()
    assert _run(meta, _draft(tmp_path, ads=1))[0].error == ""
    _campaign(meta, BASE)["daily_budget"] = "999"
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert out.error == "" and out.readback["ok"] is True
    assert _campaign(meta, BASE)["adlabels"] != _campaign(meta, BASE + " copy 1")["adlabels"]
    sets = [o for o in meta.objects.values() if o["_kind"] == "adset"]
    assert sorted(o["name"] for o in sets) == [BASE, BASE + " copy 1"]      # ACL-2250 (D1): набор тоже со счётчиком
    assert {o["campaign_id"] for o in sets} == {c["id"] for c in meta.objects.values()
                                                if c["_kind"] == "campaign"}
    ads = [o["name"] for o in meta.objects.values() if o["_kind"] == "ad"]
    assert len(ads) == 2 and ads[0] == ads[1]
    # Креативы второго запуска не берутся у первого: имя креатива содержит метку.
    assert sum(1 for o in meta.objects.values() if o["_kind"] == "creative") == 2


def test_same_name_without_our_tag_goes_to_copy_1(tmp_path):
    meta = FakeMeta()
    _foreign(meta, BASE)
    n = _writes(meta)
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert out.error == "" and out.published_as == BASE + " copy 1"
    assert _names(meta) == [BASE, BASE + " copy 1"] and _writes(meta) > n
    assert any("already on the ad account (1 earlier)" in w for w in out.warnings)


def test_identical_copy_from_an_unfinished_job_is_resumed(tmp_path):
    meta = FakeMeta()
    assert _run(meta, _draft(tmp_path, ads=1))[0].error == ""
    _campaign(meta, BASE)["daily_budget"] = "999"
    first, _ = _run(meta, _draft(tmp_path, ads=1))
    assert first.published_as == BASE + " copy 1"
    n = _writes(meta)
    again, _ = _run(meta, _draft(tmp_path, ads=1))
    assert again.error == "" and again.published_as == BASE + " copy 1"
    assert again.reused["campaign"] == first.campaign_id
    assert _writes(meta) == n and len(_names(meta)) == 2, "дубля нет, записей нет"


def test_resume_of_a_partial_run_reuses_the_copy(tmp_path):
    meta = FakeMeta()
    assert _run(meta, _draft(tmp_path, ads=1))[0].error == ""
    _campaign(meta, BASE)["daily_budget"] = "999"
    meta.fail_next["ads"] = "unsent"          # обрыв на объявлении: копия и её группа уже есть
    broken, _ = _run(meta, _draft(tmp_path, ads=1))
    assert broken.stop_kind == "unknown" and broken.published_as == BASE + " copy 1"
    meta.fail_next.clear()
    resumed, _ = _run(meta, _draft(tmp_path, ads=1))
    assert resumed.error == "" and resumed.published_as == BASE + " copy 1"
    assert resumed.reused["campaign"] and _names(meta) == [BASE, BASE + " copy 1"]


def test_twenty_one_taken_names_do_not_stop_the_run(tmp_path):
    meta = FakeMeta()
    _foreign(meta, BASE)
    for i in range(1, 21):
        _foreign(meta, f"{BASE} copy {i}")
    n = _writes(meta)
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert out.error == "" and out.stop_kind == "" and out.published_as == BASE + " copy 21"
    assert _writes(meta) > n


def test_unknown_write_readback_uses_the_resolved_name(tmp_path):
    meta = FakeMeta()
    _foreign(meta, BASE)
    meta.fail_next["campaigns"] = "lost"      # создано, ответ потерян
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert out.error == "" and out.published_as == BASE + " copy 1"
    assert _names(meta) == [BASE, BASE + " copy 1"], "ровно одна новая кампания, без дубля"
    assert out.created["campaign"] == _campaign(meta, BASE + " copy 1")["id"]


def test_deleted_and_archived_campaigns_do_not_count(tmp_path):
    meta = FakeMeta()
    _foreign(meta, BASE, status="DELETED")
    _foreign(meta, BASE, status="ARCHIVED")
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert out.error == "" and out.published_as == BASE
    assert BASE + " copy 1" not in _names(meta)


def test_a_name_already_ending_in_a_number_gets_the_counter_appended(tmp_path):
    meta = FakeMeta()
    _foreign(meta, "MX Leads_2")
    out, _ = _run(meta, _draft_named(tmp_path, "MX Leads_2"))
    assert out.error == "" and out.published_as == "MX Leads_2 copy 1"


def test_one_search_covers_the_whole_family(tmp_path):
    meta = FakeMeta()
    for i in range(0, 5):
        _foreign(meta, BASE if i == 0 else f"{BASE} copy {i}")
    meta.gets.clear()
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert out.published_as == BASE + " copy 5"
    searches = [kw for p, kw in meta.gets if p.endswith("/campaigns")]
    assert len(searches) == 1, "один поиск на всё семейство"


def test_check_plan_warns_with_the_resolved_name_and_has_no_error(tmp_path):
    meta = FakeMeta()
    assert _run(meta, _draft(tmp_path, ads=1))[0].error == ""
    _campaign(meta, BASE)["daily_budget"] = "999"
    n = _writes(meta)
    pub = _publisher(meta, dry_run=True)
    res = pub.publish_draft(_draft(tmp_path, ads=1))
    assert res["success"] is True and _writes(meta) == n and pub._client.writes == 0
    assert res["campaign_name"] == BASE and res["published_as"] == BASE + " copy 1"
    assert res["plan"]["campaign"]["name"] == BASE + " copy 1"
    summary = res["plan"]["summary"]
    assert summary["campaign"] == BASE + " copy 1" and summary["requested_name"] == BASE
    assert summary["existing"] is False
    verdict = _verdict(res)
    assert verdict["status"] == "warning"
    assert "'MX Leads 1 copy 1'" in verdict["detail"] and "already on the ad account" in verdict["detail"]
    assert not pf.blocks(res["audit"])


def test_check_plan_of_a_free_name_is_ok_and_an_unsure_name_is_an_error(tmp_path):
    res = _publisher(FakeMeta(), dry_run=True).publish_draft(_draft(tmp_path, ads=1))
    assert _verdict(res)["status"] == "ok"
    meta = FakeMeta()
    for _ in range(205):
        _foreign(meta, BASE)
    res = _publisher(meta, dry_run=True).publish_draft(_draft(tmp_path, ads=1))
    verdict = _verdict(res)
    assert verdict["status"] == "error" and "too many campaigns" in verdict["detail"]
    assert pf.blocks(res["audit"])


# ───────────────────────── ревью: имя из билдера, S1-S3, N3, N5 ─────────────────────────

def test_results_keep_the_builder_name_and_report_published_as(tmp_path):
    meta = FakeMeta()
    assert _run(meta, _draft(tmp_path, ads=1))[0].error == ""
    _campaign(meta, BASE)["daily_budget"] = "999"
    res = _publisher(meta, dry_run=True).publish_draft(_draft(tmp_path, ads=1))
    assert res["campaign_name"] == BASE and res["published_as"] == BASE + " copy 1"
    live = _publisher(meta).publish_draft(_draft(tmp_path, ads=1))
    assert live["campaign_name"] == BASE and live["published_as"] == BASE + " copy 1" and live["success"] is True
    # Без переименования поле пустое (другая «Meta»: квитанцию прошлой убираем).
    _end_job()
    plain = _publisher(FakeMeta(), dry_run=True).publish_draft(_draft(tmp_path, ads=1))
    assert plain["campaign_name"] == BASE and plain["published_as"] == ""


def test_job_progress_and_draft_status_have_no_orphan_for_a_renamed_campaign():
    from src.web import publish_jobs as pj
    from src.web import publish_progress as pp

    result = {"campaign_name": BASE, "published_as": BASE + " copy 1", "success": True, "publishable": True,
              "blocking_failures": [], "warnings": []}
    job = {"status": "done", "campaign_names": [BASE], "results": [result], "job_id": "j1"}
    prog = pp.job_progress(job, path=Path("nonexistent.jsonl"))
    assert [c["name"] for c in prog["campaigns"]] == [BASE]
    assert prog["campaigns"][0]["result"]["publishable"] is True
    assert prog["campaigns"][0]["result"]["published_as"] == BASE + " copy 1"
    status = pj._summarize_results([result], expected_total=1)
    assert status["campaign_names"] == [BASE] and status["published_as"] == {BASE: BASE + " copy 1"}


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


def test_adset_that_lost_its_tag_is_resumed_and_with_a_real_diff_it_gets_a_copy(tmp_path):
    meta = FakeMeta()
    assert _run(meta, _draft(tmp_path, ads=1))[0].error == ""
    adset = next(o for o in meta.objects.values() if o["_kind"] == "adset")
    adset["adlabels"] = []
    n = _writes(meta)
    out, client = _run(meta, _draft(tmp_path, ads=1))          # задание не закончено: дописываем ту же кампанию
    assert out.error == "" and out.published_as == BASE and out.reused["campaign"]
    assert _writes(meta) == n and client.writes == 0 and _names(meta) == [BASE]
    _campaign(meta, BASE)["daily_budget"] = "999"
    out2, _ = _run(meta, _draft(tmp_path, ads=1))
    assert out2.error == "" and out2.published_as == BASE + " copy 1"


def test_a_builder_campaign_literally_named_x_copy_1_is_family_and_a_base_with_other_ads_is_not_resumed(tmp_path):
    meta = FakeMeta()
    base = _foreign(meta, BASE)
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert out.error == "" and out.published_as == BASE + " copy 1"
    assert meta.objects[base]["name"] == BASE


def test_too_long_name_stops_before_any_write(tmp_path):
    meta = FakeMeta()
    long_name = "L" * 399
    _foreign(meta, long_name)
    out, client = _run(meta, _draft_named(tmp_path, long_name))
    assert out.stop_kind == "name_too_long" and meta.posts == [] and client.writes == 0
