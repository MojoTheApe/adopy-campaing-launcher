"""ACL-1761: найденные с прошлого запуска кампания и группа сверяются с планом ДО любой записи под ними.

Живой случай 10.10: старая группа с Advantage+ площадками, план — ручной список;
объявления писались в неё, а сверка ругалась только после. Здесь Meta — `FakeMeta`.
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_api import creator as cr  # noqa: E402
from tests.test_acl1761_api_create import FakeMeta, _draft, _publisher, _run  # noqa: E402
from tests.test_acl1761_plan_check import _PLAN_DATA, _run_plan_node  # noqa: E402


def _first_run(tmp_path, ads=1):
    meta = FakeMeta()
    out, _ = _run(meta, _draft(tmp_path, ads=ads))
    assert out.error == "", out.error
    return meta


def _make_advantage_plus(meta):
    adset = next(o for o in meta.objects.values() if o["_kind"] == "adset")
    for key in ("publisher_platforms", "facebook_positions", "instagram_positions", "device_platforms"):
        adset["targeting"].pop(key, None)


def _write_count(meta):
    return len(meta.posts)


def test_reused_adset_with_advantage_plus_goes_to_a_new_name(tmp_path):
    meta = _first_run(tmp_path, ads=1)
    _make_advantage_plus(meta)
    before = _write_count(meta)
    # В плане теперь два объявления: второе было бы создано в старой группе.
    out, client = _run(meta, _draft(tmp_path, ads=2))
    # ACL-1902: расхождение у своей старой группы - не блокировка, а новое имя; под старую ничего не пишем.
    assert out.error == "" and out.published_as == "MX Leads 1_2"
    assert any("ad set 'MX Leads 1': placements (Advantage+ vs manual list)" in w for w in out.warnings)
    old = next(o for o in meta.objects.values() if o["_kind"] == "campaign" and o["name"] == "MX Leads 1")
    assert len([o for o in meta.objects.values() if o["_kind"] == "ad"
                and meta.objects[o["adset_id"]]["campaign_id"] == old["id"]]) == 1
    assert _write_count(meta) > before


def test_reused_campaign_with_other_budget_goes_to_a_new_name(tmp_path):
    meta = _first_run(tmp_path, ads=1)
    camp = next(o for o in meta.objects.values() if o["_kind"] == "campaign")
    camp["daily_budget"] = "999"
    before = _write_count(meta)
    out, _ = _run(meta, _draft(tmp_path, ads=2))
    assert out.error == "" and out.published_as == "MX Leads 1_2"
    assert any("daily budget" in w for w in out.warnings)
    assert camp["daily_budget"] == "999" and _write_count(meta) > before


def test_difference_in_a_later_adset_goes_to_a_new_name(tmp_path):
    meta = FakeMeta()
    draft = _draft(tmp_path, ads=1)
    second = copy.deepcopy(draft.ad_sets[0])
    second.name = "MX Leads 1b"
    draft.ad_sets.append(second)
    out, _ = _run(meta, draft)
    assert out.error == "", out.error
    bad = next(o for o in meta.objects.values() if o["_kind"] == "adset" and o["name"] == "MX Leads 1b")
    bad["targeting"]["age_max"] = 65
    before = _write_count(meta)
    draft2 = _draft(tmp_path, ads=2)
    second2 = copy.deepcopy(draft2.ad_sets[0])
    second2.name = "MX Leads 1b"
    draft2.ad_sets.append(second2)
    out2, _ = _run(meta, draft2)
    assert out2.error == "" and out2.published_as == "MX Leads 1_2"
    assert any("'MX Leads 1b'" in w for w in out2.warnings)
    assert bad["targeting"]["age_max"] == 65 and _write_count(meta) > before


def test_reused_adset_that_matches_gets_its_new_ads(tmp_path):
    meta = _first_run(tmp_path, ads=1)
    n_ads = len([o for o in meta.objects.values() if o["_kind"] == "ad"])
    out, _ = _run(meta, _draft(tmp_path, ads=2))
    assert out.error == "" and out.readback["ok"] is True
    assert len([o for o in meta.objects.values() if o["_kind"] == "ad"]) == n_ads + 1
    assert any("match the builder" in s for s in out.steps)


def test_pre_check_reads_count_toward_the_call_budget(tmp_path):
    meta = _first_run(tmp_path, ads=1)
    n = len(meta.gets)
    out, client = _run(meta, _draft(tmp_path, ads=1))
    assert out.error == "" and len(meta.gets) > n
    assert any("targeting" in (kw.get("fields") or "") for p, kw in meta.gets[n:])


def test_check_plan_reports_the_differences_and_writes_nothing(tmp_path):
    meta = _first_run(tmp_path, ads=1)
    _make_advantage_plus(meta)
    n = _write_count(meta)
    pub = _publisher(meta, dry_run=True)
    res = pub.publish_draft(_draft(tmp_path, ads=1))
    assert res["success"] is True and _write_count(meta) == n and pub._client.writes == 0
    summary = res["plan"]["summary"]
    # ACL-1902: расхождения теперь в предупреждении о новом имени, не в блоке «уже существует».
    assert summary["existing"] is False and summary["existing_differences"] == []
    assert summary["campaign"] == "MX Leads 1_2" and summary["requested_name"] == "MX Leads 1"
    assert any("Will be published as 'MX Leads 1_2'" in w and "placements (Advantage+ vs manual list)" in w
               for w in res["warnings"])


def test_check_plan_without_differences_has_an_empty_list(tmp_path):
    meta = _first_run(tmp_path, ads=1)
    res = _publisher(meta, dry_run=True).publish_draft(_draft(tmp_path, ads=1))
    assert res["plan"]["summary"]["existing"] is True
    assert res["plan"]["summary"]["existing_differences"] == []
    fresh = _publisher(FakeMeta(), dry_run=True).publish_draft(_draft(tmp_path, ads=1))
    assert fresh["plan"]["summary"]["existing_differences"] == []


def test_plan_panel_shows_the_published_as_name():
    data = copy.deepcopy(_PLAN_DATA)
    data["results"][0]["published_as"] = "MX <1>_2"
    html = _run_plan_node(data)["html"]
    assert "published as" in html and "MX &lt;1&gt;_2" in html
    assert "published as" not in _run_plan_node(_PLAN_DATA)["html"]


def test_describe_diffs_names_manual_changes_too():
    checks = [{"field": "publisher_platforms", "expected": ["facebook"], "actual": ["facebook", "instagram"],
               "ok": False},
              {"field": "age_max", "expected": 55, "actual": 65, "ok": False},
              {"field": "name", "expected": "a", "actual": "b", "ok": False}]
    got = cr._describe_diffs(checks)
    assert got[0].startswith("placements (publisher platforms: builder facebook")
    assert got[1] == "maximum age (builder: 55, Meta: 65)" and len(got) == 2


def test_active_reused_adset_or_campaign_goes_to_a_new_name(tmp_path):
    # Статус ACTIVE у найденного объекта — расхождение с билдером: стоп до записей.
    for kind in ("adset", "campaign"):
        meta = _first_run(tmp_path, ads=1)
        obj = next(o for o in meta.objects.values() if o["_kind"] == kind)
        obj["status"] = "ACTIVE"
        before = _write_count(meta)
        out, client = _run(meta, _draft(tmp_path, ads=2))
        assert out.error == "" and out.published_as == "MX Leads 1_2", kind
        assert any("status (builder: PAUSED, Meta: ACTIVE)" in w for w in out.warnings), kind
        assert obj["status"] == "ACTIVE" and _write_count(meta) > before, kind
