"""ACL-123: отключённый кабинет говорит человеку, что делать дальше."""
from __future__ import annotations

from pathlib import Path

import pytest

from src.analysis_agent import account_spend_api as api
from src.analysis_agent.account_next_step import next_step

ROOT = Path(__file__).resolve().parents[1]


def _text(reason):
    return api.map_status(2, reason)[1]


def test_every_reason_the_api_reader_names_has_a_concrete_step():
    for code, name in api._DISABLE_REASONS.items():
        step = next_step("disabled", _text(code))
        assert step and "unknown" not in step.lower(), name
        assert "not recognise" not in step, name


def test_policy_violation_points_to_account_quality_and_appeal():
    step = next_step("disabled", _text(1))
    assert "policy violation" in step and "Account Quality" in step and "appeal" in step


def test_permanent_close_does_not_promise_recovery():
    step = next_step("disabled", _text(7))
    assert "permanently" in step and "replacement" in step
    assert "recover" not in step.lower() and "automatic" not in step.lower()


def test_unknown_reason_code_says_the_cause_is_unknown():
    step = next_step("disabled", _text(99))
    assert "cause is unknown" in step and "(99)" in step


def test_page_read_without_reason_says_the_reason_was_not_reported():
    step = next_step("disabled", "Account disabled")
    assert step.startswith("The reason was not reported")
    assert "Account Quality" in step


def test_other_problem_states_get_a_step_and_healthy_ones_do_not():
    assert next_step("restricted", "Ad account is restricted: payment is unsettled")
    assert next_step("review", "")
    for status in ("clear", "unknown", ""):
        assert next_step(status, "") == ""


def _render(status):
    from jinja2 import Environment, FileSystemLoader

    from src.analysis_agent.money_format import fmt_currency

    env = Environment(loader=FileSystemLoader(str(ROOT / "src" / "web" / "templates")), autoescape=True)
    env.filters["fmt_currency"] = fmt_currency
    env.filters["fmt_usd"] = str
    acc = "KLD_BFL_X84"
    country = {"country": "Kuwait", "key": "KW", "virtual": False,
               "rows": [{"account": acc, "has_data": False, "ready": True,
                         "in_workbook": True, "status": status, "to_do": ""}]}
    return env.get_template("_account_map_country.html").render(
        mc=country, mlx={},
        amap={"countries": [country], "available": [], "all_accounts": [], "live": {},
              "splits": {}, "fallback": {}, "dates": [], "date_from": "2026-09-01",
              "date_to": "2026-09-28", "span": ""}), acc


def test_map_row_shows_account_name_status_and_next_step_together():
    status = {"status": "disabled", "status_text": _text(1), "at": "2026-10-16 10:00:00",
              "at_short": "16.10.26", "stale": "", "next_step": next_step("disabled", _text(1))}
    html, acc = _render(status)
    start = html.index("<tbody"); row = html[start:html.index("</tr>", start)]
    assert acc in row, "account identity stays visible"
    assert ">disabled</span>" in row
    assert 'class="amap-st-next"' in row and "Account Quality" in row


def test_map_row_without_problem_has_no_next_step():
    html, _ = _render({"status": "clear", "at": "2026-10-16 10:00:00", "at_short": "16.10.26", "stale": "", "next_step": ""})
    assert "amap-st-next" not in html.split("<style")[0]


def test_get_status_and_reading_carry_the_step(monkeypatch):
    from src.analysis_agent import account_spend as sp

    text = _text(3)
    data = {"status|ACC": {"status": "disabled", "status_text": text, "at": "2026-10-16 10:00:00"}}
    monkeypatch.setattr(sp, "_load_all", lambda: data)
    assert "payment" in sp.get_status("ACC")["next_step"]
    assert "payment" in sp.SpendReading(account="ACC", date_from="", date_to="", status="disabled", status_text=text).to_dict()["next_step"]
