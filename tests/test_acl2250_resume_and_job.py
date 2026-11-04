"""ACL-2250: возобновление, повтор и конец задания (квитанция имени живёт до конца ЗАДАНИЯ). Fakes only."""
from __future__ import annotations

import copy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from src.fb_api import creator as cr  # noqa: E402
from src.fb_api import name_receipt as nr  # noqa: E402
from src.fb_api.graph import PageUnresponsive  # noqa: E402
from src.web import publish_jobs as pj  # noqa: E402
from tests.test_acl1761_api_create import ACT, FakeMeta, _draft, _publisher, _run  # noqa: E402
from tests.test_acl2250_copy_counter import BASE, _foreign, _names  # noqa: E402


def _fail_nth(meta, edge, nth):
    """Ответ «не создано, ответ потерян» на n-ю запись этого вида (1-based)."""
    real = meta.write
    seen = {"n": 0}

    def write(path, params, *a, **k):
        if path.rsplit("/", 1)[1] == edge:
            seen["n"] += 1
            if seen["n"] == nth:
                meta.fail_next[edge] = "unsent"
        return real(path, params, *a, **k)
    meta.write = write


def _two_sets(tmp_path, ads=1):
    d = _draft(tmp_path, ads=ads)
    second = copy.deepcopy(d.ad_sets[0])
    second.name = "Set B"
    d.ad_sets.append(second)
    return d


@pytest.mark.parametrize("edge,nth", [("adsets", 1), ("adsets", 2), ("ads", 1), ("ads", 2)])
def test_9_and_16_stop_midway_then_rerun_writes_only_what_is_missing(tmp_path, edge, nth):
    meta = FakeMeta()
    _fail_nth(meta, edge, nth)
    broken, _ = _run(meta, _two_sets(tmp_path))
    assert broken.stop_kind == "unknown" and broken.published_as == BASE
    meta.fail_next.clear()
    out, _ = _run(meta, _two_sets(tmp_path))
    assert out.error == "" and out.published_as == BASE and out.reused["campaign"]
    assert _names(meta) == [BASE] and _names(meta, "adset") == [BASE, "Set B"]
    assert sum(1 for o in meta.objects.values() if o["_kind"] == "ad") == 2


def test_10_stop_in_readback_then_rerun_writes_nothing_and_job_end_clears_the_receipt(tmp_path, monkeypatch):
    meta = FakeMeta()
    pub = _publisher(meta)
    real = cr.CampaignCreator.readback
    calls = {"n": 0}

    def readback(self, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise cr.CreationStopped("readback stopped", kind="boom")
        return real(self, *a, **k)
    monkeypatch.setattr(cr.CampaignCreator, "readback", readback)
    first = pub.publish_draft(_draft(tmp_path, ads=1))
    assert first["error"] and pub._finished_receipts == []
    posts = len(meta.posts)
    again = pub.publish_draft(_draft(tmp_path, ads=1))
    assert again["error"] is None and not again["published_as"]            # то же имя, без копии
    assert len(meta.posts) == posts, "дозаписывать нечего: только сверка"
    assert pub._finished_receipts == [(ACT, BASE)]
    pub.finish_name_records()
    assert nr.load(ACT, BASE) is None
    assert pub.publish_draft(_draft(tmp_path, ads=1))["published_as"] == BASE + " copy 1"


def test_11_tab_stop_mid_run_then_second_publish_draft_makes_one_campaign(tmp_path, monkeypatch):
    meta = FakeMeta()
    pub = _publisher(meta)
    real = cr.CampaignCreator._write
    state = {"raised": False}

    def write(self, out, what, *a, **k):
        if what.startswith("ad ") and not what.startswith("ad set") and not state["raised"]:
            state["raised"] = True
            raise PageUnresponsive("tab")
        return real(self, out, what, *a, **k)
    monkeypatch.setattr(cr.CampaignCreator, "_write", write)
    assert pub.publish_draft(_draft(tmp_path, ads=1))["error"]
    pub.stop = None                                     # как drop_frozen_tab у ACL-2345
    res = pub.publish_draft(_draft(tmp_path, ads=1))
    assert res["error"] is None and _names(meta) == [BASE] and not res["published_as"]


def _open_job(jid):
    with pj._jobs_lock:
        pj._jobs[jid] = {"job_id": jid, "status": "running", "account_label": "B97", "started_at": "t",
                         "campaign_names": [], "results": [], "errors": [], "done_count": 0, "total": 3}


@pytest.fixture
def jobs(monkeypatch):
    made = []
    monkeypatch.setattr(pj, "_persist_job", lambda *a, **k: None)
    monkeypatch.setattr(pj, "_is_cancelled", lambda job_id: False)

    def run(meta, drafts, jid):
        _open_job(jid)
        made.append(jid)
        pj._publish_pending(jid, _publisher(meta), "B97", list(drafts))
        return pj._jobs[jid]["results"]
    yield run
    with pj._jobs_lock:
        for jid in made:
            pj._jobs.pop(jid, None)


def _three(tmp_path):
    out = []
    for i in (1, 2, 3):
        d = _draft(tmp_path, ads=1)
        d.name = d.ad_sets[0].name = f"MX Leads {i}"
        out.append(d)
    return out


def test_12_publish_all_after_a_stopped_job_does_not_copy_the_finished_campaign(tmp_path, jobs):
    meta = FakeMeta()
    _fail_nth(meta, "adsets", 2)                         # обрыв на второй кампании
    jobs(meta, _three(tmp_path), "j1")
    assert _names(meta) == ["MX Leads 1", "MX Leads 2"]
    assert nr.load(ACT, "MX Leads 1") is not None and nr.load(ACT, "MX Leads 2") is not None
    meta.fail_next.clear()
    posts = len(meta.posts)
    results = jobs(meta, _three(tmp_path), "j2")
    assert [r["error"] for r in results] == [None, None, None]
    assert _names(meta) == ["MX Leads 1", "MX Leads 2", "MX Leads 3"], "копий нет"
    assert len([p for p, _ in meta.posts[posts:] if p.endswith("/campaigns")]) == 1, "новая только третья"
    for i in (1, 2, 3):
        assert nr.load(ACT, f"MX Leads {i}") is None, "конец чистого задания очистил квитанции"
    jobs(meta, _three(tmp_path)[:1], "j3")                # теперь это повтор
    assert "MX Leads 1 copy 1" in _names(meta)


def test_13_job_with_a_stopped_campaign_keeps_all_receipts_and_a_mismatch_alone_counts_as_finished(tmp_path, jobs):
    meta = FakeMeta()
    _fail_nth(meta, "adsets", 2)
    jobs(meta, _three(tmp_path), "j1")
    assert nr.load(ACT, "MX Leads 1") is not None, "один отказ - квитанции остаются у всех"
    for i in (1, 2):
        nr.delete(ACT, f"MX Leads {i}")                    # другая «Meta» - свой набор id
    meta2 = FakeMeta()
    meta2.mutate_on_read[("campaign", "daily_budget")] = "1"
    res = jobs(meta2, _three(tmp_path)[:1], "j2")[0]
    assert res["blocking_failures"] and res["error"] is None
    assert nr.load(ACT, "MX Leads 1") is None


def test_15_unknown_campaign_write_with_and_without_a_visible_row(tmp_path):
    meta = FakeMeta()
    meta.fail_next["campaigns"] = "lost"                 # создано, ответ потерян
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert out.error == "" and _names(meta) == [BASE]
    assert nr.load(ACT, BASE)["campaign_id"] == out.campaign_id
    nr.delete(ACT, BASE)
    meta2 = FakeMeta()                                    # квитанция без id, строка видна -> дописываем её
    meta2.fail_next["ads"] = "unsent"
    _run(meta2, _draft(tmp_path, ads=1))
    nr.save(ACT, BASE, BASE, 0)
    meta2.fail_next.clear()
    out2, _ = _run(meta2, _draft(tmp_path, ads=1))
    assert out2.error == "" and out2.published_as == BASE and _names(meta2) == [BASE]
    nr.delete(ACT, BASE)
    meta3 = FakeMeta()                                    # квитанция без id, строки нет -> тот же номер
    _foreign(meta3, BASE)
    nr.save(ACT, BASE, BASE + " copy 1", 1)
    out3, _ = _run(meta3, _draft(tmp_path, ads=1))
    assert out3.published_as == BASE + " copy 1"


def test_14b_receipt_with_a_missing_campaign_falls_back_to_the_listing(tmp_path, monkeypatch):
    meta = FakeMeta()
    nr.save(ACT, BASE, BASE, 0, "99999")
    real = meta.request

    def request(method, path, params=None):
        if path.strip("/") == "99999":
            return {"status": 400, "body": {"error": {"code": 100, "message": "(#100) Object does not exist"}}}
        return real(method, path, params)
    monkeypatch.setattr(meta, "request", request)
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert out.error == "" and out.published_as == BASE and _names(meta) == [BASE]


def test_14c_edited_draft_after_a_stop_takes_the_next_number(tmp_path):
    meta = FakeMeta()
    meta.fail_next["ads"] = "unsent"
    _run(meta, _draft(tmp_path, ads=1))
    meta.fail_next.clear()
    d = _draft(tmp_path, ads=1)
    d.ad_sets[0].daily_budget = 77.0
    out, _ = _run(meta, d)
    assert out.error == "" and out.published_as == BASE + " copy 1"


def test_17_check_plan_shows_the_same_name_and_leaves_receipts_alone(tmp_path):
    meta = FakeMeta()
    pub = _publisher(meta)
    pub.publish_draft(_draft(tmp_path, ads=1))
    pub.finish_name_records()
    before = nr.load(ACT, BASE)
    res = _publisher(meta, dry_run=True).publish_draft(_draft(tmp_path, ads=1))
    assert res["published_as"] == BASE + " copy 1" and res["plan"]["campaign"]["name"] == BASE + " copy 1"
    assert nr.load(ACT, BASE) == before
    rule = next(v for v in res["audit"] if v["rule"] == "existing_diffs")
    assert rule["status"] == "warning" and "Will be published as 'MX Leads 1 copy 1'" in rule["detail"]
    assert _publisher(meta).publish_draft(_draft(tmp_path, ads=1))["published_as"] == BASE + " copy 1"


def test_17b_check_plan_reports_name_stops_as_errors(tmp_path):
    from src.fb_api import preflight_rules as pf
    meta = FakeMeta()
    for _ in range(205):
        _foreign(meta, BASE)
    res = _publisher(meta, dry_run=True).publish_draft(_draft(tmp_path, ads=1))
    rule = next(v for v in res["audit"] if v["rule"] == "existing_diffs")
    assert rule["status"] == "error" and pf.blocks(res["audit"])


def test_17c_check_plan_after_a_publish_job_shows_the_next_number_not_a_cached_one(tmp_path, jobs):
    from src.web import plan_cache
    meta = FakeMeta()
    first = _publisher(meta, dry_run=True).publish_draft(_draft(tmp_path, ads=1))
    assert not first["published_as"]
    jobs(meta, _three(tmp_path)[:1], "j1")                # публикация задания: имя занято, квитанции нет
    again = _publisher(meta, dry_run=True).publish_draft(_draft(tmp_path, ads=1))
    assert again["published_as"] == BASE + " copy 1"
    assert plan_cache is not None


def test_18_unwritable_receipt_stops_before_any_write(tmp_path, monkeypatch):
    meta = FakeMeta()

    def boom(*a, **k):
        raise nr.NameReceiptError("disk full")
    monkeypatch.setattr(nr, "save", boom)
    out, client = _run(meta, _draft(tmp_path, ads=1))
    assert out.stop_kind == "name_receipt" and meta.posts == [] and client.writes == 0
