"""Web route tests for §11 Upload FB bridge."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from starlette.datastructures import FormData

from src.web.app import build_upload, templates


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
    assert b"Select at least one strategy" in resp.body


def test_build_upload_renders_form_controls() -> None:
    html = templates.env.get_template("report.html").render(
        request=MagicMock(),
        report=_minimal_report(),
        warnings=[],
        built_at="2026-06-08",
        upload_errors={},
    )
    assert 'action="/build-upload"' in html
    assert 'data-open-form' in html
    assert 'name="strategy"' in html
    assert "Submit — MX-acc-1" in html
    assert 'name="campaign_name_1"' in html
    assert 'name="title_1_0"' in html
