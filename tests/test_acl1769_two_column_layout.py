"""ACL-1769: двухколоночный билдер на текущей модели (кампания = один набор объявлений).

Страница: чистые функции раскладки (cbLayout) и дерево с редактором проверяются
в Node на живом коде шаблона (как test_acl1761_plan_check / test_acl1761_parity);
браузерный тест — при свободной памяти больше 6 ГБ. Соответствие серверу:
сообщения, которые создатель кампаний (src/fb_api/creator.py) действительно
выдаёт, ложатся на поля редактора, а константы, которые интерфейс показывает
(Learn more, Website, PAUSED, Leads), равны тем, что шлёт создатель.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TPL = ROOT / "src" / "web" / "templates" / "report.html"


def _node():
    node = shutil.which("node")
    if not node:
        pytest.skip("нет node — исполнить код страницы нечем")
    return node


def _page() -> str:
    return TPL.read_text(encoding="utf-8")


def _slot_code() -> str:
    """Тело buildBlankSlotHTML без комментариев (в комментариях ABO упомянут словами)."""
    page = _page()
    slot = page[page.index("function buildBlankSlotHTML"):]
    slot = slot[: slot.index("// ── Tracking link save")]
    return re.sub(r"/\*.*?\*/", "", slot, flags=re.S)


def _run(*args: str) -> str:
    r = subprocess.run([_node(), str(ROOT / "tests" / "builder_layout_harness.cjs"), str(TPL), *args],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_the_layout_functions_behave():
    assert _run().strip() == "ok"


# ───────────────────────────── разметка панели ─────────────────────────────

def test_the_panel_is_two_columns_with_expand_and_the_foot_kept():
    page = _page()
    assert 'class="cbd-cols"' in page
    assert page.index('id="cbd-body"') < page.index('id="cbd-editor"') < page.index('id="cbd-foot"'), (
        "порядок: дерево, редактор, подвал"
    )
    # ACL-1871: размер — на язычке (три кнопки), кнопки Expand в шапке больше нет.
    assert 'id="cbd-expand"' not in page
    assert all(f'id="cbd-preset-{n}"' in page for n in ("closed", "normal", "wide"))
    assert 'id="cbd-close"' in page, "крестик закрытия панели пропал"
    # Закрытие только прячет: у него нет обработчика, который чистит черновики.
    block = page[page.index("window.cbDrawer = (function () {"):]
    block = block[: block.index("\n})();")]
    assert "forget" not in block and "removeItem" not in block


def test_publish_all_check_plan_and_the_mode_texts_are_kept_verbatim():
    page = _page()
    run = page[page.index("window.cbPublishAll = function"):]
    run = run[: run.index("\nfunction cbPlanFormData")]
    assert "Draft — the RPA leaves a draft in Ads Manager and never clicks Publish in Facebook" in page
    assert "Draft — the RPA leaves a draft in Ads Manager and never clicks Publish in Facebook" in (
        page[page.index("function cbPublishMode"):]
    ), "текст режима RPA пропал"
    assert "'Publish selected → ' + what" in page and "'Publish all → ' + what" in page
    assert "Check plan (no writes)" in page and "Close plan ×" in page
    assert 'data-act="close-plan"' in page and "cbPlanBindFrame" in page
    assert "PAUSED campaigns" in page


def test_there_is_no_abo_cbo_choice_and_the_campaign_budget_is_shown():
    slot = _slot_code()
    assert "Campaign budget" in slot and '_budget"' in slot
    # Владелец оставил бюджет только на кампании: выбора ABO/CBO в карточке нет.
    assert not re.search(r"\b(ABO|CBO)\b", slot), "в карточке появился выбор ABO/CBO"


def test_the_campaign_name_has_an_optional_override_field():
    page = _page()
    slot = _slot_code()
    assert 'data-keep="name_override"' in slot and "_name_override" not in slot, (
        "у своего имени снова есть name — сервер получит лишнее поле"
    )
    assert "data-auto-name" in slot
    upd = page[page.index("window.cbUpdateName = function"):]
    upd = upd[: upd.index("// ── Searchable select")]
    assert "cbLayout.effectiveName(auto, override)" in upd, "своё имя не пробивается через effectiveName"
    # Серверу уходит по-прежнему одно поле имени.
    assert ".cb-name-hidden" in upd


def test_every_control_without_a_backend_is_disabled_and_names_its_task():
    out = json.loads(_run("--future"))
    assert set(out) >= {"adsets", "copyAdset", "texts", "placements", "copyTo"}
    assert not {"gender", "languages", "cta"} & set(out), "ACL-1767: это уже настоящие поля, а не заглушки"
    assert "forms" not in out, "ACL-1775: Instant Forms - настоящий выбор, а не заглушка"
    for key, v in out.items():
        for kind in ("button", "select"):
            assert " disabled " in v[kind] and " name=" not in v[kind], key
        assert v["task"] in v["tip"]
    # и страница рисует их через эту таблицу, а не руками
    page = _page()
    used = set(re.findall(r"\b(?:future|futureHtml)\('([A-Za-z]+)'", page))
    assert used <= set(out), f"страница ссылается на ключи вне таблицы: {used - set(out)}"
    assert used >= {"adsets", "copyAdset", "texts", "placements", "media", "copyTo"}


def test_old_drafts_load_without_the_new_field():
    """Форма хранения не менялась: `camp_N_name_override` — ещё одно необязательное
    поле среди `fields`. Черновик без него раскладывается как раньше (проверка — в
    builder_keep_harness.cjs, он же гоняется тестом хранилища)."""
    page = _page()
    keep = page[page.index("window.cbKeep = (function () {"):]
    keep = keep[: keep.index("\n})();")]
    assert "var VERSION = 3;" in keep and "function migrate(" in keep, "ACL-1767: v2 -> v3, миграция и её тест в builder_keep_harness.cjs"
    assert "if (!f || !(f in data.fields)) return;" in keep, "поле, которого нет в черновике, обнулится"


# ─────────────────────── соответствие создателю кампаний ───────────────────────

def _problems(tmp_path, **changes) -> str:
    from src.fb_api import creator as cr
    from tests.test_acl1761_api_create import _draft

    d = _draft(tmp_path, ads=1, **{k: v for k, v in changes.items() if k == "body2"})
    if "budget" in changes:
        d.ad_sets[0].daily_budget = changes["budget"]
    if "link" in changes:
        d.link = changes["link"]
    if "path" in changes:
        d.ad_sets[0].ads[0].creative_path = changes["path"]
    with pytest.raises(cr.PlanError) as err:
        cr.plan_from_draft(d)
    return str(err.value)


def test_creator_errors_land_on_the_fields_that_cause_them(tmp_path, tmp_path_factory):
    cases = {
        "Daily budget missing": _problems(tmp_path, budget=0),
        "Primary text 2 is not supported": _problems(tmp_path_factory.mktemp("t2"), body2="Second text"),
        "no creative file": _problems(tmp_path_factory.mktemp("cf"), path=""),
        "Tracking link is empty": _problems(tmp_path_factory.mktemp("lk"), link=""),
    }
    for needle, message in cases.items():
        assert needle in message, (needle, message)
    msg_file = ROOT / "output" / "acl1769_messages.json"
    msg_file.parent.mkdir(parents=True, exist_ok=True)
    msg_file.write_text(json.dumps(list(cases.values())), encoding="utf-8")
    try:
        got = json.loads(_run("--routes", str(msg_file)))
    finally:
        msg_file.unlink(missing_ok=True)
    by_needle = dict(zip(cases, got))
    sels = {n: [r["sel"] for r in routes] for n, routes in by_needle.items()}
    assert sels["Daily budget missing"] == ['[name$="_budget"]']
    # Ошибка второго текста подсвечивает его поле и только его (ACL-1344).
    assert sels["Primary text 2 is not supported"] == [".cb-text2-search"]
    assert sels["no creative file"] == [".cb-creative-search"]
    assert 'input[name="tracking_link"]' in sels["Tracking link is empty"]


def test_the_api_backend_second_text_note_matches_the_creator_rule(tmp_path):
    """Создатель отказывает на втором основном тексте — страница гасит поле с причиной."""
    message = _problems(tmp_path, body2="Second text")
    assert "Primary text 2 is not supported by the API backend" in message
    page = _page()
    assert "Primary text 2 is not supported by the API backend yet (ACL-1774). Leave it empty." in page
    sync = page[page.index("function syncHost(found)"):]
    sync = sync[: sync.index("function paintEditor")]
    assert "window.ACL_FB_BACKEND === 'api'" in sync
    assert "t2.disabled = api && !t2.value" in sync, "непустое поле гаснуть не должно: набранное не теряем"


def test_the_ui_shows_only_what_the_creator_sends():
    """Выключенные поля показывают значение, которое создатель шлёт на самом деле."""
    from src.fb_api import creator as cr

    page = _page()
    # ACL-1767: кнопка - настоящий выбор; умолчание то же, что у создателя
    assert cr.CTA_TYPE == "LEARN_MORE" and 'data-default="LEARN_MORE"' in page
    # ACL-1775: выбор Website / Instant Form - настоящий, значения те, что шлёт создатель
    assert cr.DESTINATION == "WEBSITE" and cr.DEST_WEBSITE == "website" and cr.DEST_FORM == "instant_form"
    assert 'value="website" selected>Website<' in page and 'value="instant_form">Instant Form<' in page
    assert cr.PAUSED == "PAUSED" and "PAUSED campaigns" in page
    assert cr.SUPPORTED_OBJECTIVES == frozenset({"leads"})
    assert cr.OBJECTIVE == "OUTCOME_LEADS"
    # Список ОС в карточке — тот, который создатель умеет превратить в таргетинг.
    slot = _slot_code()
    os_opts = set(re.search(r"mkSel\(idx, 'os', \[([^\]]*)\]", slot).group(1).replace("'", "").replace(" ", "").split(","))
    assert os_opts == {"All", "iOS", "Android"}   # ACL-1767: Android - только API (в RPA пункт выключен)
    from src.fb_api.creator import targeting_for  # noqa: F401  (ветка user_os есть в создателе)
    src = (ROOT / "src" / "fb_api" / "creator.py").read_text(encoding="utf-8")
    assert 'user_os' in src and '"ios"' in src.lower()
    # ACTIVE и ABO в карточке не предлагаются.
    assert "ACTIVE" not in slot and "ABO" not in slot


def test_a_plan_error_marks_the_field_in_the_editor():
    """«Check plan» → ошибка плана кампании подсвечивает поле и сообщает редактору."""
    page = _page()
    layout = page[page.index("window.cbLayout = (function () {"):]
    layout = layout[: layout.index("\n})();") + 6]
    mark = page[page.index("function cbMarkFields(slot, errors) {"):page.index("function cbDrawerClearFields(form) {")]
    script = f"""
const window = {{}};
{layout}
{mark}
const marked = [];
function node(sel) {{
  return {{ sel, classList: {{ add: (c) => marked.push([sel, c]) }}, setAttribute() {{}} }};
}}
const form = {{ querySelectorAll: (sel) => [node('FORM ' + sel)] }};
const slot = {{ closest: () => form, querySelectorAll: (sel) => [node('SLOT ' + sel)],
  querySelector: () => ({{ value: 'Camp A' }}) }};
const refreshed = [];
window.cbTree = {{ refreshEditor: () => refreshed.push(1) }};
cbApplyPlanResults(form2 = {{ querySelectorAll: () => [slot] }}, {{ results: [
  {{ campaign_name: 'Camp A', error: 'Ad 1: no creative file; Daily budget missing' }},
  {{ campaign_name: 'Other', error: 'Ad 1: no creative file' }}] }});
console.log(JSON.stringify({{ marked, refreshed: refreshed.length }}));
"""
    apply_fn = page[page.index("function cbApplyPlanResults(form, data) {"):page.index("function cbDrawerClearFields(form) {")]
    script = script.replace("cbApplyPlanResults(form2 =", apply_fn + "\ncbApplyPlanResults(form2 =")
    r = subprocess.run([_node(), "-e", script], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    sels = [m[0] for m in out["marked"]]
    assert "SLOT .cb-creative-search" in sels and 'SLOT [name$="_budget"]' in sels
    assert all(m[1] == "cb-field-error" for m in out["marked"])
    assert out["refreshed"] >= 1, "редактор не узнал об ошибке (вкладка уровня не пометится)"


def test_ad_tab_is_a_stack_of_cards_without_an_ad_dropdown():
    """Правка владельца 13.10: выбора Ad / Ad 2 нет, карточки идут списком.
    Подпись «Primary text 1» проверяется и без браузера (раньше её держал
    только браузерный тест)."""
    page = _page()
    assert "cbd-ed-ad" not in page and "cbd-ad-off" not in page and "adNames" not in page
    assert "Object to edit" in page, "вкладки Campaign / Ad set / Ad пропали"
    row = page[page.index("function buildAdRowHTML"):]
    row = re.sub(r"/\*.*?\*/", "", row[: row.index("function ", 30)], flags=re.S)
    row = re.sub(r"^\s*//.*$", "", row, flags=re.M)
    assert '<span class="cb-label">Primary text 1' in row and "Primary text 2" in row and "Ad Title" not in row
    for name in ("copy", "copy2", "creative_file"):
        assert "name=\"' + pfx + '" + name + "\"" in row, "имя поля camp_N_ad_M_" + name + " изменилось"
