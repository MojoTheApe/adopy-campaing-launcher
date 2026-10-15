# -*- coding: utf-8 -*-
"""Новый редактор Meta «в один экран» — разметка (ACL-1327).

Вырезка `tests/fixtures/ads_manager/simple_create_2026_09_27.html` —
живая страница 9e7d67471c6b/00_next_to_adset (27.09): дерево «New Leads
Campaign / Ad Set / Ad», три раздела `/am/editor:campaign|ad_set|ad` с
заголовками второго уровня, свёрнутые карточки (`role=button
aria-pressed=false` + `<div hidden>`), «Instant forms» и карточка «Please read
and accept Meta's Lead Ads Terms». Meta JS в вырезке нет: раскрытие карточки
и выбор цели эмулирует тест (как в стенде ACL-1324).

Проверяется: редактор узнаётся по разметке при любом адресе; до разделов
группы и объявления доходим прокруткой; карточки раскрываются, и прежний
обработчик («Further limit the reach» ACL-1324, Conversion location) работает
внутри одной страницы; цель Leads ставится на месте; требование условий Lead
Ads видно в разделе группы и уходит вместе с карточкой.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import meta_dom as md  # noqa: E402
from src.fb_ads_rpa import simple_create as sc  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "ads_manager"
EDITOR = (FIXTURES / "simple_create_2026_09_27.html").read_text(encoding="utf-8")
CLASSIC_TREE = (FIXTURES / "editor_tree_adset_2026_09_24.html").read_text(encoding="utf-8")

#: c98a1c094dfc/00_simple_create_layout (27.09): страница-заготовка нового
#: редактора, разделы ещё грузятся — есть только футер Publish и полосы Loading.
LOADING = ('<div aria-busy="false" role="button" tabindex="0" data-surface="/am/lib:syd_simple_create_publish">'
           '<span><div><div><div>Publish</div></div></div></span></div>'
           '<span aria-busy="true" role="progressbar" aria-valuemin="0" aria-valuemax="100" '
           'aria-valuetext="Loading..."></span>')

LIST_URL = "https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=1"
SIMPLE_URL = ("https://adsmanager.facebook.com/adsmanager/manage/simple_create?act=1"
              "&selected_campaign_ids=2&selected_adset_ids=3&selected_ad_ids=4&sc_1nn=true")

#: Эмуляция Meta: нажатие на свёрнутую сводку раскрывает карточку; выбор
#: варианта цели отмечает его радио и снимает остальные.
_META_JS = """
<script>
window.clicks = [];
window.cardClicks = [];
document.addEventListener('click', (ev) => {
  const any = ev.target.closest('[role=button][aria-pressed]');
  if (any) window.cardClicks.push(any.getAttribute('aria-labelledby') + ':' + any.getAttribute('aria-pressed'));
  if (ev.target.closest('#cover')) window.clicks.push('COVER');
  const btn = ev.target.closest('[role=button][aria-pressed="false"]');
  if (btn && window.cardsOpen !== false) {
    const id = btn.getAttribute('aria-labelledby');
    const all = [...document.querySelectorAll('[role=button][aria-pressed]')]
      .filter(b => b.getAttribute('aria-labelledby') === id);
    if (!window.pressedStays) all.forEach(b => b.setAttribute('aria-pressed', 'true'));
    let root = all[0].parentElement;
    while (root && !all.every(b => root.contains(b))) root = root.parentElement;
    root.querySelectorAll('[hidden]').forEach(h => { if (!all.some(b => b.contains(h))) h.hidden = false; });
    window.clicks.push('card:' + id);
  }
  const opt = ev.target.closest('[data-surface*="objective-selector-option-"]');
  if (opt && window.objectiveWorks !== false) {
    document.querySelectorAll('[data-surface*="objective-selector-option-"] input[type=radio]').forEach(r => {
      const mine = opt.contains(r);
      r.checked = mine; r.setAttribute('aria-checked', mine ? 'true' : 'false');
    });
    window.clicks.push('objective:' + opt.getAttribute('data-surface').split('option-').pop());
  }
  if (ev.target.closest('[data-surface="/am/lib:syd_simple_create_publish"]')) window.clicks.push('PUBLISH');
}, true);
</script>
"""


@pytest.fixture
def page(monkeypatch):
    rows: list = []
    monkeypatch.setattr(md._telemetry, "record", lambda step, ok=None, **kw: rows.append((step, ok, kw)))
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        try:
            p = b.new_page(viewport={"width": 1280, "height": 800})
            p.rows = rows
            yield p
        finally:
            b.close()


def _open(page, body: str, url: str = LIST_URL, *, meta: bool = True) -> None:
    html = "<html><body>" + body + (_META_JS if meta else "") + "</body></html>"
    page.route("**/*", lambda r: r.fulfill(status=200, content_type="text/html", body=html))
    page.goto(url)


# ── Узнавание ────────────────────────────────────────────────────────────

def test_the_editor_is_recognised_by_its_markup_at_any_address(page):
    """9e7d67471c6b: адрес проверили раньше, чем он сменился, — разметка
    отвечает в любой момент."""
    _open(page, EDITOR, LIST_URL)
    assert md._simple_create_layout(page) is True


def test_the_loading_shell_is_recognised_by_its_publish_footer(page):
    _open(page, LOADING, LIST_URL)
    assert md._simple_create_layout(page) is True


def test_the_classic_editor_is_not_taken_for_the_new_one(page):
    _open(page, CLASSIC_TREE, "https://adsmanager.facebook.com/adsmanager/manage/campaigns/edit/standalone?act=1")
    assert md._simple_create_layout(page) is False


def test_one_level_section_alone_is_not_the_new_editor(page):
    """Одна обёртка `/am/editor:campaign` без раздела группы и без футера —
    не повод сворачивать с прежнего пути."""
    _open(page, '<div data-surface="/am/editor:campaign"><input placeholder="Enter your campaign name here..."></div>')
    assert md._simple_create_layout(page) is False


def test_the_address_alone_still_counts():
    assert md._simple_create_layout(MagicMock(url=SIMPLE_URL)) is True


def test_the_ad_editor_address_includes_the_new_editor():
    assert md._ad_editor_url(SIMPLE_URL) is True
    assert md._ad_editor_url("https://adsmanager.facebook.com/adsmanager/manage/ads/edit/standalone?act=1") is True
    assert md._ad_editor_url(LIST_URL) is False


def test_the_creative_steps_ask_the_ad_editor_question_in_one_place():
    """Загрузка и повторное использование креатива решали «мы на странице
    объявления?» по `/ads/edit/` в адресе — в новом редакторе адрес другой, и
    удачная загрузка кончалась отказом. Вопрос задаётся только через
    `_ad_editor_url`; прямой проверки адреса в meta_dom не остаётся."""
    import inspect

    source = inspect.getsource(md)
    helper = inspect.getsource(md._ad_editor_url)
    assert source.replace(helper, "").count('"/ads/edit/" in') == 0
    assert source.replace(helper, "").count('"/ads/edit/" not in') == 0


# ── Разделы уровней ──────────────────────────────────────────────────────

@pytest.mark.parametrize("level, field", [
    ("adset", "Enter your ad set name here..."),
    ("ad", "Enter your ad name here..."),
])
def test_a_level_is_reached_by_scrolling_not_by_next(page, level, field):
    _open(page, EDITOR, SIMPLE_URL)
    assert page.get_by_role("button", name="Next").count() == 0, "в новом редакторе Next нет"
    got = sc.scroll_to_level(page, level, budget_s=2)
    assert got == {"ok": True}
    box = page.get_by_placeholder(field).bounding_box()
    assert box and 0 <= box["y"] < 800, "поле имени уровня не на экране после прокрутки"
    assert page.evaluate("window.clicks") == []


def test_a_missing_level_section_is_named(page):
    _open(page, EDITOR.replace('data-surface="/am/editor:ad"', 'data-surface="/am/other"'), SIMPLE_URL)
    got = sc.scroll_to_level(page, "ad", budget_s=0.5)
    assert got["ok"] is False and got["why"] == "no_section"


# ── Свёрнутые карточки и прежние обработчики внутри одной страницы ───────

def test_the_audience_card_is_opened_and_the_acl1324_row_is_then_found(page):
    _open(page, EDITOR, SIMPLE_URL)
    # ACL-1332: строку внутри свёрнутой карточки поиск находит, но как
    # неразложенную (laid=False) — такую робот не нажимает. Нажать её можно
    # только после раскрытия карточки.
    before = page.evaluate(md._FURTHER_LIMIT_ROW_JS)
    assert not before.get("laid"), "строка разложена до раскрытия — вырезка не та"
    assert sc.open_card(page, "Audience") == "opened"
    row = page.evaluate(md._FURTHER_LIMIT_ROW_JS)
    assert row["found"] == 1 and row["laid"] and row["surface"] == "lib:targeting-individual-setting-toggle-automation-button"
    # «Save audience» рядом — не наша строка и не нажимается.
    assert "Save audience" not in page.locator("[data-acl-further-limit]").inner_text()
    assert ("simple_card", True) in [(r[0], r[1]) for r in page.rows]


def test_a_card_is_seen_open_by_its_body_when_the_button_state_lags(page):
    """Кнопка сводки может ещё числиться `aria-pressed=false`, когда тело
    карточки уже показано: тело ищется в общем предке шапки и сводки (у них
    разная глубина), иначе раскрытую карточку назвали бы нераскрытой."""
    _open(page, EDITOR, SIMPLE_URL)
    page.evaluate("window.pressedStays = true")
    assert sc.open_card(page, "Audience") == "opened"


def _card_id(page, title):
    return page.evaluate("""(t) => { const h = [...document.querySelectorAll('[role=toolbar] [role=heading]')]
        .find(h => h.textContent.trim() === t); return h.closest('[role=toolbar]').id; }""", title)


def test_a_covered_header_never_sends_the_click_to_an_earlier_card(page):
    """Ревью PR #340 (F1): метка кнопки прежней карточки (Audience) оставалась,
    и запасной DOM-клик по Placements с перекрытой шапкой уходил в Audience —
    у Meta это свернуло бы заполненную карточку. Перекрытую шапку не жмём
    доверенным кликом вовсе (он попал бы в то, что сверху)."""
    _open(page, EDITOR, SIMPLE_URL)
    assert sc.open_card(page, "Audience") == "opened"
    audience, placements = _card_id(page, "Audience"), _card_id(page, "Placements")
    page.evaluate("""() => { window.cardClicks = []; const c = document.createElement('div'); c.id = 'cover';
        c.style.cssText = 'position:fixed;left:0;top:0;width:100vw;height:100vh;z-index:99999';
        document.body.appendChild(c); }""")
    assert sc.open_card(page, "Placements") == "opened"
    clicks = page.evaluate("window.cardClicks")
    assert not [c for c in clicks if c.startswith(audience + ":")], clicks
    assert placements + ":false" in clicks
    assert "COVER" not in page.evaluate("window.clicks"), "доверенный клик ушёл в перекрытие"


def test_only_the_current_card_carries_the_button_mark(page):
    """Ревью PR #340 (F1): после второй карточки метки первой не остаётся."""
    _open(page, EDITOR, SIMPLE_URL)
    sc.open_card(page, "Audience")
    page.evaluate("window.cardsOpen = false")          # вторая не раскроется — метка останется
    sc.open_card(page, "Placements")
    marks = page.evaluate("[...document.querySelectorAll('[data-acl-card-button]')]"
                          ".map(e => e.getAttribute('aria-labelledby'))")
    assert marks == [_card_id(page, "Placements")]


def test_the_fallback_click_takes_only_the_button_of_its_own_card(page):
    """Запасной DOM-клик сверяет метку с шапкой своей карточки: чужая метка
    (например, оставленная другим путём) не нажимается."""
    _open(page, EDITOR, SIMPLE_URL)
    audience, placements = _card_id(page, "Audience"), _card_id(page, "Placements")
    page.evaluate("""([a, p]) => {
        const btn = (id) => [...document.querySelectorAll('[role=button][aria-pressed]')]
          .find(b => b.getAttribute('aria-labelledby') === id);
        btn(a).setAttribute('data-acl-card-button', a);
        btn(p).setAttribute('data-acl-card-button', p);
        window.cardClicks = []; }""", [audience, placements])
    assert page.evaluate(sc._CARD_DOM_CLICK_JS, placements) is True
    assert page.evaluate("window.cardClicks") == [placements + ":false"]


def test_an_open_card_is_not_clicked_again(page):
    _open(page, EDITOR, SIMPLE_URL)
    assert sc.open_card(page, "Budget") == "open"
    assert page.evaluate("window.clicks") == []


def test_an_unknown_card_is_absent_and_untouched(page):
    _open(page, EDITOR, SIMPLE_URL)
    assert sc.open_card(page, "No such card") == "absent"


def test_a_card_that_does_not_open_is_reported(page):
    _open(page, EDITOR, SIMPLE_URL)
    page.evaluate("window.cardsOpen = false")
    assert sc.open_card(page, "Placements") == "failed"
    assert ("simple_card", False) in [(r[0], r[1]) for r in page.rows]


def test_the_placements_card_is_opened(page):
    _open(page, EDITOR, SIMPLE_URL)
    assert sc.open_card(page, "Placements") == "opened"
    assert page.get_by_text("Account controls").first.is_visible()


def test_conversion_location_opens_and_instant_forms_is_not_website(page):
    _open(page, EDITOR, SIMPLE_URL)
    page.evaluate("""() => { const c = document.querySelector(
        '[data-surface$="sa_conversion_location_value_surface_ON_AD"] [role=combobox]');
        c.addEventListener('click', () => c.setAttribute('aria-expanded', 'true')); }""")
    assert md.is_website_conversion_selected(page) is False
    assert md.click_conversion_location_dropdown(page) is True
    assert page.locator('[data-surface$="sa_conversion_location_value_surface_ON_AD"] [role=combobox]'
                        ).get_attribute("aria-expanded") == "true"


def test_the_level_name_fields_and_budget_are_unique_on_the_page(page):
    """Три раздела на одной странице — поля прежних обработчиков не двоятся."""
    _open(page, EDITOR, SIMPLE_URL)
    for placeholder in ("Enter your campaign name here", "Enter your ad set name here...",
                        "Enter your ad name here...", "Please enter amount"):
        assert page.get_by_placeholder(placeholder).count() == 1, placeholder
    assert page.get_by_role("spinbutton", name="Budget").count() == 1


# ── Цель кампании ────────────────────────────────────────────────────────

def test_leads_already_chosen_is_left_alone(page):
    _open(page, EDITOR, SIMPLE_URL)
    assert sc.ensure_leads_objective(page) == "leads"
    assert page.evaluate("window.clicks") == []


def test_another_objective_is_switched_to_leads_inline(page):
    _open(page, EDITOR, SIMPLE_URL)
    page.evaluate("""() => document.querySelectorAll('[data-surface*="objective-selector-option-"] input[type=radio]')
        .forEach(r => { const s = /outcome_sales/.test(r.closest('[data-surface]').getAttribute('data-surface'));
                        r.checked = s; r.setAttribute('aria-checked', s ? 'true' : 'false'); })""")
    assert sc.ensure_leads_objective(page) == "set"
    clicks = page.evaluate("window.clicks")
    assert any(c.startswith("card:") for c in clicks), "карточку Campaign details не раскрыли"
    assert "objective:outcome_leads" in clicks
    assert "PUBLISH" not in clicks


def test_an_objective_that_does_not_switch_fails(page):
    _open(page, EDITOR, SIMPLE_URL)
    page.evaluate("""() => { window.objectiveWorks = false;
        document.querySelectorAll('[data-surface*="objective-selector-option-"] input[type=radio]')
        .forEach(r => { r.checked = false; r.setAttribute('aria-checked', 'false');
                        // Радио Meta управляемое: без её JS отметка не держится.
                        r.addEventListener('click', ev => ev.preventDefault()); }); }""")
    assert sc.ensure_leads_objective(page) == "failed"


def test_no_objective_list_means_nothing_to_check(page):
    _open(page, LOADING, SIMPLE_URL)
    assert sc.ensure_leads_objective(page) == "absent"


# ── Условия Lead Ads ─────────────────────────────────────────────────────

def test_the_lead_ads_terms_card_is_seen_in_the_ad_set_section(page):
    _open(page, EDITOR, SIMPLE_URL)
    got = sc.lead_terms_block(page, wait_s=0)
    assert "Terms" in got


def test_the_review_panel_alone_is_not_a_lead_terms_stop(page):
    """После Website карточка под выбором страницы уходит; ошибка в правой
    панели («Review 1 error … (#1815089)») может отставать — по ней не стоим."""
    _open(page, EDITOR, SIMPLE_URL)
    page.evaluate("""() => {
      const sec = document.querySelector('[data-surface="/am/editor:ad_set/adset_conversion_goals_section"]');
      sec.querySelector('[data-surface$="sa_conversion_location_value_surface_ON_AD"] [role=combobox] div').textContent = 'Website';
      // Страница и её карточка условий принадлежат Instant forms — с Website их нет.
      for (const e of [...sec.querySelectorAll('div, span')]) {
        if (/Lead Ads Terms|Terms of Service Not Accepted/.test(e.textContent) && e.children.length <= 2) e.remove();
      }
    }""")
    assert page.get_by_text("(#1815089)").count() == 1, "ошибка правой панели должна остаться в тесте"
    assert sc.lead_terms_block(page, wait_s=0) == ""


def test_view_terms_is_never_pressed(page):
    _open(page, EDITOR, SIMPLE_URL)
    page.evaluate("""() => document.querySelectorAll('[role=button]').forEach(b => {
        if (/^(View terms|Accept)$/.test(b.textContent.trim())) b.addEventListener('click', () => window.clicks.push('TERMS')); })""")
    sc.lead_terms_block(page, wait_s=0)
    sc.open_card(page, "Audience")
    sc.ensure_leads_objective(page)
    assert "TERMS" not in page.evaluate("window.clicks")
    assert "PUBLISH" not in page.evaluate("window.clicks")
