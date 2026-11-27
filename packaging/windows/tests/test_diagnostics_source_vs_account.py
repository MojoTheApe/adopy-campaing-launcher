"""ACL-2605: the diagnostic bundle carries the saved source-vs-account comparison."""
from __future__ import annotations

import json
import sys
import zipfile
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

PACKAGING_DIR = Path(__file__).resolve().parents[1]
if str(PACKAGING_DIR) not in sys.path:
    sys.path.insert(0, str(PACKAGING_DIR))

from diagnostics import export_diagnostics, source_vs_account_summary  # noqa: E402
from src.analysis_agent import bi_archive  # noqa: E402

LABEL = "FacebookMKTOBFLX84"
OTHER_MANAGER = "FacebookSomebodyElse9"
ACT = "act_" + "7" * 15
PROFILE = "12345678-1234-4234-8234-123456789abc"
TOKEN = "EAAB" + "SyntheticTokenValue1234567890"
DAY = "2026-10-18"
TODAY = date(2026, 10, 20)


def _window(rows):
    frame = pd.DataFrame(rows, columns=["Date", "Account Alias", "$ Spend"])
    for column in ("Country", "Adset Name", "Ad Title", "Ad Asset", "Variant"):
        frame[column] = ""
    frame["CM_ID"] = "1"
    frame["# Leads"] = 0
    frame["# FTDs"] = 0
    frame["$ Net"] = 0.0
    return frame


def _archive(config, rows):
    bi_archive.ingest_bi_window(_window(rows), str(config / bi_archive.FILE_NAME))


def _readings(config, **overrides):
    reading = {"account": LABEL, "date_from": DAY, "date_to": DAY, "state": "ok", "source": "api",
               "spend": 14.0, "currency": "USD", "currency_code": "USD", "timezone": "UTC",
               "day_boundary": "calendar_day", "error": f"failed with Bearer {TOKEN}",
               "objects": [{"id": ACT}]}
    reading.update(overrides)
    (config / "account_spend.json").write_text(json.dumps({f"{LABEL}|{DAY}|{DAY}": reading}), encoding="utf-8")


def _map(config):
    (config / "ad_account_map.csv").write_text(
        "account_name,ad_account_id,profile_id,proxy_user,proxy_pass\n"
        f"{LABEL},{ACT},{PROFILE},proxyuser99,proxypass-99\n", encoding="utf-8")


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("ACL_CONFIG_DIR", str(tmp_path))
    for name in ("BI_CURRENCY", "BI_TIMEZONE", "BI_DAY_BOUNDARY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BI_CURRENCY", "USD")
    monkeypatch.setenv("BI_TIMEZONE", "UTC")
    monkeypatch.setenv("BI_DAY_BOUNDARY", "calendar_day")
    return tmp_path


def test_saved_values_are_compared(config):
    _archive(config, [(DAY, LABEL, 10.0), (DAY, OTHER_MANAGER, 99.0)])
    _map(config)
    _readings(config)
    # Own period: the dashboard default is the saved month, read exactly for the day.
    summary = source_vs_account_summary(config, today=TODAY)
    assert summary["available"] and summary["date_from"] == "2026-10-01" and summary["date_to"] == "2026-10-20"
    # No reading for the month period: the account side is missing, never borrowed from DAY.
    assert [r["account"] for r in summary["rows"]] == [LABEL]
    assert summary["rows"][0]["comparison_state"] == "missing_account"
    assert summary["rows"][0]["source_spend"] == 10.0


def test_matching_period_gives_the_gap(config):
    _archive(config, [(DAY, LABEL, 10.0)])
    _map(config)
    (config / "account_spend.json").write_text(json.dumps({
        f"{LABEL}|2026-10-01|2026-10-20": {"account": LABEL, "date_from": "2026-10-01", "date_to": "2026-10-20",
                                          "state": "ok", "spend": 14.0, "currency": "USD", "currency_code": "USD",
                                          "timezone": "UTC", "day_boundary": "calendar_day"}}), encoding="utf-8")
    row = source_vs_account_summary(config, today=TODAY)["rows"][0]
    assert (row["source_spend"], row["account_spend"], row["gap"], row["differs"]) == (10.0, 14.0, 4.0, True)


@pytest.mark.parametrize("prepare", ["missing", "empty", "no_dates", "corrupt"])
def test_empty_dates_and_odd_inputs_do_not_break(config, prepare):
    path = config / bi_archive.FILE_NAME
    if prepare == "empty":
        _archive(config, [])
        bi_archive._connect(str(path)).close()
    elif prepare == "no_dates":
        _archive(config, [(None, LABEL, 5.0), ("not-a-date", LABEL, 5.0)])
    elif prepare == "corrupt":
        path.write_bytes(b"not a sqlite file")
    summary = source_vs_account_summary(config, today=TODAY)
    assert summary["rows"] == [] and summary["date_from"] == "" and summary["date_to"] == ""
    assert summary["available"] is False
    export = export_diagnostics(config, config / "manifest.json")
    with zipfile.ZipFile(export) as archive:
        assert json.loads(archive.read("source_vs_account.json"))["rows"] == []


def test_machine_without_known_accounts_shows_no_foreign_rows(config):
    _archive(config, [(DAY, OTHER_MANAGER, 99.0)])
    summary = source_vs_account_summary(config, today=TODAY)
    assert summary["available"] and summary["rows"] == []


@pytest.mark.parametrize("bad", ["not json", "[]", '{"a": 1, "b|1": [], "c|x|y": "z"}', "null"])
def test_odd_readings_file(config, bad):
    _archive(config, [(DAY, LABEL, 10.0)])
    _map(config)
    (config / "account_spend.json").write_text(bad, encoding="utf-8")
    rows = source_vs_account_summary(config, today=TODAY)["rows"]
    assert rows[0]["comparison_state"] == "missing_account"


def test_failed_or_empty_reading_is_not_money(config):
    _archive(config, [(DAY, LABEL, 10.0)])
    _map(config)
    (config / "account_spend.json").write_text(json.dumps({
        f"{LABEL}|2026-10-01|2026-10-20": {"account": LABEL, "date_from": "2026-10-01", "date_to": "2026-10-20",
                                          "state": "failed", "spend": None}}), encoding="utf-8")
    row = source_vs_account_summary(config, today=TODAY)["rows"][0]
    assert row["account_spend"] is None and row["comparison_state"] == "missing_account"


def test_archive_is_not_modified(config):
    _archive(config, [(DAY, LABEL, 10.0)])
    _map(config)
    path = config / bi_archive.FILE_NAME
    before = path.read_bytes()
    source_vs_account_summary(config, today=TODAY)
    assert path.read_bytes() == before


def test_no_account_read_or_bi_fetch(config, monkeypatch):
    from src.analysis_agent import account_spend, bi_stats

    _archive(config, [(DAY, LABEL, 10.0)])
    _map(config)

    def boom(*a, **kw):
        pytest.fail("the bundle must not read BI or accounts")
    monkeypatch.setattr(bi_stats, "fetch_raw", boom)
    monkeypatch.setattr(account_spend, "start_read", boom)
    assert source_vs_account_summary(config, today=TODAY)["available"]


def test_bundle_section_masks_ids_and_carries_no_secrets(config):
    _archive(config, [(DAY, LABEL, 10.0), (DAY, f"{LABEL} {ACT}", 1.0)])
    _map(config)
    _readings(config)
    (config / ".env").write_text(f"MULTILOGIN_AUTOMATION_TOKEN_A={TOKEN}\n", encoding="utf-8")
    (config / "manifest.json").write_text("{}", encoding="utf-8")
    with zipfile.ZipFile(export_diagnostics(config, config / "manifest.json")) as archive:
        text = archive.read("source_vs_account.json").decode("utf-8")
        everything = "\n".join(archive.read(n).decode("utf-8") for n in archive.namelist())
    for secret in (ACT, PROFILE, TOKEN, "proxyuser99", "proxypass-99", "Bearer"):
        assert secret not in text and secret not in everything
    section = json.loads(text)
    assert section["available"] and any(r["account"] == LABEL for r in section["rows"])
    assert any("<redacted" in r["account"] for r in section["rows"])  # the id inside a label is masked
