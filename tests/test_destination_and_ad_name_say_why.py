# -*- coding: utf-8 -*-
"""Шаги назначения и имени объявления называют причину отказа (ACL-1371).

28.09, FacebookDoc6540 (a90d704c892a, CO_bnc Trading): `ad_destination`
упал за 8 с без причины. В снимке 01_ad_destination тип назначения —
«Instant Experience» (`aria-checked="true"`), у «Website» —
`aria-checked="false"`; при таком типе Meta не рисует ни поля «Website URL»,
ни Browser add-ons, и робот, не найдя поля, молча вернул False. Теперь он
сначала возвращает тип Website, а если не вышло — называет причину.

Sentry PYTHON-1W: `ad_name` / `ad2_name: silent` (DAN-LT25, 5844A, 28.09 —
снимка нет; 37e397574548 у нас). `_fill` глотал исключения, строка шага
шла пустой. Теперь отказ имени читает, что было с полем.

Разметка — fixtures/ads_manager/ad_destination_types_2026_09_28.html
(a90d704c892a/01_ad_destination и 7e5f7abec777/02_ad_destination) и
ad_name_section_2026_09_28.html (a90d704c892a/01_ad_destination), вычищены.
Скриптов Meta в них нет: смена типа назначения по нажатию смоделирована в
тесте — раздел заменяется разметкой Website из второго снимка.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import acl_sentry  # noqa: E402
from src.fb_ads_rpa import browser as br  # noqa: E402
from src.fb_ads_rpa import meta_dom as md  # noqa: E402
from src.fb_ads_rpa import page_capture, screenshots  # noqa: E402
from src.fb_ads_rpa import run_telemetry as rt  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "ads_manager"
URL = "https://example.com/landing?src=test"


def _fragments(name: str) -> dict[str, str]:
    text = (FIXTURES / name).read_text(encoding="utf-8")
    parts = re.split(r"<!-- fragment: (\w+) \| source: [^>]*-->\n", text)
    return {parts[i]: parts[i + 1].replace("</body></html>", "").strip()
            for i in range(1, len(parts), 2)}


DEST = _fragments("ad_destination_types_2026_09_28.html")
NAME = _fragments("ad_name_section_2026_09_28.html")


def _website_after_switch() -> str:
    """Раздел после выбора Website: разметка 7e5f, add-ons — «None», поле пусто.

    Так Meta рисует Website в 7e5f до того, как вернула «Instant form»:
    атрибут `checked` там стоит на None (состояние отрисовки).
    """
    html = DEST["website_instant_form"]
    html = re.sub(r'(<input[^>]*aria-checked=")true("[^>]*value="instant_form")', r"\1false\2", html)
    html = re.sub(r'(<input[^>]*aria-checked=")false("[^>]*value="none")', r"\1true\2", html)
    assert re.search(r'<input[^>]*aria-checked="true"[^>]*value="none"', html)
    assert not re.search(r'<input[^>]*aria-checked="true"[^>]*value="instant_form"', html)
    return html


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
    got: list = []
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(md, "wait_editor_saved", lambda *a, **k: None)
    monkeypatch.setattr(md, "_close_creative_setup_modal", lambda p: False)
    monkeypatch.setattr(md._telemetry, "record", lambda step, ok=None, **k: got.append((step, ok, k)))
    return got


@pytest.fixture
def page(browser, rows):
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    p = ctx.new_page()
    # Ни одна фикстура не тянет ничего из сети.
    p.route("**/*", lambda route: route.abort())
    try:
        yield p
    finally:
        ctx.close()


def _open(page, body: str, script: str = "") -> None:
    page.set_content(f'<!doctype html><html><body style="width:900px">{body}{script}</body></html>')


def _meta_switches_type_on_click(page) -> None:
    """Модель Meta: нажатие на «Website» перерисовывает раздел под Website."""
    page.evaluate(
        """(html) => {
          document.addEventListener('click', (e) => {
            const r = e.target.closest && e.target.closest('input[type=radio][value="external"]');
            if (!r) return;
            const sec = document.getElementById('adgroupAdDestinationGeneric');
            if (sec && !sec.querySelector('[data-surface$="/pce_website_url_field"]')) {
              sec.outerHTML = html;
            }
          }, true);
        }""",
        _website_after_switch(),
    )


# ── Снимок 6540: тип назначения — Instant Experience ────────────────────────


def test_the_6540_capture_has_instant_experience_and_no_url_field(page):
    _open(page, DEST["instant_experience"])
    state = md.destination_state(page, URL)
    assert state["dest_type"] == "instant_experience"
    assert state["url_fields"] == 0 and state["url"] == "no_field"
    assert state["addon"] == ""
    # Атрибут отрисовки остался на Website — выбор сменился уже потом.
    assert page.locator('input[value="external"]').get_attribute("checked") is not None
    assert page.locator('input[value="external"]').get_attribute("aria-checked") == "false"


def test_the_destination_goes_back_to_website_and_is_set(page, rows):
    """На старом коде шаг возвращал False: поля URL при Instant Experience нет."""
    _open(page, DEST["instant_experience"])
    _meta_switches_type_on_click(page)
    assert md.set_ad_destination_website(page, URL) is True
    assert page.locator('[data-surface$="/pce_website_url_field"] input').input_value() == URL
    assert md.destination_state(page, URL)["dest_type"] == "external"
    switched = [r for r in rows if r[0] == "destination_website_selected"]
    assert switched and switched[0][1] is True
    assert switched[0][2]["diag"]["was"] == "instant_experience"
    assert md.last_destination_why() == ""


def test_instant_experience_that_does_not_let_go_is_named(page, rows):
    _open(page, DEST["instant_experience"])
    assert md.set_ad_destination_website(page, URL) is False
    assert md.last_destination_why() == "destination_not_website"
    diag = md.last_destination_diag()
    assert diag["why"] == "destination_not_website"
    assert diag["dest_type"] == "instant_experience"
    assert [r[1] for r in rows if r[0] == "destination_website_selected"] == [False]


def test_the_website_click_goes_through_the_publish_guard(page, rows, monkeypatch):
    """Нажатие на Website — через `_cdp_mouse_click` (охрана Publish/футера)."""
    calls = []
    real = md._cdp_mouse_click
    monkeypatch.setattr(md, "_cdp_mouse_click", lambda p, x, y: calls.append((x, y)) or real(p, x, y))
    _open(page, DEST["instant_experience"])
    _meta_switches_type_on_click(page)
    assert md.set_ad_destination_website(page, URL) is True
    assert calls, "Website не нажимали"


def test_a_website_destination_is_not_touched(page, rows):
    _open(page, _website_after_switch())
    assert md._ensure_destination_website(page) == ("", {})
    assert not [r for r in rows if r[0] == "destination_website_selected"]


_KEYS_SEEN = """<script>
  window.keys = [];
  document.addEventListener('keydown', (e) => window.keys.push(e.key), true);
</script>"""


def test_a_dialog_opened_by_the_website_click_stops_the_step(page, rows):
    """Ревью PR #372 (B1): после нажатия открылся диалог Meta — больше ничего
    не жмём, Space не нажимается, диалог остаётся как был."""
    _open(page, DEST["instant_experience"], _KEYS_SEEN)
    page.evaluate("""() => document.addEventListener('click', (e) => {
        if (!(e.target.closest && e.target.closest('input[value="external"]'))) return;
        e.preventDefault();
        const d = document.createElement('div');
        d.setAttribute('role', 'dialog');
        d.innerHTML = '<p>Switch destination?</p><button id="ok">OK</button>';
        document.body.appendChild(d);
        d.querySelector('#ok').focus();
      }, true)""")
    assert md.set_ad_destination_website(page, URL) is False
    diag = md.last_destination_diag()
    assert diag["why"] == "dialog_open", diag
    assert diag["dialogs_before"] == 0 and diag["dialogs_after"] == 1
    assert page.evaluate("window.keys") == [], "в открытый диалог нажимали клавиши"
    assert page.locator('[role=dialog]').count() == 1
    assert [(r[1], r[2]["note"]) for r in rows if r[0] == "destination_website_selected"] == [
        (False, "dialog_open")]


def test_space_is_not_pressed_when_the_website_radio_has_no_focus(page, rows):
    """Ревью PR #372 (B1): фокус уходит в поле имени — пробел туда не печатается."""
    _open(page, NAME["ad_name_section"] + DEST["instant_experience"], _KEYS_SEEN)
    page.evaluate("""() => {
        const name = document.querySelector('input[placeholder="Enter your ad name here..."]');
        const web = document.querySelector('input[value="external"]');
        web.addEventListener('click', (e) => e.preventDefault());
        web.addEventListener('focus', () => name.focus());
      }""")
    assert md.set_ad_destination_website(page, URL) is False
    assert md.last_destination_why() == "website_radio_not_focused"
    assert page.locator('input[placeholder="Enter your ad name here..."]').input_value() == "Example Ad"
    assert " " not in page.evaluate("window.keys")


def test_space_goes_to_the_website_radio_when_the_click_did_not_take(page, rows):
    """Нажатие мимо, Space на радио в фокусе — тип Website."""
    _open(page, DEST["instant_experience"])
    page.evaluate("""(html) => {
        // Живое состояние, как у Meta: отмечено Instant Experience (атрибут
        // `checked` на Website — только от первой отрисовки).
        document.querySelector('input[value="instant_experience"]').checked = true;
        document.querySelector('input[value="external"]').checked = false;
        document.addEventListener('click', (e) => {
          if (!(e.target.closest && e.target.closest('input[value="external"]'))) return;
          if (e.detail !== 0) { e.preventDefault(); return; }   // мышь — мимо
          document.getElementById('adgroupAdDestinationGeneric').outerHTML = html;
        }, true);
      }""", _website_after_switch())
    assert md.set_ad_destination_website(page, URL) is True
    switched = [r for r in rows if r[0] == "destination_website_selected"]
    assert switched[-1][1] is True and switched[-1][2]["diag"]["via"] == "space"


def test_the_sweep_names_why_an_ad_was_not_fixed(monkeypatch):
    """Ревью PR #372: обход прежнего редактора тоже называет причину."""
    monkeypatch.setattr(md, "_open_tree_ad", lambda p, name: True)
    monkeypatch.setattr(md, "_ad_destination_persisted", lambda p, url: False)
    monkeypatch.setattr(md.time, "sleep", lambda s: None)

    def destination(p, url):
        md._last_destination.update(why="destination_not_website", diag={})
        return False

    monkeypatch.setattr(md, "set_ad_destination_website", destination)
    failed = md.verify_and_fix_ad_destinations(MagicMock(), ["Ad 1", "Ad 2"], URL)
    assert failed[:2] == ["Ad 1:destination_not_website", "Ad 2:destination_not_website"]
    assert acl_sentry.failure_reason("destination_sweep:" + failed[0]) == "destination-not-website"


def test_a_sweep_that_raised_is_named(monkeypatch):
    monkeypatch.setattr(md, "_open_tree_ad", lambda p, name: True)
    monkeypatch.setattr(md, "_ad_destination_persisted", lambda p, url: False)
    monkeypatch.setattr(md.time, "sleep", lambda s: None)

    def boom(p, url):
        raise RuntimeError("x")

    monkeypatch.setattr(md, "set_ad_destination_website", boom)
    assert md.verify_and_fix_ad_destinations(MagicMock(), ["Ad 1"], URL)[0] == "Ad 1:destination_exception"


# ── Снимок 7e5f: Website, а Browser add-ons — Instant form ──────────────────


def test_instant_form_that_does_not_let_go_is_named(page):
    """ACL-1543: причина называет Instant form; полевая — в `field_why`."""
    _open(page, DEST["website_instant_form"])
    assert md.set_ad_destination_website(page, URL) is False
    diag = md.last_destination_diag()
    assert diag["why"] == "instant_form_not_switched", diag
    assert diag["field_why"] == "addon_none_not_set"
    assert diag["dest_type"] == "external"
    assert diag["addon"] == "instant_form"
    assert diag["lead_terms"] is True


def test_instant_form_back_after_the_save_is_named(page, monkeypatch):
    """«None» встал, URL на месте, а к проверке add-ons снова «Instant form»."""
    _open(page, DEST["website_instant_form"])
    monkeypatch.setattr(md, "_select_browser_addon_none", lambda p: True)
    assert md.set_ad_destination_website(page, URL) is False
    diag = md.last_destination_diag()
    assert diag["why"] == "instant_form_not_switched", diag
    assert diag["field_why"] == "addon_not_none"
    assert diag["url"] == "match" and diag["addon"] == "instant_form"


def test_a_url_that_did_not_stay_is_named(page, monkeypatch):
    """Meta стирает введённый URL (модель: поле очищается на каждый ввод)."""
    _open(page, _website_after_switch(), """<script>
      document.addEventListener('input', (e) => { e.target.value = ''; }, true);
    </script>""")
    monkeypatch.setattr(md, "_select_browser_addon_none", lambda p: True)
    assert md.set_ad_destination_website(page, URL) is False
    diag = md.last_destination_diag()
    assert diag["why"] == "url_not_kept", diag
    assert diag["url"] == "empty"


def test_the_url_is_not_typed_into_a_field_without_focus(page, monkeypatch):
    _open(page, DEST["website_instant_form"])
    monkeypatch.setattr(md, "_holds_focus", lambda h: False)
    assert md.set_ad_destination_website(page, URL) is False
    # ACL-1543: add-on здесь так и остался «Instant form» — он и назван.
    assert md.last_destination_why() == "instant_form_not_switched"
    assert md.last_destination_diag()["field_why"] == "url_field_not_focused"
    assert page.locator('[data-surface$="/pce_website_url_field"] input').input_value() == ""


def test_a_missing_destination_section_is_named(page):
    _open(page, "<div>Ad creative</div>")
    assert md.set_ad_destination_website(page, URL) is False
    assert md.last_destination_why() == "destination_section_missing"


def test_the_destination_state_never_carries_the_url(page):
    _open(page, _website_after_switch())
    assert md.set_ad_destination_website(page, URL) is True
    state = md.destination_state(page, URL, full=True)
    assert state["url"] == "match"
    assert "example.com" not in json.dumps(state)


def test_a_new_attempt_forgets_the_old_reason(page):
    _open(page, DEST["instant_experience"])
    assert md.set_ad_destination_website(page, URL) is False
    _open(page, _website_after_switch())
    assert md.set_ad_destination_website(page, URL) is True
    assert md.last_destination_why() == "" and md.last_destination_diag() == {}


# ── Поле имени ──────────────────────────────────────────────────────────────


def _why(page, info=None) -> dict:
    return md.ad_name_failure(page, info if info is not None else {"tries": 5, "frozen": 0, "error": "timeout"})


def test_a_ready_name_field_names_how_the_attempt_ended(page):
    _open(page, NAME["ad_name_section"])
    got = _why(page)
    assert got["why"] == "name_fill_timeout"
    assert got["shown"] == 1 and got["sections"] == 1
    assert "Example Ad" not in json.dumps(got), "имя объявления ушло в diag"


def test_two_name_fields_are_named(page):
    _open(page, NAME["ad_name_section"] * 2)
    assert _why(page)["why"] == "name_field_duplicated"


def test_a_rewritten_placeholder_is_named(page):
    _open(page, NAME["ad_name_section"].replace("Enter your ad name here...", "Name your ad"))
    got = _why(page)
    assert got["why"] == "name_placeholder_changed"
    assert got["placeholder"] == "Name your ad"


def test_a_hidden_name_field_is_named(page):
    _open(page, NAME["ad_name_section"].replace(
        '<div id="adgroupNameEditorComponent">', '<div id="adgroupNameEditorComponent" style="display:none">'))
    assert _why(page)["why"] == "name_field_hidden"


def test_a_disabled_name_field_is_named(page):
    _open(page, NAME["ad_name_section"].replace('aria-disabled="false"', 'aria-disabled="true"'))
    assert _why(page)["why"] == "name_field_disabled"


def test_a_covered_name_field_is_named(page):
    cover = ('<div role="dialog" data-surface="/am/lib:modal" '
             'style="position:fixed;inset:0;background:#fff"><h2>Set up your creative</h2></div>')
    _open(page, NAME["ad_name_section"] + cover)
    got = _why(page)
    assert got["why"] == "name_field_covered"
    assert got["cover"]["dialog"] is True


def test_an_open_dialog_without_the_name_section_is_named(page):
    _open(page, '<div role="dialog"><h2>Set up your creative</h2></div>')
    got = _why(page)
    assert got["why"] == "dialog_open"
    assert got["dialogs"] == 1
    assert "Set up your creative" not in json.dumps(got), "текст диалога ушёл в diag"


def test_an_editor_on_the_ad_set_level_is_named(page):
    _open(page, '<span data-surface="/am/editor_drawer/editor:ad_set"><div>Audience</div></span>')
    got = _why(page)
    assert got["why"] == "not_on_ad_level"
    assert got["levels"] == ["ad_set"]


def test_an_ad_editor_without_the_name_section_is_named(page):
    _open(page, '<span data-surface="/am/editor_drawer/editor:ad"><div>Ad creative</div></span>')
    assert _why(page)["why"] == "name_section_missing"


def test_a_tab_that_never_answered_is_named(page):
    _open(page, NAME["ad_name_section"])
    assert _why(page, {"tries": 0, "frozen": 5})["why"] == "tab_frozen"


@pytest.mark.parametrize("exc, kind", [
    (Exception("Locator.fill: strict mode violation: resolved to 2 elements"), "strict_mode"),
    (Exception('<div role="dialog"> intercepts pointer events'), "intercepted"),
    (Exception("Element is not editable"), "not_editable"),
    (Exception("Element is not attached to the DOM"), "detached"),
    (TimeoutError("Timeout 8000ms exceeded."), "timeout"),
    (RuntimeError("boom"), "error"),
])
def test_the_fill_error_is_a_label_not_the_text(exc, kind):
    assert md._fill_error_kind(exc) == kind


# ── Причины годятся Sentry как метки (не «silent», не «other») ─────────────

_REASONS = [
    "destination_not_website", "website_option_missing", "destination_section_missing",
    "destination_section_hidden", "url_field_missing", "url_field_duplicated",
    "url_field_not_focused", "url_insert_error", "addon_none_not_set", "url_not_kept",
    "addon_not_none", "destination_exception", "destination_not_set", "tab_frozen", "page_closed",
    "name_field_duplicated", "name_field_disabled", "name_field_covered", "name_field_hidden",
    "name_placeholder_changed", "name_field_missing", "dialog_open", "not_on_ad_level",
    "name_section_missing", "page_unreadable", "name_fill_timeout", "name_fill_strict_mode",
    "name_fill_intercepted", "name_fill_not_editable", "name_fill_detached", "name_fill_error",
    "name_fill_failed", "not_the_new_ad_after_reload", "website_radio_not_focused",
    # ACL-1543
    "instant_form_not_switched", "instant_form_switch_on",
]


@pytest.mark.parametrize("why", _REASONS)
def test_every_reason_is_a_sentry_label(why):
    assert acl_sentry.failure_reason("ad_name", why) == why.replace("_", "-")


def test_every_reason_in_the_code_is_listed():
    """Новая причина в коде — сюда же, и она проверяется как метка Sentry выше."""
    source = (ROOT / "src" / "fb_ads_rpa" / "meta_dom.py").read_text(encoding="utf-8")
    block = source[source.index("_last_destination: dict"):source.index("def _name_in_the_field")]
    used = set(re.findall(r"why: '([a-z_]+)'", block))
    used |= set(re.findall(r'why = "([a-z_]+)"', block))
    used |= set(re.findall(r'_destination_failed\(page, "([a-z_]+)"', block))
    used |= set(re.findall(r'return "([a-z_]+)"', block)) - {"strict_mode", "intercepted", "not_editable",
                                                            "detached", "timeout", "error"}
    used |= {"name_fill_" + k for k in re.findall(r'return "([a-z_]+)"',
                                                    block[block.index("def _fill_error_kind"):])}
    # `name_field_ready` — не причина: поле в порядке, причина — метка попытки.
    used -= {"name_field_ready", "name_fill_"}
    assert len(used) > 20, used
    assert used <= set(_REASONS), used - set(_REASONS)


# ── Поток: строки отказа несут причину ─────────────────────────────────────


@pytest.fixture
def flow(monkeypatch, tmp_path):
    monkeypatch.setattr(rt, "TELEMETRY_PATH", tmp_path / "telemetry.jsonl")
    page = MagicMock()
    loc = MagicMock()
    loc.first = loc
    loc.bounding_box.return_value = None
    loc.input_value.return_value = "Example"
    loc.evaluate.return_value = None
    for name in ("get_by_role", "get_by_text", "locator"):
        getattr(page, name).return_value = loc
        getattr(loc, name).return_value = loc
    loc.get_by_placeholder.return_value = loc
    name_loc = MagicMock()
    name_loc.first = name_loc
    name_loc.wait_for.side_effect = TimeoutError("Timeout 8000ms exceeded.")
    page.get_by_placeholder.side_effect = lambda ph, **k: name_loc if ph == md._AD_NAME_PLACEHOLDER else loc
    page.evaluate.return_value = None
    page.url = "https://adsmanager.facebook.com/adsmanager/manage/ads/edit"

    monkeypatch.setattr(br, "ensure_editor_window", lambda p, section: True)
    monkeypatch.setattr(md, "_clear_blocking_overlays", MagicMock(return_value=[]))
    for name in ("_recover_from_browser_error", "_dismiss_floating_banners", "_sleep_h",
                 "wait_editor_saved", "_ensure_campaign_budget", "scroll_to_section",
                 "click_conversion_location_dropdown", "pick_website_conversion_option",
                 "is_website_conversion_selected", "select_conversion_event_lead",
                 "fill_audience_age", "fill_placements"):
        monkeypatch.setattr(md, name, MagicMock(return_value=True))
    monkeypatch.setattr(md, "_roots", lambda p: [p])
    monkeypatch.setattr(md, "_first_visible", lambda *a, **kw: None)
    monkeypatch.setattr(rt, "window_state", lambda p: {})
    monkeypatch.setattr(rt, "page_context", lambda p: {})
    monkeypatch.setattr(screenshots, "capture", lambda *a, **kw: None)
    monkeypatch.setattr(page_capture, "capture", lambda *a, **kw: None)
    monkeypatch.setattr(md, "upload_ad_creative_with_retry", lambda *a, **k: True)
    monkeypatch.setattr(md, "write_ad_copy", lambda *a, **k: True)
    monkeypatch.setattr(md, "create_additional_ad", lambda p, name: True)
    monkeypatch.setattr(md, "_open_tree_ad", lambda p, name: False)
    monkeypatch.setattr(md, "_tree_shell_ids", lambda p: frozenset())
    monkeypatch.setattr(md, "verify_and_fix_ad_destinations", lambda p, names, url: [])
    monkeypatch.setattr(md, "recover_stuck_page", lambda *a, **k: False)
    monkeypatch.setattr(md, "_loading_visible", lambda p: False)
    monkeypatch.setattr(md, "_page_read_trouble", lambda p, *a, **k: "")
    probes: list = []

    def name_state(p, js, arg=None, timeout_s=0):
        probes.append(arg)
        return {"why": "name_field_hidden", "fields": 1, "shown": 0}

    monkeypatch.setattr(md, "_eval_bounded", name_state)

    def destination(p, url):
        md._last_destination.update(why="destination_not_website", diag={"dest_type": "instant_experience"})
        return False

    monkeypatch.setattr(md, "set_ad_destination_website", destination)

    def run():
        soft: list[str] = []
        result = md.run_recorded_campaign_flow(
            page, campaign_name="Example", budget="10", adset_name="Set",
            creative_path="a.png", ad_title="t1", ad_body="b1",
            destination_url="https://example.com",
            additional_ads=[{"ad_name": "Ad 2", "creative_type": "Image", "creative_path": "b.png",
                             "ad_title": "t2", "ad_body": "b2"}],
            soft_fails_out=soft,
        )
        rows = [json.loads(line) for line in rt.TELEMETRY_PATH.read_text(encoding="utf-8").splitlines()]
        return result, soft, rows, probes

    return run


def _failed(rows, step):
    return [r for r in rows if r["step"] == step and r["ok"] is False]


def test_failed_name_rows_carry_the_reason(flow):
    _result, soft, rows, probes = flow()
    assert "ad_name" in soft and "ad2_name" in soft
    for step in ("ad_name", "ad2_name"):
        got = _failed(rows, step)
        assert got, step
        assert got[-1]["note"] == "name_field_hidden", got[-1]
        assert got[-1]["diag"]["tries"] == 5
        assert got[-1]["diag"]["error"] == "timeout"
    assert md._AD_NAME_PLACEHOLDER in probes


def test_failed_destination_rows_carry_the_reason(flow):
    _result, soft, rows, _ = flow()
    for step in ("ad_destination", "ad2_destination"):
        got = _failed(rows, step)
        assert got, step
        assert got[-1]["note"] == "destination_not_website", got[-1]
        assert got[-1]["diag"]["dest_type"] == "instant_experience"


def test_a_destination_without_a_recorded_reason_is_still_named(flow, monkeypatch):
    monkeypatch.setattr(md, "set_ad_destination_website", lambda p, url: False)
    md._last_destination.update(why="", diag={})
    _result, _soft, rows, _ = flow()
    assert _failed(rows, "ad_destination")[-1]["note"] == "destination_not_set"


def test_a_destination_that_raised_is_named(flow, monkeypatch):
    def boom(p, url):
        raise ValueError("unexpected")

    monkeypatch.setattr(md, "set_ad_destination_website", boom)
    monkeypatch.setattr(md, "_page_gone", lambda p, exc: False)
    _result, soft, rows, _ = flow()
    row = _failed(rows, "ad_destination")[-1]
    assert row["note"] == "destination_exception"
    assert row["diag"]["error"] == "ValueError"
