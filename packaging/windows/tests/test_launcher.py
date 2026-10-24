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

    def test_exclusivity_is_per_config_while_the_fixed_installer_marker_stays_live(self):
        import ctypes

        existing = set()
        error = [0]
        kernel = mock.Mock()

        def create_mutex(_security, _owner, name):
            error[0] = 183 if name in existing else 0
            existing.add(name)
            return len(existing) + 100

        kernel.CreateMutexW.side_effect = create_mutex
        kernel.CreateMutexExW.side_effect = lambda _security, name, *_: create_mutex(None, False, name)
        kernel.ConvertStringSecurityDescriptorToSecurityDescriptorW.return_value = True
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(sys, "platform", "win32"), \
                mock.patch.object(ctypes, "WinDLL", return_value=kernel, create=True), \
                mock.patch.object(ctypes, "get_last_error", side_effect=lambda: error[0], create=True), \
                mock.patch.object(ctypes, "set_last_error", create=True), \
                mock.patch.object(launcher, "_MUTEX_HANDLE", None), \
                mock.patch.object(launcher, "_INSTALLER_MUTEX_HANDLE", None, create=True), \
                mock.patch.object(launcher.local_dashboard, "find_own", return_value=None):
            for person in ("anna", "dan"):
                with mock.patch.object(launcher, "_local_acl_dir", return_value=Path(tmp) / person):
                    self.assertTrue(launcher._acquire_server_mutex(), person)
                    # Simulate a separate process retaining its named handles.
                    launcher._MUTEX_HANDLE = None
                    launcher._INSTALLER_MUTEX_HANDLE = None
            with mock.patch.object(launcher, "_local_acl_dir", return_value=Path(tmp) / "anna"):
                self.assertFalse(launcher._acquire_server_mutex())
        self.assertIn(launcher.SERVER_MUTEX_NAME, existing)
        gates = existing - {launcher.SERVER_MUTEX_NAME}
        self.assertEqual(len(gates), 2)
        self.assertTrue(all(name.startswith("Global\\") for name in gates))
        self.assertTrue(all("anna" not in name and "dan" not in name for name in gates))
        installer = (PACKAGING_DIR / "installer.iss").read_text(encoding="utf-8")
        self.assertIn("AppMutex=" + launcher.SERVER_MUTEX_NAME, installer)

    def test_missing_installer_presence_does_not_leave_an_unprotected_server_gate(self):
        import ctypes

        kernel = mock.Mock()
        kernel.CreateMutexW.return_value = 123
        with mock.patch.object(sys, "platform", "win32"), \
                mock.patch.object(ctypes, "WinDLL", return_value=kernel, create=True), \
                mock.patch.object(ctypes, "get_last_error", return_value=0, create=True), \
                mock.patch.object(ctypes, "set_last_error", create=True), \
                mock.patch.object(launcher, "_MUTEX_HANDLE", None), \
                mock.patch.object(launcher, "_INSTALLER_MUTEX_HANDLE", None), \
                mock.patch.object(launcher, "_local_acl_dir", return_value=Path("synthetic-config")), \
                mock.patch.object(launcher, "_installer_presence_mutex", return_value=None):
            self.assertFalse(launcher._acquire_server_mutex())
            self.assertIsNone(launcher._MUTEX_HANDLE)
            self.assertIsNone(launcher._INSTALLER_MUTEX_HANDLE)
        kernel.CloseHandle.assert_called_once_with(123)


if __name__ == "__main__":
    unittest.main()
