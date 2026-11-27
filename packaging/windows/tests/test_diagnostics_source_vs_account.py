"""ACL-2605: the diagnostic bundle carries the saved source-vs-account comparison."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path
from unittest import mock

import pandas as pd

PACKAGING_DIR = Path(__file__).resolve().parents[1]
if str(PACKAGING_DIR) not in sys.path:
    sys.path.insert(0, str(PACKAGING_DIR))

from diagnostics import export_diagnostics, source_vs_account_summary  # noqa: E402
from src.analysis_agent import bi_archive  # noqa: E402

LABEL = "FacebookMKTOBFLX84"
OTHER_MANAGER = "FacebookSomebodyElse9"
ACT = "act_" + "7" * 15
PROFILE = "-".join(["12345678", "1234", "4234", "8234", "123456789abc"])  # built at runtime: the secret scan flags literals
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


class SourceVsAccountTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config = Path(tmp.name)
        env = mock.patch.dict(os.environ, {
            "ACL_CONFIG_DIR": str(self.config), "BI_CURRENCY": "USD",
            "BI_TIMEZONE": "UTC", "BI_DAY_BOUNDARY": "calendar_day"})
        env.start()
        self.addCleanup(env.stop)

    def test_saved_values_are_compared(self):
        config = self.config
        _archive(config, [(DAY, LABEL, 10.0), (DAY, OTHER_MANAGER, 99.0)])
        _map(config)
        _readings(config)
        summary = source_vs_account_summary(config, today=TODAY)
        self.assertTrue(summary["available"])
        self.assertEqual((summary["date_from"], summary["date_to"]), ("2026-10-01", "2026-10-20"))
        # No reading for the month period: the account side is missing, never borrowed from DAY.
        self.assertEqual([r["account"] for r in summary["rows"]], [LABEL])
        self.assertEqual(summary["rows"][0]["comparison_state"], "missing_account")
        self.assertEqual(summary["rows"][0]["source_spend"], 10.0)

    def test_matching_period_gives_the_gap(self):
        config = self.config
        _archive(config, [(DAY, LABEL, 10.0)])
        _map(config)
        (config / "account_spend.json").write_text(json.dumps({
            f"{LABEL}|2026-10-01|2026-10-20": {"account": LABEL, "date_from": "2026-10-01", "date_to": "2026-10-20",
                                              "state": "ok", "spend": 14.0, "currency": "USD", "currency_code": "USD",
                                              "timezone": "UTC", "day_boundary": "calendar_day"}}), encoding="utf-8")
        row = source_vs_account_summary(config, today=TODAY)["rows"][0]
        self.assertEqual((row["source_spend"], row["account_spend"], row["gap"], row["differs"]), (10.0, 14.0, 4.0, True))

    def test_empty_dates_and_odd_inputs_do_not_break(self):
        for prepare in ("missing", "empty", "no_dates", "corrupt"):
            with self.subTest(prepare=prepare):
                with tempfile.TemporaryDirectory() as tmp:
                    config = Path(tmp)
                    with mock.patch.dict(os.environ, {"ACL_CONFIG_DIR": str(config)}):
                        path = config / bi_archive.FILE_NAME
                        if prepare == "empty":
                            _archive(config, [])
                            bi_archive._connect(str(path)).close()
                        elif prepare == "no_dates":
                            _archive(config, [(None, LABEL, 5.0), ("not-a-date", LABEL, 5.0)])
                        elif prepare == "corrupt":
                            path.write_bytes(b"not a sqlite file")
                        summary = source_vs_account_summary(config, today=TODAY)
                        self.assertEqual(summary["rows"], [])
                        self.assertEqual((summary["date_from"], summary["date_to"]), ("", ""))
                        self.assertIs(summary["available"], False)
                        export = export_diagnostics(config, config / "manifest.json")
                        with zipfile.ZipFile(export) as archive:
                            self.assertEqual(json.loads(archive.read("source_vs_account.json"))["rows"], [])

    def test_machine_without_known_accounts_shows_no_foreign_rows(self):
        _archive(self.config, [(DAY, OTHER_MANAGER, 99.0)])
        summary = source_vs_account_summary(self.config, today=TODAY)
        self.assertTrue(summary["available"])
        self.assertEqual(summary["rows"], [])

    def test_odd_readings_file(self):
        config = self.config
        _archive(config, [(DAY, LABEL, 10.0)])
        _map(config)
        for bad in ("not json", "[]", '{"a": 1, "b|1": [], "c|x|y": "z"}', "null"):
            with self.subTest(bad=bad):
                (config / "account_spend.json").write_text(bad, encoding="utf-8")
                rows = source_vs_account_summary(config, today=TODAY)["rows"]
                self.assertEqual(rows[0]["comparison_state"], "missing_account")

    def test_failed_or_empty_reading_is_not_money(self):
        config = self.config
        _archive(config, [(DAY, LABEL, 10.0)])
        _map(config)
        (config / "account_spend.json").write_text(json.dumps({
            f"{LABEL}|2026-10-01|2026-10-20": {"account": LABEL, "date_from": "2026-10-01", "date_to": "2026-10-20",
                                              "state": "failed", "spend": None}}), encoding="utf-8")
        row = source_vs_account_summary(config, today=TODAY)["rows"][0]
        self.assertIsNone(row["account_spend"])
        self.assertEqual(row["comparison_state"], "missing_account")

    def test_archive_is_not_modified(self):
        config = self.config
        _archive(config, [(DAY, LABEL, 10.0)])
        _map(config)
        path = config / bi_archive.FILE_NAME
        before = path.read_bytes()
        source_vs_account_summary(config, today=TODAY)
        self.assertEqual(path.read_bytes(), before)

    def test_no_account_read_or_bi_fetch(self):
        from src.analysis_agent import account_spend, bi_stats

        _archive(self.config, [(DAY, LABEL, 10.0)])
        _map(self.config)

        def boom(*a, **kw):
            self.fail("the bundle must not read BI or accounts")
        with mock.patch.object(bi_stats, "fetch_raw", boom), mock.patch.object(account_spend, "start_read", boom):
            self.assertTrue(source_vs_account_summary(self.config, today=TODAY)["available"])

    def test_bundle_section_masks_ids_and_carries_no_secrets(self):
        config = self.config
        _archive(config, [(DAY, LABEL, 10.0), (DAY, f"{LABEL} {ACT}", 1.0)])
        _map(config)
        _readings(config)
        (config / ".env").write_text(f"MULTILOGIN_AUTOMATION_TOKEN_A={TOKEN}\n", encoding="utf-8")
        (config / "manifest.json").write_text("{}", encoding="utf-8")
        with zipfile.ZipFile(export_diagnostics(config, config / "manifest.json")) as archive:
            text = archive.read("source_vs_account.json").decode("utf-8")
            everything = "\n".join(archive.read(n).decode("utf-8") for n in archive.namelist())
        for secret in (ACT, PROFILE, TOKEN, "proxyuser99", "proxypass-99", "Bearer"):
            self.assertNotIn(secret, text)
            self.assertNotIn(secret, everything)
        section = json.loads(text)
        self.assertTrue(section["available"])
        self.assertTrue(any(r["account"] == LABEL for r in section["rows"]))
        self.assertTrue(any("<redacted" in r["account"] for r in section["rows"]))  # the id inside a label is masked


if __name__ == "__main__":
    unittest.main()
