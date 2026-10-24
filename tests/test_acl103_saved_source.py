"""The saved comparison uses the actual map/model/store without another read."""
from __future__ import annotations

from html.parser import HTMLParser
from unittest.mock import MagicMock

import pytest

from src.analysis_agent import account_spend, bi_stats
from src.web import app as web
from tests.test_acl103_source_contract import history_frame, isolated_source  # noqa: F401


LABEL = "KLD_BFL_X84"
ALIAS = "FacebookMKTOBFLX84"
DAY = "2026-10-18"


class SavedPanel(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.stack, self.text, self.unsafe = [], [], []
        self.attrs, self.parents, self.depth = None, [], None
        self.entries = 0
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "details" and "data-source-check" in attrs:
            self.attrs, self.parents, self.depth = attrs, self.stack[:], len(self.stack)
        if self.depth is not None:
            self.entries += "data-source-account" in attrs
            if tag == "button" or any(k in attrs for k in ["onclick", "hx-get", "hx-post"]):
                self.unsafe.append(tag)
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag == "details" and self.depth is not None and len(self.stack) == self.depth + 1:
            self.depth = None
        if tag in self.stack:
            self.stack = self.stack[:len(self.stack) - 1 - self.stack[::-1].index(tag)]

    def handle_data(self, text):
        if self.depth is not None:
            self.text.append(text)

    def content(self):
        return " ".join(" ".join(self.text).split())


@pytest.fixture
def saved_comparison_context(monkeypatch, tmp_path):
    from src.web import account_links

    missing_map = str(tmp_path / "no-map.csv")
    monkeypatch.setattr(account_links, "get_ad_account_map_path", lambda: missing_map)
    monkeypatch.setattr(web, "get_ad_account_map_path", lambda: missing_map)
    monkeypatch.setattr(account_spend, "_UNSAVED", {})
    monkeypatch.setattr(account_spend, "_RUNNING", set())
    account_links.link(LABEL, "country", "Mexico")
    account_links.link(LABEL, "country", "Guatemala")
    report, frame = history_frame(monkeypatch, tmp_path)
    frame["Account Alias"] = ALIAS

    def save(start=DAY, end=DAY, currency="USD", timezone="UTC", spend=14.0):
        account_spend._put(account_spend.SpendReading(
            account=LABEL, date_from=start, date_to=end, state="ok", source="api",
            spend=spend, currency=currency, currency_code=currency, timezone=timezone,
            day_boundary="calendar_day", finished_at="2026-10-18 14:20:00"))
    save()
    # A tempting different-period fallback must never become this period's money.
    save("2026-10-17", "2026-10-17", spend=999.0)

    blocked = []
    def no_new_read(*a, **kw):
        blocked.append((a, kw))
        pytest.fail("saved comparison must not fetch BI, open a profile or call its API handler")
    for owner, name in [(web, "get_cached_report"), (web, "api_source_vs_account"),
                        (bi_stats, "fetch_raw"), (bi_stats, "refresh_revision"),
                        (account_spend, "start_read")]:
        monkeypatch.setattr(owner, name, no_new_read)

    def model(start=DAY, end=DAY):
        scoped = web.report_for_period(report, frame, start, end)
        web._enrich_country_overview(scoped)
        return scoped, web._build_account_map_view(scoped, frame, start, end)

    def render(start=DAY, end=DAY, full=False):
        scoped, amap = model(start, end)
        if not full:
            return web.templates.env.get_template("_account_map.html").render(amap=amap, compare=None)
        return web.templates.env.get_template("report.html").render(
            request=MagicMock(), report=scoped, warnings=[], built_at="2026-10-24",
            mlx_labels={}, upload_errors={}, account_preflight={}, account_preflight_json={},
            ref_options={}, ref_options_json={}, account_alias_map_json={}, draft_status={},
            account_map=amap, sidebar_countries=web._sidebar_countries(scoped, amap),
            home_kpis=web._home_kpis(scoped),
            period={"date_from": start, "date_to": end, "span": ""})
    return {"render": render, "model": model, "save": save, "blocked": blocked}


def test_real_map_exposes_both_saved_figures_for_one_alias_and_period(saved_comparison_context):
    panel = SavedPanel(saved_comparison_context["render"]())
    assert panel.attrs is not None, "the comparison API has no discoverable user flow"
    assert "open" not in panel.attrs
    assert panel.entries == 1, "an account linked to two countries is compared once"
    text = panel.content()
    for expected in [LABEL, ALIAS, "BI", "USD 12.50", "USD 14.00", "USD 1.50",
                     DAY, "UTC", "calendar day", "2026-10-18 14:20:00"]:
        assert expected in text
    assert "999.00" not in text
    assert not panel.unsafe and "tr" not in panel.parents
    assert saved_comparison_context["blocked"] == []


@pytest.mark.parametrize("currency,timezone,reason", [
    ("", "UTC", "Currency is unknown"),
    ("EUR", "UTC", "Currencies differ"),
    ("USD", "America/New_York", "Day boundaries differ"),
])
def test_saved_ui_preserves_both_unlike_figures_without_a_false_gap(saved_comparison_context, currency, timezone, reason):
    saved_comparison_context["save"](currency=currency, timezone=timezone)
    panel = SavedPanel(saved_comparison_context["render"]())
    assert panel.attrs is not None
    text = panel.content()
    assert "USD 12.50" in text and "14.00" in text and reason in text
    assert "1.50" not in text
    assert saved_comparison_context["blocked"] == []


def test_saved_comparison_never_substitutes_another_period(saved_comparison_context):
    panel = SavedPanel(saved_comparison_context["render"]("2026-01-05", "2026-01-05"))
    assert panel.attrs is not None
    text = panel.content()
    assert "Stats workbook" in text and "USD 7.00" in text and "2026-01-05" in text
    assert "No saved account figure for these dates" in text
    assert "14.00" not in text and "999.00" not in text
    assert saved_comparison_context["blocked"] == []


def test_saved_day_label_is_human_readable_without_changing_metadata(saved_comparison_context):
    _report, amap = saved_comparison_context["model"]()
    row = amap["saved_source_check"]["rows"][0]
    assert row["source"]["day_boundary"] == "calendar_day"
    assert row["account_day_boundary"] == "calendar_day"
    assert row["comparison_state"] == "comparable" and row["gap"] == 1.5
    panel = SavedPanel(saved_comparison_context["render"]())
    assert panel.content().count("day boundary: calendar day") == 2
    assert "calendar_day" not in panel.content()
