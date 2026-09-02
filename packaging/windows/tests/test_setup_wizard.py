from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


PACKAGING_DIR = Path(__file__).resolve().parents[1]
if str(PACKAGING_DIR) not in sys.path:
    sys.path.insert(0, str(PACKAGING_DIR))

from setup_wizard import (
    WizardValidationError,
    setup_is_complete,
    validate_creatives_directory,
    validate_stats_workbook,
    write_ready_state,
)


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


if __name__ == "__main__":
    unittest.main()
