"""ACL-2306: team connection settings from one private Google Sheet."""
from __future__ import annotations

import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / "packaging" / "windows"
for extra in (str(ROOT), str(PACKAGING)):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from src.analysis_agent import connection_settings as cs  # noqa: E402

# Obviously fake values.
FAKE_TOKEN = "ghp_FAKEFAKEFAKEFAKEFAKEFAKE0000"
FAKE_PASSWORD = "fake-bi-password-xyz"
ROWS = [
    ["ACL_LIBRARY_TOKEN", FAKE_TOKEN],
    ["BI_MYSQL_HOST", "bi.example.invalid"],
    ["BI_MYSQL_USER", "fake_user"],
    ["BI_MYSQL_PASSWORD", FAKE_PASSWORD],
    ["SOMETHING_ELSE", "ignored"],
]
ENV_KEYS = list(cs.ALLOWED_KEYS) + ["ACL_CONFIG_DIR", "ACL_CONNECTION_SHEET_ID", "FB_STATS_SOURCE"]


class FakeHttpError(Exception):
    def __init__(self, status):
        super().__init__("secret-looking google text")
        self.resp = mock.Mock(status=status)


class FakeService:
    def __init__(self, rows=None, error=None):
        self.rows, self.error, self.updated = rows, error, None

    def spreadsheets(self):
        return self

    def values(self):
        return self

    def get(self, **kw):
        self.kw = kw
        return self

    def update(self, **kw):
        self.updated = kw
        return self

    def execute(self):
        if self.error:
            raise self.error
        return {"values": self.rows}


class Base(unittest.TestCase):
    def setUp(self):
        self.saved = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["ACL_CONFIG_DIR"] = self.tmp.name
        os.environ["ACL_CONNECTION_SHEET_ID"] = "sheet-id-not-secret"
        (Path(self.tmp.name) / ".env").write_text("MY_CM_NAME=Test\n", encoding="utf-8")

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def env_text(self):
        return (Path(self.tmp.name) / ".env").read_text(encoding="utf-8")


class ReadAndApplyTests(Base):
    def test_sign_in_writes_values_to_machine_env_only(self):
        result = cs.sign_in(service=FakeService(ROWS))
        self.assertEqual(result["updated"], 4)
        text = self.env_text()
        self.assertIn(FAKE_TOKEN, text)
        self.assertIn("BI_MYSQL_HOST", text)
        self.assertNotIn("SOMETHING_ELSE", text)
        self.assertEqual(os.environ["BI_MYSQL_USER"], "fake_user")
        # nothing was written inside the checkout
        self.assertFalse((ROOT / ".env").exists() and FAKE_TOKEN in (ROOT / ".env").read_text(errors="ignore"))

    def test_no_access_is_clear_and_writes_nothing(self):
        before = self.env_text()
        with self.assertRaises(cs.SettingsError) as ctx:
            cs.sign_in(service=FakeService(error=FakeHttpError(403)))
        self.assertIn("share it with your Adopy Google account", str(ctx.exception))
        self.assertNotIn("secret-looking", str(ctx.exception))
        self.assertEqual(self.env_text(), before)

    def test_malformed_file_writes_nothing(self):
        before = self.env_text()
        for rows in ([["BI_MYSQL_HOST", "h.example.invalid"]],
                     [["ACL_LIBRARY_TOKEN", "short"]],
                     [["BI_MYSQL_PORT", "abc"], ["ACL_LIBRARY_TOKEN", FAKE_TOKEN]],
                     []):
            with self.assertRaises(cs.SettingsError):
                cs.sign_in(service=FakeService(rows))
        self.assertEqual(self.env_text(), before)
        self.assertNotIn("ACL_LIBRARY_TOKEN", os.environ)

    def test_not_configured_and_not_signed_in(self):
        os.environ.pop("ACL_CONNECTION_SHEET_ID")
        with mock.patch.object(cs, "_config_file", return_value={}):
            with self.assertRaises(cs.SettingsError) as ctx:
                cs.read_remote(FakeService(ROWS))
        self.assertIn("not configured", str(ctx.exception))
        os.environ["ACL_CONNECTION_SHEET_ID"] = "sheet-id-not-secret"
        with mock.patch.object(cs, "_service", return_value=None):
            with self.assertRaises(cs.SettingsError) as ctx:
                cs.read_remote()
        self.assertIn("Not signed in", str(ctx.exception))

    def test_rotated_value_is_picked_up_on_next_start(self):
        cs.sign_in(service=FakeService(ROWS))
        rotated = [r[:] for r in ROWS]
        rotated[3][1] = "rotated-fake-password"
        with mock.patch.object(cs, "has_google_token", return_value=True), \
                mock.patch.object(cs, "_service", return_value=FakeService(rotated)):
            out = cs.refresh_on_start()
        self.assertEqual(out["updated"], 1)
        self.assertEqual(os.environ["BI_MYSQL_PASSWORD"], "rotated-fake-password")
        self.assertIn("rotated-fake-password", self.env_text())

    def test_start_refresh_failure_keeps_values_and_is_silent(self):
        cs.sign_in(service=FakeService(ROWS))
        with mock.patch.object(cs, "has_google_token", return_value=True), \
                mock.patch.object(cs, "_service", return_value=FakeService(error=FakeHttpError(403))):
            self.assertIsNone(cs.refresh_on_start())
        self.assertEqual(os.environ["BI_MYSQL_PASSWORD"], FAKE_PASSWORD)

    def test_start_refresh_without_sign_in_does_nothing(self):
        with mock.patch.object(cs, "has_google_token", return_value=False):
            self.assertIsNone(cs.refresh_on_start())

    def test_no_secret_in_logs_or_status(self):
        with self.assertLogs(level="DEBUG") as logs:
            logging.getLogger("probe").info("probe")
            cs.sign_in(service=FakeService(ROWS))
            with mock.patch.object(cs, "has_google_token", return_value=True), \
                    mock.patch.object(cs, "_service", return_value=FakeService(error=FakeHttpError(500))):
                cs.refresh_on_start()
        joined = "\n".join(logs.output)
        for secret in (FAKE_TOKEN, FAKE_PASSWORD, "bi.example.invalid"):
            self.assertNotIn(secret, joined)
        shown = str(cs.status())
        for secret in (FAKE_TOKEN, FAKE_PASSWORD, "bi.example.invalid"):
            self.assertNotIn(secret, shown)
        self.assertTrue(cs.status()["library"]["configured"])


class ChecksTests(Base):
    def test_library_not_connected_and_connected(self):
        self.assertFalse(cs.check_library()["configured"])
        os.environ["ACL_LIBRARY_TOKEN"] = FAKE_TOKEN
        from src.analysis_agent import team_library as tl

        class Backend:
            def __init__(self, *a, **k):
                pass

            def get(self, path):
                import json
                key = "targets" if path.startswith("targets") else "texts"
                return tl.Remote(json.dumps({"version": 1, key: {}}), "sha")

        with mock.patch.object(tl, "GitHubBackend", Backend):
            line = cs.check_library()
        self.assertTrue(line["ok"])
        self.assertEqual(line["line"], "Team library: connected (0 targets, 0 texts)")

    def test_library_failure_hides_token(self):
        os.environ["ACL_LIBRARY_TOKEN"] = FAKE_TOKEN
        from src.analysis_agent import team_library as tl

        class Backend:
            def __init__(self, *a, **k):
                pass

            def get(self, path):
                raise tl.LibraryError("Could not read the team library: bad " + FAKE_TOKEN)

        with mock.patch.object(tl, "GitHubBackend", Backend):
            line = cs.check_library()
        self.assertFalse(line["ok"])
        self.assertNotIn(FAKE_TOKEN, line["line"])

    def test_bi_not_connected_ok_and_failure(self):
        self.assertFalse(cs.check_bi()["configured"])
        for k, v in (("BI_MYSQL_HOST", "bi.example.invalid"), ("BI_MYSQL_USER", "fake_user"),
                     ("BI_MYSQL_PASSWORD", FAKE_PASSWORD)):
            os.environ[k] = v
        from src.analysis_agent import bi_stats

        class Cur:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql, *a):
                pass

            def fetchone(self):
                return ["2026-10-28"]

        class Conn:
            def cursor(self):
                return Cur()

            def close(self):
                pass

        with mock.patch.object(bi_stats, "_connect", return_value=Conn()):
            ok = cs.check_bi()
        self.assertEqual(ok["line"], "BI: connected (last data date 2026-10-28)")
        with mock.patch.object(bi_stats, "_connect", side_effect=OSError(2003, "Can't connect to bi.example.invalid")):
            bad = cs.check_bi()
        self.assertFalse(bad["ok"])
        self.assertNotIn("bi.example.invalid", bad["line"])
        self.assertNotIn(FAKE_PASSWORD, bad["line"])

    def test_accounts_from_bi_by_manager_name(self):
        from src.analysis_agent import bi_stats

        seen = {}

        class Cur:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql, params=None):
                seen["params"] = params

            def fetchall(self):
                return [["Beta"], ["alpha"], [None]]

        class Conn:
            def cursor(self):
                return Cur()

            def close(self):
                pass

        os.environ.update(BI_MYSQL_HOST="h.example.invalid", BI_MYSQL_USER="u", BI_MYSQL_PASSWORD="p")
        with mock.patch.object(bi_stats, "_connect", return_value=Conn()), \
                mock.patch.object(bi_stats, "load_cm_map", return_value={"1107": "Yotam B", "9": "Other"}):
            self.assertEqual(cs.bi_accounts_for_manager("yotam b"), ["alpha", "Beta"])
            self.assertEqual(seen["params"], ["1107"])
            self.assertEqual(cs.bi_accounts_for_manager("Nobody"), [])


class LeadPublishTests(Base):
    def test_publish_reads_effective_env_and_says_what_is_missing(self):
        os.environ["ACL_LIBRARY_TOKEN"] = FAKE_TOKEN  # lead machine: library but no BI
        report = cs.shareable_report()
        self.assertTrue(report["library"])
        self.assertFalse(report["bi"])
        self.assertIn("This computer has no BI connection to share.", report["notes"])
        svc = FakeService([])
        result = cs.publish(svc)
        self.assertTrue(result["library"] and not result["bi"])
        sent = dict(map(tuple, svc.updated["body"]["values"]))
        self.assertEqual(sent, {"ACL_LIBRARY_TOKEN": FAKE_TOKEN})
        self.assertNotIn(FAKE_TOKEN, str(result))

    def test_nothing_to_share_is_an_error(self):
        with self.assertRaises(cs.SettingsError) as ctx:
            cs.publish(FakeService([]))
        self.assertIn("no BI connection", str(ctx.exception))
        self.assertIn("no team library token", str(ctx.exception))


class WizardTests(Base):
    def test_wizard_lines_and_bi_makes_workbook_optional(self):
        import setup_wizard as sw

        lines = sw.connect_with_google(
            self.tmp.name,
            sign_in=lambda: cs.sign_in(service=FakeService(ROWS)),
            checks=lambda: [
                {"name": "Team library", "ok": True, "line": "Team library: connected (3 targets, 5 texts)"},
                {"name": "BI", "ok": True, "line": "BI: connected (last data date 2026-10-28)"}],
        )
        self.assertEqual([l.detail for l in lines], [
            "Team library: connected (3 targets, 5 texts)", "BI: connected (last data date 2026-10-28)"])
        self.assertTrue(all(l.ok for l in lines))
        self.assertIn(FAKE_TOKEN, self.env_text())
        self.assertEqual(sw.validate_stats_workbook("", optional=True), "")
        with self.assertRaises(sw.WizardValidationError):
            sw.validate_stats_workbook("")  # required when BI is not connected

    def test_wizard_no_access_shows_reason(self):
        import setup_wizard as sw

        before = self.env_text()
        lines = sw.connect_with_google(
            self.tmp.name, sign_in=lambda: cs.sign_in(service=FakeService(error=FakeHttpError(403))))
        self.assertFalse(lines[0].ok)
        self.assertIn("share it with your Adopy Google account", lines[0].detail)
        self.assertEqual(self.env_text(), before)

    def test_setup_without_workbook_lists_accounts_from_bi(self):
        import setup_wizard as sw
        from machine_config import apply_machine_configuration

        config = sw.build_bi_setup(campaign_manager_name="Yotam B", accounts_for=lambda n: ["Beta", "alpha"])
        self.assertEqual([a.account_name for a in config.accounts], ["alpha", "Beta"])
        with self.assertRaises(sw.WizardValidationError):
            sw.build_bi_setup(campaign_manager_name="Yotam B", accounts_for=lambda n: [])
        out = Path(self.tmp.name) / "setup"
        out.mkdir()
        apply_machine_configuration(config, out, stats_workbook="", creatives_directory=str(out))
        self.assertNotIn("FB_STATS_EXCEL", (out / ".env").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
