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
ENV_KEYS = list(cs.ALLOWED_KEYS) + ["MULTILOGIN_AUTOMATION_TOKEN_A", "MULTILOGIN_AUTOMATION_TOKEN_B", "ACL_CONFIG_DIR", "ACL_CONNECTION_SHEET_ID", "FB_STATS_SOURCE"]


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

    def clear(self, **kw):
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
        # ACL-2318: without Windows protected storage the values stay clear text (the
        # documented fallback); the vault path has its own test below.
        from config import token_vault

        patcher = mock.patch.object(token_vault, "available", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)
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

    def test_sign_in_stores_library_token_and_bi_password_in_the_vault(self):
        from config import token_vault

        def fake_encode(plain):
            return "dpapi:v1:" + plain[::-1].encode().hex()

        with mock.patch.object(token_vault, "available", return_value=True),                 mock.patch.object(token_vault, "encode_env_value", side_effect=fake_encode):
            cs.sign_in(service=FakeService(ROWS))
        text = self.env_text()
        self.assertNotIn(FAKE_TOKEN, text)
        self.assertNotIn(FAKE_PASSWORD, text)
        self.assertEqual(text.count("dpapi:v1:"), 2)
        self.assertIn("bi.example.invalid", text)  # the host is not a secret
        # the running process keeps the plain values for the readers
        self.assertEqual(os.environ["ACL_LIBRARY_TOKEN"], FAKE_TOKEN)
        self.assertEqual(os.environ["BI_MYSQL_PASSWORD"], FAKE_PASSWORD)

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


class SecurityTests(Base):
    def test_line_separators_and_non_ascii_are_rejected_nothing_written(self):
        before = self.env_text()
        for bad in ("p\u2028HTTPS_PROXY=http://evil:8080", "p\x85X=1", "p\u2029X=1", "p\x7fX", "p\x0bX", "p\u0416"):
            for key in ("BI_MYSQL_USER", "BI_MYSQL_PASSWORD", "BI_MYSQL_HOST"):
                rows = [["ACL_LIBRARY_TOKEN", FAKE_TOKEN], ["BI_MYSQL_HOST", "bi.example.invalid"],
                        ["BI_MYSQL_USER", "fake_user"], ["BI_MYSQL_PASSWORD", FAKE_PASSWORD]]
                for row in rows:
                    if row[0] == key:
                        row[1] = bad
                with self.assertRaises(cs.SettingsError):
                    cs.sign_in(service=FakeService(rows))
        self.assertEqual(self.env_text(), before)

    def test_reviewer_payload_does_not_create_keys_after_a_rewrite(self):
        from config import paths
        from dotenv import dotenv_values

        with self.assertRaises(cs.SettingsError):
            cs.apply({"ACL_LIBRARY_TOKEN": FAKE_TOKEN, "BI_MYSQL_HOST": "h.example.invalid", "BI_MYSQL_USER": "u",
                      "BI_MYSQL_PASSWORD": "p\u2028HTTPS_PROXY=http://evil:8080"})
        cs.apply(dict((r[0], r[1]) for r in ROWS))
        paths.update_machine_env({"MY_CM_NAME": "Someone"})
        values = dotenv_values(Path(self.tmp.name) / ".env")
        self.assertNotIn("HTTPS_PROXY", values)
        self.assertEqual(values["BI_MYSQL_PASSWORD"], FAKE_PASSWORD)

    def test_env_writer_refuses_line_separators_but_keeps_cyrillic(self):
        from config import paths
        from dotenv import dotenv_values

        for bad in ("a\u2028b", "a\x85b", "a\u2029b", "a\nb"):
            with self.assertRaises(ValueError):
                paths.update_machine_env({"MY_CM_NAME": bad})
        paths.update_machine_env({"MY_CM_NAME": "\u0416\u0435\u043d\u044f"})
        self.assertEqual(dotenv_values(Path(self.tmp.name) / ".env")["MY_CM_NAME"], "\u0416\u0435\u043d\u044f")

    def test_library_repo_rejects_dot_dot(self):
        for bad in ("../..", "a/../b", ".x/y", "owner/.."):
            with self.assertRaises(cs.SettingsError):
                cs.validate({"ACL_LIBRARY_REPO": bad, "ACL_LIBRARY_TOKEN": FAKE_TOKEN})
        self.assertEqual(cs.validate({"ACL_LIBRARY_REPO": "Org/acl-lib"})["ACL_LIBRARY_REPO"], "Org/acl-lib")


class PublishSafetyTests(Base):
    def setUp(self):
        super().setUp()
        os.environ["ACL_LIBRARY_TOKEN"] = FAKE_TOKEN

    def test_refuses_the_telemetry_or_accounts_sheet(self):
        from config import paths
        from src.analysis_agent import gsheet_accounts as ga

        for other in (paths.DEFAULT_TELEMETRY_SHEET_ID, ga.DEFAULT_ACCOUNTS_SHEET_ID):
            os.environ["ACL_CONNECTION_SHEET_ID"] = other
            svc = FakeService([])
            with self.assertRaises(cs.SettingsError):
                cs.publish(svc)
            self.assertIsNone(svc.updated)

    def test_requires_existing_tab_and_clears_old_rows(self):
        with self.assertRaises(cs.SettingsError) as ctx:
            cs.publish(FakeService(error=FakeHttpError(400)))
        self.assertIn("no connection tab", str(ctx.exception))
        calls = []

        class Svc(FakeService):
            def clear(self, **kw):
                calls.append("clear")
                return self

            def update(self, **kw):
                calls.append("update")
                return super().update(**kw)

        cs.publish(Svc([]))
        self.assertEqual(calls, ["clear", "update"])


class StatusTests(Base):
    def test_multilogin_line_and_source_line(self):
        self.assertFalse(cs.check_multilogin()["ok"])
        os.environ["MULTILOGIN_AUTOMATION_TOKEN_A"] = "fake"
        try:
            self.assertTrue(cs.check_multilogin()["ok"])
        finally:
            os.environ.pop("MULTILOGIN_AUTOMATION_TOKEN_A")
        st = cs.status()
        self.assertEqual(st["source_line"], "Source: Stats workbook (BI not connected)")
        self.assertIn("Multilogin", [c["name"] for c in cs.check_all()])
        self.assertNotIn("Multilogin", [c["name"] for c in cs.check_connections()])

    def test_sign_in_timeout_reaches_the_oauth_flow(self):
        from src.analysis_agent import gsheet_accounts as ga

        client = Path(self.tmp.name) / "oauth_client.json"
        client.write_text('{"installed": {"client_id": "fake-id.apps.example"}}', encoding="utf-8")
        seen = {}

        def fake(path, token, timeout_seconds=None):
            seen["t"] = timeout_seconds
            raise RuntimeError("timed out")

        with mock.patch.object(ga, "_build_sheets_service_oauth", fake),                 mock.patch.object(cs.paths, "get_google_oauth_client_path", return_value=str(client)):
            with self.assertRaises(cs.SettingsError) as ctx:
                cs.sign_in()
        self.assertEqual(seen["t"], cs.SIGN_IN_TIMEOUT_SECONDS)
        self.assertEqual(str(ctx.exception), "Sign-in was not finished. Try again.")

    def test_invalid_client_gives_a_clear_message_without_starting_sign_in(self):
        from src.analysis_agent import gsheet_accounts as ga

        for content in ("", "not json", "{}"):
            client = Path(self.tmp.name) / "oauth_client.json"
            client.write_text(content, encoding="utf-8")
            fake = mock.Mock()
            with mock.patch.object(ga, "_build_sheets_service_oauth", fake),                     mock.patch.object(cs.paths, "get_google_oauth_client_path", return_value=str(client)):
                with self.assertRaises(cs.SettingsError) as ctx:
                    cs.sign_in()
            self.assertIn("Google sign-in is not set up on this computer", str(ctx.exception))
            fake.assert_not_called()


class BundleTests(unittest.TestCase):
    def test_oauth_client_is_never_in_git_and_only_optionally_bundled(self):
        import subprocess

        tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout.splitlines()
        self.assertFalse([t for t in tracked if t.endswith("oauth_client.json")])
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("oauth_client.json", ignore)
        spec = (PACKAGING / "acl_windows.spec").read_text(encoding="utf-8")
        self.assertIn("ACL_OAUTH_CLIENT_FILE", spec)
        self.assertIn("OAUTH_CLIENT_FILE.is_file()", spec)
        self.assertIn("WARNING", spec)
        self.assertIn('"connection_file.json"', spec)


if __name__ == "__main__":
    unittest.main()
