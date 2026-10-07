from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock


PACKAGING_DIR = Path(__file__).resolve().parents[1]
if str(PACKAGING_DIR) not in sys.path:
    sys.path.insert(0, str(PACKAGING_DIR))

import build_manifest


class BuildManifestTests(unittest.TestCase):
    @mock.patch.object(build_manifest, "_pyinstaller_version", return_value="6.test")
    @mock.patch.object(build_manifest, "_git_sha", return_value="f" * 40)
    def test_payload_identifies_core_and_installer_sources(self, _git, _pyinstaller):
        payload = build_manifest.build_payload(
            "0.1.0-test",
            built_at_utc="2026-09-02T10:00:00+00:00",
        )

        baseline = json.loads((PACKAGING_DIR / "baseline.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["version"], "0.1.0-test")
        self.assertEqual(payload["product"], "Adopy Campaign Manager")
        self.assertEqual(payload["core_baseline_sha"], "f" * 40)
        self.assertEqual(payload["integration_base_sha"], baseline["baseline_sha"])
        self.assertEqual(payload["installer_source_sha"], "f" * 40)
        self.assertEqual(payload["built_at_utc"], "2026-09-02T10:00:00+00:00")
        self.assertEqual(payload["pyinstaller_version"], "6.test")

        current = build_manifest.build_current_payload(payload)
        self.assertEqual(current["version"], "0.1.0-test")
        self.assertEqual(current["relative_path"], r"versions\0.1.0-test")
        self.assertEqual(current["source_sha"], "f" * 40)

    def test_version_cannot_escape_versioned_install_directory(self):
        with self.assertRaises(ValueError):
            build_manifest.build_payload(r"..\outside")


if __name__ == "__main__":
    unittest.main()
