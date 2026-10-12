"""ACL-1815: доказательства для правил — вердикты идентичности, окна зрелости, свежесть.

Ничего из Meta и Multilogin: снимок собирается из `campaign_inventory.parse_inventory`,
книга статистики — маленький DataFrame. Проверяется то, ради чего задача:
каждый случай идентичности даёт свой вердикт, «нет строк» никогда не даёт FTD=0,
окна зрелости асимметричны, устаревшие входы блокируют Automatic, а таблица Home
после выноса джойна не изменилась.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.analysis_agent import campaign_inventory as ci  # noqa: E402
from src.analysis_agent.rules import evidence as ev  # noqa: E402

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
TODAY = date(2026, 10, 12)
BUILT = NOW - timedelta(hours=2)


def _obj(i, name, **kw):
    return {"id": str(i), "name": name, "status": "ACTIVE", "effective_status": "ACTIVE", **kw}


def make_snapshot(campaigns, **over) -> ci.Snapshot:
    """`campaigns`: [(id, имя, [(id ad set, имя), …]), …] → снимок с чистыми шагами."""
    snap = ci.Snapshot(account="Acc", state="ok", currency="USD", date_from="2026-10-01", date_to="2026-10-11",
                       finished_at="2026-10-12 11:00:00", finished_at_utc=(NOW - timedelta(hours=1)).isoformat())
    snap.campaigns, _ = ci.parse_inventory([_obj(c[0], c[1]) for c in campaigns], "campaign")
    snap.adsets, _ = ci.parse_inventory(
        [_obj(a[0], a[1], campaign_id=str(c[0])) for c in campaigns for a in c[2]], "adset")
    snap.steps = {s: "ok" for s in ci.STEPS}
    for key, value in over.items():
        setattr(snap, key, value)
    return snap


def m(spend=100.0, leads=10, deposits=2):
    return {"spend": spend, "leads": leads, "deposits": deposits}


# ───────────────────────── вердикты идентичности ─────────────────────────

CAMPAIGNS = [
    (1, "Camp1", [(11, "Set Exact")]),
    (2, "Camp2", [(21, "Dup"), (22, "Dup")]),
    (3, "Camp3", [(31, "Orig"), (32, "Orig - Copy")]),
    (4, "Camp4", [(41, "Multi")]),
    (5, "Camp5", [(51, "Ren")]),
    (6, "Camp6", [(61, "Ghost")]),
    (7, "Camp7", [(71, "Child1"), (72, "Child2")]),
    (8, "Camp8", [(81, "P1"), (82, "P2")]),
]
BOOK = {"campaigns": {n: m() for n in ("Set Exact", "Dup", "Orig", "Orig - Copy", "Multi", "Ren",
                                       "Child1", "Child2", "P1")}, "ads": {}}


def verdicts(**kw):
    snap = make_snapshot(CAMPAIGNS)
    joined, _ = ev.exact_name_join(snap, BOOK)
    return ev.identity_verdicts(snap, joined, multi_country={"Multi"}, current_names={"adset:51": "Ren Old"}, **kw)


def test_every_identity_case_has_its_own_verdict():
    got = verdicts()
    assert got["adset:11"]["verdict"] == ev.EXACT
    assert got["adset:21"]["verdict"] == got["adset:22"]["verdict"] == ev.DUPLICATE_NAME
    assert got["adset:31"]["verdict"] == got["adset:32"]["verdict"] == ev.COPY_SUFFIX
    assert got["adset:41"]["verdict"] == ev.MULTI_COUNTRY
    assert got["adset:51"]["verdict"] == ev.RENAMED
    assert got["adset:61"]["verdict"] == ev.NOT_IN_WORKBOOK
    assert got["campaign:7"]["verdict"] == ev.EXACT_BY_CHILDREN
    assert got["campaign:8"]["verdict"] == ev.PARTIAL
    heur = verdicts(alias_quality="heuristic")
    assert heur["adset:11"]["verdict"] == ev.ALIAS_HEURISTIC
    seen = {v["verdict"] for v in got.values()} | {heur["adset:11"]["verdict"]}
    assert seen == set(ev.VERDICTS)


def test_only_exact_kinds_allow_automatic():
    got = verdicts()
    allowed = {v["verdict"] for v in got.values() if v["automatic_ok"]}
    assert allowed == {ev.EXACT, ev.EXACT_BY_CHILDREN}
    assert got["adset:31"]["blocker"] == ev.AMBIGUOUS_MAPPING  # copy_suffix: только Observe/Suggest
    assert not verdicts(alias_quality="heuristic")["adset:11"]["automatic_ok"]
    assert ev.AUTOMATIC_VERDICTS == {ev.EXACT, ev.EXACT_BY_CHILDREN}


def test_blockers_tell_missing_from_ambiguous():
    got = verdicts()
    assert got["adset:61"]["blocker"] == ev.MISSING_MAPPING
    for key in ("adset:21", "adset:41", "adset:51", "campaign:8"):
        assert got[key]["blocker"] == ev.AMBIGUOUS_MAPPING


def test_workbook_unavailable_is_not_in_workbook_everywhere():
    snap = make_snapshot(CAMPAIGNS)
    got = ev.identity_verdicts(snap, {}, bi_available=False)
    assert {v["verdict"] for v in got.values()} == {ev.NOT_IN_WORKBOOK}


def test_campaign_with_truncated_ad_sets_is_never_full_coverage():
    snap = make_snapshot(CAMPAIGNS)
    snap.steps["adsets"] = "truncated"
    joined, _ = ev.exact_name_join(snap, BOOK)
    got = ev.identity_verdicts(snap, joined)
    assert got["campaign:7"]["verdict"] == ev.PARTIAL
    assert not got["campaign:7"]["automatic_ok"]


def test_ad_inherits_a_weak_parent_verdict():
    snap = make_snapshot([(1, "C", [(11, "S")])])
    snap.ads, _ = ci.parse_inventory([_obj(111, "T", campaign_id="1", adset_id="11")], "ad")
    book = {"campaigns": {"S": m()}, "ads": {"S": {"T": m()}}}
    joined, _ = ev.exact_name_join(snap, book)
    assert ev.identity_verdicts(snap, joined)["ad:111"]["verdict"] == ev.EXACT
    assert ev.identity_verdicts(snap, joined, alias_quality="heuristic")["ad:111"]["verdict"] == ev.ALIAS_HEURISTIC
    assert ev.identity_verdicts(snap, joined, multi_country={"S"})["ad:111"]["verdict"] == ev.MULTI_COUNTRY


def test_coverage_counts_each_group():
    got = verdicts()
    cov = ev.coverage(got, evidence_only=["Zzz"])
    assert cov["assigned"] == len(got)
    assert cov["exact"] == sum(v["automatic_ok"] for v in got.values())
    assert cov["missing"] == 4 and cov["partial"] == 1  # Ghost, P2 и кампании без единого совпавшего ad set
    assert cov["ambiguous"] == 9  # дубли, multi_country, renamed, копии и их кампании
    assert cov["evidence_only"] == ["Zzz"]


# ───────────────────────── Home не изменился ─────────────────────────

def test_home_join_is_the_shared_join():
    snap = make_snapshot(CAMPAIGNS)
    assert ci.join_bi(snap, BOOK) == ev.exact_name_join(snap, BOOK)
    joined, summ = ci.join_bi(snap, BOOK)
    assert joined["adset:11"]["match"] == "name" and joined["campaign:7"]["match"] == "ad_sets"
    assert joined["campaign:8"] == {"match": "partial", "coverage": "1/2"}
    assert ci.join_bi(snap, None) == ({}, {"matched": 0, "evidence_only": [], "available": False})
    assert "adset:61" not in joined and "adset:21" not in joined


# ───────────────────────── кадры и зрелость ─────────────────────────

ALIAS = {"BR": "Acc"}


def book_df(rows):
    return pd.DataFrame(rows, columns=["Date", "Country Final", "Account Alias", "Adset Name", "Ad Title",
                                       "# Leads", "# FTDs", "$ Spend"])


def row(day, adset, spend, ftds, leads=5, country="BR", alias="Acc"):
    return (day, country, alias, adset, "Ad", leads, ftds, spend)


WINDOW_SNAP = [(1, "Camp A", [(11, "Set A")]), (2, "Camp B", [(21, "Set B")]), (3, "Camp C", [(31, "Set C")])]
WINDOW_DF = book_df([
    row("2026-10-05", "Set A", 100.0, 1), row("2026-10-10", "Set A", 40.0, 2),   # поздние FTD
    row("2026-10-05", "Set B", 100.0, 1), row("2026-10-10", "Set B", 40.0, 0),   # хвост без FTD
    row("2026-10-11", "Other", 5.0, 0),
])


def rsnap(**over):
    """Снимок, который можно сверить: USD, период = зрелое окно, spend Meta = spend книги (200)."""
    snap = make_snapshot(WINDOW_SNAP, date_from="2026-10-01", date_to="2026-10-09")
    snap.insights = {"campaign": {"1": {"spend": 100.0}, "2": {"spend": 100.0}}}
    for key, value in over.items():
        setattr(snap, key, value)
    return snap


def evidence(strategy, snap=None, df=None, **kw):
    kw.setdefault("now", NOW)
    kw.setdefault("today", TODAY)
    kw.setdefault("bi_built_at", BUILT)
    kw.setdefault("account_label", "Acc")
    return ev.build_evidence(snap or rsnap(), WINDOW_DF if df is None else df, ALIAS,
                             strategy=strategy, start="2026-10-01", **kw)


def test_maturity_windows_are_asymmetric_for_stop_loss():
    w = ev.maturity_windows(ev.STOP_LOSS, "2026-10-01", "2026-10-11", 2)
    assert w.spend == (date(2026, 10, 1), date(2026, 10, 9))
    assert w.ftd == (date(2026, 10, 1), date(2026, 10, 11))
    assert w.tail is None


def test_maturity_windows_for_scale_have_a_tail():
    w = ev.maturity_windows(ev.SCALE, "2026-10-01", "2026-10-11", 2)
    assert w.spend == w.ftd == (date(2026, 10, 1), date(2026, 10, 9))
    assert w.tail == (date(2026, 10, 10), date(2026, 10, 11))
    assert ev.maturity_windows(ev.SCALE, "2026-10-10", "2026-10-11", 2).spend is None  # зрелых дней ещё нет


def test_region_config_defaults_and_override():
    assert ev.MATURITY_DAYS_BY_REGION == {"LATAM": 2, "GCC": 2}
    assert ev.maturity_days("latam") == 2 and ev.maturity_days("GCC") == 2
    assert ev.maturity_days("EU") == ev.DEFAULT_MATURITY_DAYS
    assert ev.maturity_days("LATAM", override=4) == 4
    assert ev.maturity_days(override=99) == ev.MAX_MATURITY_DAYS


def test_stop_loss_spend_excludes_recent_days_but_ftds_run_to_today():
    out = evidence(ev.STOP_LOSS, region="LATAM")
    a = out["numbers"]["adset:11"]
    assert a["spend"] == 100.0          # 40 за 10.10 в зрелое окно не входит
    assert a["ftds"] == 3               # поздние FTD 10.10 засчитаны
    assert a["cpa"] == pytest.approx(100.0 / 3)
    assert out["windows"]["spend"] == ["2026-10-01", "2026-10-09"]
    assert out["windows"]["ftd"] == ["2026-10-01", "2026-10-11"]


def test_scale_tail_must_hold():
    out = evidence(ev.SCALE, region="GCC", target_cpa=50.0)
    good, bad = out["numbers"]["adset:11"], out["numbers"]["adset:21"]
    assert good["spend"] == 100.0 and good["ftds"] == 1
    assert good["tail"]["state"] == "tail_ok" and good["tail"]["cpa"] == pytest.approx(20.0)
    assert bad["tail"]["state"] == "tail_pending" and bad["reason"] == ev.PENDING_OUTCOMES
    assert out["automatic"]["adset:11"]["allowed"] is True
    assert out["automatic"]["adset:21"]["allowed"] is False
    assert ev.PENDING_OUTCOMES in out["automatic"]["adset:21"]["reasons"]
    no_target = evidence(ev.SCALE, target_cpa=None)["numbers"]["adset:11"]
    assert no_target["tail"]["state"] == "tail_pending"


def test_scale_with_no_mature_days_has_no_mature_spend():
    df = book_df([row("2026-10-10", "Set A", 40.0, 2)])
    out = ev.build_evidence(make_snapshot(WINDOW_SNAP), df, ALIAS, strategy=ev.SCALE, start="2026-10-10",
                            account_label="Acc", now=NOW, today=TODAY, bi_built_at=BUILT, target_cpa=50.0)
    assert out["numbers"]["adset:11"]["state"] == "no_mature_spend"
    assert out["automatic"]["adset:11"]["allowed"] is False


# ───────────────────────── «нет строк» ≠ ноль ─────────────────────────

def test_object_not_in_workbook_never_gets_zero_ftd():
    out = evidence(ev.STOP_LOSS)
    ghost = out["numbers"]["adset:31"]
    assert out["identity"]["adset:31"]["verdict"] == ev.NOT_IN_WORKBOOK
    assert ghost["state"] == "no_data"
    assert ghost["ftds"] is None and ghost["spend"] is None and ghost["cpa"] is None
    assert ghost["zero_ftd"] is False
    assert out["automatic"]["adset:31"]["allowed"] is False
    assert ev.MISSING_MAPPING in out["automatic"]["adset:31"]["reasons"]


def test_workbook_without_any_rows_gives_no_numbers_at_all():
    empty = book_df([row("2026-10-11", "Other", 5.0, 0, country="MX", alias="Zzz")])
    out = evidence(ev.STOP_LOSS, df=empty)
    for entry in out["numbers"].values():
        assert entry["ftds"] is None and entry["zero_ftd"] is False
    assert {v["verdict"] for v in out["identity"].values()} == {ev.NOT_IN_WORKBOOK}


def test_real_zero_ftd_is_still_a_zero_when_rows_exist():
    df = book_df([row("2026-10-05", "Set A", 80.0, 0), row("2026-10-11", "Other", 1.0, 0)])
    out = evidence(ev.STOP_LOSS, df=df)
    a = out["numbers"]["adset:11"]
    assert a["state"] == "zero_ftd" and a["ftds"] == 0 and a["zero_ftd"] is True


# ───────────────────────── свежесть и сверка ─────────────────────────

def test_fresh_inputs_allow_automatic_for_an_exact_object():
    out = evidence(ev.STOP_LOSS)
    assert out["global_blockers"] == []
    assert out["identity"]["adset:11"]["verdict"] == ev.EXACT
    assert out["automatic"]["adset:11"] == {"allowed": True, "reasons": []}
    assert out["coverage"]["missing"] == 2  # Set C и его кампания


def test_stale_bi_blocks_automatic():
    old_through = evidence(ev.STOP_LOSS, today=date(2026, 10, 14))      # книга кончилась позавчера
    assert ev.STALE_BI in old_through["global_blockers"]
    assert not old_through["automatic"]["adset:11"]["allowed"]
    old_build = evidence(ev.STOP_LOSS, bi_built_at=NOW - timedelta(hours=31))
    assert old_build["freshness"]["bi"]["blocker"] == ev.STALE_BI
    assert not old_build["automatic"]["adset:11"]["allowed"]
    unknown = evidence(ev.STOP_LOSS, bi_built_at=None)
    assert ev.STALE_BI in unknown["global_blockers"]


def test_stale_snapshot_blocks_automatic(monkeypatch):
    old = rsnap(finished_at_utc=(NOW - timedelta(hours=25)).isoformat())
    out = evidence(ev.STOP_LOSS, snap=old)
    assert ev.STALE_SNAPSHOT in out["global_blockers"]
    assert not out["automatic"]["adset:11"]["allowed"]
    monkeypatch.setenv("ACL_RULES_MAX_SNAPSHOT_AGE_H", "72")
    assert evidence(ev.STOP_LOSS, snap=old)["automatic"]["adset:11"]["allowed"]
    monkeypatch.setenv("ACL_RULES_MAX_SNAPSHOT_AGE_H", "500")
    assert ev.max_snapshot_age_h() == 72.0
    monkeypatch.setenv("ACL_RULES_MAX_SNAPSHOT_AGE_H", "0")
    assert ev.max_snapshot_age_h() == 1.0


def test_snapshot_with_unread_steps_or_old_format_time():
    cut = make_snapshot(WINDOW_SNAP)
    cut.steps["adsets"] = "truncated"
    assert ev.snapshot_freshness(cut, now=NOW)["blocker"] == ev.STALE_SNAPSHOT
    failed = make_snapshot(WINDOW_SNAP, state="failed")
    assert not ev.snapshot_freshness(failed, now=NOW)["fresh"]
    assert not ev.snapshot_freshness(None, now=NOW)["fresh"]
    # снимок из старого файла: ISO нет, только локальное время без зоны
    legacy = make_snapshot(WINDOW_SNAP, finished_at_utc="")
    legacy.finished_at = (datetime.now() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    assert ev.snapshot_freshness(legacy)["fresh"]
    legacy.finished_at = "garbage"
    assert not ev.snapshot_freshness(legacy)["fresh"]


def test_ads_need_the_ads_step():
    snap = make_snapshot(WINDOW_SNAP)
    assert ev.snapshot_freshness(snap, level="ad", now=NOW)["fresh"]
    snap.steps["ads"] = "failed"
    assert not ev.snapshot_freshness(snap, level="ad", now=NOW)["fresh"]
    assert ev.snapshot_freshness(snap, level="adset", now=NOW)["fresh"]


def test_reconciliation_blocks_on_a_big_gap_only_for_usd():
    snap = make_snapshot(WINDOW_SNAP, date_from="2026-10-01", date_to="2026-10-09")
    snap.insights = {"campaign": {"1": {"spend": 100.0}, "2": {"spend": 100.0}}}
    window = (date(2026, 10, 1), date(2026, 10, 9))
    assert ev.reconcile_spend(snap, window, 190.0)["status"] == "ok"
    bad = ev.reconcile_spend(snap, window, 100.0)
    assert bad["status"] == "mismatch" and bad["blocker"] == ev.DATA_MISMATCH
    snap.currency = "BRL"
    skipped = ev.reconcile_spend(snap, window, 100.0)
    assert skipped["status"] == "not_reconciled" and skipped["blocker"] == "Not reconciled: currency BRL"
    snap.currency = "USD"
    assert ev.reconcile_spend(snap, (date(2026, 10, 1), date(2026, 10, 8)), 100.0)["blocker"]         == "Not reconciled: snapshot period differs"
    assert ev.reconcile_spend(snap, window, None)["blocker"] == "Not reconciled: spend missing"
    snap.steps["insights_campaign"] = "truncated"
    assert ev.reconcile_spend(snap, window, 100.0)["blocker"] == "Not reconciled: spend missing"


def test_mismatch_blocks_automatic_end_to_end():
    snap = make_snapshot(WINDOW_SNAP, date_from="2026-10-01", date_to="2026-10-09")
    snap.insights = {"campaign": {"1": {"spend": 1000.0}}}
    out = evidence(ev.STOP_LOSS, snap=snap)
    assert ev.DATA_MISMATCH in out["global_blockers"]
    assert not out["automatic"]["adset:11"]["allowed"]


@pytest.mark.parametrize("over,blocker", [
    ({"currency": "EUR"}, "Not reconciled: currency EUR"),
    ({"date_to": "2026-10-11"}, "Not reconciled: snapshot period differs"),
    ({"insights": {}}, "Not reconciled: spend missing"),
])
def test_unreconciled_blocks_automatic_but_not_observe(over, blocker):
    out = evidence(ev.STOP_LOSS, snap=rsnap(**over))
    assert blocker in out["global_blockers"]
    assert out["automatic"]["adset:11"] == {"allowed": False, "reasons": [blocker]}
    assert out["identity"]["adset:11"]["verdict"] == ev.EXACT          # оценка идёт
    assert out["numbers"]["adset:11"]["state"] == "ok"                 # числа есть


# ───────────────────────── копия тянет за собой кампанию и объявления

def test_copy_suffix_ad_set_taints_campaign_and_ads():
    snap = make_snapshot([(3, "Camp3", [(31, "Orig"), (32, "Orig - Copy")])])
    snap.ads, _ = ci.parse_inventory([_obj(311, "T", campaign_id="3", adset_id="31")], "ad")
    book = {"campaigns": {"Orig": m(), "Orig - Copy": m()}, "ads": {"Orig": {"T": m()}}}
    joined, _ = ev.exact_name_join(snap, book)
    got = ev.identity_verdicts(snap, joined)
    assert got["adset:31"]["verdict"] == ev.COPY_SUFFIX
    assert got["campaign:3"]["verdict"] == ev.COPY_SUFFIX            # не exact_by_children
    assert got["ad:311"]["verdict"] == ev.COPY_SUFFIX                # наследует от ad set
    assert not any(v["automatic_ok"] for v in got.values())
    assert ev.coverage(got)["exact"] == 0 and ev.coverage(got)["ambiguous"] == 4


def test_snapshot_age_env_junk_falls_back_to_default(monkeypatch):
    for junk in ("nan", "inf", "-inf", "abc", ""):
        monkeypatch.setenv("ACL_RULES_MAX_SNAPSHOT_AGE_H", junk)
        assert ev.max_snapshot_age_h() == 24.0


def test_bi_built_at_is_required():
    with pytest.raises(TypeError):
        ev.build_evidence(rsnap(), WINDOW_DF, ALIAS, strategy=ev.STOP_LOSS, start="2026-10-01")


# ───────────────────────── идентичность по книге ─────────────────────────

def test_alias_classification():
    assert ev.classify_alias("Acc", ["Acc", "Other"]) == "exact"
    assert ev.classify_alias("Acc", ["FacebookAcc"]) == "heuristic"
    assert ev.classify_alias("Acc", []) == "none"


def test_heuristic_alias_from_the_workbook_blocks_automatic():
    df = book_df([row("2026-10-05", "Set A", 100.0, 1, alias="FacebookAcc")])
    out = ev.build_evidence(make_snapshot(WINDOW_SNAP), df, {"BR": "FacebookAcc"}, strategy=ev.STOP_LOSS,
                            start="2026-10-01", account_label="Acc", now=NOW, today=TODAY, bi_built_at=BUILT)
    assert out["alias_quality"] == "heuristic"
    assert out["identity"]["adset:11"]["verdict"] == ev.ALIAS_HEURISTIC
    assert out["automatic"]["adset:11"]["allowed"] is False


def test_multi_country_names_come_from_the_workbook():
    df = book_df([row("2026-10-05", "Set A", 100.0, 1), row("2026-10-05", "Set A", 30.0, 1, country="MX")])
    found = ev.find_multi_country(df, {"BR": "Acc", "MX": "Acc"})
    assert found == {"Set A"}
    out = ev.build_evidence(make_snapshot(WINDOW_SNAP), df, {"BR": "Acc"}, strategy=ev.STOP_LOSS,
                            start="2026-10-01", account_label="Acc", now=NOW, today=TODAY, bi_built_at=BUILT,
                            multi_country=found)
    assert out["identity"]["adset:11"]["verdict"] == ev.MULTI_COUNTRY
    assert out["automatic"]["adset:11"]["allowed"] is False


def test_rename_between_snapshot_and_assignment():
    out = evidence(ev.STOP_LOSS, assigned_names={"adset:11": "Set A before"})
    assert out["identity"]["adset:11"]["verdict"] == ev.RENAMED
    assert not out["automatic"]["adset:11"]["allowed"]


# ───────────────────────── поля описи ─────────────────────────

def test_inventory_reads_labels_and_times_in_the_same_calls():
    assert "adlabels{name}" in ci.CAMPAIGN_FIELDS and "adlabels{name}" in ci.ADSET_FIELDS
    for fields in (ci.CAMPAIGN_FIELDS, ci.ADSET_FIELDS, ci.AD_FIELDS):
        assert "updated_time" in fields
    rows = [{"id": "1", "name": "A", "adlabels": [{"name": "acl-batch-1"}], "updated_time": "2026-10-12T10:11:12+0300"},
            {"id": "2", "name": "B", "adlabels": {"data": [{"name": "manual"}]}},
            {"id": "3", "name": "C"}]
    got, _ = ci.parse_inventory(rows, "campaign")
    assert got[0]["acl_created"] is True and got[0]["updated_time"] == "2026-10-12T07:11:12+00:00"
    assert got[1]["acl_created"] is False and "updated_time" not in got[1]
    assert "acl_created" not in got[2]  # метки не читались — не «не ACL»


def test_snapshot_keeps_old_files_readable():
    snap = ci._snapshot_from({"account": "A", "finished_at": "2026-10-11 10:00:00"})  # noqa: SLF001
    assert snap.finished_at_utc == ""
