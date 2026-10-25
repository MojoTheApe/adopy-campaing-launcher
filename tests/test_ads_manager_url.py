"""Ads Manager receives numeric IDs; account/period guards still own the result.

All account IDs are synthetic. Browser attachment and page content are mocked;
URL construction, identity parsing and the complete reader are real.
"""
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest

from src.analysis_agent import account_spend as sp
from tests.profile_lease_fakes import fake_profile


DIGITS = "1234567890"
DAY = "2026-09-08"


def query(url):
    return parse_qs(urlsplit(url).query, keep_blank_values=True)


@pytest.mark.parametrize("stored", [DIGITS, "act_" + DIGITS, "  act_" + DIGITS + "  ", " " + DIGITS + " "])
def test_url_contains_one_numeric_account_and_round_trips(stored):
    url = sp._url(stored, DAY)
    assert query(url)["act"] == [DIGITS]
    assert sp._act_from_url(SimpleNamespace(url=url), tries=1) == "act_" + DIGITS


@pytest.mark.parametrize("start,end,exclusive_end", [
    ("2026-09-01", "2026-09-06", "2026-09-07"),
    (DAY, DAY, "2026-09-09"),
    ("2026-08-01", "2026-08-31", "2026-09-01"),
])
def test_account_fix_preserves_the_inclusive_requested_period(start, end, exclusive_end):
    assert query(sp._url(DIGITS, start, end))["date"] == [f"{start}_{exclusive_end}"]


@pytest.mark.parametrize("stored", ["", "act_", "123&act=456", "123suffix", "act_act_123", "١٢٣", "12 3"])
def test_url_refuses_malformed_account_ids(stored):
    with pytest.raises(ValueError, match="ad_account_id"):
        sp._url(stored, DAY)


@pytest.fixture
def reader(monkeypatch):
    entry = SimpleNamespace(ad_account_id="act_" + DIGITS, profile_id=fake_profile("Synthetic account"))
    page = Mock()
    page.url = "about:blank"
    page.goto.side_effect = lambda url, **kwargs: setattr(page, "url", url)
    conn = SimpleNamespace(page=page, close=Mock())
    attach = Mock(return_value=conn)
    total = Mock(return_value=(7.0, "synthetic total"))
    monkeypatch.setattr(sp, "_entry_for", lambda _: entry)
    monkeypatch.setattr(sp, "_attach_only", attach)
    monkeypatch.setattr(sp, "_wait_for_grid", lambda _: True)
    monkeypatch.setattr(sp, "_read_status", lambda _: ("clear", ""))
    monkeypatch.setattr(sp, "_shown_period", lambda _: DAY)
    monkeypatch.setattr(sp, "_read_total", total)
    monkeypatch.setattr(sp, "_currency", lambda _: "USD")
    return SimpleNamespace(entry=entry, page=page, conn=conn, attach=attach, total=total)


def assert_reader_tab_closed(reader):
    reader.page.close.assert_called_once_with()
    reader.conn.close.assert_called_once_with(keep_browser_open=True)


@pytest.mark.parametrize("stored", [DIGITS, "act_" + DIGITS, "  act_" + DIGITS + "  "])
def test_real_reader_navigates_to_the_requested_account(reader, stored):
    reader.entry.ad_account_id = stored
    out = sp.read_via_rpa("Synthetic account", DAY)
    assert query(reader.page.goto.call_args.args[0])["act"] == [DIGITS]
    assert out.state == "ok" and out.spend == 7.0 and out.currency == "USD"
    reader.total.assert_called_once_with(reader.page)
    assert_reader_tab_closed(reader)


@pytest.mark.parametrize("opened", ["456", None])
def test_wrong_or_missing_account_names_the_mismatch_and_never_reads_total(reader, opened):
    redirected = sp.ADS_MANAGER + (f"?act={opened}" if opened else "")
    reader.page.goto.side_effect = lambda url, **kwargs: setattr(reader.page, "url", redirected)
    out = sp.read_via_rpa("Synthetic account", DAY)
    assert out.state == "failed" and out.spend is None and not out.status
    assert "act_" + DIGITS in out.error
    assert ("act_456" if opened else "no act in the URL") in out.error
    reader.total.assert_not_called()
    assert_reader_tab_closed(reader)


@pytest.mark.parametrize("stored", ["act_", "123&act=456", "123suffix", "act_act_123", "١٢٣", "12 3"])
def test_malformed_map_id_is_refused_before_browser_attachment(reader, stored):
    reader.entry.ad_account_id = stored
    out = sp.read_via_rpa("Synthetic account", DAY)
    assert out.state == "failed" and out.spend is None
    assert "ad_account_id" in out.error
    reader.attach.assert_not_called()
    reader.page.goto.assert_not_called()
    reader.total.assert_not_called()
