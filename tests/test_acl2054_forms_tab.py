"""ACL-2054: вкладка Forms на странице — связи и исполнение кода.

Проверяются связи (кнопка ведёт в панель, поля, которые читает скрипт, существуют,
сообщение про неизменность форм на Страницах стоит в редакторе, билдер берёт
шаблоны из общей библиотеки) и поведение: код вкладки исполняется в node.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TPL = ROOT / "src" / "web" / "templates" / "report.html"


def _t() -> str:
    return TPL.read_text(encoding="utf-8")


def _panel() -> str:
    t = _t()
    start = t.index('id="view-forms"')
    return t[start: t.index('id="view-home"', start)]


def _script() -> str:
    t = _t()
    start = t.index("// ── Forms: общая библиотека шаблонов Instant Form (ACL-2054)")
    return t[start: t.index("</script>", start)]


def test_the_forms_button_opens_an_existing_panel_next_to_ad_titles():
    t = _t()
    assert t.index('data-view="ad-texts"') < t.index('data-view="forms"') < t.index('data-view="accounts"')
    assert 'id="view-forms"' in t
    assert "'forms'" in t[t.index("function parseHash"): t.index("function expandCountry")], "прямая ссылка #forms не открывается"


def test_every_element_the_script_reads_exists_in_the_panel():
    ids = set(re.findall(r"el\('([a-z0-9-]+)'\)", _script())) | set(re.findall(r"getElementById\('([a-z0-9-]+)'\)", _script()))
    ids |= {f"fm-c-{k}" for k in ("name", "email", "phone")}
    ids -= {"fm-c-"}
    panel = _panel()
    # Остальное (fm-migrate-btn и т.п.) рисуется скриптом.
    drawn = {"fm-migrate-btn"}
    for i in sorted(ids - drawn):
        assert f'id="{i}"' in panel, f"скрипт читает {i}, а поля нет"


def test_the_editor_says_that_existing_forms_do_not_change():
    panel = _panel()
    assert "does not change forms already created on Pages" in panel
    assert "next publish" in panel


def test_the_panel_has_the_actions_the_issue_asks_for():
    panel, script = _panel(), _script()
    for text in ("New", "Refresh", "Search templates"):
        assert text in panel
    for text in (">Edit<", ">Duplicate<", ">Delete<", ">Restore<", "Recently deleted"):
        assert text in script, text
    assert 'id="fm-import-slot"' in panel, "для Import from Page нужно место"


def test_the_tab_refreshes_the_builder_picker_and_nothing_says_this_computer_only():
    t = _t()
    assert "window.cbFormTemplates = d.templates" in _script(), "выбор шаблона в билдере не узнает об изменениях"
    assert "save it on this computer" not in t and "Saving the template on this computer" not in t


def test_the_forms_tab_behaves_when_the_code_is_actually_run():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    r = subprocess.run([node, str(ROOT / "tests" / "forms_tab_harness.cjs"), str(TPL)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    assert r.returncode == 0, r.stderr
