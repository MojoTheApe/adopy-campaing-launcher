# -*- coding: utf-8 -*-
"""Нажатие Create не попадает в левое меню Ads Manager (ACL-1313, прогон 25.09).

Живой прогон 25.09: два задания из трёх потеряли по 15–25 минут на переходе
Create → окно цели. На скриншотах не «долгая загрузка», а промах: путь
курсора шёл слева (запасная точка x−120…260 или трекер, стоявший в колонке
навигации), меню Ads Manager раскрывалось на наведение поверх кнопки Create
(x≈120), и нажатие открывало Audiences (887b2d) или ничего (8814).

Проверяется:

* `_mouse_path` подходит к цели справа, если трекера нет или курсор стоит в
  колонке навигации, а цель — нет; принимает `start=`;
* `_cdp_mouse_click` в офлайн-Chrome: меню, которое раскрывается поверх
  кнопки при наведении, замечено (`_hit_mark`/`_hit_changed`), курсор уходит,
  ждёт, пока меню свернётся, и нажимает саму кнопку; пишется
  `click_target_covered`;
* `_create_took` за секунды отличает ok / left_list / nothing;
* `_wait_objective_dialog` не ждёт загрузку, когда Meta пишет «Turn off ad
  blocker» или «internet connection was lost».

Меню здесь синтетическое: снимков страницы с раскрытым меню нет, модель
построена по описанию скриншотов 887b2d и 8814 из коммита dee25ec.
"""
from __future__ import annotations

import json
import os
import random
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import meta_dom as md  # noqa: E402
from src.fb_ads_rpa import run_telemetry  # noqa: E402


# ── путь курсора ──────────────────────────────────────────────────────────

class _TrackerPage:
    """Страница, у которой трекер курсора (pacing) видел его в `last`."""

    def __init__(self, last=None):
        self.last = last

    def evaluate(self, js, *a):
        return self.last


CREATE = (120.0, 100.0)   # кнопка Create на списке кампаний, x≈120


@pytest.mark.parametrize("seed", range(10))
def test_without_a_tracker_the_pointer_comes_from_the_right(seed):
    random.seed(seed)
    pts = md._mouse_path(_TrackerPage(None), *CREATE)
    assert pts[0][0] > CREATE[0], pts[0]
    assert all(x >= md._NAV_RAIL_X for x, _y in pts), "путь зашёл в колонку меню"
    assert pts[-1] == CREATE


@pytest.mark.parametrize("seed", range(10))
def test_a_pointer_parked_in_the_nav_rail_starts_from_the_right(seed):
    random.seed(seed)
    pts = md._mouse_path(_TrackerPage({"x": 30, "y": 400}), *CREATE)
    assert pts[0][0] > CREATE[0], pts[0]
    assert all(x >= md._NAV_RAIL_X for x, _y in pts)


def test_a_pointer_elsewhere_starts_where_the_tracker_saw_it():
    random.seed(1)
    pts = md._mouse_path(_TrackerPage({"x": 300, "y": 400}), 700.0, 400.0)
    assert pts[0][0] < 700.0 and pts[0][0] < 420, "путь начат не от курсора"
    # Цель сама в колонке меню — начинать справа незачем.
    pts = md._mouse_path(_TrackerPage({"x": 20, "y": 300}), 60.0, 300.0)
    assert pts[0][0] < 60.0


def test_an_explicit_start_wins_over_the_tracker():
    random.seed(2)
    pts = md._mouse_path(_TrackerPage({"x": 30, "y": 400}), 120.0, 100.0, start=(900.0, 100.0))
    assert pts[0][0] > 700.0
    assert pts[-1] == (120.0, 100.0)


# ── офлайн-Chrome ─────────────────────────────────────────────────────────

# Кнопка Create на x≈120. Первое наведение на неё раскрывает панель меню
# шириной 300 px поверх кнопки (пункт «Audiences» ложится ровно на неё);
# когда курсор уходит с панели, она сворачивается через 200 мс. Как вживую:
# меню снова раскрылось бы только от колонки навигации, куда путь больше не
# заходит, — поэтому второе наведение его не открывает.
_LIST_PAGE = """
<!doctype html><html><body style="margin:0">
<div id="rail" style="position:fixed;left:0;top:0;width:60px;height:100%;background:#123"></div>
<div id="create" role="button" style="position:absolute;left:90px;top:80px;width:64px;height:40px;background:#07f"
     onclick="window.__clicked.push('create')">Create</div>
<div id="menu" style="display:none;position:fixed;left:0;top:0;width:300px;height:100%;z-index:10;background:#eee">
  <div id="audiences" style="height:100%" onclick="window.__clicked.push('audiences')">Audiences</div>
</div>
<script>
  window.__clicked = [];
  window.__opened = 0;
  const menu = document.getElementById('menu');
  document.getElementById('create').addEventListener('mouseover', () => {
    if (window.__opened) return;
    window.__opened++;
    menu.style.display = 'block';
  });
  menu.addEventListener('mouseleave', () => setTimeout(() => { menu.style.display = 'none'; }, 200));
</script>
</body></html>
"""


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        try:
            yield b
        finally:
            b.close()


@pytest.fixture
def page(browser, monkeypatch, tmp_path):
    # Телеметрия — во временный файл, не в живой output/.
    monkeypatch.setattr(run_telemetry, "TELEMETRY_PATH", tmp_path / "telemetry.jsonl")
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    p = ctx.new_page()
    p.route("**/*", lambda route: route.abort())
    try:
        yield p
    finally:
        ctx.close()


def _rows(tmp_path, step):
    f = tmp_path / "telemetry.jsonl"
    if not f.exists():
        return []
    return [r for r in (json.loads(line) for line in f.read_text(encoding="utf-8").splitlines() if line.strip())
            if r["step"] == step]


def _center(page, sel):
    b = page.locator(sel).bounding_box()
    return b["x"] + b["width"] / 2, b["y"] + b["height"] / 2


def test_a_menu_that_opens_over_the_button_is_waited_out_and_create_is_pressed(page, tmp_path):
    """Ровно промах 887b2d: меню раскрылось поверх Create по пути курсора."""
    page.set_content(_LIST_PAGE)
    x, y = _center(page, "#create")
    md._cdp_mouse_click(page, x, y)
    assert page.evaluate("window.__opened") == 1, "меню в модели не раскрывалось — тест ничего не проверяет"
    assert page.evaluate("window.__clicked") == ["create"]
    rows = _rows(tmp_path, "click_target_covered")
    assert rows and rows[-1]["ok"] is True
    assert rows[-1]["diag"] == {"x": round(x), "y": round(y)}


def test_an_uncovered_button_is_pressed_without_a_detour(page, tmp_path):
    page.set_content(_LIST_PAGE)
    page.evaluate("window.__opened = 1")   # меню не раскроется
    x, y = _center(page, "#create")
    md._cdp_mouse_click(page, x, y)
    assert page.evaluate("window.__clicked") == ["create"]
    assert _rows(tmp_path, "click_target_covered") == []


def test_hover_highlight_inside_the_target_is_not_a_cover(page):
    """Подсветка кнопки при наведении (потомок/предок той же точки) — не подмена."""
    page.set_content("<div id='b' style='width:200px;height:60px'><span id='s'"
                     " style='display:none;width:200px;height:60px'>hover</span></div>")
    md._hit_mark(page, 50, 20)
    page.evaluate("document.getElementById('s').style.display = 'block'")
    assert md._hit_changed(page, 50, 20) is False
    page.evaluate("""() => { const o = document.createElement('div');
      o.style = 'position:fixed;inset:0;z-index:5'; document.body.appendChild(o); }""")
    assert md._hit_changed(page, 50, 20) is True


def test_network_trouble_is_read_from_the_page(page):
    page.set_content("<div>Your internet connection was lost.</div>")
    assert md._network_trouble(page) is True
    page.set_content("<div>Turn off ad blocker</div>")
    assert md._network_trouble(page) is True
    page.set_content("<div>Loading creation</div>")
    assert md._network_trouble(page) is False


# ── _create_took ──────────────────────────────────────────────────────────

LIST_URL = "https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=1&nav_source=x"


class _FakePage:
    def __init__(self, url=LIST_URL, radio=False):
        self.url = url
        self.radio = radio

    def get_by_role(self, role, name=None, **kw):
        # ACL-1320: радио ждут `wait_for` с таймаутом драйвера — у
        # `is_visible` таймаут не соблюдается.
        def wait_for(state="visible", timeout=0):
            if not self.radio:
                raise TimeoutError(f"Timeout {timeout}ms exceeded")

        return SimpleNamespace(first=SimpleNamespace(is_visible=lambda timeout=0: self.radio,
                                                     wait_for=wait_for))


@pytest.fixture
def quick(monkeypatch):
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    state = {"loading": False}
    monkeypatch.setattr(md, "_loading_visible", lambda p: state["loading"])
    return state


def test_the_objective_dialog_means_create_took(quick):
    assert md._create_took(_FakePage(radio=True), LIST_URL, timeout_s=0.2) == "ok"


def test_a_loading_modal_means_create_took(quick):
    quick["loading"] = True
    assert md._create_took(_FakePage(), LIST_URL, timeout_s=0.2) == "ok"


def test_a_different_page_means_the_press_left_the_list(quick):
    page = _FakePage("https://adsmanager.facebook.com/adsmanager/audiences?act=1")
    assert md._create_took(page, LIST_URL, timeout_s=0.2) == "left_list"


def test_a_changed_query_is_still_the_list(quick):
    page = _FakePage("https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=1&other=2")
    assert md._create_took(page, LIST_URL, timeout_s=0.2) == "nothing"


def test_nothing_on_screen_is_reported_after_the_short_timeout(quick):
    import time
    t0 = time.time()
    assert md._create_took(_FakePage(), LIST_URL, timeout_s=0.3) == "nothing"
    assert time.time() - t0 < 3


# ── _wait_objective_dialog и сеть ─────────────────────────────────────────

class _Clock:
    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now


class _Radio:
    def __init__(self, clock):
        self.clock = clock
        self.waits = []

    def wait_for(self, state="visible", timeout=30000):
        self.waits.append(timeout)
        self.clock.now += timeout / 1000
        raise TimeoutError("never")


class _ObjectivePage:
    def __init__(self, radio, banner=""):
        self.radio = radio
        self.banner = banner

    def get_by_role(self, role, name=None, **kw):
        return SimpleNamespace(first=self.radio)

    def get_by_text(self, pattern, **kw):
        hit = bool(self.banner) and bool(re.search(pattern, self.banner))
        return SimpleNamespace(first=SimpleNamespace(is_visible=lambda timeout=0: hit))


@pytest.mark.parametrize("banner", ["Turn off ad blocker", "Your internet connection was lost."])
def test_network_trouble_ends_the_loading_wait(monkeypatch, banner):
    clock = _Clock()
    monkeypatch.setattr(md, "time", SimpleNamespace(time=clock.time, sleep=lambda s: None))
    monkeypatch.setattr(md, "_loading_visible", lambda p: True)
    radio = _Radio(clock)
    assert md._wait_objective_dialog(_ObjectivePage(radio, banner)) is False
    assert len(radio.waits) == 1, "с разорванной сетью ждали загрузку дальше"


def test_without_network_trouble_the_loading_wait_goes_on(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(md, "time", SimpleNamespace(time=clock.time, sleep=lambda s: None))
    monkeypatch.setattr(md, "_loading_visible", lambda p: True)
    radio = _Radio(clock)
    assert md._wait_objective_dialog(_ObjectivePage(radio, "Loading creation")) is False
    assert len(radio.waits) > 1
