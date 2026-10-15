# -*- coding: utf-8 -*-
"""Остановленный профиль не раскручивает робота вхолостую (ACL-1320).

Живой прогон c00d8c3, 26.09 15:51 UTC (Facebook_1443): профиль остановили,
пока робот ждал окно цели. Поток публикации застрял в
`_page_responsive` → `wait_for_function` (через `_tab_frozen` ←
`_loading_visible` ← `_wait_objective_dialog` ← `_open_campaign`) в
состоянии `active+gil`, и дешборд перестал отвечать: на ушедшей вкладке
каждый вызов Playwright падает за миллисекунды, проба отвечала «спросить не
удалось», и циклы ожидания шли на новый круг без паузы.

Здесь:

* настоящий Chromium, к которому подключились по CDP (как к профилю
  Multilogin), закрывают — проба, ожидание окна цели и открытие кампании
  возвращаются за секунды с «вкладки нет», а соседний поток всё это время
  получает процессор;
* «драйвер оборвался» (`Connection closed`) и «браузер отсоединён» без
  закрытой вкладки — то же самое, без обращений к странице после первого;
* страница, где все вызовы падают сразу, но вкладка не ушла, — круги
  ожидания окна цели не короче своего шага, а не тысячи в секунду;
* поиск свежей вкладки на отсоединённом браузере не крутится до таймаута.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from playwright.sync_api import sync_playwright

# ACL-1582: файл идёт только в Windows-задание CI; причина — в маркере.
pytestmark = pytest.mark.windows_only(reason="stops a real Chromium process tree with taskkill /T")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import browser as br  # noqa: E402
from src.fb_ads_rpa import meta_dom as md  # noqa: E402
from src.fb_ads_rpa import page_liveness  # noqa: E402

LIST_URL = "https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=1"


class _Counter:
    """Соседний поток на чистом Python — как цикл событий дешборда."""

    def __init__(self):
        self.n = 0
        self._stop = False
        self._t = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self._stop:
            self.n += 1

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *exc):
        self._stop = True
        self._t.join(2)

    def rate_over(self, fn):
        """Сколько шагов в секунду сделал поток, пока шёл `fn`, и сколько шёл `fn`."""
        n0, t0 = self.n, time.perf_counter()
        got = fn()
        dt = max(time.perf_counter() - t0, 1e-6)
        return got, (self.n - n0) / dt, dt

    def baseline(self, seconds=0.5):
        n0, t0 = self.n, time.perf_counter()
        time.sleep(seconds)
        return (self.n - n0) / (time.perf_counter() - t0)


def _devtools_port(udd: Path, proc, timeout_s: float = 60.0) -> int:
    """Порт отладки, который Chromium выбрал сам (`--remote-debugging-port=0`).

    ACL-1397: прежде порт брали заранее (bind на 0, закрыть, отдать браузеру).
    При параллельном прогоне между закрытием и запуском браузера этот порт
    успевал занять соседний воркер, и подключение падало с ECONNREFUSED (CI
    3.13, 30.09). Chromium пишет выбранный порт первой строкой файла
    `DevToolsActivePort` в своей папке профиля — гонки нет."""
    marker = udd / "DevToolsActivePort"
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            first = marker.read_text(encoding="utf-8").splitlines()[0].strip()
            if first.isdigit():
                return int(first)
        except (OSError, IndexError):
            pass
        if proc.poll() is not None:
            raise RuntimeError(f"Chromium exited with {proc.returncode} before opening DevTools")
        time.sleep(0.1)
    raise TimeoutError(f"no DevToolsActivePort in {udd} after {timeout_s:.0f} s")


def _kill(proc) -> None:
    # Весь процесс Chromium с детьми; ждать его выхода тесту не нужно.
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
    try:
        proc.wait(30)
    except subprocess.TimeoutExpired:
        pass


def _start_chromium(exe: str):
    """Chromium с отладкой по CDP: `(процесс, порт)`.

    ACL-1491: на нагруженном раннере Chromium однажды не написал
    `DevToolsActivePort` за 60 с. Это сбой запуска, а не поведения под
    проверкой, — один повтор с новой папкой профиля; второй отказ падает."""
    for attempt in (1, 2):
        udd = Path(tempfile.mkdtemp(prefix='acl1320_'))
        proc = subprocess.Popen(
            [exe, "--headless=new", "--remote-debugging-port=0",
             f"--user-data-dir={udd}", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            return proc, _devtools_port(udd, proc)
        except (TimeoutError, RuntimeError):
            _kill(proc)
            if attempt == 2:
                raise


@pytest.fixture
def stopped_profile_page():
    """Вкладка браузера, подключённого по CDP, после остановки самого браузера.

    Так робот видит профиль Multilogin, который остановили через MLX API или
    закрыли руками, после того как Playwright узнал о разрыве: соединение
    отвалилось (`is_connected()` — False), объект страницы остался у нас.
    Разрыв делаем со своей стороны (`close()` подключения по CDP) — замер
    26.09: убитый процесс браузера Playwright замечает только через ~45 с, а
    до того проба честно отвечает таймаутом (это «не отвечает», не спин).
    """
    with sync_playwright() as pw:
        exe = os.environ.get("ACL_BROWSER_EXECUTABLE") or pw.chromium.executable_path
        proc, port = _start_chromium(exe)
        try:
            attached = None
            deadline = time.time() + 20
            while attached is None:
                try:
                    attached = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
                except Exception:
                    if time.time() > deadline:
                        raise
                    time.sleep(0.2)
            # CI 28.09: на Windows-раннере подключение успевает раньше, чем
            # Chromium откроет свою первую вкладку, и pages[0] падал IndexError.
            # Ждём вкладку до 10 с, иначе открываем свою.
            ctx = attached.contexts[0]
            wait_until = time.time() + 10
            while not ctx.pages and time.time() < wait_until:
                time.sleep(0.1)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.set_content("<p>campaigns</p>")
            assert md._page_responsive(page, 3.0) is True
            attached.close()
            assert attached.is_connected() is False
            yield page
        finally:
            _kill(proc)


# ACL-1491: границы — доля медленного пути, а не абсолютные секунды. На
# остановленном профиле честный путь возвращается сразу (замер: проба —
# доли миллисекунды, окно цели — 0.8–1.0 с, открытие — 0.1–0.4 с), а
# сломанный ждёт весь свой таймаут: пробу — `PROBE_S`, окно цели — до
# `idle_s`, открытие — весь `CAMPAIGN_OPEN_BUDGET_S`. Таймауты взяты
# длиннее прежних, чтобы четверть каждого выдерживала нагруженный раннер.
PROBE_S = 10.0
IDLE_S = 24.0
OPEN_BUDGET_S = 30.0
# Окно замера скорости соседнего потока: короче — GIL мог ни разу не смениться.
MIN_RATE_WINDOW_S = 0.25


def test_the_probe_and_the_waits_on_a_stopped_profile_return_at_once(stopped_profile_page, monkeypatch):
    page = stopped_profile_page
    with _Counter() as counter:
        base = counter.baseline()
        got, rate, dt = counter.rate_over(lambda: md._page_responsive(page, PROBE_S))
        assert got is None
        assert dt < PROBE_S / 4, dt

        out: dict = {}
        found, rate, dt = counter.rate_over(lambda: md._wait_objective_dialog(
            page, first_s=IDLE_S + 6, idle_s=IDLE_S,
            loading_max_s=IDLE_S + 16, out=out))
        assert found is False
        assert out.get("outcome") == "gone", out
        assert dt < IDLE_S / 4, f"ожидание окна цели на остановленном профиле шло {dt:.1f} с"
        # Ревью PR #323 (F12): запас на шум полного прогона (0.49 против 0.5
        # однажды); горячий цикл отдаёт соседу меньше — главное здесь время.
        # ACL-1491: под нагрузкой раннера опорная скорость до вызова бывала
        # выше, чем стала к нему (соседний воркер поднимал свой Chromium).
        # Сравниваем с меньшей из опор до и после — при той же нагрузке.
        # ACL-1628: CI 06.10 (Windows) — `rate 0.0` при опоре 8.3 млн/с: ожидание
        # вернулось сразу (профиль уже признан ушедшим) и не отпустило GIL ни на
        # один квант, поэтому соседний поток не успел сделать ни шага. Это не
        # спин, а ровно то, что тест требует, — «вернулось сразу». Скорость
        # соседа осмысленна только на окне, где GIL успел бы смениться много
        # раз (переключение — 5 мс); короткий возврат доказан границей по
        # времени выше, а долгий спин с голодным соседом ловит эта проверка.
        if dt >= MIN_RATE_WINDOW_S:
            if rate <= 0.25 * base:
                base = min(base, counter.baseline())
            assert rate > 0.25 * base, (rate, base, dt)

        monkeypatch.setattr(md, "CAMPAIGN_OPEN_BUDGET_S", OPEN_BUDGET_S)
        pressed: list[str] = []
        opened, rate, dt = counter.rate_over(lambda: md._open_campaign(
            page, LIST_URL, press=lambda p, where: pressed.append(where) or False,
            shot=lambda label: None))
        assert opened == "browser_closed"
        assert pressed == [], "Create жали в ушедшую вкладку"
        assert dt < OPEN_BUDGET_S / 4, f"открытие кампании на остановленном профиле шло {dt:.1f} с"


class _DeadDriverPage:
    """Страница, у которой оборвался драйвер: всё падает «Connection closed»,
    а сама она об этом ещё не знает (`is_closed()` — False)."""

    def __init__(self, message="Connection closed"):
        self.calls = 0
        self.message = message
        self.url = LIST_URL
        self.context = SimpleNamespace(browser=SimpleNamespace(is_connected=lambda: True))

    def is_closed(self):
        return False

    def _fail(self, *a, **k):
        self.calls += 1
        raise RuntimeError(self.message)

    wait_for_function = evaluate = goto = reload = _fail

    def get_by_role(self, *a, **k):
        return SimpleNamespace(first=SimpleNamespace(wait_for=self._fail, is_visible=self._fail))

    get_by_text = get_by_role


def test_a_dead_driver_is_gone_after_the_first_call():
    page = _DeadDriverPage()
    t0 = time.time()
    assert md._page_responsive(page, 3.0) is None
    assert md._tab_gone(page) is True
    calls = page.calls
    assert md._page_responsive(page, 3.0) is None
    assert page.calls == calls, "ушедшую вкладку спросили ещё раз"
    out: dict = {}
    assert md._wait_objective_dialog(page, first_s=8.0, idle_s=6.0, loading_max_s=10.0, out=out) is False
    assert out["outcome"] == "gone"
    assert time.time() - t0 < 3.0


def test_a_disconnected_browser_is_gone_without_asking_the_page():
    page = _DeadDriverPage()
    page.context = SimpleNamespace(browser=SimpleNamespace(is_connected=lambda: False))
    assert md._tab_gone(page) is True
    assert md._page_responsive(page, 3.0) is None
    assert page.calls == 0


def test_a_mock_page_is_not_taken_for_a_gone_one():
    from unittest.mock import MagicMock

    assert page_liveness.tab_gone(MagicMock()) is False


class _FailsFastPage(_DeadDriverPage):
    """Вкладка на месте, но каждый вызов падает сразу — не «закрыто»."""

    def __init__(self):
        super().__init__(message="strict mode violation")


def test_a_poll_round_is_never_shorter_than_its_step():
    page = _FailsFastPage()
    polls = {"n": 0}

    def wait_for(*a, **k):
        polls["n"] += 1
        raise RuntimeError("strict mode violation")

    page.get_by_role = lambda *a, **k: SimpleNamespace(first=SimpleNamespace(wait_for=wait_for, is_visible=page._fail))
    with _Counter() as counter:
        base = counter.baseline()
        found, rate, dt = counter.rate_over(lambda: md._wait_objective_dialog(
            page, first_s=3.0, idle_s=2.0, loading_max_s=4.0, poll_s=0.5))
    assert found is False
    assert dt >= 1.5, dt
    assert polls["n"] <= dt / 0.5 + 2, f"{polls['n']} кругов опроса за {dt:.1f} с при шаге 0.5 с"
    # Главная проверка — число кругов выше; доля процессора — с запасом на шум.
    assert rate > 0.5 * base, f"соседний поток получил {rate / base:.0%} процессора"


def test_a_fresh_tab_on_a_disconnected_browser_does_not_spin():
    waits = {"n": 0}

    def wait_for_event(*a, **k):
        waits["n"] += 1
        raise RuntimeError("Target page, context or browser has been closed")

    connected = {"v": True}
    session = SimpleNamespace(send=lambda *a, **k: {"targetId": "T1"}, detach=lambda: None)
    browser = SimpleNamespace(is_connected=lambda: connected["v"], new_browser_cdp_session=lambda: session)
    context = SimpleNamespace(pages=[], browser=browser, wait_for_event=wait_for_event)
    t0 = time.time()
    assert br._open_tab_via_browser(context, timeout_s=2.0) is None
    assert waits["n"] <= 2.0 / 0.5 + 2, waits["n"]
    assert time.time() - t0 < 3.5
    connected["v"] = False
    waits["n"] = 0
    assert br._open_tab_via_browser(context, timeout_s=2.0) is None
    assert waits["n"] == 0


def test_campaign_open_rounds_that_fail_at_once_are_paced(monkeypatch):
    """Ревью PR #323 (F12, мутация M3): раунд открытия, в котором всё упало
    сразу (нажатие не взялось, вкладка на месте), не начинает следующий без
    паузы. Поддельные часы: без `_pace` время не идёт, и раунды крутятся."""
    from tests.test_a_campaign_opens_within_two_minutes import _Clock, _P

    clock = _Clock()
    monkeypatch.setattr(md, "time", SimpleNamespace(time=clock.time, sleep=clock.sleep, monotonic=clock.time))
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(md._telemetry, "record", lambda *a, **k: None)
    monkeypatch.setattr(md, "_tab_frozen", lambda p: False)
    monkeypatch.setattr(md, "_clear_blocking_overlays", lambda p, where, **kw: [])
    monkeypatch.setattr(md, "_open_list_again", lambda p, url, deadline: None)
    monkeypatch.setattr(md, "_profile_trouble", lambda p, **kw: "")
    monkeypatch.setattr(br, "wake_page_quietly", lambda p: {})
    monkeypatch.setattr(br, "fresh_tab", lambda p, url, **kw: _P("new"))
    presses = {"n": 0}

    def press(p, where):
        presses["n"] += 1
        if presses["n"] > 500:
            raise AssertionError("раунды открытия крутятся без паузы")
        return False

    got = md._open_campaign(_P("old"), LIST_URL, press=press, shot=lambda label: None)
    assert got == "create"
    assert presses["n"] <= md.CAMPAIGN_OPEN_BUDGET_S / md.CAMPAIGN_OPEN_MIN_ROUND_S + 2, presses["n"]
