"""Home: состояния, счётчики и порядок стран в `_build_account_map_view`."""
import pytest

from src.web import account_links as links
from src.analysis_agent import account_spend
from src.web.app import _build_account_map_view


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(links, "get_local_config_dir", lambda: str(tmp_path))
    monkeypatch.setattr(links, "get_ad_account_map_path", lambda: str(tmp_path / "nope.csv"))
    monkeypatch.setattr("src.web.app.get_ad_account_map_path", lambda: str(tmp_path / "nope.csv"))


def _acc(label, spend):
    return {"account_label": label, "account_excel": {
        "has_data": spend is not None, "spend": spend, "leads": 10, "deposits": 1,
        "cpl": 1.0, "cpa": 10.0, "cr": 10.0}}


def _report():
    return {"countries": [
        {"country": "Chile", "my_spend": 0, "account_rows": [_acc("FbIdle", None)]},
        {"country": "Kuwait", "my_spend": 120, "account_rows": [_acc("FbSpend", 120.0), _acc("FbDead", 40.0)]},
        {"country": "Peru", "my_spend": 900, "my_totals": {"spend": 900}, "account_rows": []},
    ]}


def test_states_counts_and_active_countries_first(monkeypatch):
    for acc, c in (("FbIdle", "Chile"), ("FbSpend", "Kuwait"), ("FbDead", "Kuwait")):
        links.link(acc, "country", c)
    monkeypatch.setattr(account_spend, "get_status",
                        lambda a: {"status": "disabled", "status_text": "disabled", "at": "2026-11-01T10:00:00"}
                        if a == "FbDead" else {})
    view = _build_account_map_view(_report())
    names = [mc["country"] for mc in view["countries"]]
    assert names[0] == "Kuwait", "страна с активным аккаунтом идёт первой"
    assert names.index("Peru") > names.index("Kuwait"), "виртуальная страна не активна"
    kw = view["countries"][0]
    assert kw["active"] and kw["state_counts"]["active"] == 1 and kw["state_counts"]["dead"] == 1
    assert [r["state"]["key"] for r in kw["rows_aov"]] == ["active", "dead"]
    st = view["state_totals"]
    assert st["all"] == 3 and st["active"] == 1 and st["dead"] == 1
    assert st["countries_active"] == 1 and st["countries_total"] == 3


def test_no_live_reads(monkeypatch):
    """Открытие Home не ходит ни в Meta, ни в Multilogin."""
    links.link("FbSpend", "country", "Kuwait")
    import src.analysis_agent.account_spend as sp

    def boom(*a, **k):
        raise AssertionError("live read")

    for name in ("enqueue", "read_account", "read_status", "read_now"):
        if hasattr(sp, name):
            monkeypatch.setattr(sp, name, boom)
    view = _build_account_map_view(_report())
    assert view["state_totals"]["all"] >= 1


def test_fragment_totals_come_from_the_selected_period(monkeypatch):
    """ACL-2384: при смене периода /account-map считает отчёт за этот период, а не отдаёт тоталы всех дат книги.

    Строки аккаунтов карта пересчитывает сама, а «Total · all countries» берёт из отчёта; отчёт без периода давал
    итог за всю книгу рядом со строками за выбранные дни.
    """
    from starlette.requests import Request
    from src.web import app as web

    seen = []
    whole = {"countries": [{"country": "Chile", "my_totals": {"spend": 832093, "leads": 34514, "deposits": 680}, "account_rows": []}]}
    period = {"countries": [{"country": "Chile", "my_totals": {"spend": 662, "leads": 43, "deposits": 1}, "account_rows": []}]}

    def for_period(report, df, date_from, date_to):
        seen.append((date_from, date_to))
        return period

    monkeypatch.setattr(web, "get_cached_report", lambda *a, **k: (whole, None, []))
    monkeypatch.setattr(web, "report_for_period", for_period)
    monkeypatch.setattr(web, "_enrich_country_overview", lambda r: None)
    monkeypatch.setattr(web, "mlx_instance_labels", lambda: {})
    links.add_value("country", "Chile")
    request = Request({"type": "http", "method": "GET", "path": "/account-map", "query_string": b"", "headers": [],
                       "server": ("testserver", 80), "scheme": "http"})
    response = web.account_map_fragment.__wrapped__(request, date_from="2026-10-25", date_to="2026-11-03", span="")
    assert seen == [("2026-10-25", "2026-11-03")]
    assert response.context["amap"]["totals"]["spend"] == 662 and response.context["amap"]["totals"]["deposits"] == 1
