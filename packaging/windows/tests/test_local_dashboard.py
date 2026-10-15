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

import launcher  # noqa: E402
import local_dashboard  # noqa: E402
from config.paths import config_identity  # noqa: E402


_REGISTRY = mock.patch.dict(os.environ, {"ACL_PORT_REGISTRY_DIR": ""})


def setUpModule():  # noqa: N802 — так зовёт unittest
    """Ни один тест не пишет в настоящий %ProgramData%\\ACL\\ports; тесты
    закреплённого порта дают свой реестр во временной папке."""
    _REGISTRY.start()


def tearDownModule():  # noqa: N802
    _REGISTRY.stop()


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
            ports = _free_ports(2)
            with mock.patch.object(updater, "apply_update", side_effect=capture), \
                    mock.patch.object(local_dashboard, "PORTS", ports):
                self.assertEqual(updater.main(args, local), 0)
                neighbour = _Dashboard(ports[0], {"app": "ACL", "ok": True, "version": "v1",
                                                  "config": config_identity(Path(tmp) / "dan")})
                own = _Dashboard(ports[1], {"app": "ACL", "ok": True, "version": "v2",
                                            "config": config_identity(local)})
                try:
                    self.assertEqual((seen["health"]() or {}).get("version"), "v2")
                finally:
                    own.close()
                    neighbour.close()


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


class _SharedMachine(unittest.TestCase):
    """Общая машина в миниатюре: двое — Анна и Дан, — у каждого свой каталог
    настроек, общий ряд портов и общий реестр закреплённых портов.

    Порты берём у системы, реестр — во временной папке: тест не зависит от
    того, что работает на машине разработчика, и ничего не пишет в настоящий
    %ProgramData%.
    """

    PORT_COUNT = 4

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.anna = root / "anna" / "ACL"
        self.dan = root / "dan" / "ACL"
        self.anna.mkdir(parents=True)
        self.dan.mkdir(parents=True)
        self.registry = root / "registry"
        self.ports = _free_ports(self.PORT_COUNT)
        for patcher in (mock.patch.object(local_dashboard, "PORTS", self.ports),
                        mock.patch.dict(os.environ, {"ACL_PORT_REGISTRY_DIR": str(self.registry),
                                                     "ACL_CONFIG_DIR": str(self.anna)})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _serve(self, port: int, payload: dict | None) -> _Dashboard:
        dashboard = _Dashboard(port, payload)
        self.addCleanup(dashboard.close)
        return dashboard

    @staticmethod
    def _acl(config_dir: Path, version: str = "x") -> dict:
        return {"app": "ACL", "ok": True, "version": version, "config": config_identity(config_dir)}


class LauncherOnASharedMachineTests(_SharedMachine):
    """Лаунчер против настоящих серверов на настоящих портах.

    Ревью ACL-1309: прежние тесты лаунчера подменяли `find_own` и на чистой
    машине зеленели и с возвращённой исходной ошибкой. Здесь ничего из
    `local_dashboard` не подменено: «чей это дешборд» решает сеть.
    """

    def _launch(self):
        """Запуск лаунчера; «сервер», который он поднимает, — наш стаб с
        отпечатком Анны на том порту, который лаунчер ему назвал."""
        started: list[int] = []

        def popen(command, **_):
            port = int(command[command.index("--port") + 1])
            started.append(port)
            self._serve(port, self._acl(self.anna))
            process = mock.Mock()
            process.poll.return_value = None
            return process

        with mock.patch.object(launcher.subprocess, "Popen", side_effect=popen), \
                mock.patch.object(launcher.webbrowser, "open") as browser_open, \
                mock.patch.object(launcher, "_show_message") as show_message, \
                mock.patch.object(launcher.time, "sleep"):
            result = launcher._launch_normal()
        return result, started, browser_open, show_message

    def test_someone_elses_dashboard_is_left_alone_and_mine_starts_beside_it(self):
        """Ровно 24.09: на первом порту работал дешборд владельца, и лаунчер
        второго человека открыл его как свой. Теперь свой встаёт рядом."""
        self._serve(self.ports[0], self._acl(self.dan))

        result, started, browser_open, _ = self._launch()

        self.assertEqual(result, 0)
        self.assertEqual(started, [self.ports[1]], "свой дешборд не поднят рядом с чужим")
        browser_open.assert_called_once_with(f"http://127.0.0.1:{self.ports[1]}/")
        self.assertEqual(local_dashboard.health(self.ports[0]), self._acl(self.dan),
                         "чужой дешборд тронут")

    def test_my_dashboard_is_opened_where_it_actually_runs(self):
        self._serve(self.ports[0], self._acl(self.dan))
        self._serve(self.ports[2], self._acl(self.anna))

        result, started, browser_open, _ = self._launch()

        self.assertEqual(result, 0)
        self.assertEqual(started, [], "второй сервер при уже работающем своём")
        browser_open.assert_called_once_with(f"http://127.0.0.1:{self.ports[2]}/")

    def test_clicking_the_shortcut_twice_opens_the_one_dashboard(self):
        result, started, browser_open, _ = self._launch()
        self.assertEqual((result, started), (0, [self.ports[0]]))

        result, started, browser_open, _ = self._launch()

        self.assertEqual(result, 0)
        self.assertEqual(started, [], "второй щелчок поднял второй сервер")
        browser_open.assert_called_once_with(f"http://127.0.0.1:{self.ports[0]}/")

    def test_an_unrelated_program_on_the_first_port_is_stepped_over(self):
        self._serve(self.ports[0], None)

        result, started, browser_open, _ = self._launch()

        self.assertEqual(result, 0)
        self.assertEqual(started, [self.ports[1]])

    def test_every_port_taken_is_an_error_not_a_takeover(self):
        for port in self.ports:
            self._serve(port, None)

        result, started, browser_open, show_message = self._launch()

        self.assertEqual(result, 2)
        self.assertEqual(started, [])
        self.assertTrue(show_message.call_args.kwargs["error"])
        browser_open.assert_not_called()


class SetupOnASharedMachineTests(_SharedMachine):
    """Мастер установки: чужой дешборд на порту не пропускает и не запрещает
    настройку. Утром 24.09 свежая установка владельца молча открыла уже
    работающий дешборд вместо мастера, а второму человеку настройка была
    запрещена: «закройте дешборд», которого у него нет."""

    def _first_launch(self):
        with mock.patch.object(sys, "argv", ["ACL.exe"]), \
                mock.patch("setup_wizard.setup_is_complete", return_value=False), \
                mock.patch.object(launcher, "_run_setup", return_value=False) as run_setup, \
                mock.patch.object(launcher, "_launch_normal", return_value=0) as launch:
            launcher.main()
        return run_setup, launch

    def test_someone_elses_dashboard_does_not_skip_my_setup(self):
        self._serve(self.ports[0], self._acl(self.dan))
        run_setup, _ = self._first_launch()
        run_setup.assert_called_once()

    def test_an_empty_port_does_not_skip_my_setup(self):
        run_setup, _ = self._first_launch()
        run_setup.assert_called_once()

    def test_my_own_running_dashboard_is_opened_without_setup(self):
        self._serve(self.ports[1], self._acl(self.anna))
        run_setup, launch = self._first_launch()
        run_setup.assert_not_called()
        launch.assert_called_once()

    def _run_setup(self):
        import setup_wizard

        with mock.patch.object(launcher, "_acquire_server_mutex", return_value=True), \
                mock.patch.object(launcher, "_release_server_mutex"), \
                mock.patch.object(launcher, "_version_text", return_value="ACL test"), \
                mock.patch.object(launcher, "_show_message"), \
                mock.patch.object(setup_wizard, "run_setup_wizard", return_value=True) as wizard:
            return launcher._run_setup(), wizard

    def test_setup_runs_while_someone_elses_dashboard_holds_the_port(self):
        self._serve(self.ports[0], self._acl(self.dan))
        ok, wizard = self._run_setup()
        self.assertTrue(ok)
        wizard.assert_called_once()

    def test_setup_does_not_write_while_my_dashboard_runs(self):
        self._serve(self.ports[0], self._acl(self.anna))
        ok, wizard = self._run_setup()
        self.assertFalse(ok)
        wizard.assert_not_called()


class StickyPortTests(_SharedMachine):
    """Порт закреплён за человеком, а не за тем, кто раньше запустился.

    Ревью ACL-1309: «кто первым запустился, тот и на 9090». Задача прямо
    требует, чтобы человек знал свой адрес, а не находил дешборд каждый день
    на новом порту.
    """

    def test_the_first_launch_takes_the_first_free_port_and_keeps_it(self):
        self.assertEqual(local_dashboard.choose_port(self.anna), self.ports[0])
        self.assertEqual(local_dashboard.remembered_port(self.anna), self.ports[0])
        self.assertEqual(local_dashboard.claim_owner(self.ports[0]), config_identity(self.anna))

    def test_a_newcomer_does_not_take_the_port_of_someone_who_is_asleep(self):
        """Анна закрепила первый порт и выключила дешборд. Дан запускается
        впервые — первый порт свободен, но он Аннин."""
        local_dashboard.choose_port(self.anna)
        self.assertEqual(local_dashboard.choose_port(self.dan), self.ports[1])

    def test_whoever_starts_first_everyone_keeps_their_own_port(self):
        anna_port = local_dashboard.choose_port(self.anna)
        dan_port = local_dashboard.choose_port(self.dan)
        # Следующее утро: Дан включился раньше Анны — и встал на свой порт.
        self.assertEqual(local_dashboard.choose_port(self.dan), dan_port)
        self._serve(dan_port, self._acl(self.dan))
        self.assertEqual(local_dashboard.choose_port(self.anna), anna_port)
        self.assertEqual((anna_port, dan_port), (self.ports[0], self.ports[1]))

    def test_without_the_shared_registry_my_own_memory_keeps_my_port(self):
        """Реестр недоступен — человек всё равно помнит свой порт сам."""
        with mock.patch.dict(os.environ, {"ACL_PORT_REGISTRY_DIR": ""}):
            (self.anna / local_dashboard.MEMORY_FILE).write_text(
                json.dumps({"port": self.ports[2]}), encoding="utf-8")
            self.assertEqual(local_dashboard.choose_port(self.anna), self.ports[2])

    def test_my_port_held_by_another_program_is_a_detour_not_a_move(self):
        """Мой порт занят посторонней программой: на этот раз встаю рядом, но
        закреплённый порт остаётся моим — и я на него вернусь."""
        local_dashboard.choose_port(self.anna)
        local_dashboard.choose_port(self.dan)
        squatter = self._serve(self.ports[0], None)

        self.assertEqual(local_dashboard.choose_port(self.anna), self.ports[2],
                         "встал на порт Дана или на занятый")
        self.assertEqual(local_dashboard.remembered_port(self.anna), self.ports[0])
        squatter.close()
        self.assertEqual(local_dashboard.choose_port(self.anna), self.ports[0])

    def test_a_neighbours_mark_is_never_overwritten(self):
        """Когда встать больше некуда, можно постоять на месте спящего соседа,
        но отметка в реестре остаётся его."""
        local_dashboard.choose_port(self.dan)  # Дан — первый порт
        for port in self.ports[1:]:
            self._serve(port, None)
        self.assertEqual(local_dashboard.choose_port(self.anna), self.ports[0])
        self.assertEqual(local_dashboard.claim_owner(self.ports[0]), config_identity(self.dan))

    def test_the_wizard_names_the_port_the_launcher_will_take_and_claims_nothing(self):
        import setup_wizard

        local_dashboard.choose_port(self.dan)  # первый порт закреплён за Даном
        checks = {c.label: c for c in setup_wizard.system_readiness(self.anna)}

        check = checks["Local dashboard port"]
        self.assertTrue(check.ok, check.detail)
        self.assertIn(str(self.ports[1]), check.detail)
        self.assertIsNone(local_dashboard.remembered_port(self.anna),
                          "проверка мастера закрепила порт")
        self.assertEqual(local_dashboard.choose_port(self.anna), self.ports[1])

    def test_the_launcher_starts_my_dashboard_on_my_port(self):
        """Сквозная проверка: лаунчер Анны, первый порт закреплён за спящим
        Даном — Анна встаёт на второй, и завтра снова на второй."""
        local_dashboard.choose_port(self.dan)
        ports = []
        for _ in range(2):
            with mock.patch.object(launcher.subprocess, "Popen") as popen, \
                    mock.patch.object(launcher.local_dashboard, "find_own", return_value=None), \
                    mock.patch.object(launcher, "_show_message"), \
                    mock.patch.object(launcher.time, "monotonic", side_effect=[0, 10 ** 6]):
                launcher._launch_normal(open_browser=False)
            command = popen.call_args.args[0]
            ports.append(int(command[command.index("--port") + 1]))
        self.assertEqual(ports, [self.ports[1], self.ports[1]])


def _ipv6_listener(port: int, *, v6only: int) -> socket.socket | None:
    if not socket.has_ipv6:
        return None
    try:
        holder = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        holder.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, v6only)
        holder.bind(("::", port))
        holder.listen()
    except OSError:
        return None
    return holder


class DualStackTests(unittest.TestCase):
    """Ревью ACL-1309: слушатель на `[::]` проверке занятости не виден —
    привязка к одному `0.0.0.0` его не замечала, и мы встали бы поверх."""

    def _assert_seen(self, v6only: int):
        port = _free_ports(1)[0]
        holder = _ipv6_listener(port, v6only=v6only)
        if holder is None:
            self.skipTest("IPv6 недоступен на этой машине")
        self.addCleanup(holder.close)
        self.assertTrue(local_dashboard.port_in_use(port), "слушатель на [::] не виден")

    def test_a_dual_stack_listener_is_seen(self):
        self._assert_seen(v6only=0)

    def test_an_ipv6_only_listener_is_seen(self):
        self._assert_seen(v6only=1)

    def test_a_free_port_is_still_free(self):
        self.assertFalse(local_dashboard.port_in_use(_free_ports(1)[0]))


if __name__ == "__main__":
    unittest.main()
