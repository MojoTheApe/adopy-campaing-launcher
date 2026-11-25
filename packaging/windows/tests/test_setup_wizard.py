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
    WizardValidationError,
    build_manual_setup,
    default_creatives_directory,
    sanitized_setup_summary,
    setup_is_complete,
    validate_campaign_manager_name,
    validate_creatives_directory,
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

    def test_campaign_manager_name_rejects_example_and_accepts_real_name(self):
        with self.assertRaises(WizardValidationError):
            validate_campaign_manager_name("Campaign Manager Name")
        self.assertEqual(validate_campaign_manager_name("  Anna Smith  "), "Anna Smith")

    def test_manual_setup_needs_no_workbook(self):
        # ACL-2867: без BI мастер не читает Excel, аккаунты подключаются позже.
        configuration = build_manual_setup(campaign_manager_name="  Anna Smith ")
        self.assertEqual(configuration.campaign_manager_name, "Anna Smith")
        self.assertEqual(configuration.accounts, ())
        with self.assertRaises(WizardValidationError):
            build_manual_setup(campaign_manager_name="x")

    def test_wizard_has_no_workbook_surface(self):
        import setup_wizard as wizard_module

        source = Path(wizard_module.__file__).read_text(encoding="utf-8")
        for gone in ("Stats workbook", "STATS_WORKBOOK_HELP", "Excel workbook", "FB_STATS_EXCEL"):
            self.assertNotIn(gone, source)

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
