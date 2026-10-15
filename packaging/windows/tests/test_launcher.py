from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PACKAGING_DIR = Path(__file__).resolve().parents[1]
if str(PACKAGING_DIR) not in sys.path:
    sys.path.insert(0, str(PACKAGING_DIR))

import launcher


class LauncherTests(unittest.TestCase):
    def test_version_output_uses_embedded_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "version": "0.1.0-test",
                        "core_baseline_sha": "a" * 40,
                        "installer_source_sha": "b" * 40,
                    }
                ),
                encoding="utf-8",
            )
            output = io.StringIO()
            with mock.patch.dict(os.environ, {"ACL_BUILD_MANIFEST": str(manifest)}):
                with contextlib.redirect_stdout(output):
                    result = launcher._print_version()

        self.assertEqual(result, 0)
        self.assertIn("Adopy Campaign Manager 0.1.0-test", output.getvalue())
        self.assertIn("a" * 40, output.getvalue())
        self.assertIn("b" * 40, output.getvalue())

    def test_runtime_directory_is_per_user(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": tmp, "ACL_CONFIG_DIR": ""}):
                result = launcher._runtime_workdir()

            self.assertEqual(result, Path(tmp) / "ACL" / "workspace")
            self.assertTrue(result.is_dir())


if __name__ == "__main__":
    unittest.main()
