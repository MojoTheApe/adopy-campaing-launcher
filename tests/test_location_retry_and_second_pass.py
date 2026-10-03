# -*- coding: utf-8 -*-
"""Страна: лестница попыток на месте и второй проход в конце кампании (ACL-1390).

30.09 07:36 UTC, FacebookDoc19, SA_arc_All_Image_PF_35_30.09 (прогон
64cc3d5510b2): набрано «Saudi Arabia», подсказок нет, повторный набор
(ACL-1226) тоже без подсказок → `no_location_suggestions` →
`locations_open: nothing_to_click` → кампания остановлена «страна не
выставлена». Первая кампания того же кабинета за 25 минут до этого поставила
Saudi Arabia нормально — поиск Meta просто подвис.

Здесь в офлайн-Chrome: подсказки появляются только на 3-й/4-й попытке →
страна ставится на месте; не появляются в шаге вовсе, но появляются на втором
проходе → страна стоит, черновик можно публиковать (с предупреждением); не
появляются никогда → прежняя остановка `audience_location`, больше ничего.
Оба редактора: прежний (дерево слева) и новый в один экран.
"""
from __future__ import annotations

import inspect
import os
import sys
import time
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import meta_dom as md  # noqa: E402
from src.fb_ads_rpa import failure_policy, retry_policy  # noqa: E402
from src.fb_ads_rpa.ads_manager_flow import apply_step_failures  # noqa: E402
from src.fb_ads_rpa.draft import RpaPublishResult  # noqa: E402

TREE = (ROOT / "tests" / "fixtures" / "ads_manager"
        / "editor_tree_adset_2026_09_24.html").read_text(encoding="utf-8")

# Поле поиска локаций. `session` — номер набора: первый набор делает
# вызывающий, каждая очистка поля начинает следующий. Список подсказок
# появляется с набора `SHOW`; до набора `REGION_UNTIL` включительно в нём
# только регион с тем же именем — его брать нельзя никогда.
_SEARCH = """
<div id="loc" style="width:600px">
  <div>Locations</div>
  %(rows)s
  <div><div>Include</div><div role="combobox"><input id="q" placeholder="Search locations" aria-expanded="false"></div></div>
  <ul id="list"></ul>
</div>
<script>
  window.SHOW = %(show)d; window.REGION_UNTIL = %(region_until)d;
  window.session = 1; window.typed = {}; window.picked = null; window.entered = false;
  const q = document.getElementById('q'), list = document.getElementById('list');
  const WANT = 'Saudi Arabia';
  function row(c) {
    return '<div class="grp" data-c="' + c + '"><div>' + c + ' <div role="button" '
      + 'style="display:inline-block;width:20px;height:20px;overflow:hidden" '
      + 'onclick="drop(\\'' + c + '\\')"><span>Close</span></div></div><div>' + c + '</div></div>';
  }
  window.drop = (c) => { const g = document.querySelector('.grp[data-c="' + c + '"]'); if (g) g.remove(); };
  window.pick = (el) => {
    window.picked = el.textContent;
    if (/\\(Country\\)$/.test(el.textContent)) q.closest('[role=combobox]').parentElement.insertAdjacentHTML('beforebegin', row(WANT));
    list.innerHTML = ''; q.value = ''; q.setAttribute('aria-expanded', 'false');
  };
  q.addEventListener('input', () => {
    if (!q.value) { window.session++; list.innerHTML = ''; q.setAttribute('aria-expanded', 'false'); return; }
    window.typed[window.session] = q.value;
    if (window.session < window.SHOW || q.value.length < 4 || !WANT.startsWith(q.value)) return;
    let html = "<li role='option' onclick='pick(this)'>Saudi Arabia, Riyadh Region (Region)</li>";
    if (window.session > window.REGION_UNTIL)
      html += "<li role='option' onclick='pick(this)'>Saudi Arabia (Country)</li>";
    list.innerHTML = html; q.setAttribute('aria-expanded', 'true');
  });
  document.addEventListener('keydown', e => { if (e.key === 'Enter') window.entered = true; });
</script>
"""

_US = ('<div class="grp" data-c="United States"><div>United States <div role="button" '
       'style="display:inline-block;width:20px;height:20px;overflow:hidden" '
       'onclick="drop(\'United States\')"><span>Close</span></div></div><div>United States</div></div>')


def _search_page(show: int, region_until: int = 0, rows: str = _US) -> str:
    return ("<!doctype html><html><body>"
            + _SEARCH % {"show": show, "region_until": region_until, "rows": rows}
            + "</body></html>")


#: Та же лестница, что в коде, но без ожиданий и медленного набора.
_FAST = tuple((what, 0, 0.6) for what, _delay, _wait in md._LOCATION_ATTEMPTS)


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        try:
            yield b
        finally:
            b.close()


@pytest.fixture
def rec(monkeypatch):
    rows: list[tuple] = []
    monkeypatch.setattr(md._telemetry, "record",
                        lambda step, ok=None, **k: rows.append((step, ok, k)))
    return rows


@pytest.fixture
def page(browser, monkeypatch, rec):
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(md, "_human_jitter", lambda base, spread: 0.0)
    monkeypatch.setattr(md, "_LOCATION_ATTEMPTS", _FAST)
    monkeypatch.setattr(md, "wait_editor_saved", lambda *a, **k: None)
    import src.fb_ads_rpa.page_capture as pc
    monkeypatch.setattr(pc, "capture", lambda *a, **k: None)
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    p = ctx.new_page()
    try:
        yield p
    finally:
        ctx.close()


def _type_first(page, text: str = "Saudi Arabia") -> None:
    """Первый набор — как его делает `fill_audience_location`."""
    page.click("#q")
    page.keyboard.type(text)


def _attempts(rec) -> list[dict]:
    return [k.get("diag") or {} for step, _ok, k in rec if step == "audience_location_attempt"]


# ── Лестница попыток на месте ────────────────────────────────────────────

def test_the_ladder_has_four_escalating_attempts():
    ladder = md._LOCATION_ATTEMPTS
    assert len(ladder) == 4
    whats = [a[0] for a in ladder]
    delays = [a[1] for a in ladder]
    waits = [a[2] for a in ladder]
    # Первая — то, что уже набрал вызывающий, с прежним ожиданием 6 с:
    # обычный путь, когда подсказки есть сразу, не медленнее прежнего.
    assert whats[0] == "full" and waits[0] == 6.0
    assert "prefix" in whats
    assert delays[1] > 40 and delays == sorted(delays)
    assert waits == sorted(waits) and waits[-1] > waits[0]


def test_the_short_prefix_is_the_first_word():
    assert md._location_prefix("Saudi Arabia") == "Saudi"
    assert md._location_prefix("United Arab Emirates") == "United"
    # Одно слово — короче, но не меньше четырёх букв.
    assert md._location_prefix("Colombia") == "Colom"
    assert len(md._location_prefix("Chile")) >= 4


def test_suggestions_at_once_take_one_attempt_and_no_retype(page, rec):
    page.set_content(_search_page(show=1))
    _type_first(page)
    t0 = time.monotonic()
    assert md._pick_country_suggestion(page, page, "Saudi Arabia") is True
    assert time.monotonic() - t0 < 3.0
    assert page.evaluate("window.picked") == "Saudi Arabia (Country)"
    assert page.evaluate("window.session") == 1          # поле ни разу не чистили
    assert _attempts(rec) == []                           # обычный путь без лишних строк


@pytest.mark.parametrize("show", [3, 4])
def test_suggestions_only_on_a_late_attempt_set_the_country_in_place(page, rec, show):
    page.set_content(_search_page(show=show))
    _type_first(page)
    assert md._pick_country_suggestion(page, page, "Saudi Arabia") is True
    assert page.evaluate("window.picked") == "Saudi Arabia (Country)"
    assert page.evaluate("window.entered") is False
    tries = _attempts(rec)
    assert [t["attempt"] for t in tries] == list(range(1, show + 1))
    # Каждая попытка в телеметрии: что набрано и что показал список.
    assert all("typed" in t and "options" in t for t in tries)
    assert tries[-1]["options"] and tries[0]["options"] == []
    # Третья попытка набирает короткий префикс.
    assert page.evaluate("window.typed[3]") == "Saudi"


def test_a_region_with_the_same_name_is_never_picked(page, rec):
    page.set_content(_search_page(show=1, region_until=2))
    _type_first(page)
    assert md._pick_country_suggestion(page, page, "Saudi Arabia") is True
    assert page.evaluate("window.picked") == "Saudi Arabia (Country)"
    tries = _attempts(rec)
    assert tries[0]["outcome"] == "no_country_option"
    assert any("Region" in o for o in tries[0]["options"])


def test_no_suggestions_on_any_attempt_fails_without_blind_enter(page, rec):
    page.set_content(_search_page(show=99))
    _type_first(page)
    assert md._pick_country_suggestion(page, page, "Saudi Arabia") is False
    assert page.evaluate("window.entered") is False
    assert page.evaluate("window.picked") is None
    assert len(_attempts(rec)) == 4
    assert any(step == "audience_location" and k.get("note") == "no_location_suggestions"
               for step, _ok, k in rec)
    # Поле оставлено пустым: второй проход наберёт страну начисто.
    assert page.input_value("#q") == ""


# ── Второй проход в конце кампании ────────────────────────────────────────

#: Без CSS Meta строки вырезки дерева выше своих 46 px и обрезаются сеткой
#: (overflow:hidden) — раскладываем их подряд, как их видит человек.
_TREE_CSS = ("<style>#tree [role=row]{position:static!important;height:auto!important}"
             "#tree [role=grid],#tree [role=grid]>div{height:auto!important;max-height:none!important;"
             "overflow:visible!important}</style>")


def _editor_page(show: int, *, tree: bool = True, rows: str = _US) -> str:
    return ("<!doctype html><html><head>" + _TREE_CSS + "</head><body>"
            + ("<div id='tree' style='position:absolute;left:0;top:0'>" + TREE + "</div>" if tree else "")
            + "<div style='position:absolute;left:420px;top:0'>"
            + "<input placeholder='Enter your ad set name here...' value='Campaign_A'>"
            + _SEARCH % {"show": show, "region_until": 0, "rows": rows}
            + "</div></body></html>")


@pytest.fixture
def clicks(page, monkeypatch):
    """Клик по координатам — настоящий DOM-клик в точке, как у CDP."""
    got: list[str] = []

    def _click(pg, x, y):
        got.append(pg.evaluate(
            "([x, y]) => { const e = document.elementFromPoint(x, y);"
            " const row = e && e.closest('[role=rowheader]');"
            " (row || e) && (row || e).setAttribute('data-hit', '1');"
            " if (e) e.click(); return row ? row.getAttribute('aria-label') : (e ? e.tagName : ''); }",
            [x, y]))
        return True

    monkeypatch.setattr(md, "_cdp_mouse_click", _click)
    return got


@pytest.fixture
def fill(page, monkeypatch):
    """`fill_audience_location` без поиска заголовка: набрать и выбрать —
    лестница, снятие страны по умолчанию и проверка строк настоящие."""
    calls: list[str] = []

    def _fill(pg, country):
        calls.append(country)
        term = md._country_search_term(country)
        pg.click("#q")
        pg.keyboard.type(term)
        return md._pick_country_and_clear_defaults(pg, pg, term)

    monkeypatch.setattr(md, "fill_audience_location", _fill)
    # 30.09: карточку заново раскрыть было нечем (`nothing_to_click`).
    monkeypatch.setattr(md, "_click_locations_edit", lambda pg: False)
    return calls


def test_the_ad_set_row_of_the_tree_is_opened_not_the_campaign_row(page, clicks):
    page.set_content(_editor_page(show=1))
    assert md._open_tree_adset(page, "Campaign_A") is True
    assert clicks == ["Ad set"]


def test_no_suggestions_in_the_step_but_on_the_second_pass_sets_the_country(page, rec, clicks, fill):
    page.set_content(_editor_page(show=99))
    # Шаг: подсказок нет ни на одной попытке — прежний отказ шага.
    assert md.fill_audience_location(page, "SA") is False
    soft_fails = ["audience_location"]
    # Кампания доделана; на втором проходе поиск Meta снова отвечает.
    page.evaluate("window.SHOW = 0")
    md.retry_location_at_end(page, soft_fails, country="SA", simple=False, adset_name="Campaign_A")
    assert clicks and clicks[0] == "Ad set"
    names = page.evaluate("[...document.querySelectorAll('.grp')].map(g => g.dataset.c)")
    assert names == ["Saudi Arabia"]                      # США снята, как и прежде
    assert "audience_location" not in [failure_policy.base_name(f) for f in soft_fails]
    assert any(failure_policy.base_name(f) == "audience_location_second_pass" for f in soft_fails)
    result = apply_step_failures(RpaPublishResult(campaign_name="c", success=True), soft_fails)
    assert result.publishable is True
    assert any("second pass" in w for w in result.warnings)
    second = [(ok, k) for step, ok, k in rec if step == "audience_location_second_pass"]
    assert second and second[-1][0] is True
    assert second[-1][1]["diag"]["listed_after"] == ["Saudi Arabia"]


def test_no_suggestions_ever_keeps_the_audience_location_stop(page, rec, clicks, fill):
    page.set_content(_editor_page(show=99))
    assert md.fill_audience_location(page, "SA") is False
    soft_fails = ["audience_location"]
    md.retry_location_at_end(page, soft_fails, country="SA", simple=False, adset_name="Campaign_A")
    assert soft_fails == ["audience_location"]
    result = apply_step_failures(RpaPublishResult(campaign_name="c", success=True), soft_fails)
    assert result.publishable is False and result.blocking_failures == ["audience_location"]
    assert page.evaluate("[...document.querySelectorAll('.grp')].map(g => g.dataset.c)") == ["United States"]
    second = [ok for step, ok, k in rec if step == "audience_location_second_pass"]
    assert second == [False]
    # Кампанию не повторяют: остановка по-прежнему «на проверку человеку».
    assert retry_policy.disposition("audience_location").action == retry_policy.BLOCK_AND_REVIEW


def test_the_second_pass_in_the_single_screen_editor_opens_the_audience_card(page, rec, clicks, fill,
                                                                             monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(md._simple, "scroll_to_level",
                        lambda pg, level, **k: seen.append("level:" + level) or {"ok": True})
    monkeypatch.setattr(md._simple, "open_card", lambda pg, title: seen.append("card:" + title) or "opened")
    page.set_content(_editor_page(show=0, tree=False))
    soft_fails = ["audience_age", "audience_location"]
    md.retry_location_at_end(page, soft_fails, country="Saudi Arabia", simple=True, adset_name="Campaign_A")
    assert seen[:2] == ["level:adset", "card:Audience"]
    assert clicks == [] or "Ad set" not in clicks           # дерево не трогали
    assert soft_fails[0] == "audience_age"
    assert [failure_policy.base_name(f) for f in soft_fails] == [
        "audience_age", "audience_location_second_pass"]


def test_a_country_already_listed_is_only_read_back(page, rec, clicks, fill):
    only_sa = _US.replace("United States", "Saudi Arabia")
    page.set_content(_editor_page(show=99, rows=only_sa))
    soft_fails = ["audience_location"]
    md.retry_location_at_end(page, soft_fails, country="SA", simple=False, adset_name="Campaign_A")
    assert fill == []                                     # набирать нечего
    assert [failure_policy.base_name(f) for f in soft_fails] == ["audience_location_second_pass"]


def test_a_wrong_read_back_does_not_clear_the_stop(page, rec, clicks, monkeypatch):
    monkeypatch.setattr(md, "fill_audience_location", lambda pg, c: True)
    page.set_content(_editor_page(show=99))               # в карточке всё ещё United States
    soft_fails = ["audience_location"]
    md.retry_location_at_end(page, soft_fails, country="SA", simple=False, adset_name="Campaign_A")
    assert soft_fails == ["audience_location"]


def test_nothing_happens_when_the_country_was_set_in_the_step(page, clicks, monkeypatch):
    monkeypatch.setattr(md, "fill_audience_location",
                        lambda *a: pytest.fail("second pass must not run"))
    page.set_content(_editor_page(show=1))
    soft_fails = ["languages"]
    md.retry_location_at_end(page, soft_fails, country="SA", simple=False, adset_name="Campaign_A")
    assert soft_fails == ["languages"] and clicks == []


def test_the_ad_set_that_cannot_be_reached_keeps_the_stop(page, rec, monkeypatch):
    def _miss(pg, name, miss=None):
        miss.append("not_found")
        return False

    monkeypatch.setattr(md, "_open_tree_adset", _miss)
    monkeypatch.setattr(md, "fill_audience_location",
                        lambda *a: pytest.fail("must not type outside the ad set"))
    page.set_content(_editor_page(show=0))
    soft_fails = ["audience_location"]
    md.retry_location_at_end(page, soft_fails, country="SA", simple=False, adset_name="Campaign_A")
    assert soft_fails == ["audience_location"]
    # Ревью PR #393, m1: промах дерева — одна строка второго прохода с
    # пометкой open:<почему>, без отдельного шага *_open.
    assert [(step, ok, k.get("note")) for step, ok, k in rec] == [
        ("audience_location_second_pass", False, "open:not_found")]


def test_a_tree_miss_is_not_an_outcome_step_in_robot_health(page, rec):
    from src.web.robot_health import is_outcome_step

    page.set_content("<html><body><p>no tree here</p></body></html>")
    soft_fails = ["audience_location"]
    md.retry_location_at_end(page, soft_fails, country="SA", simple=False, adset_name="Campaign_A")
    assert soft_fails == ["audience_location"]
    assert rec and all(step == "audience_location_second_pass" for step, _ok, _k in rec)
    assert rec[-1][2]["note"].startswith("open:")
    assert not any(is_outcome_step(step, ok) for step, ok, _k in rec)


def test_the_second_pass_is_a_warning_not_a_block():
    assert failure_policy.severity("audience_location_second_pass:Saudi Arabia") == failure_policy.WARNING
    assert failure_policy.severity("audience_location") == failure_policy.BLOCKING


def test_the_job_panel_says_the_country_was_set_on_the_second_pass():
    item = failure_policy.problem(
        "audience_location_second_pass:Saudi Arabia was set on the second pass "
        "and read back from Locations")
    assert item["severity"] == failure_policy.WARNING
    assert item["title"] == "Country set on the second pass"
    assert len(item["title"]) <= 60
    assert item["impact"] and item["action"]


def test_the_flow_runs_the_second_pass_after_the_rest_of_the_campaign():
    src = inspect.getsource(md.run_recorded_campaign_flow)
    at = src.index("retry_location_at_end(")
    # После всей кампании (объявления и обход назначений), до итоговой сводки
    # и до передачи отказов вызывающему.
    assert src.rindex("destination_sweep") < at < src.index('"ad_level_summary"')
    assert at < src.rindex("soft_fails_out.extend(soft_fails)")
    assert "Publish" not in inspect.getsource(md.retry_location_at_end)
    assert "Publish" not in inspect.getsource(md._location_second_pass)


# ── Ревью PR #393, круг 1 ─────────────────────────────────────────────────

def test_the_second_pass_after_more_ads_runs_under_the_open_ad_guard(page, rec, monkeypatch):
    """F2: новый редактор, объявления 2+ — открыто объявление N, адрес держит
    объявление 1. Весь второй проход — под охраной ACL-1353: ни перезагрузки,
    ни открытия объявления или группы через дерево."""
    held: list[str] = []
    for name in ("_reload_editor", "recover_stuck_page", "_open_tree_ad", "_open_tree_adset"):
        monkeypatch.setattr(md, name, lambda *a, _n=name, **k: pytest.fail(f"{_n} during the second pass"))
    monkeypatch.setattr(md._simple, "scroll_to_level",
                        lambda pg, level, **k: held.append(md.reloads_held()) or {"ok": True})
    monkeypatch.setattr(md._simple, "open_card", lambda pg, title: "opened")

    def _fill(pg, country):
        held.append(md.reloads_held())
        pg.click("#q")
        pg.keyboard.type("Saudi Arabia")
        return md._pick_country_and_clear_defaults(pg, pg, "Saudi Arabia")

    monkeypatch.setattr(md, "fill_audience_location", _fill)
    page.set_content(_editor_page(show=0, tree=False))
    soft_fails = ["audience_location"]
    md.retry_location_at_end(page, soft_fails, country="SA", simple=True, adset_name="Campaign_A",
                             hold="simple_create_open_ad_n")
    assert held == ["simple_create_open_ad_n", "simple_create_open_ad_n"]
    assert md.reloads_held() == ""                        # охрана снята после прохода
    assert [failure_policy.base_name(f) for f in soft_fails] == ["audience_location_second_pass"]


def test_the_flow_holds_the_open_ad_after_more_ads_in_the_single_screen_editor():
    src = inspect.getsource(md.run_recorded_campaign_flow)
    call = src[src.index("retry_location_at_end("):]
    call = call[: call.index(")\n") + 1]
    assert 'hold="simple_create_open_ad_n" if simple and simple_extra_tried else ""' in call
    # Переменная задана до блока объявлений: кампания без креатива её тоже знает.
    assert src.index("simple_extra_tried = False") < src.index("    if creative_path:")


def test_an_empty_field_is_typed_again_on_the_first_attempt(page, rec):
    """F3: путь «раскрыть заново» зовёт лестницу с пустым полем."""
    page.set_content(_search_page(show=1))
    page.click("#q")                                      # поле пустое, фокус в нём
    assert md._pick_country_suggestion(page, page, "Saudi Arabia") is True
    assert page.evaluate("window.picked") == "Saudi Arabia (Country)"
    assert _attempts(rec) == []                           # хватило первой попытки


def test_the_attempt_row_says_what_was_really_in_the_field(page, rec):
    page.set_content(_search_page(show=2))
    page.click("#q")
    page.keyboard.type("Saudi Ara")                       # набор оборвался
    assert md._pick_country_suggestion(page, page, "Saudi Arabia") is True
    assert _attempts(rec)[0]["typed"] == "Saudi Ara"


def test_no_keys_are_sent_when_focus_left_the_location_search(page, rec):
    """F4: Ctrl+A/Backspace не должны стереть имя группы."""
    page.set_content("<input id='name' value='Adset name'>"
                     "<input id='q' placeholder='Search locations' style='display:none'>")
    page.focus("#name")
    assert md._retype_location_search(page, page, "Saudi Arabia", 0) is False
    assert page.input_value("#name") == "Adset name"


def test_a_region_row_does_not_read_back_as_the_country(page):
    """F5: «Riyadh, Saudi Arabia» — регион, не страна целиком."""
    region = _US.replace("United States", "Riyadh, Saudi Arabia")
    page.set_content(_editor_page(show=99, rows=region))
    ok, names = md._locations_read_back(page, "Saudi Arabia")
    assert ok is False and names == ["Riyadh, Saudi Arabia"]
    page.set_content(_editor_page(show=99, rows=_US.replace("United States", "Saudi Arabia")))
    assert md._locations_read_back(page, "Saudi Arabia")[0] is True
