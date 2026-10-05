# -*- coding: utf-8 -*-
"""Чужие окна Meta: закрыть известные, незнакомые — безопасно или назвать (ACL-1573, срез A).

Стенд — вырезка tests/fixtures/ads_manager/foreign_windows_doc21_2026_10_04.html
(Doc21, прогон 254ee3b3e0c7, шаг `conversion_website`, ACL-1572): шапка с
панелью Notifications (`#fbNotificationsFlyout`, role=dialog, «Notifications ·
Unread»), раздел имени ad set с «Create template», раздел Conversion с
триггером «Website and instant forms» и футер редактора (Close / Back / Next).
Вырезка снимает классы — живая панель на снимке закрыта (`toggleTargetClosed`,
так на всех 284 снимках rpa_pages, в том числе удачных шагов); тест
возвращает этот класс и правило CSS Meta `.toggleTargetClosed{display:none}`.
Тост сети — tests/fixtures/ads_manager/network_toast_2026_10_01.html (20583efb8c41).

Синтетика (живой разметки нет: порталы окон в конце body, снимок обрезан на
4 МБ): открытое меню Conversion location (как в
test_conversion_website_reopens_a_closed_menu.py), диалог шаблона имени (как в
test_naming_template_dialog_is_cancelled.py, по скриншотам 308c4f7c8a5a и
254ee3b3e0c7 — это и есть окно ACL-1572), незнакомые окна, «перехват фокуса»
открытой панелью/окном (пока окно открыто, триггер меню не открывается).
"""
from __future__ import annotations

import pytest

from tests.bench_support import FIXTURES, browser, fragment  # noqa: F401

from src.fb_ads_rpa import failure_policy  # noqa: E402
from src.fb_ads_rpa import foreign_windows as fw  # noqa: E402
from src.fb_ads_rpa import meta_dom as md  # noqa: E402
from src.fb_ads_rpa import run_telemetry  # noqa: E402

DOC21 = FIXTURES / "foreign_windows_doc21_2026_10_04.html"
TOAST = FIXTURES / "network_toast_2026_10_01.html"
SIMPLE_FOOTER = FIXTURES / "editor_footer_simple_create_2026_09_28.html"
OBJECTIVE = FIXTURES / "objective_open_2026_09_28.html"

_CSS = """<style>
  body { font: 14px sans-serif; margin: 0 }
  .toggleTargetClosed { display: none }
  #fbNotificationsFlyout { position: fixed; top: 40px; left: 70px; width: 360px; height: 420px;
    background: #fff; border: 1px solid #ccc; z-index: 50 }
  #editor { margin: 60px 0 0 480px; width: 520px }
  #menu { border: 1px solid #ccc }
  .modal { position: fixed; top: 30px; left: 450px; width: 420px; background: #fff;
    border: 1px solid #999; z-index: 60; padding: 8px }
</style>"""

_MENU = """
<div id="menu" style="display:none">
  <div role="heading">Multiple</div>
  <div class="opt"><span>Website and instant forms</span></div>
  <div role="heading">Single</div>
  <div>Send people to one location where they're most likely to convert.</div>
  <div class="opt" role="radio" aria-checked="false"><span id="web">Website</span></div>
</div>
"""

#: Эмуляция: панель/окно перехватывает фокус — триггер меню не открывается,
#: открытое меню закрывается; Escape закрывает только панель Notifications.
_EMULATION = """
<script>
  window.clicked = [];
  window.keys = [];
  const flyout = () => document.getElementById('fbNotificationsFlyout');
  const panelOpen = () => !!flyout() && !flyout().classList.contains('toggleTargetClosed');
  const blocking = () => panelOpen() || [...document.querySelectorAll('.modal')]
    .some(m => m.style.display !== 'none' && m.isConnected);
  window.openPanel = () => flyout().classList.remove('toggleTargetClosed');
  const menu = document.getElementById('menu');
  const trigger = [...document.querySelectorAll('[role=combobox]')]
    .find((e) => /Website and instant forms/.test(e.textContent));
  trigger.id = 'trigger';
  trigger.parentElement.appendChild(menu);
  trigger.addEventListener('click', () => {
    if (blocking()) return;
    menu.style.display = menu.style.display === 'none' ? 'block' : 'none';
  });
  for (const o of document.querySelectorAll('.opt')) o.addEventListener('click', () => {
    trigger.querySelector('#syn_6').textContent = o.textContent.trim();
    menu.style.display = 'none';
  });
  document.addEventListener('keydown', (e) => {
    window.keys.push(e.key);
    if (e.key === 'Escape' && panelOpen()) flyout().classList.add('toggleTargetClosed');
    const m = document.querySelector('.modal.escapable');
    if (e.key === 'Escape' && m) m.remove();
  }, true);
  document.addEventListener('click', (e) => {
    const b = e.target.closest('[role=button], button');
    if (!b) return;
    window.clicked.push((b.textContent || '').trim() || b.getAttribute('aria-label') || '?');
    if (b.closest('.modal') && /^(Close|Cancel|Not now|Dismiss|×)$/.test((b.textContent || '').trim()
        || b.getAttribute('aria-label') || '')) b.closest('.modal').remove();
  }, true);
</script>
"""


def _doc21(extra: str = "", *, footer: str = "") -> str:
    head = fragment(DOC21, "notifications_flyout").replace(
        'id="fbNotificationsFlyout"', 'id="fbNotificationsFlyout" class="toggleTargetClosed"', 1)
    body = (fragment(DOC21, "adset_name_section") + fragment(DOC21, "conversion_section")
            + fragment(DOC21, "editor_footer"))
    return (f"<!doctype html><html><head>{_CSS}</head><body>{head}<div id='editor'>{body}</div>"
            f"{footer}{_MENU}{extra}{_EMULATION}</body></html>")


@pytest.fixture
def rows(monkeypatch):
    got = []
    monkeypatch.setattr(run_telemetry, "record", lambda step, ok=None, **k: got.append((step, ok, k)))
    return got


@pytest.fixture
def reported(monkeypatch):
    """Снимки страницы и события Sentry сторожа."""
    from src import acl_sentry
    from src.fb_ads_rpa import page_capture

    got = {"captures": [], "sentry": []}
    monkeypatch.setattr(page_capture, "capture", lambda page, **k: got["captures"].append(k) or "cap")
    monkeypatch.setattr(acl_sentry, "report_step_failure",
                        lambda step, why="", **k: got["sentry"].append((step, why, k)) or "ev")
    return got


@pytest.fixture
def page(browser, monkeypatch, rows):  # noqa: F811
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(md, "WEBSITE_VALUE_WAIT_S", 0.3)
    monkeypatch.setattr(md, "dismiss_publish_draft_dialog", lambda *a, **k: False)
    fw.reset()
    ctx = browser.new_context(viewport={"width": 1500, "height": 950})
    ctx.route("**/*", lambda route: route.abort())
    p = ctx.new_page()
    try:
        yield p
    finally:
        ctx.close()
        fw.reset()


def _fw_rows(rows):
    return [(ok, k) for step, ok, k in rows if step == "foreign_window"]


def _menu_open(p) -> bool:
    return p.evaluate("document.getElementById('menu').style.display !== 'none'")


def _fake_methods(monkeypatch, *impls):
    names = ["_click_website_via_keyboard_focus", "_click_website_via_cdp",
             "_pick_website_via_recorded_recipe", "_click_website_via_mouse",
             "_click_website_via_force_locator", "_click_website_via_js_row",
             "_click_website_via_keyboard", "_pick_website_from_aria_controls"]
    calls = []
    for i, name in enumerate(names):
        impl = impls[i] if i < len(impls) else (lambda p: False)

        def wrapped(p, _impl=impl, _name=name):
            calls.append((_name, _menu_open(p)))
            return _impl(p)
        monkeypatch.setattr(md, name, wrapped)
    return calls


def _clicks_website(p) -> bool:
    if not p.is_visible("#web"):
        return False
    p.click("#web")
    return True


def _trigger_text(p) -> str:
    return p.inner_text("#trigger").replace(chr(0x200b), "").strip()


# ── Notifications (ACL-1572) ─────────────────────────────────────────────────

def test_the_captured_notifications_panel_is_hidden_and_left_alone(page, rows):
    """На снимке Doc21 панель есть в разметке, но закрыта — это не окно поверх страницы."""
    page.set_content(_doc21())
    assert fw.scan(page) == []
    assert fw.before_step(page, "conversion_location_open") == []
    assert page.evaluate("window.keys") == [] and page.evaluate("window.clicked") == []
    assert _fw_rows(rows) == []


def test_an_open_notifications_panel_is_detected_and_closed_before_the_conversion_menu(page, monkeypatch, rows):
    page.set_content(_doc21())
    page.evaluate("openPanel()")
    wins = fw.scan(page)
    assert [(w.kind, w.title, w.role) for w in wins] == [("notifications", "Notifications", "dialog")]
    _fake_methods(monkeypatch, _clicks_website)
    assert md._pick_website_from_open_menu(page) is True
    assert _trigger_text(page) == "Website"
    assert page.evaluate("document.getElementById('fbNotificationsFlyout').classList.contains('toggleTargetClosed')")
    closed = [k for ok, k in _fw_rows(rows) if ok is True]
    assert closed and closed[0]["note"] == "notifications:escape"
    diag = closed[0]["diag"]
    assert diag["title"] == "Notifications" and diag["role"] == "dialog"
    # Ни номеров, ни адресов в строке.
    assert not any(ch.isdigit() for ch in str(diag["title"]) + str(diag["surface"]))


def test_the_conversion_menu_is_reopened_between_strategies(page, monkeypatch, rows):
    """Способ 1 промахнулся, меню закрылось, а поверх встала панель — способ 2 получает открытое меню."""
    page.set_content(_doc21())

    def _miss_and_panel(p) -> bool:
        p.evaluate("document.getElementById('menu').style.display = 'none'; openPanel()")
        return False

    calls = _fake_methods(monkeypatch, _miss_and_panel, _clicks_website)
    assert md._pick_website_from_open_menu(page) is True
    assert calls[1] == ("_click_website_via_cdp", True)
    step, ok, kw = [r for r in rows if r[0] == "conversion_website_pick"][-1]
    assert ok is True and kw["note"] == "cdp"
    assert kw["diag"]["reopened"] >= 1
    assert "notifications:escape" in kw["diag"].get("windows_closed", [])


def test_the_naming_template_dialog_of_doc21_is_cancelled_before_the_menu(page, monkeypatch, rows):
    """Скриншоты 308c4f7c8a5a / 254ee3b3e0c7: «Ad set name — Template» поверх меню — Cancel, не Save."""
    naming = """
    <div role="dialog" id="naming" class="modal"><div><span>Ad set name</span>
      <div aria-label="Close" role="button" style="width:16px;height:16px"></div></div>
      <div>Name your ad sets consistently by creating a template based on your naming convention.</div>
      <div>Template</div><div><div role="button">Add component</div><a>Choose existing template</a></div>
      <div><div role="button">Cancel</div><div role="button" aria-disabled="true">Save</div></div></div>
    """
    page.set_content(_doc21(naming))
    _fake_methods(monkeypatch, _clicks_website)
    assert md._pick_website_from_open_menu(page) is True
    clicked = page.evaluate("window.clicked")
    assert "Cancel" in clicked and "Save" not in clicked and "Add component" not in clicked
    assert any(k["note"].startswith("naming_template:") and ok for ok, k in _fw_rows(rows))


# ── незнакомые окна ─────────────────────────────────────────────────────────

_UNSAFE_ONLY = """
<div role="dialog" class="modal" id="unknown"><div role="heading">Review your campaign before publishing</div>
  <div>Some settings changed.</div>
  <div role="button">Publish</div><div role="button">Confirm</div><div role="button">Continue</div>
  <div role="button">Accept</div><div role="button">Submit</div><div role="button">Create</div>
</div>
"""


def test_an_unknown_dialog_with_only_unsafe_buttons_is_never_clicked_but_captured_and_reported(
        page, rows, reported):
    page.set_content(_doc21(_UNSAFE_ONLY))
    tries = []
    ok = fw.guarded(page, "conversion_location_open", lambda: tries.append(1) or False)
    assert ok is False and len(tries) == 1, "без закрытия окна шаг не повторяется"
    assert page.evaluate("window.clicked") == []
    assert page.locator("#unknown").count() == 1
    assert reported["captures"] and reported["captures"][0]["step"] == "foreign_window"
    (step, why, kw), = reported["sentry"]
    assert step == "foreign_window" and "Review your campaign before publishing" in why
    assert kw["reason"] == "unknown-window"
    blocked = [k for ok_, k in _fw_rows(rows) if ok_ is False]
    assert blocked and blocked[-1]["note"] == "unknown:blocked"
    assert blocked[-1]["diag"]["reason"] == (
        "Unknown Meta window blocked the step: Review your campaign before publishing")
    stop = fw.attach_block("conversion_location_open")
    assert stop == ("conversion_location_open:Unknown Meta window blocked the step: "
                    "Review your campaign before publishing (unknown_window)")
    problem = failure_policy.problem(stop)
    assert problem["title"] == "Unknown Meta window blocked the step: Review your campaign before publishing"
    assert problem["severity"] == failure_policy.BLOCKING


def test_an_unknown_dialog_with_a_safe_close_is_closed_and_the_step_retried_once(page, rows, reported):
    dialog = """
    <div role="dialog" class="modal" id="unknown"><div role="heading">What's new in Ads Manager</div>
      <div role="button">Learn more</div><div role="button">Not now</div></div>
    """
    page.set_content(_doc21(dialog))
    tries = []

    def attempt() -> bool:
        tries.append(1)
        return page.locator("#unknown").count() == 0

    assert fw.guarded(page, "adset_name", attempt) is True
    assert len(tries) == 2
    assert page.evaluate("window.clicked") == ["Not now"]
    assert reported["sentry"] and "What's new in Ads Manager" in reported["sentry"][0][1]
    assert fw.blocked() is None
    # Не больше одного закрытия на шаг: второй отказ того же шага окно не трогает.
    page.evaluate("document.body.insertAdjacentHTML('beforeend', "
                  "'<div role=dialog class=modal id=again><div role=heading>Again</div>"
                  "<div role=button>Close</div></div>')")
    assert fw.after_failure(page, "adset_name") is False
    assert page.locator("#again").count() == 1


def test_publish_and_the_editor_footer_are_never_pressed_by_the_window_guard(page, rows, reported):
    """Футер редактора (Close / Back / Next) и Publish нового редактора — вне окна, не трогаем."""
    publish = SIMPLE_FOOTER.read_text(encoding="utf-8")
    trap = """
    <div role="dialog" class="modal" id="unknown"><div role="heading">Something needs attention</div>
      <span data-surface="/am/lib:syd_dialog_footer/lib:cancel-button">
        <div role="button">Close</div></span>
      <div role="button">Publish</div></div>
    """
    page.set_content(_doc21(trap, footer=f"<div id='simple'>{publish}</div>"))
    assert fw.after_failure(page, "conversion_location_open") is False
    assert page.evaluate("window.clicked") == []
    assert fw.blocked()["title"] == "Something needs attention"


def test_owned_windows_are_never_closed(page, rows, reported):
    wizard = """
    <div role="dialog" class="modal" id="wizard"><div role="heading">Set up your creative</div>
      <div role="button" aria-label="Close"></div><div role="button">Next</div></div>
    """
    page.set_content(_doc21(wizard))
    assert fw.before_step(page, "ad_creative_upload") == []
    assert fw.after_failure(page, "ad_creative_upload") is False
    assert page.evaluate("window.clicked") == [] and page.evaluate("window.keys") == []
    assert reported["sentry"] == []


# ── известные окна — как прежде ─────────────────────────────────────────────

def test_the_real_network_toast_is_closed_by_its_own_x(page, rows):
    """Снимок 20583efb8c41: «×» без подписи — прежний поиск баннеров его не видел."""
    toast = fragment(TOAST, "network_toast")
    page.set_content(_doc21(f"<div id='toast' style='position:fixed;top:5px;left:500px'>{toast}</div>"))
    page.evaluate("document.querySelector('#toast [role=button]').addEventListener('click',"
                  " () => document.getElementById('toast').remove())")
    assert fw.before_step(page, "adset") == ["network_toast:own_close"]
    assert page.locator("#toast").count() == 0


def test_the_ad_blocker_modal_still_goes_through_its_own_handler(page, rows):
    """Живая разметка 279b9539df43 (objective_open_2026_09_28.html): OK, как прежде, без перезагрузки посреди шага."""
    adblock = fragment(OBJECTIVE, "ad_blocker").replace(
        'role="dialog"', 'role="dialog" id="adblock" style="position:fixed;top:180px;left:530px;'
        'width:450px;background:#eee;z-index:70"', 1)
    page.set_content(_doc21(adblock))
    page.evaluate("document.addEventListener('click', (e) => { const b = e.target.closest('[role=button]');"
                  " if (b && b.closest('#adblock')) document.getElementById('adblock').remove(); }, true)")
    assert fw.before_step(page, "adset") == ["ad_blocker:ok_or_x"]
    assert page.locator("#adblock").count() == 0
    clicked = page.evaluate("window.clicked")
    # OK окна; дальше прежний сбор закрывает и карточку «Set up the Conversions
    # API» её крестиком (banners) — футер редактора не трогается.
    assert clicked[0] == "OK" and not {"Next", "Back"} & set(clicked)
    overlay = [k for step, ok, k in rows if step == "overlays_cleared"]
    assert overlay and "ad_blocker_modal:ok" in overlay[-1]["diag"]["closed"]
    assert overlay[-1]["diag"]["reloaded"] is False


def test_clear_blocking_overlays_also_closes_the_notifications_panel(page, rows, monkeypatch):
    monkeypatch.setattr(md, "_tab_frozen", lambda p: False)
    page.set_content(_doc21())
    page.evaluate("openPanel()")
    closed = md._clear_blocking_overlays(page, "adset")
    assert "notifications:escape" in closed


# ── отчёт: триаж, Sentry, остановка потока ──────────────────────────────────

def test_the_triage_map_points_unknown_windows_at_acl_1573():
    from scripts import sentry_triage as st

    rows = st.load_map()
    assert st.known_task("foreign_window", "unknown-window", rows)["task"] == "ACL-1573"
    assert st.known_task("conversion_website", "unknown-window", rows)["task"] == "ACL-1573"


def test_a_flow_stopped_by_an_unknown_window_names_it_and_keeps_its_step():
    """Имя шага (до «:») прежнее — политика повторов читает его как раньше."""
    from src import acl_sentry
    from src.fb_ads_rpa import retry_policy

    fw.reset()

    @md._under_flow_budget
    def flow():
        fw._st()["blocked"] = {"where": "conversion_location_open", "title": "Rate your experience"}
        return "conversion_location_open"

    got = flow()
    assert got == ("conversion_location_open:Unknown Meta window blocked the step: "
                   "Rate your experience (unknown_window)")
    assert retry_policy.disposition(got).action == retry_policy.disposition("conversion_location_open").action
    assert acl_sentry.failure_reason(got) == "unknown-window"
    # Следующий прогон начинается без чужой остановки.
    assert flow.__wrapped__ and fw.blocked() is not None
    fw.reset()
    assert fw.blocked() is None


def test_a_later_successful_step_clears_the_stop():
    fw.reset()
    fw._st()["blocked"] = {"where": "a", "title": "T"}
    fw.step_ok("b")
    assert fw.attach_block("next_to_ad") == "next_to_ad"


def test_titles_lose_ids_urls_and_the_campaign_name(monkeypatch):
    monkeypatch.setattr(run_telemetry, "current_campaign", lambda: "GT_Sample_35_04.10")
    t = fw.clean_title("Ad 120250775108750465 of GT_Sample_35_04.10 at https://x.example.com/a?act=123")
    assert "120250775108750465" not in t and "GT_Sample" not in t and "https" not in t
    assert fw.stop_reason("") == "Unknown Meta window blocked the step: untitled window"


# ── ревью PR #575: каждое закрытие на своём месте ───────────────────────────

def test_a_window_over_the_open_menu_is_closed_between_strategies(page, monkeypatch, rows):
    """Меню осталось открытым, но поверх встало окно: закрывает только проверка между способами."""
    naming = """
    <div role="dialog" id="naming" class="modal"><div><span>Ad set name</span></div>
      <div>Name your ad sets consistently by creating a template based on your naming convention.</div>
      <div><div role="button">Add component</div></div>
      <div><div role="button">Cancel</div><div role="button" aria-disabled="true">Save</div></div></div>
    """
    page.set_content(_doc21())

    def _opens_dialog(p) -> bool:
        p.evaluate("(h) => document.body.insertAdjacentHTML('beforeend', h)", naming)
        return False

    def _clicks_website_unless_covered(p) -> bool:
        if p.locator(".modal").count():
            return False
        return _clicks_website(p)

    calls = _fake_methods(monkeypatch, _opens_dialog, _clicks_website_unless_covered)
    assert md._pick_website_from_open_menu(page) is True
    assert calls[1] == ("_click_website_via_cdp", True)
    step, ok, kw = [r for r in rows if r[0] == "conversion_website_pick"][-1]
    assert kw["diag"]["reopened"] == 1, "меню закрывать не пришлось — окно закрыто между способами"
    assert any(c.startswith("naming_template:") for c in kw["diag"]["windows_closed"])


def test_a_window_that_appears_right_before_the_trigger_click_is_closed_there(page, monkeypatch, rows):
    """Окно встаёт между проверкой способа и нажатием на триггер — закрывает проверка перед нажатием,
    без запасного хода сторожа."""
    page.set_content(_doc21())
    fired = []

    def _panel_now(p) -> bool:
        # Вызов 1 — в начале выбора, вызов 2 — внутри открытия меню, прямо
        # перед нажатием на триггер: тогда и встаёт панель.
        fired.append(1)
        if len(fired) == 2:
            p.evaluate("openPanel()")
        return False

    monkeypatch.setattr(md, "dismiss_publish_draft_dialog", _panel_now)
    _fake_methods(monkeypatch, _clicks_website)
    assert md._pick_website_from_open_menu(page) is True
    step, ok, kw = [r for r in rows if r[0] == "conversion_website_pick"][-1]
    assert "notifications:escape" in kw["diag"]["windows_closed"]
    assert "fallback" not in kw["diag"]["windows_closed"]
    assert kw["diag"]["reopen_failed"] == 0
    assert len(fired) >= 2


# ── ревью PR #575 (B2): мастер креатива и окно Replace — «свои» ──────────────

WIZARD_BLOCKS = [
    ("creative_wizard_steps_2026_10_01.html", "image_creative_setup"),
    ("creative_wizard_steps_2026_10_01.html", "image_crop"),
    ("creative_wizard_steps_2026_10_01.html", "image_text"),
    ("creative_wizard_steps_2026_10_01.html", "image_enhancements"),
    ("creative_wizard_steps_2026_10_01.html", "video_trim"),
    ("creative_wizard_steps_2026_10_01.html", "video_text"),
    ("creative_wizard_steps_2026_10_01.html", "video_creative_generation"),
    ("creative_wizard_steps_2026_10_01.html", "pick_media_library"),
    ("crop_replace_in_wizard_picker_2026_10_03.html", "in_wizard_picker"),
    ("wizard_crop_story_missing_2026_10_04.html", "crop_busy"),
    ("wizard_crop_story_missing_2026_10_04.html", "in_wizard_picker"),
    ("wizard_media_picker_2026_10_04.html", "loaded"),
]


@pytest.mark.parametrize("fixture,name", WIZARD_BLOCKS)
def test_the_robots_own_media_wizard_is_owned_and_untouched(page, rows, reported, fixture, name):
    block = fragment(FIXTURES / fixture, name)
    page.set_content(_doc21(
        "<div style='position:fixed;top:0;left:0;width:1400px;height:900px;overflow:auto;"
        f"background:#fff;z-index:80'>{block}</div>"))
    wins = fw.scan(page)
    assert wins, "окно мастера не прочитано — тест ничего не проверил бы"
    assert all(w.owned and w.kind == "creative_wizard" for w in wins), [(w.kind, w.title) for w in wins]
    assert fw.before_step(page, "ad2_name") == []
    assert fw.after_failure(page, "ad2_name") is False
    assert fw.blocked() is None
    assert page.evaluate("window.clicked") == [] and page.evaluate("window.keys") == []
    assert reported["sentry"] == [] and reported["captures"] == []
    assert _fw_rows(rows) == []


# ── ревью PR #575, R2-3: «свои» виды по тексту не забирают окна реестра ──────

_NAMING_LOADING = """
<div role="dialog" id="naming" class="modal"><div><span>Ad set name</span></div>
  <div>Name your ad sets consistently by creating a template based on your naming convention.</div>
  <div>Loading...</div>
  <div><div role="button">Add component</div></div>
  <div><div role="button">Cancel</div><div role="button" aria-disabled="true">Save</div></div></div>
"""


def test_a_naming_dialog_with_a_loading_line_keeps_its_kind_and_is_cancelled(page, rows, reported):
    page.set_content(_doc21(_NAMING_LOADING))
    (w,) = fw.scan(page)
    assert (w.kind, w.owned) == ("naming_template", False)
    closed = fw.before_step(page, "conversion_location_open")
    assert closed and closed[0].startswith("naming_template:")
    assert page.locator("#naming").count() == 0
    assert "Cancel" in page.evaluate("window.clicked") and "Save" not in page.evaluate("window.clicked")


def test_a_notifications_panel_with_a_loading_line_is_still_closed(page, rows):
    page.set_content(_doc21())
    page.evaluate("openPanel(); document.getElementById('fbNotificationsFlyout')"
                  ".insertAdjacentHTML('beforeend', '<div>Loading...</div>')")
    (w,) = fw.scan(page)
    assert (w.kind, w.owned) == ("notifications", False)
    assert fw.before_step(page, "adset") == ["notifications:escape"]


def test_create_new_campaigns_in_a_survey_does_not_make_it_the_objective_window(page, rows):
    survey = """
    <div role="dialog" class="modal" id="survey"><div role="heading">How satisfied are you with Ads Manager?</div>
      <div>Create new campaigns faster with our latest tools.</div>
      <div role="button">Submit</div><div role="button">Not now</div></div>
    """
    page.set_content(_doc21(survey))
    (w,) = fw.scan(page)
    assert (w.kind, w.owned) == ("survey", False)
    assert fw.before_step(page, "adset") == ["survey:own_close"]
    assert page.evaluate("window.clicked") == ["Not now"]


def test_the_wizard_structure_wins_over_registry_text_inside_it(page, rows, reported):
    """Устройство мастера проверяется ПЕРВЫМ: текст опроса внутри мастера его не делает опросом."""
    block = fragment(FIXTURES / "creative_wizard_steps_2026_10_01.html", "image_text")
    page.set_content(_doc21(
        "<div style='position:fixed;top:0;left:0;width:1400px;height:900px;overflow:auto;"
        f"background:#fff;z-index:80'>{block}</div>"))
    page.evaluate("document.querySelectorAll('[role=dialog]').forEach((d) => d.insertAdjacentHTML("
                  "'afterbegin', '<div>How satisfied are you with the new creative tools?</div>'))")
    wins = fw.scan(page)
    assert wins and all(w.kind == "creative_wizard" and w.owned for w in wins)
    assert fw.before_step(page, "ad_copy") == []
    assert page.evaluate("window.clicked") == [] and page.evaluate("window.keys") == []


def test_close_safely_refuses_an_owned_window(page):
    page.set_content(_doc21(_UNSAFE_ONLY.replace("Publish", "Close")))
    (w,) = fw.scan(page)
    w.owned = True
    assert fw._close_safely(page, w) == ""
    assert page.evaluate("window.clicked") == [] and page.evaluate("window.keys") == []
    assert page.locator("#unknown").count() == 1


def test_the_hidden_panel_is_skipped_by_its_class_even_without_metas_css(page):
    """Без правила `.toggleTargetClosed{display:none}` панель видна — пропускает её сама проверка класса."""
    page.set_content(_doc21().replace(".toggleTargetClosed { display: none }", ""))
    assert page.is_visible("#fbNotificationsFlyout")
    assert fw.scan(page) == []
