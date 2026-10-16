"""ACL-106: an empty period says whose emptiness it is.

"The workbook has no rows for anyone on these dates" means go update the file;
"the workbook has rows, just none for this manager" means nothing to review.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from src.web.app import _period_owner, templates  # noqa: E402
from test_sidebar_without_workbook import _piece  # noqa: E402

def _report(cms, my_cm="Bob", period=("2026-09-07", "2026-09-07")):
    return {
        "my_cm": my_cm,
        "period_from": period[0],
        "period_to": period[1],
        "countries": [
            {"country": "Saudi Arabia",
             "cm_comparison": [{"cm": c, "is_me": c.strip().lower() == my_cm.lower()} for c in cms]}
        ] if cms is not None else [],
    }


def test_manager_without_rows_while_others_have_them():
    owner = _period_owner(_report(["Ann"]))
    assert owner["kind"] == "manager" and owner["manager"] == "Bob"


def test_nobody_has_rows_is_left_to_the_workbook_notice():
    assert _period_owner(_report(None)) is None


def test_manager_with_rows_gets_no_notice():
    assert _period_owner(_report(["Ann", " bob "])) is None


def test_blank_or_nan_cm_rows_are_not_other_managers():
    assert _period_owner(_report(["", "nan", "None"])) is None


def test_no_period_or_no_manager_gets_no_notice():
    assert _period_owner(_report(["Ann"], period=("", ""))) is None
    assert _period_owner(_report(["Ann"], my_cm="")) is None


def test_owner_never_touches_the_frame(monkeypatch):
    import src.web.app as app

    def boom(*a, **k):
        raise AssertionError("workbook frame rescanned")

    monkeypatch.setattr(app, "_filter_by_period", boom)
    assert _period_owner(_report(["Ann"]))["kind"] == "manager"


def _render(owner, *, date_from, dates=("2026-09-07", "2026-09-08")):
    return templates.env.from_string(_piece()).render(
        report={"countries": [{"country": "Saudi Arabia"}]},
        account_map={"date_from": date_from, "date_to": date_from, "dates": list(dates)},
        period={"date_from": date_from, "date_to": date_from},
        period_owner=owner,
    )


def test_manager_case_names_manager_and_dates_and_keeps_map_off_the_notice():
    out = _render(_period_owner(_report(["Ann"])), date_from="2026-09-07")
    assert "No rows for Bob" in out and "2026-09-07" in out
    assert "other managers" in out
    assert "The workbook has no rows" not in out
    assert "may need updating" not in out


def test_whole_workbook_case_blames_the_file_not_the_manager():
    out = _render(_period_owner(_report(None)), date_from="2026-09-09")
    assert "The workbook has no rows" in out and "may need updating" in out
    assert "No rows for Bob" not in out


def test_non_empty_period_has_no_notice():
    out = _render(None, date_from="2026-09-08")
    assert "workbook-period-notice" not in out


def test_full_page_keeps_account_map_and_reading_controls_in_manager_case():
    from unittest.mock import MagicMock

    from src.web.app import _enrich_country_overview, mlx_instance_labels
    from tests.test_web_build_upload import _minimal_report

    report = _minimal_report()
    _enrich_country_overview(report)
    amap = dict(countries=[], available=[], all_accounts=[], live={}, span="",
                date_from="2026-09-07", date_to="2026-09-07", dates=["2026-09-07", "2026-09-08"])
    html = templates.env.get_template("report.html").render(
        request=MagicMock(), report=report, mlx_labels=mlx_instance_labels(),
        warnings=[], built_at="2026-09-12", upload_errors={}, account_map=amap,
        account_preflight={}, account_preflight_json={}, ref_options={},
        ref_options_json={}, account_alias_map_json={}, draft_status={},
        period={"date_from": "2026-09-07", "date_to": "2026-09-07", "span": ""},
        period_owner={"kind": "manager", "manager": "Bob", "other_rows": 1},
    )
    assert "No rows for Bob" in html
    assert html.index('id="workbook-period-notice"') < html.index('<div class="dashboard-shell">')
    assert "Read all" in html and 'amap' in html
