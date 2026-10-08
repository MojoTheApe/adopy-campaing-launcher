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

    def test_existing_healthy_acl_is_opened_without_starting_process(self):
        with mock.patch.object(launcher, "_is_acl_healthy", return_value=True):
            with mock.patch.object(launcher.webbrowser, "open") as browser_open:
                with mock.patch.object(launcher.subprocess, "Popen") as popen:
                    result = launcher._launch_normal()

        self.assertEqual(result, 0)
        browser_open.assert_called_once_with("http://127.0.0.1:9090/")
        popen.assert_not_called()

    def test_unrelated_port_owner_is_not_stopped(self):
        with mock.patch.object(launcher, "_is_acl_healthy", return_value=False):
            with mock.patch.object(launcher, "_port_is_in_use", return_value=True):
                with mock.patch.object(launcher, "_show_message") as show_message:
                    with mock.patch.object(launcher.subprocess, "Popen") as popen:
                        result = launcher._launch_normal()

        self.assertEqual(result, 2)
        self.assertTrue(show_message.call_args.kwargs["error"])
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
