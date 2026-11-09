"""ACL-2419: статус кабинета из Graph, а не устаревшее «no warning». Офлайн, фикстуры синтетические."""
from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.analysis_agent import account_spend as sp  # noqa: E402
from src.analysis_agent import account_spend_api as api  # noqa: E402
from src.analysis_agent import campaign_inventory as ci  # noqa: E402
from src.analysis_agent import status_resolve as R  # noqa: E402
from src.web import account_state as A  # noqa: E402

ACC = "FacebookDocXXX - Example"
ACT = "act_100000000000001"
NOW = datetime(2026, 11, 6, 12, 0, 0)


# ── map_status: коды из задачи ──────────────────────────────────────────

@pytest.mark.parametrize("code,reason,word", [
    (1, 0, "clear"), (2, 1, "disabled"), (3, 0, "restricted"), (7, 0, "review"),
    (9, 0, "restricted"), (100, 0, "disabled"), (101, 0, "disabled"), (55, 0, "unknown"),
])
def test_map_status_words(code, reason, word):
    assert api.map_status(code, reason)[0] == word


def test_status_from_account_gives_code_and_reason_name():
    status, text, code, reason = api.status_from_account({"account_status": 2, "disable_reason": 1})
    assert (status, code, reason) == ("disabled", 2, "ADS_INTEGRITY_POLICY")
    assert "ADS_INTEGRITY_POLICY" in text
    assert api.status_from_account({}) == ("", "", None, "")      # нет кода — не `clear`
    assert api.status_from_account({"account_status": 1, "disable_reason": 1})[3] == ""


# ── resolve_status ──────────────────────────────────────────────────────

def test_fresh_clear_is_known():
    r = R.resolve_status({"status": "clear", "at": "2026-11-06 08:00:00"}, "", NOW)
    assert r["shown"] == "clear" and r["known"] and not r["stale"]


def test_old_clear_after_failed_read_is_unknown():
    r = R.resolve_status({"status": "clear", "at": "2026-10-24 08:00:00"}, "2026-11-05 14:02:00", NOW)
    assert r["shown"] == "unknown" and not r["known"]


def test_clear_older_than_a_day_is_unknown_even_without_a_failed_read():
    r = R.resolve_status({"status": "clear", "at": "2026-11-05 08:00:00"}, "", NOW)
    assert r["shown"] == "unknown" and not r["fresh"]


def test_old_disabled_stays_disabled_but_not_refreshed():
    r = R.resolve_status({"status": "disabled", "at": "2026-10-24 08:00:00"}, "", NOW)
    assert r["shown"] == "disabled" and r["known"] and r["stale"]


def test_unrecognised_api_status_is_unknown():
    assert R.resolve_status({"status": "unknown", "at": "2026-11-06 08:00:00"}, "", NOW)["shown"] == "unknown"


def test_read_label_year_only_when_not_this_year():
    assert R.read_label("2026-11-05 14:02:11", NOW) == "05.11 14:02"
    assert R.read_label("2025-11-05 14:02:11", NOW) == "05.11 14:02.2025"


# ── слияние API и страницы ──────────────────────────────────────────────

def test_heavier_status_wins_inside_one_cycle():
    old = {"status": "disabled", "at": "2026-11-06 08:00:00", "source": "page"}
    new = {"status": "clear", "at": "2026-11-06 08:05:00", "source": "api"}
    assert R.should_replace(old, new) is False


def test_newer_api_clear_replaces_an_old_page_disabled():
    old = {"status": "disabled", "at": "2026-11-05 08:00:00", "source": "page"}
    new = {"status": "clear", "at": "2026-11-06 08:00:00", "source": "api"}
    assert R.should_replace(old, new) is True


def test_older_reading_does_not_replace_a_newer_one():
    old = {"status": "clear", "at": "2026-11-06 08:00:00"}
    assert R.should_replace(old, {"status": "disabled", "at": "2026-11-04 08:00:00"}) is False


# ── хранилище ───────────────────────────────────────────────────────────

@pytest.fixture
def store(monkeypatch):
    data: dict = {}
    monkeypatch.setattr(sp, "_load_all", lambda: data)
    monkeypatch.setattr(sp, "_save_all", lambda d: None)
    monkeypatch.setattr(ci, "get_attempt", lambda account: None)
    return data


def test_put_status_then_get_status_carries_the_new_fields(store):
    assert sp.put_status(ACC, "disabled", "Ad account is disabled", source="api",
                         at="2026-11-05 14:02:11", disable_reason="ADS_INTEGRITY_POLICY", account_status=2)
    got = sp.get_status(ACC)
    assert got["status"] == "disabled" and got["source"] == "api" and got["shown"] == "disabled"
    assert got["disable_reason"] == "ads integrity policy" and got["read_label"].startswith("05.11 14:02")
    assert got["account_status"] == "2" and got["not_refreshed"] == "1"
    for key in ("status_text", "at", "at_short", "stale", "tried_at", "tried_error", "next_step", "shot_id", "shot_at"):
        assert key in got, "прежний набор ключей не уменьшается (publish_preflight читает его)"


def test_empty_status_is_not_written(store):
    assert sp.put_status(ACC, "", "") is False and store == {}


def test_failed_list_attempt_after_the_status_marks_it_unknown(store, monkeypatch):
    sp.put_status(ACC, "clear", "ok", source="api", at="2026-10-24 08:00:00", account_status=1)
    monkeypatch.setattr(ci, "get_attempt", lambda account: {"at": "2026-11-05 14:02:00", "error": "logged out"})
    got = sp.get_status(ACC)
    assert got["stale"] == "1" and got["tried_at"] == "2026-11-05 14:02:00" and got["shown"] == "unknown"


def test_spend_reader_status_goes_through_the_merge(store):
    sp.put_status(ACC, "disabled", "d", source="page", at="2026-11-06 08:00:00")
    rec = sp.SpendReading(account=ACC, date_from="2026-11-06", date_to="2026-11-06", source="api",
                          state="ok", status="clear", finished_at="2026-11-06 08:03:00")
    sp._put(rec)
    assert sp.get_status(ACC)["status"] == "disabled"             # тот же цикл: весомый выигрывает


# ── чтение списка отдаёт статус без лишних запросов ─────────────────────

class FakeClient:
    def __init__(self, account):
        self._acc, self.calls, self.truncated = account, 0, []

    def account(self, act):
        self.calls += 1
        return self._acc

    def get_all(self, path, params, max_pages=5):
        self.calls += 1
        return []


def test_read_into_keeps_account_status_and_adds_no_request():
    client = FakeClient({"account_id": ACT[4:], "currency": "USD", "account_status": 2, "disable_reason": 1})
    snap = ci.Snapshot(account=ACC)
    ci.read_into(snap, client, ACT, structure_only=True)
    assert (snap.account_status, snap.disable_reason) == (2, 1)
    assert client.calls == 2                                     # /act_X и список кампаний, как раньше


def test_record_status_writes_after_a_good_read(store):
    snap = ci.Snapshot(account=ACC, state="ok", steps={"campaigns": "ok"}, account_status=2, disable_reason=1)
    assert ci.record_status(snap) is True
    assert sp.get_status(ACC)["status"] == "disabled"


def test_record_status_skips_a_failed_read(store):
    snap = ci.Snapshot(account=ACC, state="failed", error="boom", account_status=2)
    assert ci.record_status(snap) is False and store == {}


def test_record_status_survives_a_store_failure(monkeypatch):
    def boom(*a, **k):
        raise OSError("disk")
    monkeypatch.setattr(sp, "put_status", boom)
    snap = ci.Snapshot(account=ACC, state="ok", steps={"campaigns": "ok"}, account_status=1)
    assert ci.record_status(snap) is False


def test_old_snapshot_without_the_new_fields_loads():
    assert ci._snapshot_from({"account": ACC, "state": "ok"}).account_status is None


# ── Home: статус раньше спенда ──────────────────────────────────────────

TODAY = date(2026, 11, 6)


def row(status, spend=40, **kw):
    return {"status": status, "spend": spend, "has_data": True, "in_workbook": True, "act_id": ACT, **kw}


def test_disabled_with_spend_stays_dead_and_shows_time_and_reason():
    s = A.account_state(row({"status": "disabled", "status_text": "x", "at": "2026-11-05 14:02:11", "source": "api",
                             "disable_reason": "ads integrity policy"}), TODAY)
    assert s["key"] == "dead"
    assert s["status_line"] == "disabled · 05.11 14:02 · ads integrity policy"
    old = A.account_state(row({"status": "disabled", "at": "2026-10-20 14:02:11"}), TODAY)
    assert old["key"] == "dead" and old["status_line"].endswith(" · not refreshed")


def test_stale_clear_with_spend_is_unknown_not_active():
    s = A.account_state(row({"status": "clear", "at": "2026-10-23 14:02:11"}), TODAY)
    assert s["key"] == "unknown" and s["reason"] == "last status 23.10 14:02"
    assert s["status_line"] == "unknown · last status 23.10 14:02"


def test_failed_read_after_clear_is_unknown_with_the_failure_date():
    s = A.account_state(row({"status": "clear", "at": "2026-11-06 08:00:00", "stale": "1",
                             "tried_at": "2026-11-06 09:30:00", "tried_error": "logged out"}), TODAY)
    assert s["key"] == "unknown" and s["status_line"] == "unknown · read failed 06.11 09:30"


def test_fresh_api_clear_with_spend_is_active():
    s = A.account_state(row({"status": "clear", "at": "2026-11-06 08:12:00", "source": "api"}), TODAY)
    assert s["key"] == "active" and s["status_line"] == "active · 06.11 08:12"


def test_fresh_page_clear_is_no_warning():
    s = A.account_state(row({"status": "clear", "at": "2026-11-06 08:12:00", "source": "page"}), TODAY)
    assert s["status_line"] == "no warning · 06.11 08:12"


def test_no_status_with_spend_is_active_as_before():
    assert A.account_state(row({}), TODAY)["key"] == "active"


def test_never_read_account_with_a_binding_says_so():
    s = A.account_state(row({}, spend=0, has_data=False), TODAY)
    assert s["key"] == "unknown" and s["status_line"] == "unknown · never read"


def test_restricted_words():
    s = A.account_state(row({"status": "restricted", "status_text": "restricted: payment is unsettled (Meta API: UNSETTLED)",
                             "at": "2026-11-06 08:00:00"}), TODAY)
    assert s["key"] == "issue" and s["status_line"].startswith("restricted · payment unsettled · 06.11 08:00")


# ── экран ───────────────────────────────────────────────────────────────

def _manage_cell(status: dict) -> str:
    from jinja2 import Environment, FileSystemLoader

    from src.analysis_agent.money_format import fmt_currency

    env = Environment(loader=FileSystemLoader(str(ROOT / "src" / "web" / "templates")), autoescape=True)
    env.filters["fmt_currency"] = fmt_currency
    env.filters["fmt_usd"] = str
    country = {"country": "Kuwait", "key": "KW", "virtual": False,
               "rows": [{"account": ACC, "has_data": False, "ready": True, "in_workbook": True,
                         "status": status, "to_do": ""}]}
    html = env.get_template("_account_map_country.html").render(
        mc=country, mlx={},
        amap={"countries": [country], "available": [], "all_accounts": [], "live": {}, "splits": {},
              "fallback": {}, "dates": [], "date_from": "2026-09-01", "date_to": "2026-09-28", "span": ""})
    cell = html[html.index('<td class="amap-status">'):]
    return cell[: cell.index("</td>")]


def test_manage_cell_stale_clear_reads_unknown_not_no_warning():
    cell = _manage_cell({"status": "clear", "shown": "unknown", "source": "api", "at": "2026-10-23 14:02:11",
                         "at_short": "23.10.26", "read_label": "23.10 14:02", "stale": "", "not_refreshed": "1"})
    assert ">unknown</span>" in cell and ">no warning</span>" not in cell and ">active</span>" not in cell
    assert "23.10 14:02" in cell and "not refreshed" in cell


def test_manage_cell_fresh_api_clear_is_active_and_page_clear_is_no_warning():
    base = {"status": "clear", "shown": "clear", "at": "2026-11-06 08:00:00", "at_short": "06.11.26",
            "read_label": "06.11 08:00", "stale": "", "not_refreshed": ""}
    assert ">active</span>" in _manage_cell({**base, "source": "api"})
    assert ">no warning</span>" in _manage_cell({**base, "source": "page"})


def test_manage_cell_old_disabled_is_red_and_marked():
    cell = _manage_cell({"status": "disabled", "shown": "disabled", "status_text": "Ad account is disabled",
                         "at": "2026-10-23 14:02:11", "at_short": "23.10.26", "read_label": "23.10 14:02",
                         "stale": "", "not_refreshed": "1", "source": "api"})
    assert "amap-st bad" in cell and ">disabled</span>" in cell and "· not refreshed" in cell


def test_live_update_script_uses_the_same_words():
    js = (ROOT / "src" / "web" / "templates" / "report.html").read_text(encoding="utf-8")
    assert "(s.source === 'api' ? 'active' : 'no warning')" in js


# ── Open ────────────────────────────────────────────────────────────────

def test_open_queues_one_status_read_only_for_a_good_open(monkeypatch):
    from src.analysis_agent import account_refresh
    from src.web import warm_api

    asked: list[tuple] = []

    class Q:
        def request(self, account, trigger, *, force=False, window=""):
            asked.append((account, trigger, force))
            return {"queued": account != "Busy", "reason": "x"}

    monkeypatch.setattr(account_refresh, "queue", lambda: Q())
    seen: dict = {}
    monkeypatch.setattr(warm_api, "_set_result", lambda job, label, res: seen.__setitem__(label, res))
    warm_api._on_open_result("j", "Acc", {"ok": True})
    warm_api._on_open_result("j", "Busy", {"ok": True})
    warm_api._on_open_result("j", "Bad", {"ok": False, "code": "open_by_hand"})
    assert asked == [("Acc", "open", True), ("Busy", "open", True)]
    assert (seen["Acc"]["status_read"], seen["Busy"]["status_read"], seen["Bad"]["status_read"]) == ("queued", "skipped", "n/a")


def test_open_never_breaks_when_the_queue_raises(monkeypatch):
    from src.analysis_agent import account_refresh
    from src.web import warm_api

    def boom():
        raise RuntimeError("no queue")
    monkeypatch.setattr(account_refresh, "queue", boom)
    assert warm_api.request_status_read("Acc") == "skipped"


def test_open_trigger_is_known_to_the_queue_and_not_a_user_trigger():
    from src.analysis_agent import account_refresh
    assert "open" in account_refresh.PRIORITY and "open" not in ci.USER_TRIGGERS
