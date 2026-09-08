from __future__ import annotations

import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


PACKAGING_DIR = Path(__file__).resolve().parents[1]
if str(PACKAGING_DIR) not in sys.path:
    sys.path.insert(0, str(PACKAGING_DIR))

from diagnostics import assert_sanitized, export_diagnostics, sanitize_text


class DiagnosticsTests(unittest.TestCase):
    def test_sanitizer_removes_tokens_accounts_uuids_and_user_path(self):
        token = "ey" + "SensitiveTokenBody123456789"
        account = "act_" + ("8" * 15)
        profile = "12345678" + "-1234-4234-8234-" + "123456789abc"
        source = f"Bearer {token} {account} {profile} C:\\Users\\Alice\\file.xlsx"
        result = sanitize_text(source)
        assert_sanitized(result)
        self.assertNotIn(token, result)
        self.assertNotIn(account, result)
        self.assertNotIn(profile, result)
        self.assertNotIn("Alice", result)

    def test_export_contains_summary_and_sanitized_logs_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config"
            logs = config / "logs"
            logs.mkdir(parents=True)
            secret = "ey" + "LocalSecretValue123456789"
            (config / ".env").write_text(
                f"MULTILOGIN_AUTOMATION_TOKEN_A={secret}\nFB_RPA_ENGINE=recorded_flow\n",
                encoding="utf-8",
            )
            (logs / "server.log").write_text(f"failed Authorization: Bearer {secret}\n", encoding="utf-8")
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"version": "test", "product": "ACL"}), encoding="utf-8")

            output = export_diagnostics(config, manifest)
            with zipfile.ZipFile(output) as archive:
                names = set(archive.namelist())
                combined = b"\n".join(archive.read(name) for name in names).decode("utf-8")

            self.assertIn("summary.json", names)
            self.assertNotIn(".env", names)
            self.assertNotIn(secret, combined)
            self.assertIn("multilogin_token_a_present", combined)


if __name__ == "__main__":
    unittest.main()
