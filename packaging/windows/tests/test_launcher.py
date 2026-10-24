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
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        tmp = temporary.name
        held = []
        self.addCleanup(lambda: [lock.__exit__(None, None, None) for lock in held])
        with mock.patch.object(sys, "platform", "win32"), \
                mock.patch.object(ctypes, "WinDLL", return_value=kernel, create=True), \
                mock.patch.object(ctypes, "get_last_error", side_effect=lambda: error[0], create=True), \
                mock.patch.object(launcher, "_MUTEX_HANDLE", None), \
                mock.patch.object(launcher, "_CONFIG_LOCK", None), \
                mock.patch.object(launcher.local_dashboard, "find_own", return_value=None):
            for person in ("anna", "dan"):
                with mock.patch.object(launcher, "_local_acl_dir", return_value=Path(tmp) / person):
                    self.assertTrue(launcher._acquire_server_mutex(), person)
                    # Retain two real, separate OS file locks like two processes.
                    held.append(launcher._CONFIG_LOCK)
                    launcher._CONFIG_LOCK = None
                    launcher._MUTEX_HANDLE = None
            with mock.patch.object(launcher, "_local_acl_dir", return_value=Path(tmp) / "anna"):
                self.assertFalse(launcher._acquire_server_mutex())
        self.assertIn(launcher.SERVER_MUTEX_NAME, existing)
        self.assertEqual(existing, {launcher.SERVER_MUTEX_NAME})
        installer = (PACKAGING_DIR / "installer.iss").read_text(encoding="utf-8")
        self.assertIn("AppMutex=" + launcher.SERVER_MUTEX_NAME, installer)

    def test_missing_installer_presence_does_not_leave_an_unprotected_server_gate(self):
        import ctypes

        kernel = mock.Mock()
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(sys, "platform", "win32"), \
                mock.patch.object(ctypes, "WinDLL", return_value=kernel, create=True), \
                mock.patch.object(ctypes, "get_last_error", return_value=0, create=True), \
                mock.patch.object(launcher, "_MUTEX_HANDLE", None), \
                mock.patch.object(launcher, "_CONFIG_LOCK", None), \
                mock.patch.object(launcher, "_local_acl_dir", return_value=Path(tmp)), \
                mock.patch.object(launcher, "_installer_presence_mutex", return_value=None):
            self.assertFalse(launcher._acquire_server_mutex())
            self.assertIsNone(launcher._MUTEX_HANDLE)
            self.assertIsNone(launcher._CONFIG_LOCK)
            from config.paths import machine_file_lock
            with machine_file_lock(Path(tmp) / "server-instance.lock", timeout=0):
                pass  # a failed installer marker must not leak the actual config gate

    def test_config_gate_does_not_require_the_global_namespace(self):
        import ctypes
        from config.paths import machine_file_lock

        kernel = mock.Mock()
        kernel.CreateMutexW.return_value = 0  # Global namespace unavailable
        kernel.CreateMutexExW.return_value = 456  # original Local installer marker is available
        kernel.ConvertStringSecurityDescriptorToSecurityDescriptorW.return_value = True
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(sys, "platform", "win32"), \
                mock.patch.object(ctypes, "WinDLL", return_value=kernel, create=True), \
                mock.patch.object(ctypes, "set_last_error", create=True), \
                mock.patch.object(launcher, "_MUTEX_HANDLE", None), \
                mock.patch.object(launcher, "_CONFIG_LOCK", None, create=True), \
                mock.patch.object(launcher, "_local_acl_dir", return_value=Path(tmp)):
            try:
                self.assertTrue(launcher._acquire_server_mutex())
                with self.assertRaises(TimeoutError):
                    with machine_file_lock(Path(tmp) / "server-instance.lock", timeout=0):
                        pass
                kernel.CreateMutexW.assert_not_called()
            finally:
                launcher._release_server_mutex()
            with machine_file_lock(Path(tmp) / "server-instance.lock", timeout=0):
                pass


if __name__ == "__main__":
    unittest.main()
