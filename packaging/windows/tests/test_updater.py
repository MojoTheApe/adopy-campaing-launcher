"""The update helper: install, start the new version, or fall back (ACL-1240)."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

PACKAGING_DIR = Path(__file__).resolve().parents[1]
if str(PACKAGING_DIR) not in sys.path:
    sys.path.insert(0, str(PACKAGING_DIR))

import build_manifest  # noqa: E402
import updater  # noqa: E402


class Machine:
    """A fake Windows machine: which ACL answers on /health, what Setup does."""

    def __init__(self, root: Path, *, setup_code=0, new_starts=True, old_stops=True):
        self.programs = root / "Programs" / "ACL"
        self.old_exe = self.programs / "versions" / "2026-09-03e" / "ACL.exe"
        self.old_exe.parent.mkdir(parents=True)
        self.old_exe.write_text("old")
        (self.programs / "current.json").write_text('{"version": "2026-09-03e"}')
        self.running = "2026-09-03e"
        self.setup_code, self.new_starts, self.old_stops = setup_code, new_starts, old_stops
        self.commands: list[list[str]] = []
        self.shortcuts: list[Path] = []
        self.t = 0.0

    def health(self):
        if self.running == "2026-09-03e" and self.old_stops and self.t > 3:
            self.running = None  # старый сервер вышел сам
        return {"app": "ACL", "ok": True, "version": self.running} if self.running else None

    def run(self, cmd, timeout):
        self.commands.append(cmd)
        if cmd[1] == "/VERYSILENT":
            if self.setup_code == 0:
                new = self.programs / "versions" / "2026-09-20" / "ACL.exe"
                new.parent.mkdir(parents=True, exist_ok=True)
                new.write_text("new")
                (self.programs / "current.json").write_text('{"version": "2026-09-20"}')
            return self.setup_code
        if cmd[1] == "--stop-server":
            self.running = None
        return 0

    def start(self, cmd):
        self.commands.append(cmd)
        if "2026-09-20" in cmd[0]:
            self.running = "2026-09-20" if self.new_starts else None
        else:
            self.old_stops = False
            self.running = "2026-09-03e"

    def env(self, updates: Path) -> updater.Environment:
        def sleep(s):
            self.t += s
        return updater.Environment(programs_dir=self.programs, updates_dir=updates, health=self.health,
                                   run=self.run, start=self.start, sleep=sleep, clock=lambda: self.t,
                                   shortcuts=self.shortcuts.append)


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.updates = self.root / "ACL" / "updates"
        self.updates.mkdir(parents=True)
        (self.updates / "pending.json").write_text("{}")
        self.setup = self.updates / "Setup.exe"

    def tearDown(self):
        self.tmp.cleanup()

    def apply(self, machine: Machine) -> dict:
        return updater.apply_update(machine.env(self.updates), self.setup, "2026-09-20", machine.old_exe)

    def last(self) -> dict:
        return json.loads((self.updates / "last_update.json").read_text(encoding="utf-8"))

    def test_a_working_release_is_installed_silently_and_started(self):
        m = Machine(self.root)
        self.assertEqual(self.apply(m)["result"], "updated")
        setup = next(c for c in m.commands if c[0] == str(self.setup))
        self.assertIn("/VERYSILENT", setup)
        self.assertTrue(any("/LOG=" in a for a in setup))
        self.assertEqual(m.running, "2026-09-20")
        self.assertEqual(self.last()["result"], "updated")
        self.assertFalse((self.updates / "pending.json").exists())
        self.assertEqual(m.shortcuts, [])

    def test_a_failed_installer_brings_the_previous_version_back(self):
        m = Machine(self.root, setup_code=5)
        result = self.apply(m)
        self.assertEqual(result["result"], "rolled_back")
        self.assertIn("code 5", result["reason"])
        self.assertEqual(m.running, "2026-09-03e")
        self.assertEqual(m.shortcuts, [m.old_exe])
        self.assertTrue(self.last()["previous_running"])

    def test_a_new_version_that_never_answers_is_stopped_and_rolled_back(self):
        m = Machine(self.root, new_starts=False)
        result = self.apply(m)
        self.assertEqual(result["result"], "rolled_back")
        self.assertIn("did not start", result["reason"])
        self.assertIn([str(self.root / "Programs/ACL/versions/2026-09-20/ACL.exe"), "--stop-server"], m.commands)
        self.assertEqual(json.loads((m.programs / "current.json").read_text())["version"], "2026-09-03e")
        self.assertEqual(m.running, "2026-09-03e")

    def test_nothing_is_installed_while_the_old_dashboard_keeps_running(self):
        m = Machine(self.root, old_stops=False)
        self.assertEqual(self.apply(m)["result"], "not_started")
        self.assertFalse(any(c[0] == str(self.setup) for c in m.commands))
        self.assertEqual(m.running, "2026-09-03e")

    def test_bad_arguments_do_nothing(self):
        self.assertEqual(updater.main(["--apply-update"], self.root), 2)

    def test_starting_new_executable_can_fail_without_losing_old_version(self):
        m = Machine(self.root)
        env = m.env(self.updates)
        def start(cmd):
            if "2026-09-20" in cmd[0]:
                raise OSError("executable is blocked")
            return m.start(cmd)
        env.start = start
        result = updater.apply_update(env, self.setup, "2026-09-20", m.old_exe)
        self.assertEqual(result["result"], "rolled_back")
        self.assertEqual(m.running, "2026-09-03e")

    def test_failed_rollback_is_not_reported_as_restored(self):
        m = Machine(self.root, setup_code=5)
        env = m.env(self.updates)
        env.start = lambda cmd: None
        result = updater.apply_update(env, self.setup, "2026-09-20", m.old_exe)
        self.assertEqual(result["result"], "rollback_failed")
        self.assertFalse(result["previous_running"])

    def test_uncertain_installer_shutdown_does_not_launch_another_version(self):
        m = Machine(self.root)
        env = m.env(self.updates)
        def run(*args, **kwargs):
            raise updater.InstallationStillRunning()
        env.run = run
        result = updater.apply_update(env, self.setup, "2026-09-20", m.old_exe)
        self.assertEqual(result["result"], "rollback_failed")
        self.assertEqual(m.commands, [])

    def test_failed_shortcut_restore_is_not_a_successful_rollback(self):
        m = Machine(self.root, setup_code=5)
        env = m.env(self.updates)
        def fail(exe): raise OSError("shortcut is locked")
        env.shortcuts = fail
        result = updater.apply_update(env, self.setup, "2026-09-20", m.old_exe)
        self.assertEqual(result["result"], "rollback_failed")
        self.assertTrue(result["previous_running"])


class UpdateChannelTests(unittest.TestCase):
    def test_channel_is_all_or_nothing(self):
        self.assertEqual(build_manifest.build_update_channel({}), {"schema": 1, "repo": "", "token": ""})
        self.assertEqual(build_manifest.build_update_channel({"ACL_RELEASES_REPO": "adopy/acl-releases",
                                                               "ACL_UPDATE_TOKEN": "t"})["repo"], "adopy/acl-releases")
        with self.assertRaises(ValueError):
            build_manifest.build_update_channel({"ACL_RELEASES_REPO": "adopy/acl-releases"})

    def test_signature_enforcement_is_opt_in_per_build(self):
        self.assertNotIn("signatures", build_manifest.build_update_channel({}))
        for value in ("true", "1", "enforce"):
            self.assertEqual(build_manifest.build_update_channel(
                {"ACL_ENFORCE_RELEASE_SIGNATURES": value})["signatures"], "enforce")
        self.assertNotIn("signatures", build_manifest.build_update_channel({"ACL_ENFORCE_RELEASE_SIGNATURES": "no"}))


if __name__ == "__main__":
    unittest.main()
