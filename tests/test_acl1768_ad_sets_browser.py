"""ACL-1768: наборы объявлений в редакторе билдера (настоящий DOM, офлайн-Chromium).

Проверяется: набор - узел дерева под кампанией; «+ Ad set» и «Copy ad set» работают;
объявление принадлежит набору и лежит в форме по порядку наборов; поля набора уходят под
`camp_N_adset_K_*` и их читает сервер; черновик в localStorage переживает перезагрузку, а
старый (v3, без наборов) открывается как один набор; снимки дерева (светлая и тёмная тема)
пишутся, если задан ACL_SHOTS_DIR.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_builder_duplicate_buttons import browser, page  # noqa: E402,F401
from test_drawer_review_browser import add  # noqa: E402

SLOT = ".cbd-host .cb-slot:not([hidden])"


def _open(p, level="adset"):
    add(p)
    p.locator(".cbd-camp-row").first.click()
    p.locator(f".cbd-seg [data-level='{level}']").click()


def _names(p):
    return p.evaluate(f"() => Array.from(document.querySelector('{SLOT}').querySelectorAll('[name]')).map(e => e.name)")


def _entries(p):
    return p.evaluate("() => Array.from(new FormData(document.querySelector('.cbd-host form.open-account-form')).entries())")


def _sets(p):
    return p.locator(".cbd-sets .cbd-set")


def _ad_sets(p):
    """Номер набора у каждого объявления по порядку в форме."""
    return p.evaluate(f"() => Array.from(document.querySelector('{SLOT}').querySelectorAll('.cb-ad-row')).map(r => r.querySelector('input[name$=\"_adset\"]').value)")


def _fill_set(p, k, **vals):
    p.evaluate("""([k, vals]) => {
      const slot = document.querySelector('.cbd-host .cb-slot:not([hidden])');
      const sec = slot.querySelectorAll('.cb-adset-sec')[k];
      Object.keys(vals).forEach(f => {
        const el = sec.querySelector('[name$="_' + f + '"]');
        el.value = vals[f];
        el.dispatchEvent(new Event('input', {bubbles: true}));
        el.dispatchEvent(new Event('change', {bubbles: true}));
      });
    }""", [k, vals])


def test_a_new_campaign_has_one_ad_set_node_and_the_old_field_names(page):
    _open(page)
    assert _sets(page).count() == 1 and _sets(page).first.get_attribute("aria-current") == "true"
    names = _names(page)
    # прежние имена полей набора 0 не тронуты
    for f in ("targeting", "os", "age", "language", "gender", "languages", "device", "os_min", "start_time", "destination"):
        assert f"camp_0_{f}" in names, f
    assert "camp_0_adset_count" in names and "camp_0_ad_0_adset" in names and "camp_0_adset_0_name" in names
    # одна кампания - один набор: «Remove ad set» выключен
    assert page.locator("[data-act='remove-adset']").is_disabled()


def test_plus_ad_set_adds_a_tree_node_a_section_and_one_ad(page):
    _open(page)
    page.locator("[data-act='add-adset']").click()
    assert _sets(page).count() == 2
    assert _sets(page).nth(1).get_attribute("aria-current") == "true", "новый набор не открыт"
    names = _names(page)
    for f in ("targeting", "os", "age", "language", "gender", "languages", "device", "os_min", "start_time", "destination", "name"):
        assert f"camp_0_adset_1_{f}" in names, f
    assert dict(_entries(page))["camp_0_adset_count"] == "2"
    assert _ad_sets(page) == ["0", "1"], "у нового набора должно быть своё объявление"
    # показан только выбранный набор
    visible = page.evaluate(f"() => Array.from(document.querySelectorAll('{SLOT} .cb-adset-sec')).map(s => getComputedStyle(s).display !== 'none')")
    assert visible == [False, True]
    # выбор узла в дереве переключает набор
    _sets(page).first.click()
    assert page.evaluate(f"() => Array.from(document.querySelectorAll('{SLOT} .cb-adset-sec')).map(s => getComputedStyle(s).display !== 'none')") == [True, False]
    assert page.locator(".cbd-ed-set").input_value() == "0"


def test_copy_ad_set_copies_fields_and_ads_but_not_the_name(page):
    _open(page)
    _fill_set(page, 0, age="41", gender="women", os="iOS", os_min="15", name="Mine")
    page.locator("[data-act='copy-adset']").click()
    assert _sets(page).count() == 2
    e = dict(_entries(page))
    assert e["camp_0_adset_1_age"] == "41" and e["camp_0_adset_1_gender"] == "women"
    assert e["camp_0_adset_1_os"] == "iOS" and e["camp_0_adset_1_os_min"] == "15"
    assert e.get("camp_0_adset_1_name", "") == "", "копия набора не должна получать то же имя"
    assert _ad_sets(page) == ["0", "1"], "объявление источника должно быть скопировано в копию"
    # копия независима от источника
    _fill_set(page, 1, age="22")
    assert dict(_entries(page))["camp_0_age"] == "41"


def test_new_ads_join_the_selected_ad_set_and_the_form_keeps_ads_in_set_order(page):
    _open(page)
    page.locator("[data-act='add-adset']").click()            # набор 1 открыт
    page.locator(f"{SLOT} .cb-add-btn-sm").evaluate("b => b.click()")   # + Ad в наборе 1
    assert _ad_sets(page) == ["0", "1", "1"]
    _sets(page).first.click()                                 # набор 0
    page.locator(f"{SLOT} .cb-add-btn-sm").evaluate("b => b.click()")
    assert _ad_sets(page) == ["0", "0", "1", "1"], "новое объявление набора 0 обязано встать до объявлений набора 1"
    names = _names(page)
    assert [n for n in names if n.endswith("_adset") and "_ad_" in n] == [f"camp_0_ad_{i}_adset" for i in range(4)]
    assert dict(_entries(page))["camp_0_ad_count"] == "4"


def test_removing_an_ad_set_removes_its_ads_and_renumbers_the_next(page):
    _open(page)
    for _ in range(2):
        page.locator("[data-act='add-adset']").click()
    _fill_set(page, 2, age="50")
    _sets(page).nth(1).click()
    page.on("dialog", lambda d: d.accept())
    page.locator("[data-act='remove-adset']").click()
    assert _sets(page).count() == 2
    e = dict(_entries(page))
    assert e["camp_0_adset_count"] == "2" and e["camp_0_adset_1_age"] == "50"
    assert "camp_0_adset_2_age" not in e
    assert _ad_sets(page) == ["0", "1"]


def test_the_server_reads_what_the_page_sends(page):
    from src.fb_ads_rpa.build_draft import slots_to_campaign_drafts
    from src.web import app as web

    page.evaluate("window.ACL_FB_BACKEND = 'api'")
    _open(page)
    page.locator("[data-act='add-adset']").click()
    _fill_set(page, 1, age="33", gender="men", name="Second")
    page.evaluate("""() => {
      const s = document.querySelector('.cbd-host .cb-slot:not([hidden])');
      s.querySelector('[name="camp_0_budget"]').value = '25';
      s.querySelector('[name="camp_0_ad_1_creative_file"]').value = 'second.png';
      cbSyncApiOnly(s);
    }""")
    form = {k: v for k, v in _entries(page)}
    country, label, slots = web._parse_build_upload_form(form)
    [draft] = slots_to_campaign_drafts(slots, account_label=label or "Acc", country=country)
    assert [s.adset_index for s in draft.ad_sets] == [0, 1]
    assert draft.ad_sets[1].name == "Second" and (draft.ad_sets[1].age_min, draft.ad_sets[1].gender) == (33, "men")
    assert [len(s.ads) for s in draft.ad_sets] == [1, 1]
    assert draft.ad_sets[1].ads[0].creative_filename == "second.png"
    assert draft.ad_sets[0].daily_budget == draft.ad_sets[1].daily_budget == 25.0


def test_ad_sets_survive_a_reload_and_a_v3_draft_opens_as_one_ad_set(page):
    _open(page)
    page.locator("[data-act='add-adset']").click()
    _fill_set(page, 1, age="44", name="Persisted")
    page.evaluate("() => cbKeep.save()")
    saved = json.loads(page.evaluate("() => localStorage.getItem('acl.builder.v2')"))
    assert saved["v"] == 4
    camp = next(iter(saved["forms"].values()))["campaigns"][0]
    assert camp["fields"]["adset_count"] == "2" and camp["fields"]["ad_1_adset"] == "1"
    page.reload()
    page.evaluate("cbDrawer.open()")
    page.wait_for_function("document.querySelectorAll('.cbd-camp').length > 0")
    page.locator(".cbd-camp-row").first.click()
    page.locator(".cbd-seg [data-level='adset']").click()
    assert _sets(page).count() == 2
    assert dict(_entries(page))["camp_0_adset_1_age"] == "44"
    assert _ad_sets(page) == ["0", "1"]
    # v3 без наборов
    v3 = page.evaluate("""() => {
      const raw = JSON.parse(localStorage.getItem('acl.builder.v2'));
      raw.v = 3;
      Object.values(raw.forms).forEach(f => f.campaigns.forEach(c => {
        Object.keys(c.fields).forEach(k => { if (/^adset_|_adset$/.test(k)) delete c.fields[k]; });
        c.fields.ad_count = '2'; c.ads = 2; c.fields.age = '37';
      }));
      return JSON.stringify(raw);
    }""")
    # Уходящая страница на pagehide пишет своё живое состояние поверх; v3 кладём заново
    # до скриптов новой страницы.
    page.add_init_script("try { localStorage.setItem('acl.builder.v2', " + json.dumps(v3) + "); } catch (e) {}")
    page.reload()
    page.evaluate("cbDrawer.open()")
    page.wait_for_function("document.querySelectorAll('.cbd-camp').length > 0")
    page.locator(".cbd-camp-row").first.click()
    page.locator(".cbd-seg [data-level='adset']").click()
    assert _sets(page).count() == 1, "старый черновик должен открыться одним набором"
    e = dict(_entries(page))
    assert e["camp_0_adset_count"] == "1" and e["camp_0_age"] == "37"
    assert _ad_sets(page) == ["0", "0"], "все объявления старого черновика - в наборе 0"


def test_audit_lists_the_ad_set_with_a_problem(page):
    _open(page)
    # Общая отрисовка страницы не несёт таблицу правил; без неё аудит пуст.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from src.fb_api import preflight_rules as pf
    page.evaluate("rules => { window.CB_PREFLIGHT_RULES = rules; }", pf.export_rules())
    page.locator("[data-act='add-adset']").click()
    _fill_set(page, 1, age="17")
    page.wait_for_function("document.querySelector('.cbd-pf-list').textContent.includes('Age 18-65')")
    line = page.locator(".cbd-pf-line", has_text="Age 18-65")
    assert "Ad set 2" in line.inner_text()


def test_screenshots_of_the_ad_set_tree(page):
    out = os.environ.get("ACL_SHOTS_DIR")
    if not out:
        return
    Path(out).mkdir(parents=True, exist_ok=True)
    page.set_viewport_size({"width": 1400, "height": 1000})
    _open(page)
    _fill_set(page, 0, age="30")
    page.locator("[data-act='add-adset']").click()
    _fill_set(page, 1, age="40", name="Interests - test")
    page.locator("[data-act='copy-adset']").click()
    _sets(page).first.click()
    for theme in ("light", "dark"):
        page.evaluate(f"() => {{ document.documentElement.dataset.theme = '{theme}'; }}")
        page.emulate_media(color_scheme=theme)
        page.wait_for_timeout(200)
        page.locator("#cbd-panel").screenshot(path=str(Path(out) / f"ad-set-tree-{theme}.png"))
