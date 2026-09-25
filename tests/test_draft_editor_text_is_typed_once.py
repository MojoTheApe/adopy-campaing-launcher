# -*- coding: utf-8 -*-
"""Основной текст в редакторе Draft.js ложится один раз (ACL-1313, прогон 24.09).

GT FX (7364047ef03c, снимок 04_ad_copy): хвост текста уходил одной вставкой
`insert_text` вместе с переносами строк. Draft.js кладёт такую вставку в свою
модель целиком (первый ребёнок блока `[data-block="true"]` держит весь текст),
а Chrome вдобавок режет её на абзацы-копии — ещё семь детей того же блока на
семь переносов. `Ctrl+A` + `Backspace` чистит модель, но не копии, повтор
вставлял текст заново — на экране «текст дважды».

Проверяется:

* на настоящей разметке снимка — `_draft_orphans` видит копии и убирает их,
  после чего текст в поле стоит ровно один раз;
* `_type_human` и `_insert_lines` никогда не передают перенос внутри вставки
  или набора — перенос только нажатием Enter;
* `fill_ad_copy` на Draft-подобном поле в офлайн-Chrome: многострочный текст
  ложится один раз, оставшиеся от прошлой попытки копии снимаются до ввода, а
  корень редактора не обнуляется (`textContent = ''` сносит узлы React).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import meta_dom as md  # noqa: E402
from src.fb_ads_rpa import run_telemetry  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "ads_manager" / "ad_copy_draft_split_2026_09_24.html"

# Текст фикстуры (синтетическая замена живого текста, та же форма: 8 строк,
# две пустые, эмодзи и диакритика).
FIXTURE_TEXT = "\n".join([
    "¿Listo para empezar hoy?", "Un paso pequeño es suficiente.", "",
    "Si esperas, pasa una de dos cosas:", "lo olvidas o cambia el precio.", "",
    "📈 No esperes más.", "Empieza con lo que tengas.",
])

NEW_TEXT = "Primera línea del anuncio.\nSegunda línea, más larga.\n\nÚltima línea: empieza hoy."


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        try:
            yield b
        finally:
            b.close()


@pytest.fixture
def page(browser, monkeypatch, tmp_path):
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(md, "_human_jitter", lambda *a, **k: 0.0)
    # Телеметрия — во временный файл, не в живой output/.
    monkeypatch.setattr(run_telemetry, "TELEMETRY_PATH", tmp_path / "telemetry.jsonl")
    monkeypatch.delenv("FB_RPA_HUMAN_TYPING", raising=False)
    monkeypatch.delenv("FB_RPA_TYPING_LEAD", raising=False)
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    p = ctx.new_page()
    p.route("**/*", lambda route: route.abort())
    try:
        yield p
    finally:
        ctx.close()


def _field(page):
    return page.query_selector("[contenteditable=true]")


def _squash(s: str) -> str:
    return "".join((s or "").split())


# ── настоящая разметка снимка ─────────────────────────────────────────────

def test_the_real_split_field_has_seven_orphan_paragraphs(page):
    """Снимок 04_ad_copy: один блок, в нём модель Draft.js и семь копий."""
    page.set_content(FIXTURE.read_text(encoding="utf-8"))
    handle = _field(page)
    assert md._draft_orphans(handle) == 7
    # Ровно поэтому сверка видела «не тот текст»: на экране текст и копии.
    assert not md._copy_matches(md._handle_text(handle), FIXTURE_TEXT)
    assert md._draft_orphans(handle) == 7, "подсчёт без strip ничего не удаляет"


def test_stripping_the_orphans_leaves_the_text_exactly_once(page):
    page.set_content(FIXTURE.read_text(encoding="utf-8"))
    handle = _field(page)
    assert md._draft_orphans(handle, strip=True) == 7
    assert md._draft_orphans(handle) == 0
    seen = md._handle_text(handle)
    assert md._copy_matches(seen, FIXTURE_TEXT), seen
    assert seen.count("Empieza con lo que tengas.") == 1
    assert seen.count("Un paso pequeño es suficiente.") == 1
    # Модель Draft.js (первый ребёнок блока) не тронута.
    assert page.locator("[data-block=true] > *").count() == 1
    assert page.locator("[data-contents=true]").count() == 1


def test_the_real_field_is_recognised_as_a_draft_editor(page):
    page.set_content(FIXTURE.read_text(encoding="utf-8"))
    assert md._is_draft_editor(_field(page)) is True
    page.set_content("<div contenteditable=true>plain</div><input id=i>")
    assert md._is_draft_editor(_field(page)) is False
    assert md._is_draft_editor(page.query_selector("#i")) is False


# ── нажатия: перенос никогда не уходит внутри вставки ─────────────────────

class _Keyboard:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def type(self, text, delay=None):
        self.calls.append(("type", text))

    def insert_text(self, text):
        self.calls.append(("insert_text", text))

    def press(self, key):
        self.calls.append(("press", key))


class _Page:
    def __init__(self):
        self.keyboard = _Keyboard()


def _typed(calls) -> str:
    """Что оказалось бы в поле: текст вставок/набора, Enter — перенос."""
    return "".join("\n" if kind == "press" and arg == "Enter" else arg
                   for kind, arg in calls if kind in ("type", "insert_text") or arg == "Enter")


def _assert_no_newline_inside_text(calls):
    for kind, arg in calls:
        if kind in ("type", "insert_text"):
            assert "\n" not in arg and "\r" not in arg, (kind, arg)


@pytest.mark.parametrize("text", [
    "a\nb\nc",
    "Primera línea larga del anuncio\nSegunda línea larga\nTercera línea",
    "Una línea de más de ocho letras\r\nOtra línea con salto de Windows\r\nFin",
])
def test_type_human_presses_enter_for_every_line_break(monkeypatch, text):
    monkeypatch.setattr(md, "_human_jitter", lambda *a, **k: 0.0)
    monkeypatch.delenv("FB_RPA_HUMAN_TYPING", raising=False)
    monkeypatch.delenv("FB_RPA_TYPING_LEAD", raising=False)
    page = _Page()
    md._type_human(page, text)
    calls = page.keyboard.calls
    _assert_no_newline_inside_text(calls)
    assert [c for c in calls if c[0] == "press"] == [("press", "Enter")] * 2
    assert _typed(calls) == text.replace("\r\n", "\n")


def test_insert_lines_presses_enter_between_lines_and_inserts_the_rest():
    page = _Page()
    md._insert_lines(page, "a\nb\nc")
    assert page.keyboard.calls == [
        ("insert_text", "a"), ("press", "Enter"),
        ("insert_text", "b"), ("press", "Enter"),
        ("insert_text", "c"),
    ]


def test_insert_lines_keeps_empty_lines_as_extra_enters():
    page = _Page()
    md._insert_lines(page, "uno\r\n\r\ndos")
    calls = page.keyboard.calls
    _assert_no_newline_inside_text(calls)
    assert calls == [("insert_text", "uno"), ("press", "Enter"), ("press", "Enter"),
                     ("insert_text", "dos")]


# ── fill_ad_copy на Draft-подобном поле ────────────────────────────────────

# Поведение Draft.js, которое здесь важно, без самого Draft.js: всё идёт
# через модель (первый ребёнок блока), `Backspace` чистит только модель, а
# вставку с переносами Chrome вдобавок режет на абзацы-копии мимо модели —
# как на снимке 04_ad_copy.
_DRAFT_JS = r"""
(root, cfg) => {
  const contents = root.querySelector('[data-contents]');
  const block = contents.querySelector('[data-block="true"]');
  const leaf = () => block.firstElementChild.querySelector('[data-text]');
  let model = leaf().textContent;
  window.__contents = contents;
  window.__inserted = [];
  const render = () => { const s = leaf(); if (s) s.textContent = model; };
  root.addEventListener('beforeinput', e => {
    e.preventDefault();
    const t = e.inputType;
    if (t === 'insertText') {
      window.__inserted.push(e.data);
      model += e.data;
      const lines = e.data.split('\n');
      for (const line of lines.slice(1)) {        // Chrome режет вставку сам
        const d = document.createElement('div');
        d.textContent = line;
        block.appendChild(d);
      }
    } else if (t === 'insertParagraph' || t === 'insertLineBreak') {
      model += '\n';
    } else if (t.startsWith('delete')) {
      if (cfg.backspaceClears) model = '';
    }
    render();
  });
}
"""

_EMPTY_FIELD = """
<div data-surface="/am/editor_drawer/editor:ad/ad_creative_section/message_field">
<label>Primary text</label>
<div contenteditable="true" role="combobox" style="white-space: pre-wrap;">
<div data-contents="true"><div data-block="true"><div><span><span data-text="true">%s</span></span></div></div></div>
</div></div>
"""


@pytest.fixture
def draft_page(page, monkeypatch):
    monkeypatch.setattr(md, "_reveal_ad_copy_fields", lambda p: True)
    monkeypatch.setattr(md, "_await_creative_settled", lambda p, **kw: True)

    def mark(p, key, label, m):
        if key != "message_field":
            return False
        p.evaluate("m => document.querySelector('[contenteditable=true]').setAttribute('data-rpa-field', m)", m)
        return True

    monkeypatch.setattr(md, "mark_ad_copy_field", mark)
    monkeypatch.setattr(md, "_focus_via_cdp_click", lambda p, loc: (loc.focus(), loc.element_handle())[1])

    def load(html: str, *, backspace_clears: bool = True):
        page.set_content(html)
        page.eval_on_selector("[contenteditable=true]", _DRAFT_JS, {"backspaceClears": backspace_clears})
        return page

    return load


def _screen_text(page) -> str:
    return page.inner_text("[contenteditable=true]")


def test_multiline_text_is_typed_once_through_the_real_typing_path(draft_page):
    """Живой путь: настоящий `_type_human`, текст с переносами и пустой строкой."""
    page = draft_page(_EMPTY_FIELD % "")
    report: dict = {}
    assert md.fill_ad_copy(page, "", NEW_TEXT, report) is True, report
    assert not report["attempts"], "повтор не должен понадобиться"
    assert md._draft_orphans(_field(page)) == 0
    assert _squash(_screen_text(page)) == _squash(NEW_TEXT)
    assert all("\n" not in d for d in page.evaluate("window.__inserted"))


def test_orphans_left_by_a_previous_attempt_are_removed_before_typing(draft_page):
    """Снимок 04_ad_copy как стартовое состояние: копии прошлой вставки на месте."""
    page = draft_page(FIXTURE.read_text(encoding="utf-8"))
    assert md._draft_orphans(_field(page)) == 7
    report: dict = {}
    assert md.fill_ad_copy(page, "", NEW_TEXT, report) is True, report
    seen = _screen_text(page)
    assert _squash(seen) == _squash(NEW_TEXT), seen
    assert "Empieza con lo que tengas" not in seen, "копии старого текста остались"
    assert md._draft_orphans(_field(page)) == 0


def test_a_retry_after_lost_keystrokes_inserts_lines_not_one_paste(draft_page, monkeypatch):
    """Повтор после расхождения вставляет строки по одной: перенос — нажатие."""
    page = draft_page(_EMPTY_FIELD % "")
    tries = {"n": 0}

    def lose_the_tail_once(p, t):
        tries["n"] += 1
        # Первая попытка: часть нажатий пропала (как вживую под нагрузкой).
        p.keyboard.insert_text(t.split("\n")[0] if tries["n"] == 1 else t.replace("\n", " "))

    monkeypatch.setattr(md, "_type_human", lose_the_tail_once)
    report: dict = {}
    assert md.fill_ad_copy(page, "", NEW_TEXT, report) is True, report
    assert any(a.get("stage") == "typed_text_mismatch" for a in report["attempts"])
    assert tries["n"] == 1, "повтор идёт через _insert_lines, а не через набор"
    inserted = page.evaluate("window.__inserted")
    assert all("\n" not in d for d in inserted), inserted
    assert md._draft_orphans(_field(page)) == 0
    assert _squash(_screen_text(page)) == _squash(NEW_TEXT)


def test_the_draft_root_is_never_wiped_when_select_all_does_not_clear(draft_page):
    """`Ctrl+A` + `Backspace` не очистили поле: старая страховка обнуляла
    `textContent` корня и сносила узлы, которыми управляет React."""
    page = draft_page(_EMPTY_FIELD % "СТАРЫЙ ТЕКСТ", backspace_clears=False)
    md.fill_ad_copy(page, "", "Nuevo texto del anuncio", {})
    assert page.evaluate("window.__contents.isConnected") is True, "корень Draft.js обнулён"
    assert page.evaluate(
        "window.__contents === document.querySelector('[contenteditable=true] > [data-contents]')")
    assert page.locator("[data-block=true]").count() == 1


def test_a_plain_field_is_still_wiped_when_select_all_does_not_clear(page, monkeypatch):
    """Для обычного поля (не Draft.js) прежняя страховка остаётся."""
    monkeypatch.setattr(md, "_reveal_ad_copy_fields", lambda p: True)
    monkeypatch.setattr(md, "_await_creative_settled", lambda p, **kw: True)

    def mark(p, key, label, m):
        if key != "message_field":
            return False
        p.evaluate("m => document.getElementById('body').setAttribute('data-rpa-field', m)", m)
        return True

    monkeypatch.setattr(md, "mark_ad_copy_field", mark)
    monkeypatch.setattr(md, "_focus_via_cdp_click", lambda p, loc: (loc.focus(), loc.element_handle())[1])
    monkeypatch.setattr(md, "_type_human", lambda p, t: p.keyboard.insert_text(t))
    page.set_content(
        "<div id='body' contenteditable='true'>СТАРЫЙ ТЕКСТ</div>"
        "<script>document.getElementById('body').addEventListener('keydown', e => {"
        " if (e.key === 'Backspace') e.preventDefault(); });</script>")
    assert md.fill_ad_copy(page, "", "Nuevo texto") is True
    assert page.inner_text("#body").strip() == "Nuevo texto"
