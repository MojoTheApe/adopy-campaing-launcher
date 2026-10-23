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
    default_creatives_directory,
    sanitized_setup_summary,
    setup_is_complete,
    validate_campaign_manager_name,
    validate_creatives_directory,
    validate_stats_workbook,
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

    def test_creatives_directory_is_created_when_asked(self):
        with tempfile.TemporaryDirectory() as tmp:
            wanted = Path(tmp) / "Documents" / "ACL" / "Creatives"
            self.assertEqual(validate_creatives_directory(str(wanted), create=True), str(wanted.resolve()))
            self.assertTrue(wanted.is_dir())

    def test_default_creatives_directory_is_in_documents(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict("os.environ", {"USERPROFILE": tmp}):
            self.assertEqual(default_creatives_directory(), Path(tmp) / "Documents" / "ACL" / "Creatives")
            self.assertEqual(validate_creatives_directory("", create=True),
                             str((Path(tmp) / "Documents" / "ACL" / "Creatives").resolve()))

    def test_missing_stats_workbook_is_actionable(self):
        with self.assertRaisesRegex(WizardValidationError, "does not exist"):
            validate_stats_workbook("missing.xlsx")

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
            stats_sheet.append(stats_row("anna smith", "FacebookAnna1"))
            stats_sheet.append(stats_row("Other Person", "FacebookOther2"))
            stats_book.save(stats)

            configuration = build_manual_setup(campaign_manager_name="Anna Smith", stats_workbook=str(stats))
            with self.assertRaisesRegex(WizardValidationError, "is not in the CM column"):
                build_manual_setup(campaign_manager_name="Nobody Here", stats_workbook=str(stats))

        self.assertEqual(configuration.campaign_manager_name, "Anna Smith")
        self.assertEqual([a.account_name for a in configuration.accounts], ["FacebookAnna1"])
        self.assertEqual(configuration.accounts[0].ad_account_id, "")
        self.assertEqual(configuration.accounts[0].profile_id, "")

    def test_setup_summary_omits_identity_and_account_identifiers(self):
        account_id = "act_" + ("7" * 15)
        configuration = MachineConfiguration(
            "Private Person",
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
        )
        summary = sanitized_setup_summary(configuration, "0.1.0-test")
        self.assertNotIn("Private Person", summary)
        self.assertNotIn("Private Account Label", summary)
        self.assertNotIn(account_id, summary)
        self.assertNotIn("Google", summary)
        self.assertIn("Assigned accounts: 1", summary)


if __name__ == "__main__":
    unittest.main()
