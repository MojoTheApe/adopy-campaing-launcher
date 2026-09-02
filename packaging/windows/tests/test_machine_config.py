from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


PACKAGING_DIR = Path(__file__).resolve().parents[1]
if str(PACKAGING_DIR) not in sys.path:
    sys.path.insert(0, str(PACKAGING_DIR))

from machine_config import (
    AccountAssignment,
    MachineConfigError,
    MachineConfiguration,
    apply_machine_configuration,
    extract_sheet_id,
    validate_oauth_client,
)


class MachineConfigTests(unittest.TestCase):
    def test_google_sheet_link_is_normalized(self):
        sheet_id = "1WlSGohkev4h_n0gw2vl0jlJEDFpTGl306fF7bIiRt9k"
        self.assertEqual(
            extract_sheet_id(f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"),
            sheet_id,
        )

    def test_google_access_file_rejects_user_tokens(self):
        with self.assertRaises(MachineConfigError):
            validate_oauth_client(b'{"token":"secret"}')

    def test_apply_is_atomic_and_preserves_unrelated_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp)
            (config / ".env").write_text("KEEP_ME=unchanged\nFB_RPA_ENGINE=unsafe\n", encoding="utf-8")
            configuration = MachineConfiguration(
                "Anna Smith",
                "sheet" + ("x" * 30),
                (AccountAssignment("FacebookAnna1", "act_" + ("8" * 15)),),
                b'{"installed":{"client_id":"id","auth_uri":"a","token_uri":"t"}}',
            )

            apply_machine_configuration(
                configuration,
                config,
                stats_workbook=r"C:\Data\stats.xlsx",
                upload_workbook=r"C:\Data\upload.xlsx",
                creatives_directory=r"C:\Data\Creatives",
                multilogin_tokens={"A": "private-token"},
            )

            env = (config / ".env").read_text(encoding="utf-8")
            self.assertIn("KEEP_ME=unchanged", env)
            self.assertIn('FB_RPA_ENGINE="recorded_flow"', env)
            self.assertIn('FB_BROWSER_PROVIDER="multilogin_attach"', env)
            self.assertIn('MULTILOGIN_AUTOMATION_TOKEN_A="private-token"', env)
            account_map = (config / "ad_account_map.csv").read_text(encoding="utf-8")
            self.assertIn("FacebookAnna1,act_" + ("8" * 15), account_map)
            self.assertTrue((config / "oauth_client.json").is_file())


if __name__ == "__main__":
    unittest.main()
