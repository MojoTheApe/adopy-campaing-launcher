"""ACL-2650: отклонённые объявления на строках кампаний, ad set и аккаунта — только справка.

Синтетические деревья, Graph — заглушка. Проверяется: бейдж и причина, «ads not read»,
нейтральность (статус аккаунта не меняется), ни одного лишнего вызова, разбор мусорных входов.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.analysis_agent import campaign_inventory as ci  # noqa: E402
from tests.test_acl1798_campaign_inventory import (  # noqa: E402
    ACT, D, FakeClient, ad, adset, camp, snapshot,
)

TEMPLATE = ROOT / "src" / "web" / "templates" / "_campaign_table.html"


@pytest.fixture(autouse=True)
def config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ACL_CONFIG_DIR", str(tmp_path))


def _snap(ads, steps_ads="ok"):
    snap = snapshot()
    snap.ads, _ = ci.parse_inventory(ads, "ad")
    snap.steps["ads"] = steps_ads
    return snap


def _rows(snap):
    by = {r["id"]: r for r in ci.campaign_rows(snap)}
    kids = {r["id"]: r for r in ci.child_rows(snap, "adset", "1")["rows"]}
    return by, kids


def _issue(summary="Ad rejected", message=""):
    return [{"error_code": 1487, "error_summary": summary, "error_message": message, "level": "AD"}]


def test_one_disapproved_ad_rolls_up_to_campaign_and_adset():
    bad = ad(111, "Ad 1", 11, 1, eff="DISAPPROVED")
    bad["issues_info"] = _issue("Ad does not meet policy")
    snap = _snap([bad, ad(112, "Ad 2", 11, 1), ad(211, "Ad 3", 21, 2)])
    by, kids = _rows(snap)
    rej = by["1"]["rejected"]
    assert rej["state"] == "ok" and rej["count"] == 1
    assert rej["ads"] == [{"name": "Ad 1", "reason": "Ad does not meet policy"}]
    assert kids["11"]["rejected"]["count"] == 1
    assert kids["12"]["rejected"]["count"] == 0
    assert by["2"]["rejected"]["count"] == 0


def test_with_issues_counts_and_reason_comes_from_message_when_no_summary():
    bad = ad(111, "Ad 1", 11, 1, eff="WITH_ISSUES")
    bad["issues_info"] = _issue("", "Image text is too long")
    by, _ = _rows(_snap([bad]))
    assert by["1"]["rejected"]["ads"][0]["reason"] == "Image text is too long"


def test_ad_level_not_read_says_so_not_zero():
    snap = _snap([], steps_ads="skipped")
    by, kids = _rows(snap)
    assert by["1"]["rejected"] == {"state": "not_read", "count": 0, "ads": []}
    assert kids["11"]["rejected"]["state"] == "not_read"
    assert ci.summary(snap)["rejected"] == {"state": "not_read", "count": 0, "ads": []}


def test_partial_read_marks_state_partial():
    bad = ad(111, "Ad 1", 11, 1, eff="DISAPPROVED")
    by, _ = _rows(_snap([bad], steps_ads="truncated"))
    assert by["1"]["rejected"]["state"] == "partial" and by["1"]["rejected"]["count"] == 1


def test_ad_rows_carry_no_rollup():
    snap = _snap([ad(111, "Ad 1", 11, 1, eff="DISAPPROVED")])
    rows = ci.child_rows(snap, "ad", "11")["rows"]
    assert rows and rows[0]["rejected"] is None


def test_account_summary_counts_all_rejected_ads_including_orphans_and_is_neutral():
    ads = [ad(111, "Ad 1", 11, 1, eff="DISAPPROVED"), ad(211, "Ad 3", 21, 2, eff="WITH_ISSUES"),
           ad(999, "Orphan", 777, 888, eff="DISAPPROVED"), ad(112, "Ok", 11, 1)]
    clean = _snap([ad(112, "Ok", 11, 1)])
    snap = _snap(ads)
    out, base = ci.summary(snap), ci.summary(clean)
    assert out["rejected"]["count"] == 3 and out["rejected"]["state"] == "ok"
    # Всё, что красит аккаунт, от отказов не зависит: состояние чтения, ошибка, шаги.
    for key in ("state", "error", "steps", "problems", "currency"):
        assert out[key] == base[key]


def test_account_state_module_ignores_rejections():
    src = (ROOT / "src" / "web" / "account_state.py").read_text(encoding="utf-8").lower()
    assert "rejected" not in src


def test_oddities_do_not_raise():
    odd = [
        ad(1, "", 11, 1, eff="DISAPPROVED"),                                   # без имени
        {**ad(2, "A2", 11, 1, eff="DISAPPROVED"), "issues_info": None},        # нет списка
        {**ad(3, "A3", 11, 1, eff="DISAPPROVED"), "issues_info": []},          # пустой
        {**ad(4, "A4", 11, 1, eff="DISAPPROVED"), "issues_info": "oops"},      # не список
        {**ad(5, "A5", 11, 1, eff="DISAPPROVED"), "issues_info": ["x", 7, {}]},  # мусор внутри
        ad(6, "A6", 11, 1, eff="SOMETHING_NEW"),                               # неизвестный статус
        ad(7, "A7", 11, 1, eff=""),                                            # без статуса
        {**ad(8, "A8", 11, 1, eff="disapproved")},                             # регистр
    ]
    by, kids = _rows(_snap(odd))
    rej = by["1"]["rejected"]
    assert rej["count"] == 6 and rej["state"] == "ok"
    assert rej["ads"][0]["name"] == "(no name)"
    assert all(a["reason"] == "" for a in rej["ads"])
    assert kids["11"]["rejected"]["count"] == 6


def test_no_ads_and_empty_account():
    by, kids = _rows(_snap([]))
    assert by["1"]["rejected"] == {"state": "ok", "count": 0, "ads": []}
    empty = ci.Snapshot(account="Acc")
    assert ci.summary(empty)["rejected"]["state"] == "not_read"
    assert ci.campaign_rows(empty) == []


def test_tooltip_list_is_capped_but_count_is_total():
    ads = [ad(100 + i, f"Ad {i}", 11, 1, eff="DISAPPROVED") for i in range(9)]
    by, _ = _rows(_snap(ads))
    assert by["1"]["rejected"]["count"] == 9
    assert len(by["1"]["rejected"]["ads"]) == ci.REJECTED_TIP_ADS


def test_old_snapshot_without_issues_key_still_works():
    snap = _snap([ad(111, "Ad 1", 11, 1, eff="DISAPPROVED")])
    for a in snap.ads:
        a.pop("issues", None)
    by, _ = _rows(snap)
    assert by["1"]["rejected"]["ads"][0]["reason"] == ""


def test_no_extra_graph_calls():
    """Те же запросы, что и до задачи: счёт и состав вызовов не зависят от отказов."""
    def run(ads):
        table = {"campaigns": [camp(1, "C")], "adsets": [adset(11, "S", 1)], "ads": ads,
                 "insights_campaign": [], "insights_adset": [], "insights_ad": []}
        client = FakeClient(table)
        snap = ci.Snapshot(account="Acc", date_from=D, date_to=D)
        ci.read_into(snap, client, ACT)
        return client, snap

    plain, _ = run([ad(111, "Ad", 11, 1)])
    bad_ad = ad(111, "Ad", 11, 1, eff="DISAPPROVED")
    bad_ad["issues_info"] = _issue()
    bad, snap = run([bad_ad])
    assert bad.calls == plain.calls
    assert [(m, p) for m, p, _ in bad.log] == [(m, p) for m, p, _ in plain.log]
    ci.campaign_rows(snap), ci.child_rows(snap, "adset", "11"), ci.summary(snap)
    assert bad.calls == plain.calls
    assert ci.summary(snap)["rejected"]["count"] == 1


def test_template_wires_neutral_chip_not_ads_state_and_sort():
    html = TEMPLATE.read_text(encoding="utf-8")
    assert "function rejectedChip" in html and "ads not read" in html and " rejected</span>" in html
    chip = html[html.index("function rejectedChip"):html.index("function rejectedFirst")]
    assert 'class="hc-chip note"' in chip and "alert" not in chip.replace("rejected", "")
    assert 'data-sort="rejected"' in html and "S.rejFirst" in html
    assert "sum.rejected" in html
