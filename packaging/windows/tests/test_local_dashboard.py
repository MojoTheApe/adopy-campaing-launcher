"""Каждый человек попадает в свой дешборд, а не в тот, что занял порт (ACL-1309).

24.09, установка команде: на машине, где уже работал дешборд владельца, у
второго человека лаунчер увидел на 9090 живой ACL, решил, что это его, и
открыл браузер — чужие аккаунты, чужие цифры, ни слова о том, что что-то не
так. Стоило погасить дешборд владельца, и у второго поднялся свой.

Здесь настоящие HTTP-серверы на настоящих портах, а не подменённые ответы:
вопрос «чей это ACL» решается сетью, и проверять его надо сетью.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

PACKAGING_DIR = Path(__file__).resolve().parents[1]
ROOT = PACKAGING_DIR.parents[1]
for entry in (str(PACKAGING_DIR), str(ROOT)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import local_dashboard  # noqa: E402
from config.paths import config_identity  # noqa: E402


def _free_ports(count: int) -> list[int]:
    """Порты, свободные прямо сейчас. Берём у системы, чтобы не столкнуться
    ни с чем, что уже работает на машине разработчика или раннере."""
    sockets, ports = [], []
    for _ in range(count):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        sockets.append(s)
        ports.append(s.getsockname()[1])
    for s in sockets:
        s.close()
    return ports


class _Dashboard:
    """Настоящий HTTP-сервер, отвечающий на /health так, как ответил бы ACL."""

    def __init__(self, port: int, payload: dict | None):
        body = json.dumps(payload).encode("utf-8") if payload is not None else b"not json at all"

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 — так зовёт метод http.server
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class OwnDashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mine = Path(self.tmp.name) / "anna" / "ACL"
        self.theirs = Path(self.tmp.name) / "dan" / "ACL"
        self.mine.mkdir(parents=True)
        self.theirs.mkdir(parents=True)
        self.ports = _free_ports(4)
        patcher = mock.patch.object(local_dashboard, "PORTS", self.ports)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _serve(self, port: int, payload: dict | None) -> None:
        dashboard = _Dashboard(port, payload)
        self.addCleanup(dashboard.close)

    @staticmethod
    def _acl(config_dir: Path) -> dict:
        return {"app": "ACL", "ok": True, "version": "x", "config": config_identity(config_dir)}

    def test_someone_elses_dashboard_on_the_first_port_is_not_mine(self):
        """Ровно то, что случилось 24.09: на первом порту живой ACL — но чужой."""
        self._serve(self.ports[0], self._acl(self.theirs))
        self.assertIsNone(local_dashboard.find_own(self.mine))

    def test_my_dashboard_is_found_behind_someone_elses(self):
        self._serve(self.ports[0], self._acl(self.theirs))
        self._serve(self.ports[2], self._acl(self.mine))
        found = local_dashboard.find_own(self.mine)
        self.assertIsNotNone(found)
        self.assertEqual(found[0], self.ports[2])

    def test_a_dashboard_that_does_not_say_whose_it_is_is_not_mine(self):
        """Дешборд версии до ACL-1309 отпечатка не сообщает. Считать его своим —
        значит вернуть ровно ту ошибку, которую чиним."""
        self._serve(self.ports[0], {"app": "ACL", "ok": True, "version": "old"})
        self.assertIsNone(local_dashboard.find_own(self.mine))

    def test_an_unrelated_program_on_the_port_is_not_mine(self):
        self._serve(self.ports[0], None)
        self.assertIsNone(local_dashboard.find_own(self.mine))

    def test_the_recorded_port_is_asked_first(self):
        """Обычный случай — одним запросом: сервер записал свой порт в server.json."""
        (self.mine / "server.json").write_text(json.dumps({"port": self.ports[3]}), encoding="utf-8")
        asked: list[int] = []

        def probe(port):
            asked.append(port)
            return self._acl(self.mine) if port == self.ports[3] else None

        found = local_dashboard.find_own(self.mine, probe=probe, in_use=lambda port: True)
        self.assertEqual(found[0], self.ports[3])
        self.assertEqual(asked, [self.ports[3]], "записанный порт спрошен не первым")

    def test_a_stale_record_is_only_a_hint(self):
        """Сервер упал, не убрав запись, а потом поднялся на другом порту."""
        (self.mine / "server.json").write_text(json.dumps({"port": self.ports[3]}), encoding="utf-8")
        self._serve(self.ports[1], self._acl(self.mine))
        found = local_dashboard.find_own(self.mine)
        self.assertEqual(found[0], self.ports[1])

    def test_looking_for_my_dashboard_does_not_stall_a_cold_start(self):
        """Первая версия спрашивала каждый порт по HTTP. На Windows закрытый
        порт отвечает не отказом, а секундой ожидания: двадцать портов — 19
        секунд на каждом холодном старте, и столько же на каждом витке
        ожидания запуска (замер 24.09). Сейчас — десятки миллисекунд."""
        import time

        ports = _free_ports(20)
        with mock.patch.object(local_dashboard, "PORTS", ports):
            started = time.perf_counter()
            self.assertIsNone(local_dashboard.find_own(self.mine))
            elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 3.0, f"поиск своего дешборда занял {elapsed:.1f} с")

    def test_a_program_listening_on_every_address_is_seen(self):
        """Программа на 0.0.0.0 держит и 127.0.0.1. Проверка «занят ли порт»
        через 127.0.0.1 её не видела — и мы встали бы поверх неё."""
        holder = socket.socket()
        holder.bind(("0.0.0.0", self.ports[0]))
        holder.listen()
        self.addCleanup(holder.close)
        self.assertTrue(local_dashboard.port_in_use(self.ports[0]))
        self.assertEqual(local_dashboard.free_port(), self.ports[1])

    def test_the_free_port_is_next_to_what_is_taken_not_instead_of_it(self):
        self._serve(self.ports[0], self._acl(self.theirs))
        self._serve(self.ports[1], None)
        self.assertEqual(local_dashboard.free_port(), self.ports[2])

    def test_no_free_port_is_said_as_such(self):
        for port in self.ports:
            self._serve(port, None)
        self.assertIsNone(local_dashboard.free_port())


class WizardPortCheckTests(unittest.TestCase):
    """Шаг System check мастера горел красным «port 9090 is already in use», и
    второй человек на общей машине упирался в него на втором шаге из шести."""

    def test_a_taken_first_port_does_not_fail_the_system_check(self):
        import setup_wizard

        ports = _free_ports(3)
        with mock.patch.object(local_dashboard, "PORTS", ports):
            busy = _Dashboard(ports[0], {"app": "ACL", "ok": True, "config": "someone-else"})
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    checks = {c.label: c for c in setup_wizard.system_readiness(tmp)}
            finally:
                busy.close()
        check = checks["Local dashboard port"]
        self.assertTrue(check.ok, check.detail)
        self.assertIn(str(ports[1]), check.detail, "не сказано, на какой порт встанет дешборд")

    def test_no_free_port_at_all_still_fails_the_check(self):
        import setup_wizard

        ports = _free_ports(2)
        with mock.patch.object(local_dashboard, "PORTS", ports):
            taken = [_Dashboard(p, None) for p in ports]
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    checks = {c.label: c for c in setup_wizard.system_readiness(tmp)}
            finally:
                for server in taken:
                    server.close()
        self.assertFalse(checks["Local dashboard port"].ok)


class UpdaterTests(unittest.TestCase):
    """Обновление ждёт, пока остановится дешборд, и потом — пока поднимется
    новая версия. Спрашивало оно порт 9090: на общей машине там отвечал бы
    сосед, и обновление ждало бы остановки чужого дешборда, а после установки
    могло принять чужой запуск за свой."""

    def test_the_update_asks_about_my_dashboard_not_the_first_port(self):
        import updater

        with tempfile.TemporaryDirectory() as tmp:
            mine, theirs = Path(tmp) / "anna", Path(tmp) / "dan"
            ports = _free_ports(2)
            with mock.patch.object(local_dashboard, "PORTS", ports):
                neighbour = _Dashboard(ports[0], {"app": "ACL", "ok": True,
                                                  "config": config_identity(theirs)})
                try:
                    probe = updater.own_health(mine)
                    self.assertIsNone(probe(), "дешборд соседа принят за мой: обновление ждало бы его")
                    own = _Dashboard(ports[1], {"app": "ACL", "ok": True, "version": "v2",
                                                "config": config_identity(mine)})
                    try:
                        self.assertEqual((probe() or {}).get("version"), "v2")
                    finally:
                        own.close()
                finally:
                    neighbour.close()

    def test_the_real_update_is_wired_to_my_dashboard(self):
        """Функция выше бесполезна, если `main` её не передаёт: без неё
        обновление упало бы на первом же вопросе «дешборд остановился?»."""
        import updater

        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "ACL"
            (local / "updates").mkdir(parents=True)
            setup = local / "updates" / "setup.exe"
            setup.write_bytes(b"")
            seen = {}

            def capture(env, *_):
                seen["health"] = env.health
                return {"result": "updated"}

            args = ["--apply-update", str(setup), "--expect-version", "v2", "--previous-exe", sys.executable]
            with mock.patch.object(updater, "apply_update", side_effect=capture), \
                    mock.patch.object(local_dashboard, "find_own", return_value=(9095, {"version": "v2"})) as find:
                self.assertEqual(updater.main(args, local), 0)
                self.assertEqual(seen["health"](), {"version": "v2"})
            find.assert_called_with(local)


class IdentityTests(unittest.TestCase):
    """Лаунчер и дешборд обязаны считать отпечаток одного каталога одинаково,
    иначе свой дешборд окажется «чужим» и каждый запуск будет плодить новый."""

    def test_the_launcher_and_the_dashboard_agree_on_whose_it_is(self):
        import launcher
        from config.paths import get_local_config_dir

        with tempfile.TemporaryDirectory() as tmp:
            for env in ({"ACL_CONFIG_DIR": str(Path(tmp) / "cfg"), "LOCALAPPDATA": tmp},
                        {"ACL_CONFIG_DIR": "", "LOCALAPPDATA": tmp}):
                with self.subTest(env=env), mock.patch.dict(os.environ, env):
                    self.assertEqual(local_dashboard.identity(launcher._local_acl_dir()),
                                     config_identity(get_local_config_dir()))

    def test_two_people_on_one_machine_are_told_apart(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertNotEqual(config_identity(Path(tmp) / "anna" / "ACL"),
                                config_identity(Path(tmp) / "dan" / "ACL"))

    def test_the_identity_does_not_give_away_the_path(self):
        """/health слышит любой процесс на машине. Путь выдал бы имя чужой
        учётной записи Windows."""
        with tempfile.TemporaryDirectory() as tmp:
            value = config_identity(Path(tmp) / "SomeoneElse" / "ACL")
        self.assertRegex(value, r"^[0-9a-f]{16}$")


if __name__ == "__main__":
    unittest.main()
