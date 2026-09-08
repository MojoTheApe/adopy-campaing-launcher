from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PACKAGING_DIR = Path(__file__).resolve().parents[1]
if str(PACKAGING_DIR) not in sys.path:
    sys.path.insert(0, str(PACKAGING_DIR))

from setup_wizard import (
    STATS_COLUMNS,
    WizardValidationError,
    build_manual_setup,
    sanitized_setup_summary,
    setup_is_complete,
    validate_campaign_manager_name,
    validate_creatives_directory,
    validate_stats_workbook,
    test_multilogin_access,
    write_ready_state,
)
from machine_config import AccountAssignment, MachineConfiguration


class SetupWizardTests(unittest.TestCase):
    def test_ready_state_requires_configuration_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp)
            write_ready_state(config, "0.1.0-test")
            self.assertFalse(setup_is_complete(config))
            (config / ".env").write_text("MY_CM_NAME=Test\n", encoding="utf-8")
            (config / "ad_account_map.csv").write_text("account_name,ad_account_id\n", encoding="utf-8")
            self.assertTrue(setup_is_complete(config))
            state = json.loads((config / "setup-state.json").read_text(encoding="utf-8"))
            self.assertFalse(state["automatic_updates"])

    def test_creatives_directory_uses_real_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(validate_creatives_directory(tmp), str(Path(tmp).resolve()))
            with self.assertRaises(WizardValidationError):
                validate_creatives_directory(str(Path(tmp) / "missing"))

    def test_missing_stats_workbook_is_actionable(self):
        with self.assertRaisesRegex(WizardValidationError, "does not exist"):
            validate_stats_workbook("missing.xlsx")

    def test_multilogin_check_requires_a_token(self):
        with self.assertRaisesRegex(WizardValidationError, "access code"):
            test_multilogin_access({"A": "", "B": ""})

    @mock.patch("urllib.request.urlopen")
    def test_multilogin_check_accepts_a_working_token(self, urlopen):
        urlopen.return_value.__enter__.return_value.status = 200
        result = test_multilogin_access({"A": "working-token"})
        self.assertEqual(result, "Multilogin connection verified (A).")
        self.assertNotIn("working-token", result)

    def test_campaign_manager_name_rejects_example_and_accepts_real_name(self):
        with self.assertRaises(WizardValidationError):
            validate_campaign_manager_name("Campaign Manager Name")
        self.assertEqual(validate_campaign_manager_name("  Anna Smith  "), "Anna Smith")

    def test_manual_setup_discovers_only_this_managers_accounts(self):
        try:
            from openpyxl import Workbook
        except ImportError:
            self.skipTest("openpyxl is installed in the packaging job")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stats = root / "stats.xlsx"
            upload = root / "upload.xlsx"
            oauth = root / "google-access.json"

            headers = sorted(STATS_COLUMNS)
            stats_book = Workbook()
            stats_sheet = stats_book.active
            stats_sheet.append(headers)
            positions = {name: index for index, name in enumerate(headers)}

            def stats_row(manager: str, account: str) -> list[str]:
                row = [""] * len(headers)
                row[positions["CM"]] = manager
                row[positions["Account Alias"]] = account
                return row

            stats_sheet.append(stats_row("Anna Smith", "FacebookAnna1"))
            stats_sheet.append(stats_row("Other Person", "FacebookOther2"))
            stats_book.save(stats)

            upload_book = Workbook()
            accounts = upload_book.active
            accounts.title = "Accounts"
            accounts.append(["Account", "Ad account ID"])
            accounts.append(["FacebookAnna1", "8" * 15])
            for sheet_name in ("Ad copy", "Countries", "Interests", "Upload all"):
                upload_book.create_sheet(sheet_name)
            upload_book.save(upload)

            oauth.write_text(
                json.dumps(
                    {
                        "installed": {
                            "client_id": "test-client",
                            "auth_uri": "https://accounts.example/auth",
                            "token_uri": "https://accounts.example/token",
                        }
                    }
                ),
                encoding="utf-8",
            )

            configuration = build_manual_setup(
                campaign_manager_name="Anna Smith",
                accounts_sheet="sheet" + ("x" * 30),
                oauth_client_path=str(oauth),
                stats_workbook=str(stats),
                upload_workbook=str(upload),
            )

        self.assertEqual(configuration.campaign_manager_name, "Anna Smith")
        self.assertEqual([a.account_name for a in configuration.accounts], ["FacebookAnna1"])
        self.assertEqual(configuration.accounts[0].ad_account_id, "act_" + ("8" * 15))
        self.assertEqual(configuration.accounts[0].profile_id, "")

    def test_setup_summary_omits_identity_and_account_identifiers(self):
        account_id = "act_" + ("7" * 15)
        sheet_id = "sheet" + ("x" * 30)
        configuration = MachineConfiguration(
            "Private Person",
            sheet_id,
            (
                AccountAssignment(
                    account_name="Private Account Label",
                    ad_account_id=account_id,
                    page_id="7" * 15,
                    multilogin_instance="A",
                    folder_id="private-folder-id",
                    profile_id="private-profile-id",
                ),
            ),
            b"{}",
        )
        summary = sanitized_setup_summary(configuration, "0.1.0-test")
        self.assertNotIn("Private Person", summary)
        self.assertNotIn("Private Account Label", summary)
        self.assertNotIn(account_id, summary)
        self.assertNotIn(sheet_id, summary)
        self.assertIn("Assigned accounts: 1", summary)


if __name__ == "__main__":
    unittest.main()
