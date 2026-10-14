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
    assert out.error == "" and out.campaign_name == BASE
    assert _names(meta) == [BASE]
    assert not any("ublished as" in w for w in out.warnings)


def test_ours_and_identical_is_reused_with_no_new_write(tmp_path):
    meta = FakeMeta()
    first, _ = _run(meta, _draft(tmp_path, ads=1))
    assert first.error == ""
    n = _writes(meta)
    again, _ = _run(meta, _draft(tmp_path, ads=1))
    assert again.error == "" and again.campaign_name == BASE
    assert again.reused["campaign"] == first.campaign_id and _names(meta) == [BASE]
    assert _writes(meta) == n, "повторный запуск ничего не пишет"


def test_ours_but_different_goes_to_suffix_2_then_3(tmp_path):
    meta = FakeMeta()
    assert _run(meta, _draft(tmp_path, ads=1))[0].error == ""
    _campaign(meta, BASE)["daily_budget"] = "999"
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert out.error == "" and out.campaign_name == BASE + "_2"
    assert _names(meta) == [BASE, BASE + "_2"]
    assert out.created["campaign"] and "campaign" not in out.reused
    assert any("ublished as 'MX Leads 1_2'" in w for w in out.warnings)
    # _2 тоже разошлась - берём _3.
    _campaign(meta, BASE + "_2")["daily_budget"] = "777"
    out3, _ = _run(meta, _draft(tmp_path, ads=1))
    assert out3.error == "" and out3.campaign_name == BASE + "_3"
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
    assert out.error == "" and out.campaign_name == BASE + "_2"
    assert _names(meta) == [BASE, BASE + "_2"] and _writes(meta) > n
    assert any("another campaign" in w for w in out.warnings)


def test_identical_suffixed_campaign_from_an_earlier_run_is_reused(tmp_path):
    meta = FakeMeta()
    assert _run(meta, _draft(tmp_path, ads=1))[0].error == ""
    _campaign(meta, BASE)["daily_budget"] = "999"
    first, _ = _run(meta, _draft(tmp_path, ads=1))
    assert first.campaign_name == BASE + "_2"
    n = _writes(meta)
    again, _ = _run(meta, _draft(tmp_path, ads=1))
    assert again.error == "" and again.campaign_name == BASE + "_2"
    assert again.reused["campaign"] == first.campaign_id
    assert _writes(meta) == n and len(_names(meta)) == 2, "дубля нет, записей нет"


def test_resume_of_a_partial_run_reuses_the_suffixed_campaign(tmp_path):
    meta = FakeMeta()
    assert _run(meta, _draft(tmp_path, ads=1))[0].error == ""
    _campaign(meta, BASE)["daily_budget"] = "999"
    meta.fail_next["ads"] = "unsent"          # обрыв на объявлении: кампания _2 и группа уже есть
    broken, _ = _run(meta, _draft(tmp_path, ads=1))
    assert broken.stop_kind == "unknown" and broken.campaign_name == BASE + "_2"
    meta.fail_next.clear()
    resumed, _ = _run(meta, _draft(tmp_path, ads=1))
    assert resumed.error == "" and resumed.campaign_name == BASE + "_2"
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
    assert out.error == "" and out.campaign_name == BASE + "_2"
    assert _names(meta) == [BASE, BASE + "_2"], "ровно одна новая кампания, без дубля"
    assert out.created["campaign"] == _campaign(meta, BASE + "_2")["id"]


def test_deleted_and_archived_campaigns_do_not_count(tmp_path):
    meta = FakeMeta()
    _foreign(meta, BASE, status="DELETED")
    _foreign(meta, BASE, status="ARCHIVED")
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert out.error == "" and out.campaign_name == BASE
    assert BASE + "_2" not in _names(meta)


def test_a_name_already_ending_in_a_number_gets_the_suffix_appended(tmp_path):
    meta = FakeMeta()
    _foreign(meta, "MX Leads_2")
    out, _ = _run(meta, _draft_named(tmp_path, "MX Leads_2"))
    assert out.error == "" and out.campaign_name == "MX Leads_2_2"


def test_one_search_covers_all_candidates(tmp_path):
    meta = FakeMeta()
    for i in range(1, 5):
        _foreign(meta, BASE if i == 1 else f"{BASE}_{i}")
    meta.gets.clear()
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert out.campaign_name == BASE + "_5"
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
    assert res["campaign_name"] == BASE + "_2"
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
