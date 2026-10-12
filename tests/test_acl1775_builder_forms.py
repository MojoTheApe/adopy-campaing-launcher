"""ACL-1775: билдер - «Conversion location: Website / Instant Form» и выбор формы Страницы.

Код страницы исполняется в Node на живом шаблоне (`builder_forms_harness.cjs`);
браузерный тест - при свободной памяти больше 6 ГБ. Здесь же - то, что
проверяется по тексту шаблона: разметка карточки, отсутствие опроса и то,
что значения страницы равны тем, что понимает сервер.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TPL = ROOT / "src" / "web" / "templates" / "report.html"


def _page() -> str:
    return TPL.read_text(encoding="utf-8")


def _node() -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("нет node - исполнить код страницы нечем")
    return node


def _code(start: str, end: str) -> str:
    page = _page()
    i = page.index(start)
    return page[i:page.index(end, i)]


def _slot_code() -> str:
    page = _page()
    slot = page[page.index("function buildBlankSlotHTML"):]
    return re.sub(r"/\*.*?\*/", "", slot[: slot.index("// ── Tracking link save")], flags=re.S)


def _ad_row_code() -> str:
    page = _page()
    row = page[page.index("function buildAdRowHTML"):]
    return re.sub(r"/\*.*?\*/", "", row[: row.index("// ── Add / Remove ad within campaign")], flags=re.S)


def test_the_page_code_behaves_in_node():
    r = subprocess.run([_node(), str(ROOT / "tests" / "builder_forms_harness.cjs"), str(TPL)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "ok"


# ───────────────────────────── разметка карточки ─────────────────────────────

def test_the_ad_set_has_a_real_conversion_location_choice_not_a_stub():
    slot = _slot_code()
    assert "future('forms'" not in slot, "заглушка ACL-1775 осталась"
    assert "'<select name=\"camp_' + idx + '_destination\"" in slot
    assert '<option value="website" selected>Website</option>' in slot
    assert '<option value="instant_form">Instant Form</option>' in slot
    # Умолчание - сайт: старая карточка и старые черновики ничего не меняют.
    assert slot.index('value="website" selected') < slot.index('value="instant_form"')
    assert "data-forms-api-only hidden" in slot, "причина для RPA не нарисована"
    assert "Instant Form works only with the API backend." in slot


def test_the_form_picker_exists_in_every_ad_and_starts_hidden_and_not_submitted():
    row = _ad_row_code()
    assert row.count("cb-form-only") == 2
    assert "'<span class=\"cb-label cb-form-only\" hidden>Instant Form</span>'" in row
    assert "'<div class=\"cb-form-wrap cb-form-only\" hidden>'" in row
    # Скрытое поле: имя по номеру объявления (переименовывается вместе с рядом), выключено до выбора формы.
    assert "'<input type=\"hidden\" name=\"' + pfx + 'form_id\" value=\"\" disabled>'" in row
    assert ">Refresh forms</button>" in row and 'onclick="cbRefreshForms(this)"' in row
    # У самого списка имени нет: он ничего не отправляет, отправляет скрытое поле.
    select = re.search(r"<select class=\"cb-input cb-select cb-form-select\"[^>]*>", row).group(0)
    assert "name=" not in select and 'onchange="cbFormChosen(this)"' in select


def test_the_forms_are_read_only_on_a_click_never_on_a_timer():
    page = _page()
    glue = _code("window.cbForms = window.cbForms || {};", "// ── Ad row HTML builder")
    assert glue.count("fetch('/api/page-forms'") == 1
    assert "setInterval" not in glue and "setTimeout" not in glue and "requestAnimationFrame" not in glue
    uses = [m.start() for m in re.finditer(r"cbRefreshForms", page)]
    # определение + onclick кнопки; никаких других вызовов
    assert len(uses) == 2, "cbRefreshForms зовётся не только кнопкой"
    assert 'onclick="cbRefreshForms(this)"' in _ad_row_code()
    assert "'/api/page-forms'" not in page.replace(glue, ""), "второе место, где читаются формы"
    # одно нажатие в полёте - второе игнорируется
    assert "btn.disabled" in glue


def test_copying_an_ad_carries_the_form():
    copy = _code("function _copyAdRowValues(srcRow, newRow) {", "// \"⧉\" sits on each ad card")
    assert 'input[name$="_form_id"]' in copy and "newForm.value = srcForm.value" in copy


def test_a_stored_draft_restores_the_destination_and_resyncs():
    page = _page()
    write = page[page.index("function writeSlot(slot, data) {"):]
    write = write[: write.index("\n  }\n")]
    assert "window.cbSyncDestination(slot)" in write


def test_every_place_that_builds_or_copies_rows_resyncs_the_destination():
    page = _page()
    for anchor in ("window.cbAddSlot = function(form) {", "window.cbAddAd = function(btn) {",
                   "window.cbDuplicateAd = function(btn) {", "window.cbDuplicateSlot = function(btn) {"):
        body = page[page.index(anchor):]
        body = body[: body.index("\n  };\n")]
        assert "cbSyncDestination(" in body, anchor
    sync = page[page.index("function syncHost(found) {"):]
    assert "cbSyncDestination(found.camp.slot)" in sync[: sync.index("function paintEditor")]


# ───────────────────────────── соответствие серверу ─────────────────────────────

def test_the_page_and_the_server_agree_on_destinations_and_form_statuses():
    from src.fb_api import creator as cr
    from src.fb_api import forms as fm

    page = _page()
    assert f"{cr.DEST_WEBSITE}: 'Website'" in page and f"{cr.DEST_FORM}: 'Instant Form'" in page
    m = re.search(r"var USABLE_FORM_STATUS = '([^']*)'", page)
    assert m.group(1) == fm.USABLE_STATUS == "ACTIVE"
    # имя поля, которое разбирает сервер
    src = (ROOT / "src" / "web" / "app.py").read_text(encoding="utf-8")
    assert 'f"camp_{idx}_destination"' in src and 'f"camp_{idx}_ad_{a_idx}_form_id"' in src


def test_the_page_never_names_a_way_to_create_a_form_or_accept_terms():
    glue = _code("window.cbForms = window.cbForms || {};", "// ── Ad row HTML builder")
    low = glue.lower()
    # ACL-1776: владелец разрешил роботу создавать формы ИЗ ШАБЛОНА, но не принимать условия.
    # Страница не знает ни ребра Graph, ни ключей условий, ни обработчика «accept».
    for bad in ("leadgen_forms", "tos_accepted", "leadgen_tos", "cbaccept", "acceptterms", "accept_terms"):
        assert bad not in low, bad
    assert "fetch('/api/form-templates'" in glue and "/api/page-forms" in glue
    assert "graph.facebook" not in low


# ───────────────────────────── ссылка отслеживания ─────────────────────────────

_TRACK = r"""
const mk = (dests, typed) => ({
  querySelector: (s) => s === 'input[name="tracking_link"]' ? { value: typed }
    : s === '[name="account_label"]' ? { value: 'Acc' } : null,
  querySelectorAll: (s) => s === '.cb-destination' ? dests.map((v) => ({ value: v })) : [] });
globalThis.ACCOUNT_PREFLIGHT = { Acc: { link: 'https://t.invalid/x', link_status: 'tracking' } };
const out = {
  none:    cbTrackingWarning(mk([], '')),
  website: cbTrackingWarning(mk(['website'], '')),
  instant: cbTrackingWarning(mk(['instant_form'], '')),
  allInst: cbTrackingWarning(mk(['instant_form', 'instant_form'], 'https://other.invalid')),
  mixed:   cbTrackingWarning(mk(['instant_form', 'website'], '')),
};
process.stdout.write(JSON.stringify({ none: !!out.none, website: !!out.website, instant: out.instant,
  allInst: out.allInst, mixed: !!out.mixed }));
"""


def test_the_tracking_warning_is_silent_only_when_every_ad_set_is_an_instant_form():
    import json

    js = _code("window.cbTrackingWarning = function(form) {", "/* Подсветить поле так")
    script = "window = globalThis;\n" + js + "\n" + _TRACK
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(script)
        path = fh.name
    try:
        done = subprocess.run([_node(), path], capture_output=True, text=True, encoding="utf-8", timeout=60)
    finally:
        Path(path).unlink(missing_ok=True)
    assert done.returncode == 0, done.stderr
    got = json.loads(done.stdout)
    assert got["none"] is True and got["website"] is True and got["mixed"] is True
    assert got["instant"] is None and got["allInst"] is None
