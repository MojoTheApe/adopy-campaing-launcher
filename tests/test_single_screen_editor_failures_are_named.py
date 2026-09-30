# -*- coding: utf-8 -*-
"""Отказы нового редактора Meta 28.09 названы и ограничены по времени (ACL-1354).

Живые прогоны 28.09 (1443, 9943; редактор `/adsmanager/manage/simple_create`):

* 9d113c3d6ac3, 0149090ef2c7 — после Create 120 с ждали модалку «Loading...»
  самой таблицы списка (`data-surface="/am/table/lib:ads_progress_dialog_modal_…"`),
  а не окна Create (`/am/lib:…`, «Loading creation»); окно цели не пришло, и
  отказ `objective_leads` был без причины. 279b9539df43 — в конце на экране
  «Turn off ad blocker» Meta.
* aa1e2fbf45c9 — `languages` и `targeting_open` без причины. Detailed
  targeting: строка раскрыта, в поле — `role=progressbar` «Loading...», а
  шаг жал строку снова (сворачивал). Languages: поле пустое, список
  подсказок так и не появился — причина по снимку не видна, теперь шаг
  называет её и снимает разметку до очистки поля.
* 9d3e41a0b373 — вкладку закрыли на Locations; шесть отказов подряд молча и
  остановка `simple_create_layout: ad-section` (`error:TargetClosedError`).
* a71e4fc14aa1 — шаг креатива 14:31–16:25 UTC: страница не читалась, а
  проверка медиа при каждой ошибке чтения шла на следующий круг до потолка
  `_UPLOAD_MAX_S` (20 мин) — дважды подряд видно по телеметрии (15:44:49 →
  16:05:06 → 16:25:09); у шага целиком срока не было.

Вырезки: tests/fixtures/ads_manager/objective_open_2026_09_28.html,
audience_fields_simple_create_2026_09_28.html (см. README). Разметка без JS
Meta: раскрытие строки, догрузка поля и подсказки эмулирует тест.
"""
from __future__ import annotations

import os
import re
import sys
import time as _real_time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import meta_dom as md  # noqa: E402
from src.fb_ads_rpa import page_capture  # noqa: E402
from src.fb_ads_rpa import run_telemetry as rt  # noqa: E402
from src.fb_ads_rpa import simple_create as sc  # noqa: E402
from tests.test_create_quick_retry_returns_to_list import flow  # noqa: E402,F401
from tests.test_the_single_screen_editor_flow import sim  # noqa: E402,F401

FIX = ROOT / "tests" / "fixtures" / "ads_manager"


def _fragments(name: str) -> dict[str, str]:
    text = (FIX / name).read_text(encoding="utf-8")
    return {m.group(1): m.group(2) for m in re.finditer(
        r"<!-- fragment: (\w+) \| source: [^>]*-->\n(.*?)(?=\n<!-- fragment: |\Z)", text, re.S)}


OBJ = _fragments("objective_open_2026_09_28.html")
AUD = _fragments("audience_fields_simple_create_2026_09_28.html")


def _wrap(fragment: str) -> str:
    return f"<!doctype html><html><body><div style='width:600px'>{fragment}</div></body></html>"


@pytest.fixture(autouse=True)
def _fresh_creative_step():
    """Причина и фазы шага креатива живут в потоке — не переносить между тестами."""
    tls = getattr(md, "_creative_tls", SimpleNamespace())
    for name, value in (("why", ""), ("phases", []), ("deadline", None)):
        setattr(tls, name, value)
    yield
    for name, value in (("why", ""), ("phases", []), ("deadline", None)):
        setattr(tls, name, value)


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        try:
            yield b
        finally:
            b.close()


@pytest.fixture
def rows(monkeypatch):
    got: list[tuple] = []
    monkeypatch.setattr(md._telemetry, "record",
                        lambda step, ok=None, **k: got.append((step, ok, k.get("note", ""), k)))
    return got


@pytest.fixture
def page(browser, monkeypatch, rows):
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(md, "_stall_evidence", lambda *a, **k: None)
    monkeypatch.setattr(page_capture, "capture", lambda *a, **k: None)
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    p = ctx.new_page()
    p.route("**/*", lambda route: route.abort())
    try:
        yield p
    finally:
        ctx.close()


# ══ окно цели: модалка таблицы — не загрузка Create ═══════════════════════

def test_the_table_modal_is_told_from_the_create_modal(page):
    page.set_content(_wrap(OBJ["table_loading"]))
    assert md._table_loading(page) is True
    assert md._loading_kind(page) == "table:Loading..."
    page.set_content(_wrap(OBJ["create_loading"]))
    assert md._table_loading(page) is False
    assert md._loading_kind(page) == "text:Loading creation"


def test_a_create_under_the_table_modal_did_not_take(page):
    """9d113c3d6ac3: `create_took ok` — по «Loading...» таблицы."""
    page.set_content(_wrap(OBJ["table_loading"]))
    assert md._create_took(page, "", timeout_s=1.5) == "table_loading"
    page.set_content(_wrap(OBJ["create_loading"]))
    assert md._create_took(page, "", timeout_s=1.5) == "ok"


def test_the_wait_on_the_table_modal_ends_as_idle_not_at_the_ceiling(page, rows):
    """Прежде — 120 с до `loading_ceiling`; теперь раунд кончается через idle_s."""
    page.set_content(_wrap(OBJ["table_loading"]))
    out: dict = {}
    start = _real_time.time()
    assert md._wait_objective_dialog(page, first_s=3.0, idle_s=2.0, loading_max_s=60.0,
                                     poll_s=0.5, out=out) is False
    assert out["outcome"] == "table_loading"
    assert _real_time.time() - start < 40.0, "ожидание досидело до потолка"
    wait = [r for r in rows if r[0] == "objective_wait"][-1]
    assert wait[3]["diag"]["table_polls"] >= 1
    assert wait[3]["diag"]["loading_polls"] == 0


def test_the_missing_objective_dialog_is_named_by_the_screen(page, monkeypatch):
    page.set_content(_wrap(OBJ["ad_blocker"]))
    assert md._objective_missing_reason(page) == "network_trouble"
    page.set_content(_wrap(OBJ["table_loading"]))
    assert md._objective_missing_reason(page) == "table_loading"
    page.set_content(_wrap("<p>Campaigns</p>"))
    monkeypatch.setattr(md._LAST_OPEN, "outcome", "loading_ceiling", raising=False)
    assert md._objective_missing_reason(page) == "no_objective_dialog:loading_ceiling"


def test_the_objective_leads_stop_carries_its_reason(flow, monkeypatch):
    """Прежде `objective_leads` уходил с пустой причиной у всех трёх прогонов 9943."""
    got: list[tuple] = []
    monkeypatch.setattr(rt, "record", lambda step, ok=None, **k: got.append((step, ok, k.get("note", ""))))

    def open_campaign(*a, **k):
        md._LAST_OPEN.outcome = "table_loading"
        return "objective_leads"

    monkeypatch.setattr(md, "_open_campaign", open_campaign)
    monkeypatch.setattr(md, "_network_trouble", lambda p: False)
    monkeypatch.setattr(md, "_table_loading", lambda p: False)
    absent = MagicMock()
    absent.first = absent
    absent.count.return_value = 0
    absent.is_visible.return_value = False
    absent.wait_for.side_effect = TimeoutError("absent")
    flow["radio_absent"].update(on=True, loc=absent)
    assert flow["run"]() == "objective_leads"
    assert ("objective_leads", False, "table_loading") in got


# ══ Languages: причина вместо молчания ════════════════════════════════════

def test_languages_without_suggestions_say_so_and_leave_the_markup(page, rows, monkeypatch):
    """aa1e2fbf45c9/02_languages: поле пустое, `aria-expanded=false`, списка нет."""
    shots: list[dict] = []
    monkeypatch.setattr(page_capture, "capture", lambda p, **k: shots.append(k) or "saved")
    page.set_content(_wrap(AUD["languages_open_empty"]))
    report: dict = {}
    assert md.fill_audience_languages(page, ["Spanish"], report=report) is False
    assert report["why"] == "no_suggestion:Spanish"
    pick = [r for r in rows if r[0] == "languages_pick"]
    assert pick and pick[-1][1] is False and pick[-1][2] == "no_suggestion"
    seen = pick[-1][3]["diag"]
    assert seen["typed"] == "Spanish" and seen["listbox"] is False and seen["option_count"] == 0
    assert [s["step"] for s in shots] == ["languages_no_suggestion"]
    # След за собой не оставляем: поле очищено после снимка.
    assert page.locator("input[placeholder='Search languages']").input_value() == ""


def test_the_languages_stop_row_carries_the_reason(sim, monkeypatch):
    def fill(p, names, report=None):
        if report is not None:
            report["why"] = "no_suggestion:Spanish"
        return False

    monkeypatch.setattr(md, "fill_audience_languages", fill)
    sim["run"](language_names=["Spanish"])
    assert ("languages", False, "no_suggestion:Spanish") in sim["rows"]


# ══ Detailed targeting: раскрытое грузящееся поле ждём, а не сворачиваем ═══

_TOGGLE_JS = """
(loaded) => {
  const row = document.querySelector('[role=button][aria-expanded]');
  const region = document.querySelector('[role=region]');
  const loading = region.innerHTML;
  let timer = null;
  window.__clicks = 0;
  const startLoad = () => {
    clearTimeout(timer);
    region.innerHTML = loading;
    timer = setTimeout(() => { region.innerHTML = loaded; }, window.__loadMs);
  };
  window.__finish = () => { clearTimeout(timer); region.innerHTML = loaded; };
  row.addEventListener('click', () => {
    window.__clicks += 1;
    const open = row.getAttribute('aria-expanded') === 'true';
    row.setAttribute('aria-expanded', String(!open));
    region.hidden = open;
    if (open) clearTimeout(timer); else startLoad();
  });
  startLoad();
}
"""
"""Эмуляция (не Meta): клик по строке сворачивает/раскрывает её; раскрытое
поле догружается за `__loadMs` (свёрнутое — не догружается, и новое
раскрытие начинает загрузку заново)."""


def _targeting_page(page, load_ms: int) -> None:
    page.set_content(_wrap(AUD["targeting_loading"]))
    page.evaluate(f"window.__loadMs = {load_ms}")
    page.evaluate(_TOGGLE_JS, AUD["targeting_search_loaded"])


def test_a_loading_targeting_field_is_waited_for_not_collapsed(page, monkeypatch):
    monkeypatch.setattr(md, "_sleep_h", lambda base, spread=0.3: _real_time.sleep(base))
    _targeting_page(page, 4000)
    assert md.open_detailed_targeting_field(page) is True
    assert page.evaluate("window.__clicks") == 0, "раскрытую строку жали — она свернулась"
    assert page.locator("input[placeholder='Add demographics, interests or behaviors']").is_visible()


def test_the_field_is_waited_for_beyond_the_three_short_tries(page, monkeypatch):
    """Поле догружается дольше трёх заходов по 15 опросов — его ждут, пока оно
    грузится (до TARGETING_FIELD_LOAD_S), а не сдаются после заходов."""
    _targeting_page(page, 10 ** 9)
    real = md._targeting_row_state
    seen = {"n": 0}

    def row_state(p):
        seen["n"] += 1
        if seen["n"] == 12:          # догрузилось, пока шаг ждал
            page.evaluate("window.__finish()")
        return real(p)

    monkeypatch.setattr(md, "_targeting_row_state", row_state)
    assert md.open_detailed_targeting_field(page) is True
    assert page.evaluate("window.__clicks") == 0, "раскрытую строку жали — она свернулась"
    assert page.locator("input[placeholder='Add demographics, interests or behaviors']").is_visible()


def test_a_field_that_never_loads_is_named(page, monkeypatch):
    monkeypatch.setattr(md, "TARGETING_FIELD_LOAD_S", 0.5)
    _targeting_page(page, 10 ** 9)
    report: dict = {}
    assert md.open_detailed_targeting_field(page, report=report) is False
    assert report["why"] == "field_loading"


def test_the_targeting_stop_row_carries_the_reason(sim, monkeypatch):
    def open_field(p, report=None):
        if report is not None:
            report["why"] = "field_loading"
        return False

    monkeypatch.setattr(md, "open_detailed_targeting_field", open_field)
    sim["run"](targeting_label="Forex")
    assert ("targeting_open", False, "field_loading") in sim["rows"]


# ══ закрытая вкладка: остановка browser_closed, а не шаги вслепую ══════════

class TargetClosedError(Exception):
    """Как у Playwright: класс по имени (page_liveness.is_gone_error)."""


def test_the_level_scroll_on_a_closed_tab_returns_at_once(monkeypatch):
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    calls = {"n": 0}

    def closed(*a, **k):
        calls["n"] += 1
        raise TargetClosedError("Target page, context or browser has been closed")

    monkeypatch.setattr(md, "_eval_bounded", closed)
    fake = SimpleNamespace(get_by_placeholder=lambda s: SimpleNamespace(first=None))
    got = sc.scroll_to_level(fake, "ad", budget_s=5.0)
    assert got == {"ok": False, "why": "page_closed"}
    assert calls["n"] == 1


def test_a_closed_tab_at_the_ad_section_is_browser_closed(sim, monkeypatch):
    """9d3e41a0b373: `simple_create_layout: ad-section` при закрытой вкладке."""
    monkeypatch.setattr(sc, "scroll_to_level",
                        lambda p, level, **kw: {"ok": True} if level == "adset"
                        else {"ok": False, "why": "page_closed"})
    assert sim["run"]() == "browser_closed"
    assert ("browser_closed", False, "page_closed") in sim["rows"]
    assert not [r for r in sim["rows"] if r[0] == "simple_create_layout"]


def test_a_tab_closed_on_locations_stops_before_languages(sim, monkeypatch):
    page = sim["page"]
    languages: list = []

    def location(p, country):
        page.is_closed.return_value = True   # вкладку закрыли на этом шаге
        return False

    monkeypatch.setattr(md, "fill_audience_location", location)
    monkeypatch.setattr(md, "fill_audience_languages",
                        lambda p, names, report=None: languages.append(names) or True)
    assert sim["run"](country="Colombia", language_names=["Spanish"]) == "browser_closed"
    assert languages == [], "после закрытия вкладки шли дальше по аудитории"
    assert ("audience_location", False, "page_closed") in sim["rows"]


# ══ креатив: нечитаемая вкладка и срок шага ═══════════════════════════════

class _Clock:
    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now

    def sleep(self, s):
        self.now += s


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(md, "time", SimpleNamespace(time=c.time, sleep=c.sleep))
    monkeypatch.setattr(md, "_sleep_h", lambda base, spread=0.3: c.sleep(base))
    monkeypatch.setattr(md, "wait_editor_saved", lambda *a, **k: None)
    monkeypatch.setattr(md, "_upload_progress", lambda p: None)
    return c


class _MediaPage:
    """Блок Media: `evaluate` отдаёт `state` или бросает `error`."""

    url = ""

    def __init__(self, state=None, error=None):
        self.state, self.error, self.calls = state, error, 0

    def evaluate(self, *a, **k):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return dict(self.state)


def test_an_unreadable_media_block_is_not_polled_for_twenty_minutes(clock, rows, monkeypatch):
    """a71e4fc14aa1: 15:44:49 → 16:05:06 → 16:25:09 — два потолка по 20 мин."""
    monkeypatch.setattr(md, "_page_read_trouble", lambda p, timeout_s=2.0: "error:Error", raising=False)
    fake = _MediaPage(error=PlaywrightError("Execution context was destroyed"))
    start = clock.now
    assert md._verify_ad_media_present(fake) is False
    assert clock.now - start < 60, f"ждали {clock.now - start:.0f} с на нечитаемой странице"
    assert [r[2] for r in rows if r[0] == "ad_media_check"] == ["page_unreadable"]


def test_a_closed_tab_ends_the_media_check_at_once(clock, rows):
    fake = _MediaPage(error=TargetClosedError("Target page, context or browser has been closed"))
    assert md._verify_ad_media_present(fake) is False
    assert fake.calls == 1
    assert [r[2] for r in rows if r[0] == "ad_media_check"] == ["page_closed"]


def _stub_retry(monkeypatch, clock, calls, *, upload_s: float = 0.0):
    def upload(p, *a, **k):
        calls.append("upload")
        clock.now += upload_s
        return False

    monkeypatch.setattr(md, "upload_ad_creative", upload)
    monkeypatch.setattr(md, "media_refused_on_ad", lambda: None)
    monkeypatch.setattr(md, "_close_creative_setup_modal", lambda p: False)
    monkeypatch.setattr(md, "_upload_stalled", lambda p, **k: True)
    monkeypatch.setattr(md, "_upload_retry_can_reload", lambda p, edit_url="": True)
    monkeypatch.setattr(md, "_reload_editor", lambda p, edit_url="": calls.append("reload"))


def test_the_creative_step_has_its_own_ceiling(clock, rows, monkeypatch, tmp_path):
    """Надпись «Uploading video …» без полосы прогресса не уходит: прежде
    попытка (20 мин) + проверка (20) + попытка (20) + проверка (20) = 80 мин."""
    calls: list = []
    _stub_retry(monkeypatch, clock, calls, upload_s=md._UPLOAD_MAX_S)
    monkeypatch.setattr(md, "_page_read_trouble", lambda p, timeout_s=2.0: "", raising=False)
    fake = _MediaPage(state={"empty": False, "filled": False, "uploading": True})
    clip = tmp_path / f"clip_{_real_time.time_ns()}.mp4"
    clip.write_bytes(b"0")
    start = clock.now
    assert md.upload_ad_creative_with_retry(fake, "Video", str(clip)) is False
    took = clock.now - start
    budget = getattr(md, "CREATIVE_STEP_BUDGET_S", 25 * 60)
    assert took <= budget + 60, f"шаг креатива шёл {took / 60:.0f} мин"
    assert calls == ["upload"], "второй заход начат без срока на него"
    assert md.last_upload_diag().get("why") == "creative_budget"
    assert "creative_budget" in [r[2] for r in rows if r[0] == "ad_creative_upload"]


def test_an_unreadable_tab_ends_the_creative_step_after_the_attempt(clock, rows, monkeypatch, tmp_path):
    calls: list = []
    _stub_retry(monkeypatch, clock, calls)
    monkeypatch.setattr(md, "_page_read_trouble", lambda p, timeout_s=2.0: "crashed", raising=False)
    checks: list = []
    monkeypatch.setattr(md, "_verify_ad_media_present", lambda p: checks.append(1) or False)
    clip = tmp_path / f"clip_{_real_time.time_ns()}.mp4"
    clip.write_bytes(b"0")
    assert md.upload_ad_creative_with_retry(_MediaPage(state={}), "Video", str(clip)) is False
    assert calls == ["upload"] and checks == []
    assert md.last_upload_diag().get("why") == "page_unreadable"


def test_the_creative_stop_row_carries_the_reason(flow, monkeypatch):
    """a71e4fc14aa1: `ad_creative_upload` отказал с пустой причиной."""
    got: list[tuple] = []
    monkeypatch.setattr(rt, "record", lambda step, ok=None, **k: got.append((step, ok, k.get("note", ""))))

    def upload(*a, **k):
        md._creative_tls.why = "page_unreadable"
        return False

    monkeypatch.setattr(md, "upload_ad_creative_with_retry", upload)
    flow["run"]()
    assert ("ad_creative_upload", False, "page_unreadable") in got
