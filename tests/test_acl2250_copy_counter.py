"""ACL-2250: повтор имени получает счётчик `copy 1, 2, 3 ...` у кампании и всех её наборов.

Meta - `FakeMeta`. Квитанции имён лежат во временной папке теста (`ACL_CONFIG_DIR`); «конец задания»
в тестах без издателя - `_end_job` (удаляет квитанцию, как `publisher.finish_job`).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_api import creator as cr  # noqa: E402
from src.fb_api import name_receipt as nr  # noqa: E402
from tests.test_acl1761_api_create import ACT, FakeMeta, _draft, _run  # noqa: E402

BASE = "MX Leads 1"


def _names(meta, kind="campaign"):
    return sorted(o["name"] for o in meta.objects.values() if o["_kind"] == kind)


def _campaign(meta, name):
    return next(o for o in meta.objects.values() if o["_kind"] == "campaign" and o["name"] == name)


def _end_job(base=BASE):
    nr.delete(ACT, base)


def _foreign(meta, name, status="PAUSED"):
    return meta._new("campaign", name=name, status=status, adlabels=[], objective="OUTCOME_LEADS",
                     daily_budget="2000", bid_strategy="LOWEST_COST_WITHOUT_CAP")


def _draft_named(tmp_path, name, ads=1):
    d = _draft(tmp_path, ads=ads)
    d.name = name
    return d


def _finished_copy(meta, tmp_path, ads=1):
    out, _ = _run(meta, _draft(tmp_path, ads=ads))
    assert out.error == "", out.error
    _end_job()
    return out


# ───────────────────────── имена ─────────────────────────

def test_1_no_family_keeps_the_builder_name_and_ad_set_names(tmp_path):
    meta = FakeMeta()
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert out.error == "" and out.published_as == BASE
    assert _names(meta) == [BASE] and _names(meta, "adset") == [BASE]
    assert not any("ublished as" in w for w in out.warnings)


def test_2_finished_campaign_gets_copy_1_on_campaign_and_ad_sets(tmp_path):
    meta = FakeMeta()
    _finished_copy(meta, tmp_path)
    writes = len(meta.posts)
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert out.error == "" and out.published_as == BASE + " copy 1"
    assert out.readback["ok"] is True
    assert _names(meta) == [BASE, BASE + " copy 1"]
    assert _names(meta, "adset") == [BASE, BASE + " copy 1"]
    assert _names(meta, "ad")[0] == _names(meta, "ad")[1], "объявления сохраняют имена билдера"
    assert len([p for p, _ in meta.posts[writes:] if p.endswith("/campaigns")]) == 1
    assert any("Published as 'MX Leads 1 copy 1'" in w for w in out.warnings)


def test_3_next_number_is_highest_plus_one(tmp_path):
    meta = FakeMeta()
    _finished_copy(meta, tmp_path)
    assert _run(meta, _draft(tmp_path, ads=1))[0].published_as == BASE + " copy 1"
    _end_job()
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert out.published_as == BASE + " copy 2"
    _foreign(meta, BASE + " copy 4")            # дырка: 3 нет, верх - 4
    _end_job()
    assert _run(meta, _draft(tmp_path, ads=1))[0].published_as == BASE + " copy 5"


def test_3b_base_deleted_but_copy_1_live_gives_copy_2(tmp_path):
    meta = FakeMeta()
    _foreign(meta, BASE + " copy 1")
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert out.error == "" and out.published_as == BASE + " copy 2"


def test_4_twenty_five_finished_copies_give_copy_26_reading_only_the_top_row(tmp_path):
    meta = FakeMeta()
    _foreign(meta, BASE)
    for i in range(1, 26):
        _foreign(meta, f"{BASE} copy {i}")
    ids = {o["id"] for o in meta.objects.values() if o["_kind"] == "campaign"}
    meta.gets.clear()
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert out.error == "" and out.published_as == BASE + " copy 26"
    # Верхняя строка чужая (без метки) - ни одна из 26 строк семейства не читается подробно.
    assert not [p for p, _ in meta.gets if p.strip("/").split("/")[0] in ids - {"111"}]      # 111 - id Страницы
    assert len([p for p, _ in meta.gets if p.endswith("/campaigns")]) == 1


def test_5_foreign_different_or_turned_on_gets_a_new_copy(tmp_path):
    meta = FakeMeta()
    _foreign(meta, BASE)
    assert _run(meta, _draft(tmp_path, ads=1))[0].published_as == BASE + " copy 1"
    _end_job()
    _campaign(meta, BASE + " copy 1")["daily_budget"] = "999"
    assert _run(meta, _draft(tmp_path, ads=1))[0].published_as == BASE + " copy 2"
    _end_job()
    meta.fail_next["ads"] = "unsent"
    broken, _ = _run(meta, _draft(tmp_path, ads=1))
    assert broken.published_as == BASE + " copy 3"
    meta.fail_next.clear()
    _end_job()
    # Своя неполная копия, но включённая: не дописываем, и стопа campaign_not_paused нет.
    _campaign(meta, BASE + " copy 3")["status"] = "ACTIVE"
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert out.error == "" and out.published_as == BASE + " copy 4" and out.stop_kind == ""


def test_6_legacy_suffix_is_ignored_and_digits_in_the_name_are_not_parsed(tmp_path):
    meta = FakeMeta()
    _foreign(meta, BASE + "_2")
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert out.published_as == BASE
    meta2 = FakeMeta()
    _foreign(meta2, "X copy 3")
    out2, _ = _run(meta2, _draft_named(tmp_path, "X copy 3"))
    assert out2.published_as == "X copy 3 copy 1"


def test_6b_base_campaign_with_other_ads_gets_the_next_number(tmp_path):
    meta = FakeMeta()
    _finished_copy(meta, tmp_path)
    for o in meta.objects.values():
        if o["_kind"] == "ad":
            o["name"] = "Somebody else ad"
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert out.published_as == BASE + " copy 1"


def test_7_truncated_search_resolved_and_unresolved(tmp_path):
    meta = FakeMeta()
    for i in range(205):
        _foreign(meta, f"{BASE} decoy {i}")
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert out.error == "" and out.published_as == BASE
    ops = [kw["filtering"][0]["operator"] for p, kw in meta.gets if p.endswith("/campaigns")]
    assert "EQUAL" in ops
    _end_job()
    meta2 = FakeMeta()
    for _ in range(205):
        _foreign(meta2, BASE)
    out2, client = _run(meta2, _draft(tmp_path, ads=1))
    assert out2.stop_kind == "name_unsure" and meta2.posts == [] and client.writes == 0


def test_7b_ad_set_name_too_long_stops_before_any_write(tmp_path):
    meta = FakeMeta()
    d = _draft(tmp_path, ads=1)
    d.ad_sets[0].name = "S" * 401
    out, client = _run(meta, d)
    assert out.stop_kind == "name_too_long" and meta.posts == [] and client.writes == 0
    assert "ad set" in out.error


def test_8_readback_passes_with_counter_names(tmp_path):
    meta = FakeMeta()
    _finished_copy(meta, tmp_path, ads=2)
    out, _ = _run(meta, _draft(tmp_path, ads=2))
    assert out.readback["ok"] is True and out.readback["checked"] > 10


# ───────────────────────── возобновление без квитанции ─────────────────────────

def test_14_receipt_missing_and_top_row_partial_resumes(tmp_path):
    meta = FakeMeta()
    meta.fail_next["ads"] = "unsent"
    broken, _ = _run(meta, _draft(tmp_path, ads=1))
    assert broken.stop_kind == "unknown"
    meta.fail_next.clear()
    _end_job()                                       # квитанция потеряна
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert out.error == "" and out.published_as == BASE and out.reused["campaign"]
    assert _names(meta) == [BASE]


def test_family_parsing_is_exact():
    rows = [{"name": "X"}, {"name": "X copy 1"}, {"name": "X copy 12"}, {"name": "X Copy 3"},
            {"name": "X copy 01"}, {"name": "X copy 0"}, {"name": "X_2"}, {"name": "Y copy 4"},
            {"name": "X copy 2 copy 1"}, {"name": " X copy 5 "}]
    assert sorted(cr.family_rows("X", rows)) == [0, 1, 5, 12]


def test_copy_plan_keeps_the_video_tag_and_numbers_all_ad_sets(tmp_path):
    plan = cr.plan_from_draft(_draft(tmp_path, ads=1))
    copy = cr.copy_plan(plan, 3)
    assert copy.name == BASE + " copy 3" and copy.tag == cr.campaign_tag(plan.act, copy.name)
    assert copy.video_tag == plan.tag and copy.label != plan.label
    assert [s.name for s in copy.ad_sets] == [BASE + " copy 3"]
    assert [a.name for a in copy.ad_sets[0].ads] == [a.name for a in plan.ad_sets[0].ads]
    assert cr.copy_plan(plan, 0) is plan and plan.video_tag == plan.tag
