"""The Home account map: what it shows, and what its buttons send.

Two joins in this view have already gone wrong once each, and both failed
quietly — the page rendered, the buttons returned HTTP 200, and nothing was
right. They are the reason this file exists.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from src.web import account_links as links
from src.web.app import _build_account_map_view, app


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(links, "get_local_config_dir", lambda: str(tmp_path))
    monkeypatch.setattr(links, "get_ad_account_map_path", lambda: str(tmp_path / "nope.csv"))
    monkeypatch.setattr("src.web.app.get_ad_account_map_path", lambda: str(tmp_path / "nope.csv"))
    yield


def _report_with(country: str, label: str, spend: float) -> dict:
    return {
        "countries": [{
            "country": country,
            "account_rows": [{
                "account_label": label,
                "account_excel": {
                    "has_data": True, "spend": spend, "leads": 10,
                    "deposits": 1, "cpl": 1.0, "cpa": 10.0, "cr": 10.0,
                },
            }],
        }]
    }


def test_a_country_stored_as_a_code_still_finds_its_numbers():
    """The map stores "KW", the workbook says "Kuwait".

    Matching on one of them alone renders a map where every account reads
    "no data" — which looks like a data problem and is a join that missed.
    """
    links.link("FacebookX", "country", "KW")

    view = _build_account_map_view(_report_with("Kuwait", "FacebookX", 120.0))

    assert len(view["countries"]) == 1
    country = view["countries"][0]
    assert country["country"] == "Kuwait", "the reader sees the name used everywhere else"
    assert country["key"] == "KW", "the buttons must send what the store knows"
    assert country["rows"][0]["spend"] == 120.0


def test_the_buttons_send_the_stored_key_not_the_label():
    """A button that sends the label returns HTTP 200 having changed nothing."""
    links.link("FacebookX", "country", "KW")
    view = _build_account_map_view(_report_with("Kuwait", "FacebookX", 0.0))

    async def _close(value: str):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            r = await client.post(
                "/account-links",
                content=json.dumps({"action": "close", "kind": "country", "value": value}),
                headers={"Content-Type": "application/json"},
            )
            return r.json()

    # What the template now sends.
    assert asyncio.run(_close(view["countries"][0]["key"]))["freed"] == ["FacebookX"]

    links.link("FacebookX", "country", "KW")
    # What it used to send.
    assert asyncio.run(_close("Kuwait"))["freed"] == []


def test_an_account_spending_today_is_not_taken_out_without_a_word(monkeypatch):
    """The map is also how somebody notices money is still running."""
    links.link("FacebookX", "country", "KW")
    monkeypatch.setattr(
        "src.web.app.get_cached_report",
        lambda: (_report_with("Kuwait", "FacebookX", 250.0), None, []),
    )

    async def _try(confirm: bool):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            return await client.post(
                "/account-links",
                content=json.dumps({
                    "action": "unlink", "kind": "country", "value": "KW",
                    "account": "FacebookX", "confirm": confirm,
                }),
                headers={"Content-Type": "application/json"},
            )

    blocked = asyncio.run(_try(False))
    assert blocked.status_code == 409
    assert blocked.json()["needs_confirm"] is True
    assert "keep running" in blocked.json()["error"]
    assert links.accounts_for("country", "KW") == ["FacebookX"], "nothing moved"

    assert asyncio.run(_try(True)).status_code == 200
    assert links.accounts_for("country", "KW") == []
