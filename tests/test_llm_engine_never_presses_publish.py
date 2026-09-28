# -*- coding: utf-8 -*-
"""Движок `llm_agent` не нажимает Publish ни при каком совпадении (ACL-1359, аудит S08).

Прежде `LLMAgentPlayer._click` проверял имя, которое предложила модель, а
нажимал `loc.click` — мимо охраны `_press_allowed` — любой элемент, найденный
по подстроке: «Publ» проходил проверку имени и нажимал Publish, то же — Enter
через `press_key`. Теперь подсказка сначала превращается в конкретный
элемент, и охрана (`meta_dom.press_element_guarded`) проверяет именно его:
текст, aria-label / aria-labelledby, предков-кнопок, поверхность и футер
нового редактора, локализованные надписи; для клавиш — ещё и то, что на
самом деле в фокусе. Мышь, кроме того, проходит охрану по точке ACL-1327.

Страницы — офлайн-подделки в Chromium; обычные контролы нажимаются как раньше.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest
from dotenv import dotenv_values
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import llm_agent as la  # noqa: E402
from src.fb_ads_rpa import meta_dom as md  # noqa: E402
from src.fb_ads_rpa import wizard_guard as wg  # noqa: E402
from src.fb_ads_rpa.draft import AdSetDraft, CampaignDraft  # noqa: E402

FOOTER = (ROOT / "tests" / "fixtures" / "ads_manager"
          / "editor_footer_simple_create_2026_09_28.html").read_text(encoding="utf-8")

SIMPLE_URL = ("https://adsmanager.facebook.com/adsmanager/manage/simple_create"
              "?act=1&selected_campaign_ids=2&selected_adset_ids=3&selected_ad_ids=4")
CLASSIC_URL = "https://adsmanager.facebook.com/adsmanager/manage/ads/edit/standalone?act=1"

# Длинный текст страницы: футер-правило охраны смотрит на короткие блоки.
_FILLER = "<p>" + ("Campaign settings text. " * 40) + "</p>"

# Обычные контролы; любое нажатие по `.pub` (мышь, Enter/Space, отправка
# формы) считается в window.hits.publish.
_PAGE = """
<!doctype html><html><body style="margin:0; padding:20px">
FILLER
<button id="next">Next</button>
<div role="radio" id="website" tabindex="0" aria-checked="false"
     style="display:inline-block; width:120px; height:24px">Website</div>
<label>Campaign name <input id="cname" aria-label="Campaign name"></label>
<div style="margin-top:20px">PUBLISH</div>
<script>
  window.hits = {publish: 0, next: 0, website: 0, websiteKeys: 0};
  document.addEventListener('click', (e) => {
    if (e.target.closest('.pub, [data-surface*="syd_simple_create_publish"]')) window.hits.publish++;
    if (e.target.closest('#next')) window.hits.next++;
    if (e.target.closest('#website')) window.hits.website++;
  }, true);
  document.addEventListener('keydown', (e) => {
    if ((e.key === 'Enter' || e.key === ' ')
        && e.target.closest('.pub, [data-surface*="syd_simple_create_publish"]')) window.hits.publish++;
    if (e.key === ' ' && e.target.closest('#website')) window.hits.websiteKeys++;
  }, true);
  document.addEventListener('submit', (e) => { e.preventDefault(); window.hits.publish++; }, true);
</script>
</body></html>
"""

# (что вставлено на страницу, role, имя от модели). Имя нигде не равно
# запрещённому в `rpa_forbidden_zones.yaml` — ловить должна охрана элемента.
PUBLISH_TARGETS = {
    "partial": ("<button class='pub'>Publish</button>", "button", "Publ"),
    "partial_review_count": ("<div class='pub' role='button' tabindex='0'>Review and publish (2)</div>",
                             "button", "Review and"),
    "localized": ("<div class='pub' role='button' tabindex='0'>Publicar</div>", "button", "Publicar"),
    "localized_review": ("<div class='pub' role='button' tabindex='0'>Revisar y publicar</div>",
                         "button", "Revisar y publicar"),
    "aria_only": ("<div class='pub' role='button' tabindex='0' aria-label='Publish'"
                  " style='width:40px; height:24px'><svg width='16' height='16'></svg></div>",
                  "button", "Pub"),
    "aria_labelledby": ("<span id='lbl' style='display:none'>Publicar</span>"
                        "<div class='pub' role='button' tabindex='0' aria-labelledby='lbl'"
                        " style='width:40px; height:24px'><svg width='16' height='16'></svg></div>",
                        "button", "Publi"),
    "nested_span": ("<div class='pub' role='button' tabindex='0'>"
                    "<span role='img' aria-label='Rocket' tabindex='0'"
                    " style='display:inline-block; width:16px; height:16px'></span>"
                    "<span>Publish</span></div>",
                    "img", "Rocket"),
    # В длинной обёртке: футер-правило молчит, ловит только `data-surface`.
    "surface_any_label": ("<div>" + ("Long editor text. " * 40)
                          + "<div role='button' tabindex='0'"
                          " data-surface='/am/lib:syd_simple_create_publish'>Go live</div></div>",
                          "button", "Go live"),
}


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        try:
            yield b
        finally:
            b.close()


@pytest.fixture
def records(monkeypatch):
    got: list[tuple] = []
    monkeypatch.setattr(md._telemetry, "record", lambda *a, **k: got.append((a, k)))
    return got


@pytest.fixture
def page(browser, monkeypatch, records):
    from src.fb_ads_rpa import page_capture

    monkeypatch.setattr(md, "_sleep_h", lambda s, *a, **k: time.sleep(min(float(s), 0.2)))
    monkeypatch.setattr(md, "_human_jitter", lambda base, spread: 0.0)
    monkeypatch.setattr(la.time, "sleep", lambda s: None)
    monkeypatch.setattr(page_capture, "capture", lambda *a, **k: None)
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    p = ctx.new_page()
    try:
        yield p
    finally:
        ctx.close()


def _open(page, publish_html: str, url: str = CLASSIC_URL) -> la.LLMAgentPlayer:
    html = _PAGE.replace("FILLER", _FILLER).replace("PUBLISH", publish_html)
    page.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=html)
               if route.request.url.startswith("https://adsmanager.facebook.com/") else route.abort())
    page.goto(url)
    return la.LLMAgentPlayer(page)


def _hits(page) -> dict:
    return page.evaluate("window.hits")


def _refusals(records) -> list[dict]:
    return [k for a, k in records if a and a[0] == "click_refused"]


# ── Publish: мышью ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("url", [CLASSIC_URL, SIMPLE_URL])
@pytest.mark.parametrize("case", sorted(PUBLISH_TARGETS))
def test_a_click_that_resolves_to_publish_is_refused(page, records, case, url):
    html, role, name = PUBLISH_TARGETS[case]
    agent = _open(page, html, url)
    assert wg.forbidden_click_reason(name) is None  # имя само по себе не запрещено
    out = agent._execute_tool("click", {"role": role, "name": name})
    assert out.startswith("REFUSED"), out
    assert _hits(page)["publish"] == 0
    refusal = _refusals(records)
    assert refusal and refusal[-1]["diag"]["engine"] == "llm_agent", records


def test_an_exact_publish_is_refused_even_if_the_name_list_misses_it(page, records, monkeypatch):
    """Список имён — первая линия; охрана элемента держит и без неё."""
    agent = _open(page, "<button class='pub'>Publish</button>")
    assert agent._click("button", "Publish").content.startswith("REFUSED")  # по имени
    monkeypatch.setattr(wg, "forbidden_click_reason", lambda *a, **k: None)
    out = agent._click("button", "Publish").content
    assert out.startswith("REFUSED") and "(publish)" in out, out
    assert _hits(page)["publish"] == 0


def test_the_editor_footer_is_refused_even_if_the_name_list_misses_it(page, monkeypatch):
    """Close в футере нового редактора — рядом с Publish; туда не жмём."""
    monkeypatch.setattr(wg, "forbidden_click_reason", lambda *a, **k: None)
    agent = _open(page, FOOTER, SIMPLE_URL)
    out = agent._click("button", "Close").content
    assert out.startswith("REFUSED") and "editor_footer" in out, out
    assert _hits(page)["publish"] == 0


def test_the_point_guard_still_runs_for_the_llm_click(page, monkeypatch):
    """Элемент чист, но в точке нажатия его накрыл Publish — охрана ACL-1327."""
    agent = _open(page, "<div class='pub' role='button' style='position:fixed; left:0; top:0;"
                        " width:100%; height:100%; background:#eee'>Publish</div>")
    out = agent._click("button", "Next").content
    assert out.startswith("REFUSED") and "point_guard" in out, out
    assert _hits(page) == {"publish": 0, "next": 0, "website": 0, "websiteKeys": 0}


# ── Publish: с клавиатуры ──────────────────────────────────────────────────

@pytest.mark.parametrize("key", ["Enter", " ", "Space"])
@pytest.mark.parametrize("case", sorted(PUBLISH_TARGETS))
def test_keyboard_activation_of_publish_is_refused(page, records, case, key):
    html, role, name = PUBLISH_TARGETS[case]
    agent = _open(page, html)
    out = agent._execute_tool("press_key", {"role": role, "name": name, "key": key})
    assert out.startswith("REFUSED"), out
    assert _hits(page)["publish"] == 0
    assert _refusals(records)[-1]["diag"] == {"engine": "llm_agent", "action": "key", "key": key}


@pytest.mark.parametrize("key", ["Enter", " "])
def test_focus_that_jumps_to_publish_is_refused(page, key):
    """Элемент чист, но при фокусе страница переводит фокус на Publish."""
    agent = _open(page, "<div role='button' id='decoy' tabindex='0'>Continue</div>"
                        "<button class='pub' id='p'>Publish</button>"
                        "<script>document.getElementById('decoy').addEventListener('focus',"
                        " () => document.getElementById('p').focus());</script>")
    out = agent._press_key("button", "Continue", key).content
    assert out.startswith("REFUSED") and "(publish)" in out, out
    assert _hits(page)["publish"] == 0


def test_enter_in_a_form_that_submits_through_publish_is_refused(page):
    agent = _open(page, "<form><input aria-label='Budget'>"
                        "<button class='pub' type='submit'>Publish</button></form>")
    out = agent._press_key("textbox", "Budget", "Enter").content
    assert out.startswith("REFUSED") and "form_submits_publish" in out, out
    assert _hits(page)["publish"] == 0


def test_typing_a_newline_into_a_form_that_submits_through_publish_is_refused(page, monkeypatch):
    monkeypatch.setattr(md, "fill_input_near_label_verified", lambda *a, **k: False)
    agent = _open(page, "<form><input aria-label='Budget'>"
                        "<button class='pub' type='submit'>Publish</button></form>")
    out = agent._fill("Budget", "10\n").content
    assert out.startswith("REFUSED") and "form_submits_publish" in out, out
    assert _hits(page)["publish"] == 0


# ── Обычные контролы нажимаются ────────────────────────────────────────────

@pytest.mark.parametrize("url", [CLASSIC_URL, SIMPLE_URL])
def test_ordinary_controls_are_still_clicked(page, records, url):
    agent = _open(page, "<button class='pub'>Publish</button>", url)
    assert agent._click("button", "Next").content.startswith("Clicked")
    assert agent._click("radio", "Website").content.startswith("Clicked")
    assert _hits(page) == {"publish": 0, "next": 1, "website": 1, "websiteKeys": 0}
    assert not _refusals(records)


def test_ordinary_keyboard_activation_still_works(page):
    agent = _open(page, "<button class='pub'>Publish</button>")
    out = agent._press_key("radio", "Website", " ").content
    assert out.startswith("Focused"), out
    assert _hits(page)["websiteKeys"] == 1 and _hits(page)["publish"] == 0


def test_an_input_in_a_form_with_publish_still_takes_plain_text(page, monkeypatch):
    """Печать без Enter форму не отправит — поле заполняется."""
    monkeypatch.setattr(md, "fill_input_near_label_verified", lambda *a, **k: False)
    agent = _open(page, "<form><input aria-label='Budget'>"
                        "<button class='pub' type='submit'>Publish</button></form>")
    out = agent._fill("Budget", "25").content
    assert "verified" in out and not out.startswith("REFUSED"), out
    assert page.input_value("input[aria-label='Budget']") == "25"
    assert _hits(page)["publish"] == 0


def test_fill_by_keyboard_still_works(page, monkeypatch):
    monkeypatch.setattr(md, "fill_input_near_label_verified", lambda *a, **k: False)
    agent = _open(page, "<button class='pub'>Publish</button>")
    out = agent._fill("Campaign name", "Spring test").content
    assert "verified" in out, out
    assert page.input_value("#cname") == "Spring test"


# ── Движок выключен по умолчанию и не просит модель жать Publish ───────────

@pytest.mark.parametrize("value", [None, "", "recorded_flow", "recipe", "LLM"])
def test_the_llm_engine_is_off_unless_explicitly_selected(monkeypatch, value):
    from src.fb_ads_rpa.config import get_use_llm_agent

    if value is None:
        monkeypatch.delenv("FB_RPA_ENGINE", raising=False)
    else:
        monkeypatch.setenv("FB_RPA_ENGINE", value)
    assert get_use_llm_agent() is False
    monkeypatch.setenv("FB_RPA_ENGINE", "llm_agent")
    assert get_use_llm_agent() is True


def test_shipped_configs_do_not_select_the_llm_engine():
    example = dotenv_values(ROOT / ".env.example")
    assert (example.get("FB_RPA_ENGINE") or "").strip().lower() != "llm_agent"
    sys.path.insert(0, str(ROOT / "packaging" / "windows"))
    import machine_config

    assert machine_config.SAFETY_ENV["FB_RPA_ENGINE"] == "recorded_flow"


@pytest.mark.parametrize("dry_run,status", [(True, "DRAFT"), (False, "DRAFT"), (False, "ACTIVE")])
def test_the_prompt_never_asks_to_press_publish(dry_run, status):
    draft = CampaignDraft(name="C1", country="US", account_label="A", status=status,
                          ad_sets=[AdSetDraft(name="S1", daily_budget=10)])
    prompt = la.LLMAgentPlayer(page=None)._build_system_prompt(draft, dry_run=dry_run)
    assert "click the 'Publish' button" not in prompt
    assert "this engine never publishes" in prompt
