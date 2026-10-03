# -*- coding: utf-8 -*-
"""Single-screen editor: "Instant form (suggested)" goes back to Website (ACL-1543).

03.10, Facebook_1443 (live build f09a6db): run 04509d5eed75 ad 1 and run
0e86c42023da ad 2 stopped on `ad_destination` with `url_field_not_focused`
(diag `dest_type: external`, `addon: instant_form`, `url_fields: 0`,
`section_shown: false`, Lead Gen error #100). `browser_addon_repair` could not
read the add-ons either (`none_checked: null`), and the campaign ended
"do not publish" with a reason that did not say Instant form.

Fixture: `ad_destination_instant_form_2026_10_03.html` — the destination
section of 04509d5eed75/05_ad_destination, scrubbed by
`scripts/capture_to_fixture.py`: Website selected, the "Instant form" opt-in
switch, Browser add-ons on "Instant form (suggested)" with the "Please read
and accept Meta's Lead Ads Terms…" card (View terms / Close / Create form),
and the Lead Gen error from the ad's error block.

In the capture the section is `display: none !important` inside Meta's
`data-virtualized` wrapper — every ad section of that page was, the editor
was scrolled elsewhere. Meta's CSS and scripts are not in the bench, so the
test models them:

* virtualisation: while the wrapper is off screen the section is hidden again
  whenever its style changes or the page scrolls (forcing `display: block`
  alone does not hold);
* Browser add-ons: a click on a radio checks it; leaving "Instant form" takes
  the Lead Ads Terms card and the Lead Gen error away;
* the "Instant form" opt-in switch toggles on a click.
"""
from __future__ import annotations

import pytest

from src.fb_ads_rpa import failure_policy as fp
from src.fb_ads_rpa import meta_dom as md
from src.fb_ads_rpa import simple_create as sc
from tests.bench_support import FIXTURES, browser, fragment, offline_page  # noqa: F401

FIXTURE = FIXTURES / "ad_destination_instant_form_2026_10_03.html"
URL = "https://example.com/landing?src=test"

#: Модель Meta поверх вырезки (см. описание модуля).
_META_MODEL = r"""
([sticky, stickySwitch]) => {
  const sec = document.getElementById('adgroupAdDestinationGeneric');
  const wrap = sec.closest('[data-virtualized]');
  const inView = () => {
    const r = wrap.getBoundingClientRect();
    return r.bottom > 0 && r.top < window.innerHeight;
  };
  const sync = () => {
    if (inView()) {
      if (sec.style.getPropertyValue('display') === 'none') sec.style.removeProperty('display');
    } else if (sec.style.getPropertyValue('display') !== 'none') {
      sec.style.setProperty('display', 'none', 'important');
    }
  };
  new MutationObserver(sync).observe(sec, {attributes: true, attributeFilter: ['style']});
  window.addEventListener('scroll', sync, true);
  sync();

  window.__clicks = [];
  document.addEventListener('click', (e) => {
    const b = e.target.closest && e.target.closest('[role=button], button, a');
    if (b) window.__clicks.push((b.textContent || '').replace(/\s+/g, ' ').trim());
  }, true);

  const group = '[data-surface$="/pce_website_extension_field"] input[type=radio]';
  const setAddon = (value) => {
    for (const r of sec.querySelectorAll(group)) {
      const on = r.value === value;
      r.checked = on;
      r.setAttribute('aria-checked', on ? 'true' : 'false');
    }
    if (value !== 'instant_form') {
      for (const card of document.querySelectorAll(
          '[data-surface$="/pce_website_extension_instant_form_field"]')) card.remove();
      for (const err of document.querySelectorAll('[data-surface*="GeoCollapsibleGuidanceCard/error"]')) {
        err.remove();
      }
    }
  };
  document.addEventListener('click', (e) => {
    const r = e.target.closest && e.target.closest(group);
    if (!r) return;
    setAddon(r.value);
    // «Упрямая» Meta: подсказка Instant form возвращается сразу же.
    if (sticky) queueMicrotask(() => setAddon('instant_form'));
  });
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'ArrowUp') return;
    const r = e.target.closest && e.target.closest(group);
    if (!r) return;
    const all = [...sec.querySelectorAll(group)];
    const i = all.indexOf(r);
    if (i > 0) {
      e.preventDefault();
      setAddon(all[i - 1].value);
      all[i - 1].focus();
      if (sticky) queueMicrotask(() => setAddon('instant_form'));
    }
  });
  const sw = sec.querySelector('[data-surface$="/pce_lead_gen_instant_form_opt_in_field"] input[role=switch]');
  sw.addEventListener('click', () => {
    if (stickySwitch) { sw.checked = true; sw.setAttribute('aria-checked', 'true'); return; }
    sw.setAttribute('aria-checked', sw.checked ? 'true' : 'false');
  });
}
"""


def _open(page, *, sticky: bool = False, switch_on: bool = False, sticky_switch: bool = False) -> None:
    section = fragment(FIXTURE, "simple_create_instant_form_terms")
    if switch_on:
        section = section.replace('aria-checked="false" aria-label="Instant form" role="switch"',
                                  'aria-checked="true" aria-label="Instant form" role="switch" checked=""')
    page.set_content(
        '<!doctype html><html><body style="margin:0;width:1200px">'
        '<div style="height:2600px">Ad creative</div>'
        '<span aria-hidden="true"></span>'
        f'<div data-virtualized="true" style="min-height:1181px"><div>{section}</div></div>'
        f'<div>{fragment(FIXTURE, "errors_lead_gen")}</div>'
        '<div style="height:2600px">Tracking</div>'
        '</body></html>')
    page.evaluate(_META_MODEL, [sticky, sticky_switch])


@pytest.fixture
def rows(monkeypatch):
    got: list = []
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(md, "wait_editor_saved", lambda *a, **k: None)
    monkeypatch.setattr(md, "_close_creative_setup_modal", lambda p: False)
    monkeypatch.setattr(md, "_tab_gone", lambda p: False)
    monkeypatch.setattr(md._telemetry, "record", lambda step, ok=None, **k: got.append((step, ok, k)))
    return got


def test_the_capture_reads_as_it_did_live(offline_page, rows):
    _open(offline_page)
    state = md.destination_state(offline_page, URL, full=True)
    assert state["dest_type"] == "external"
    assert state["addon"] == "instant_form"
    assert state["opt_in"] == "off"
    assert state["section_shown"] is False and state["url_fields"] == 0
    # Как в живом diag: ошибка Lead Gen видна, а текст карточки условий — нет
    # (раздел спрятан, `innerText` его не читает).
    assert state["lead_gen_error"] is True and state["lead_terms"] is False


def test_instant_form_goes_back_to_website_and_the_url_is_filled(offline_page, rows):
    """На прежнем коде шаг падал: раздел снова прятался, поле URL без фокуса."""
    _open(offline_page)
    assert md.set_ad_destination_website(offline_page, URL) is True, md.last_destination_diag()
    state = md.destination_state(offline_page, URL, full=True)
    assert state["dest_type"] == "external"
    assert state["addon"] == "none"
    assert state["url"] == "match"
    assert state["lead_gen_error"] is False and state["lead_terms"] is False
    assert md.last_destination_why() == ""


def test_lead_ads_terms_and_create_form_are_never_pressed(offline_page, rows):
    _open(offline_page)
    md.set_ad_destination_website(offline_page, URL)
    clicks = offline_page.evaluate("window.__clicks")
    assert not [c for c in clicks if c in ("View terms", "Create form", "Close")], clicks


def test_the_switch_is_recorded_with_before_and_after(offline_page, rows):
    _open(offline_page)
    assert md.set_ad_destination_website(offline_page, URL) is True
    got = [r for r in rows if r[0] == "destination_instant_form_off"]
    assert len(got) == 1, rows
    step, ok, kw = got[0]
    assert ok is True and kw["note"] == "switched_to_website"
    diag = kw["diag"]
    assert diag["before"]["addon"] == "instant_form"
    assert diag["after"]["addon"] == "none"
    assert diag["before"]["dest_type"] == diag["after"]["dest_type"] == "external"
    assert diag["via"] == ["addon_none"]
    # URL в строку телеметрии не попадает.
    assert "example.com" not in repr(kw)


def test_the_opt_in_switch_is_turned_off(offline_page, rows):
    _open(offline_page, switch_on=True)
    assert md.destination_state(offline_page)["opt_in"] == "on"
    assert md.set_ad_destination_website(offline_page, URL) is True, md.last_destination_diag()
    state = md.destination_state(offline_page, URL)
    assert state["opt_in"] == "off" and state["addon"] == "none" and state["url"] == "match"
    diag = [r for r in rows if r[0] == "destination_instant_form_off"][0][2]["diag"]
    assert diag["via"] == ["opt_in_switch", "addon_none"]
    assert diag["before"]["opt_in"] == "on" and diag["after"]["opt_in"] == "off"


def test_instant_form_that_comes_back_is_blocking_and_named(offline_page, rows):
    _open(offline_page, sticky=True)
    assert md.set_ad_destination_website(offline_page, URL) is False
    assert md.last_destination_why() == "instant_form_not_switched"
    diag = md.last_destination_diag()
    assert diag["addon"] == "instant_form"
    assert diag["field_why"] in ("addon_not_none", "addon_none_not_set")
    assert diag["instant_form_before"]["addon"] == "instant_form"
    off = [r for r in rows if r[0] == "destination_instant_form_off"]
    assert off and off[0][1] is False and off[0][2]["note"] == "instant_form_kept"
    clicks = offline_page.evaluate("window.__clicks")
    assert not [c for c in clicks if c in ("View terms", "Create form", "Close")], clicks

    fail = md.destination_soft_fail("ad2", "Investment ad 2")
    assert fail.startswith("ad2_destination:instant_form_not_switched - Investment ad 2: ")
    assert "Instant form" in fail and "Website" in fail
    assert fp.severity(fail) == fp.BLOCKING
    problem = fp.problem(fail)
    assert "Instant form" in problem["title"]
    assert problem["ad_index"] == 2 and problem["ad_name"] == "Investment ad 2"
    assert "Instant form" in fp.reason(fail)


def test_other_destination_failures_keep_the_plain_row(monkeypatch):
    md._last_destination.update(why="url_not_kept", diag={})
    assert md.destination_soft_fail("ad", "Ad 1") == "ad_destination"
    md._last_destination.update(why="", diag={})


def test_the_why_text_names_instant_form():
    text = sc.destination_why_text("instant_form_not_switched")
    assert "Instant form" in text and "Lead Ads Terms" in text
    assert sc.destination_why_text("something_else") == ""


@pytest.mark.parametrize("label", ["View terms", "Create form", "Close"])
def test_the_press_guard_refuses_the_lead_ads_terms_card(offline_page, rows, label):
    """Даже если точка нажатия попала в карточку условий — нажатия нет."""
    _open(offline_page)
    offline_page.evaluate(
        "() => document.getElementById('adgroupAdDestinationGeneric')"
        ".closest('[data-virtualized]').scrollIntoView({block: 'start'})")
    btn = offline_page.locator('[role=button]', has_text=label).last
    box = btn.bounding_box()
    assert box, label
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    assert md._press_allowed(offline_page, x, y, "") is False
    refused = [r for r in rows if r[0] == "click_refused"]
    assert refused and refused[-1][2]["note"] == "lead_terms"


# ── Ревью PR #545 ──────────────────────────────────────────────────────────


def test_a_switch_that_stays_on_is_blocking_and_named(offline_page, rows):
    """B2: «Instant form» opt-in не выключился — URL и «None» встали, но это не Website."""
    _open(offline_page, switch_on=True, sticky_switch=True)
    assert md.set_ad_destination_website(offline_page, URL) is False
    assert md.last_destination_why() == "instant_form_not_switched"
    diag = md.last_destination_diag()
    assert diag["opt_in"] == "on" and diag["field_why"] == "instant_form_switch_on"
    off = [r for r in rows if r[0] == "destination_instant_form_off"]
    assert off and off[0][1] is False
    assert md.destination_soft_fail("ad", "Ad 1").startswith("ad_destination:instant_form_not_switched")


def test_the_sweep_does_not_accept_a_switch_that_is_on(offline_page, rows):
    _open(offline_page, switch_on=True, sticky_switch=True)
    md._show_destination_section(offline_page)
    box = md.destination_url_box(offline_page)
    box.fill(URL)
    offline_page.evaluate(
        "() => { for (const r of document.querySelectorAll('input[type=radio]')) {"
        " if (r.value === 'none') r.setAttribute('aria-checked', 'true');"
        " if (r.value === 'instant_form') r.setAttribute('aria-checked', 'false'); }"
        " for (const e of document.querySelectorAll('[data-surface*=\"GeoCollapsibleGuidanceCard/error\"]'))"
        " e.remove(); }")
    assert md._browser_addon_none_checked(offline_page) is True
    assert md._ad_destination_persisted(offline_page, URL) is False


def test_the_url_field_hidden_again_is_retried_after_re_showing(offline_page, rows, monkeypatch):
    """Раздел спрятали между пометкой поля и нажатием: вторая попытка после показа."""
    _open(offline_page)
    real = md._focus_via_cdp_click
    calls = {"n": 0}

    def first_misses(page, locator):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return real(page, locator)

    monkeypatch.setattr(md, "_focus_via_cdp_click", first_misses)
    assert md.set_ad_destination_website(offline_page, URL) is True, md.last_destination_diag()
    assert calls["n"] >= 2


def _press_target(page, html: str) -> tuple[float, float]:
    page.set_content(f'<!doctype html><html><body>{html}</body></html>')
    box = page.locator("#target").bounding_box()
    return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2


def test_the_guard_reaches_a_button_deep_inside_the_card(offline_page, rows):
    """Живьём «View terms» на 16 уровней ниже карточки; кнопка без особого текста тоже."""
    deep = "<div>" * 20 + '<div role="button" id="target">Learn more</div>' + "</div>" * 20
    x, y = _press_target(
        offline_page,
        '<span data-surface="/am/editor:ad/ad_destination_generic_section/pce_website_extension_field/'
        f'pce_website_extension_instant_form_field">{deep}</span>')
    assert md._press_allowed(offline_page, x, y, "") is False
    assert rows[-1][0] == "click_refused" and rows[-1][2]["note"] == "lead_terms"


def test_the_guard_reads_the_aria_label_of_an_icon_button(offline_page, rows):
    x, y = _press_target(offline_page, '<div role="button" aria-label="Accept" id="target">&#10003;</div>')
    assert md._press_allowed(offline_page, x, y, "") is False
    assert rows[-1][2]["note"] == "lead_terms"


def test_the_guard_lets_the_destination_controls_through(offline_page, rows):
    _open(offline_page)
    md._show_destination_section(offline_page)
    for sel in ('input[value="none"]', 'input[value="external"]', 'input[role=switch]',
                '[data-surface$="/pce_website_url_field"] input'):
        el = offline_page.locator(sel).first
        el.scroll_into_view_if_needed()
        box = el.bounding_box()
        assert box, sel
        assert md._press_allowed(offline_page, box["x"] + box["width"] / 2,
                                 box["y"] + box["height"] / 2, "") is True, sel
