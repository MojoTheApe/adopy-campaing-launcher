"""Web route tests for §11 Upload FB bridge."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from starlette.datastructures import FormData

from src.web.app import mlx_instance_labels, _enrich_country_overview, build_upload, templates

def _render_html(report: dict) -> str:
    """Render report.html the way the app does.

    Every test that built this context by hand drifted from the real one and
    then failed on whichever key was added last — `my_totals`,
    `account_preflight` — rather than on anything the test was about. The app
    fills in per-country totals before rendering, so do that here too.
    """
    report = {**report, "countries": [dict(c) for c in report.get("countries", [])]}
    _enrich_country_overview(report)
    return templates.env.get_template("report.html").render(
        mlx_labels=mlx_instance_labels(),
        request=MagicMock(),
        report=report,
        warnings=[],
        built_at="2026-06-08",
        upload_errors={},
        account_preflight={},
        account_preflight_json={},
        ref_options={},
        ref_options_json={},
        account_alias_map_json={},
        draft_status={},
    )



def _minimal_report() -> dict:
    return {
        "date": "2026-06-08",
        "my_cm": "Evgeni Sedin",
        "source_excel": "test.xlsx",
        "trend_days": 7,
        "total_combos": 1,
        "countries": [
            {
                "country": "Mexico",
                "trend": {"has_date": False},
                "cm_comparison": [],
                "top_combos": [],
                "targeting": [],
                "creatives": [],
                "close_recs": {},
                "account_rows": [],
                "open_recs": {
                    "MX-acc-1": [
                        {
                            "strategy_type": 1,
                            "strategy_label": "1 · trend + stable",
                            "campaign_name": "MX_Amz_All_Mix_trading_25_08.06",
                            "offer": "Amazon",
                            "targeting": "trading",
                            "device": "all",
                            "age": "25",
                            "creatives": [{"name": "2046 - Text 4"}],
                        }
                    ],
                },
            }
        ],
    }


def test_build_upload_requires_strategy(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.web.app.get_cached_report",
        lambda **kw: (_minimal_report(), None, []),
    )
    request = MagicMock()
    request.form = AsyncMock(
        return_value=FormData([("country", "Mexico"), ("account_label", "MX-acc-1")]),
    )
    resp = asyncio.run(build_upload(request))
    assert resp.status_code == 200
    # The campaign builder replaced "strategies" with campaign slots, so the
    # message changed. Kept as a test because the panel it renders into was
    # dead: errors were keyed by country CODE while the template looks them up
    # by country NAME, so a failed build silently returned the plain page.
    assert b"Add at least one campaign slot" in resp.body
    assert b"Cannot build upload" in resp.body


def test_build_upload_renders_form_controls() -> None:
    """The form must carry the fields the server parser reads.

    The per-campaign inputs (camp_N_ad_M_*) are built in the browser as slots
    are added, so asserting `campaign_name_1` or `title_1_0` in the static HTML
    tested a form shape that stopped existing when the campaign builder
    replaced the strategy checkboxes. What the server still depends on is the
    hidden trio below — _parse_build_upload_form reads exactly these.
    """
    html = _render_html(_minimal_report())
    assert 'action="/build-upload"' in html
    for field in ("country", "account_label", "camp_count"):
        assert f'name="{field}"' in html, f"форма не отправляет {field}, а парсер его читает"


def _upload(monkeypatch, extra):
    monkeypatch.setattr("src.web.app.get_cached_report", lambda **kw: (_minimal_report(), None, []))
    request = MagicMock()
    request.form = AsyncMock(return_value=FormData(
        [("country", "Mexico"), ("account_label", "MX-acc-1"), ("camp_count", "1"), ("camp_0_name", "C")] + extra))
    return asyncio.run(build_upload(request))


def test_build_upload_refuses_explicit_page_instead_of_dropping_it(monkeypatch) -> None:
    """ACL-1880: a Page chosen explicitly must not vanish from the exported xlsx."""
    resp = _upload(monkeypatch, [("camp_0_page_id", "123456")])
    assert resp.status_code == 200
    assert b"cannot be exported to Upload FB" in resp.body
    assert b"Page" in resp.body


def test_build_upload_automatic_page_is_not_refused(monkeypatch) -> None:
    resp = _upload(monkeypatch, [("camp_0_page_id", "")])
    assert b"Page chosen for a campaign cannot be exported" not in resp.body
