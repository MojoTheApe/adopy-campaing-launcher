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

    def test_my_dashboard_is_opened_where_it_actually_runs(self):
        """Свой дешборд открываем там, где он стоит, — не обязательно на 9090."""
        with mock.patch.object(launcher.local_dashboard, "find_own", return_value=(9093, {})):
            with mock.patch.object(launcher.webbrowser, "open") as browser_open:
                with mock.patch.object(launcher.subprocess, "Popen") as popen:
                    result = launcher._launch_normal()

        self.assertEqual(result, 0)
        browser_open.assert_called_once_with("http://127.0.0.1:9093/")
        popen.assert_not_called()

    def test_someone_elses_dashboard_is_left_alone_and_mine_starts_beside_it(self):
        """ACL-1309, 24.09: на 9090 работал дешборд владельца, и лаунчер второго
        человека открыл его как свой. Теперь чужой не трогаем, а свой поднимаем
        на следующем свободном порту и открываем его."""
        found = iter([None, None, (9091, {})])
        with mock.patch.object(launcher.local_dashboard, "find_own", side_effect=lambda *_: next(found)):
            with mock.patch.object(launcher.local_dashboard, "free_port", return_value=9091):
                with mock.patch.object(launcher.webbrowser, "open") as browser_open:
                    with mock.patch.object(launcher.subprocess, "Popen") as popen:
                        popen.return_value.poll.return_value = None
                        with mock.patch.object(launcher.time, "sleep"):
                            result = launcher._launch_normal()

        self.assertEqual(result, 0)
        command = popen.call_args.args[0]
        self.assertEqual(command[command.index("--port") + 1], "9091")
        browser_open.assert_called_once_with("http://127.0.0.1:9091/")

    def test_every_port_taken_is_an_error_not_a_takeover(self):
        """Когда встать некуда — говорим об этом. Чужое не останавливаем."""
        with mock.patch.object(launcher.local_dashboard, "find_own", return_value=None):
            with mock.patch.object(launcher.local_dashboard, "free_port", return_value=None):
                with mock.patch.object(launcher, "_show_message") as show_message:
                    with mock.patch.object(launcher.subprocess, "Popen") as popen:
                        result = launcher._launch_normal()

        self.assertEqual(result, 2)
        self.assertTrue(show_message.call_args.kwargs["error"])
        popen.assert_not_called()

    def _first_launch(self, *, own, on_first_port):
        """Первый запуск после установки: настройка не пройдена.

        Сеть задана целиком — и мой дешборд, и то, что отвечает на 9090. Иначе
        тест зависит от того, что работает на машине разработчика: первая
        версия этого теста проходила при сломанном лаунчере, потому что на
        9090 в ту минуту действительно что-то работало.
        """
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"ACL_CONFIG_DIR": tmp}),                     mock.patch.object(sys, "argv", ["ACL.exe"]),                     mock.patch.object(launcher.local_dashboard, "find_own", return_value=own),                     mock.patch.object(launcher.local_dashboard, "health", return_value=on_first_port),                     mock.patch("setup_wizard.setup_is_complete", return_value=False),                     mock.patch.object(launcher, "_run_setup", return_value=False) as run_setup,                     mock.patch.object(launcher, "_launch_normal", return_value=0) as launch:
                launcher.main()
        return run_setup, launch

    def test_someone_elses_dashboard_does_not_skip_my_setup(self):
        """Утром 24.09 свежая установка не показала мастер: на 9090 работал
        чужой дешборд, и лаунчер счёл, что настраивать нечего."""
        someone_else = {"app": "ACL", "ok": True, "config": "not-mine"}
        run_setup, _ = self._first_launch(own=None, on_first_port=someone_else)
        run_setup.assert_called_once()

    def test_an_empty_port_does_not_skip_my_setup(self):
        run_setup, _ = self._first_launch(own=None, on_first_port=None)
        run_setup.assert_called_once()

    def test_my_own_running_dashboard_is_opened_without_setup(self):
        """Работает мой дешборд — значит настройка уже есть, мастер не нужен."""
        mine = {"app": "ACL", "ok": True, "config": "mine"}
        run_setup, launch = self._first_launch(own=(9090, mine), on_first_port=mine)
        run_setup.assert_not_called()
        launch.assert_called_once()


if __name__ == "__main__":
    unittest.main()
