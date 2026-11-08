"""Нажатие Publish видно сразу: между нажатием и окном подтверждения идёт проверка плана.

Владелец 08.11: «нажимаю на кнопку запуска и ничего не происходит» - кнопка только серела,
а перерисовка ворот могла включить её обратно. Теперь кнопки Publish держат отметку
«Checking before publishing… 0:12» со спиннером, пока не откроется окно подтверждения.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPORT = Path(__file__).resolve().parent.parent / "src" / "web" / "templates" / "report.html"


def _src() -> str:
    return REPORT.read_text(encoding="utf-8")


def _module() -> str:
    src = _src()
    start = src.index("window.cbPublishPending = (function")
    end = src.index("})();", start) + len("})();")
    return src[start:end]


def test_both_confirm_paths_clear_the_mark_when_the_window_opens():
    src = _src()
    assert src.count("window.cbPreflightFetch(forms.map(cbFormAccount)).then(pfAskShown)") == 1
    assert src.count("window.cbPreflightFetch([accLabel]).then(pfAskShown)") == 1
    assert src.count("window.CB_CONFIRM_WAIT = true;") == 2


def test_the_gate_starts_the_mark_and_stops_it_on_a_block():
    src = _src()
    gate = src[src.index("window.cbPreflightGate = function"):]
    gate = gate[:gate.index("\n};\n")]
    assert "pend.start()" in gate
    assert re.search(r"function stop\(bad\) \{\s*if \(pend\) pend\.end\(\);", gate)
    assert "if (pend && !window.CB_CONFIRM_WAIT) pend.end();" in gate


@pytest.fixture
def page():
    sync_api = pytest.importorskip("playwright.sync_api")
    import os
    with sync_api.sync_playwright() as pw:
        try:
            b = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        except Exception as exc:  # noqa: BLE001 - нет браузера на машине
            pytest.skip(f"no browser: {exc}")
        pg = b.new_page()
        errors: list[str] = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.set_content('<button class="cbd-publish-all">Publish 2 selected</button>'
                       '<button class="cb-publish-btn">Publish</button>')
        pg.add_script_tag(content=_module())
        yield pg
        assert not errors, errors
        b.close()


def test_pressing_shows_a_busy_counting_button_and_ending_restores_it(page):
    page.evaluate("cbPublishPending.start()")
    st = page.evaluate("""() => [...document.querySelectorAll('button')].map(b =>
        [b.textContent, b.disabled, b.classList.contains('is-pending'), b.getAttribute('aria-busy')])""")
    for text, disabled, pending, busy in st:
        assert text.startswith("Checking before publishing… 0:0")
        assert disabled is True and pending is True and busy == "true"
    assert page.evaluate("cbPublishPending.active()") is True
    page.evaluate("cbPublishPending.end()")
    st = page.evaluate("""() => [...document.querySelectorAll('button')].map(b =>
        [b.textContent, b.disabled, b.classList.contains('is-pending')])""")
    assert st == [["Publish 2 selected", False, False], ["Publish", False, False]]
    assert page.evaluate("cbPublishPending.active()") is False


def test_a_repainted_button_gets_the_mark_back(page):
    page.evaluate("cbPublishPending.start()")
    page.evaluate("""() => { document.querySelector('.cbd-publish-all').outerHTML =
        '<button class="cbd-publish-all">Publish 2 selected</button>'; cbPublishPending.paint(); }""")
    assert page.text_content(".cbd-publish-all").startswith("Checking before publishing…")
    page.evaluate("cbPublishPending.end()")
    assert page.text_content(".cbd-publish-all") == "Publish 2 selected"
