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


# ── acl-357: спенд, привязанный к стране самим кабинетом ──────────────────
#
# Соседняя колонка «Account read» подписана «not country-attributed spend» —
# это весь спенд привязанных кабинетов по всем странам. Здесь числа про
# страну: 15.09 живое чтение DQ501 отнесло деньги к Chile на аккаунте, где
# весь вечер запускали кампании на Гватемалу.

@pytest.fixture
def splits(monkeypatch, tmp_path):
    from src.analysis_agent import country_split as cs

    monkeypatch.setattr(cs, "_store_path", lambda: tmp_path / "splits.json")
    return cs


def _split(cs, account, rows, date_from="2026-09-01", date_to="2026-09-15"):
    cs.save_split(cs.CountrySplit(account=account, date_from=date_from,
                                  date_to=date_to, state="ok", rows=rows))


def test_a_mapped_country_carries_what_the_account_attributed_to_it(splits):
    links.link("FacebookX", "country", "Kuwait")
    _split(splits, "FacebookX", [{"country": "Kuwait", "spend": 23.89, "currency": "KWD"}])

    view = _build_account_map_view(_report_with("Kuwait", "FacebookX", 120.0),
                                   date_from="2026-09-01", date_to="2026-09-15")

    country = next(c for c in view["countries"] if c["country"] == "Kuwait")
    assert country["country_read"] == [
        {"country": "Kuwait", "currency": "KWD", "spend": 23.89,
         "accounts": ["FacebookX"], "summed": True}
    ]


def test_a_country_only_the_accounts_know_about_gets_its_own_row(splits):
    """Страны нет ни в связях, ни в книге — но деньги туда ушли.

    Ради этого случая разбивка и заводилась. Промолчать о нём значит
    потерять спенд: показывать его негде.
    """
    links.link("FacebookX", "country", "Kuwait")
    _split(splits, "FacebookX", [{"country": "Chile", "spend": 44.17, "currency": "$"}])

    view = _build_account_map_view(_report_with("Kuwait", "FacebookX", 120.0),
                                   date_from="2026-09-01", date_to="2026-09-15")

    chile = next((c for c in view["countries"] if c["country"] == "Chile"), None)
    assert chile is not None, "страна из показаний не попала на экран"
    assert chile["virtual"] is True and chile["from_reading"] is True
    assert chile["rows"] == [] and chile["country_read"][0]["spend"] == 44.17


def test_a_country_already_on_the_map_is_not_duplicated(splits):
    links.link("FacebookX", "country", "Kuwait")
    _split(splits, "FacebookX", [{"country": "Kuwait", "spend": 1.0, "currency": "KWD"}])

    view = _build_account_map_view(_report_with("Kuwait", "FacebookX", 120.0),
                                   date_from="2026-09-01", date_to="2026-09-15")

    assert [c["country"] for c in view["countries"]].count("Kuwait") == 1


def test_without_a_period_nothing_country_attributed_is_claimed(splits):
    """Без дат читать нечего: показания хранятся по периоду."""
    links.link("FacebookX", "country", "Kuwait")
    _split(splits, "FacebookX", [{"country": "Chile", "spend": 44.17, "currency": "$"}])

    view = _build_account_map_view(_report_with("Kuwait", "FacebookX", 120.0), span="all")

    assert all(not c.get("country_read") for c in view["countries"])
    assert "Chile" not in [c["country"] for c in view["countries"]]


def test_the_country_attributed_part_shows_only_amounts(splits):
    """ACL-1264: «in Kuwait: …» — как сводка рядом (ACL-1258): без «currency
    unknown» и без имён кабинетов. Суммы под знаком не складываются."""
    from src.web.app import templates

    links.link("FacebookX", "country", "Kuwait")
    links.link("FacebookY", "country", "Kuwait")
    links.link("FacebookZ", "country", "Kuwait")
    links.link("FacebookW", "country", "Kuwait")
    _split(splits, "FacebookW", [{"country": "Kuwait", "spend": 3.0, "currency": ""}])
    _split(splits, "FacebookX", [{"country": "Kuwait", "spend": 10.5, "currency": "$"}])
    _split(splits, "FacebookY", [{"country": "Kuwait", "spend": 20.25, "currency": "$"}])
    _split(splits, "FacebookZ", [{"country": "Kuwait", "spend": 7.0, "currency": "KWD"}])

    view = _build_account_map_view(_report_with("Kuwait", "FacebookX", 120.0),
                                   date_from="2026-09-01", date_to="2026-09-15")
    html = templates.env.get_template("_account_map.html").render(amap=view, mlx={})
    # ACL-2063 supersedes the live-account spend display; its retained model
    # and historical readings remain intact, but no Meta number is shown as BI.
    assert 'class="amap-country-read"' not in html
    assert 'class="amap-live-summary"' not in html
    assert 'Campaign list' in html and 'numbers from the stats workbook' in html
