"""Нажатие Publish видно сразу: между нажатием и окном подтверждения идёт проверка плана.

Владелец 08.11: «нажимаю на кнопку запуска и ничего не происходит» - кнопка только серела,
а перерисовка ворот могла включить её обратно. Теперь кнопки Publish держат отметку
«Checking before publishing… 0:12» со спиннером, пока не откроется окно подтверждения.

Поведение проверяется в странице с управляемыми часами (page.clock) и подменённой
медленной проверкой, без реальных пауз.
"""
from __future__ import annotations

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


def _gate() -> str:
    src = _src()
    start = src.index("window.cbPreflightGate = function")
    return src[start:src.index("\n};\n", start) + 3]


def test_both_confirm_paths_clear_the_mark_when_the_window_opens():
    src = _src()
    assert src.count("window.cbPreflightFetch(forms.map(cbFormAccount)).then(pfAskShown)") == 1
    assert src.count("window.cbPreflightFetch([accLabel]).then(pfAskShown)") == 1
    assert src.count("window.CB_CONFIRM_WAIT = true;") == 2


def test_refresh_publication_does_not_overwrite_the_counter():
    src = _src()
    block = src[src.index("function refreshPublication(t)"):]
    block = block[:block.index("var count")]
    assert "cbPublishPending.active()" in block
    assert "dataset.cbIdleLabel = publishLabel(t)" in block


STUBS = """
window.ACL_FB_BACKEND = 'api';
window.CB_PF_RUNNING = false; window.CB_CONFIRM_WAIT = false;
window.BLOCK = false; window.RUNS = 0; window.PROCEEDED = 0; window.AUD = 0; window.UNAVAIL = 0;
window.RESOLVE = null; window.REJECT = null; window.RUN_THROWS = false; window.PROCEED_THROWS = false;
window.IV = new Set();
var _si = window.setInterval, _ci = window.clearInterval;
window.setInterval = function (f, t) { var id = _si.call(window, f, t); window.IV.add(id); return id; };
window.clearInterval = function (id) { window.IV.delete(id); return _ci.call(window, id); };
function cbApiMode() { return true; }
function cbPfUnavailable() { window.UNAVAIL += 1; }
function cbPfWarnings() { return []; }
window.cbPreflight = { table: function () { return [1]; }, blocks: function () { return window.BLOCK; } };
window.cbTree = { auditLines: function () { return []; }, refreshAudit: function () { window.AUD += 1; }, open: function () {} };
window.cbPreflightRun = function () {
  window.RUNS += 1;
  if (window.RUN_THROWS) throw new Error('boom');
  return new Promise(function (res, rej) { window.RESOLVE = res; window.REJECT = rej; });
};
window.PROCEED = function () { if (window.PROCEED_THROWS) throw new Error('proceed boom'); window.PROCEEDED += 1; };
window.PRESS = function () {
  var form = document.createElement('form');
  window.cbPreflightGate([{ form: form, slots: [{}] }], window.PROCEED);
};
"""

IDLE = [["Publish 2 selected", False, False, None], ["Publish", False, False, None]]


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
        pg.clock.install()
        errors: list[str] = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.set_content('<button class="cbd-publish-all">Publish 2 selected</button>'
                       '<button class="cb-publish-btn">Publish</button>')
        pg.add_script_tag(content=_module())
        pg.add_script_tag(content=STUBS + _gate())
        pg.errors = errors
        yield pg
        b.close()


def _state(pg):
    return pg.evaluate("""() => [...document.querySelectorAll('button')].map(b =>
        [b.textContent, b.disabled, b.classList.contains('is-pending'), b.getAttribute('aria-busy')])""")


def test_pressing_shows_a_busy_counting_button_and_ending_restores_it(page):
    page.evaluate("cbPublishPending.start()")
    for text, disabled, pending, busy in _state(page):
        assert text == "Checking before publishing… 0:00"
        assert disabled is True and pending is True and busy == "true"
    assert page.evaluate("cbPublishPending.active()") is True
    page.evaluate("cbPublishPending.end()")
    assert _state(page) == IDLE
    assert page.evaluate("cbPublishPending.active()") is False


def test_a_repainted_button_gets_the_mark_back(page):
    page.evaluate("cbPublishPending.start()")
    page.evaluate("""() => { document.querySelector('.cbd-publish-all').outerHTML =
        '<button class="cbd-publish-all">Publish 2 selected</button>'; cbPublishPending.paint(); }""")
    assert page.text_content(".cbd-publish-all").startswith("Checking before publishing…")
    page.evaluate("cbPublishPending.end()")
    assert page.text_content(".cbd-publish-all") == "Publish 2 selected"


def test_the_counter_advances_with_the_clock(page):
    page.evaluate("PRESS()")
    assert page.text_content(".cbd-publish-all") == "Checking before publishing… 0:00"
    page.clock.run_for(5000)
    assert page.text_content(".cbd-publish-all") == "Checking before publishing… 0:05"
    assert page.text_content(".cb-publish-btn") == "Checking before publishing… 0:05"
    page.clock.run_for(65000)
    assert page.text_content(".cbd-publish-all") == "Checking before publishing… 1:10"


def test_the_five_minute_safety_releases_and_clears_the_interval(page):
    page.evaluate("PRESS()")
    assert page.evaluate("IV.size") == 1
    page.clock.run_for(5 * 60 * 1000 + 2000)
    assert page.evaluate("cbPublishPending.active()") is False
    assert page.evaluate("IV.size") == 0
    assert _state(page) == IDLE


def test_a_second_press_while_pending_is_ignored(page):
    page.evaluate("PRESS()")
    page.evaluate("PRESS()")
    assert page.evaluate("RUNS") == 1
    assert page.evaluate("IV.size") == 1


def test_a_synchronous_error_releases_the_buttons(page):
    page.evaluate("RUN_THROWS = true")
    with pytest.raises(Exception):
        page.evaluate("PRESS()")
    assert page.evaluate("cbPublishPending.active()") is False
    assert page.evaluate("CB_PF_RUNNING") is False
    assert page.evaluate("IV.size") == 0
    assert _state(page) == IDLE


def test_an_error_while_proceeding_releases_the_buttons(page):
    page.evaluate("PROCEED_THROWS = true; PRESS(); RESOLVE()")
    page.clock.run_for(10)
    assert page.evaluate("cbPublishPending.active()") is False
    assert page.evaluate("CB_PF_RUNNING") is False
    assert page.evaluate("IV.size") == 0
    assert _state(page) == IDLE
    page.errors.clear()      # ожидаемое необработанное исключение proceed


def test_a_failed_check_releases_the_buttons(page):
    page.evaluate("PRESS(); REJECT(new Error('net'))")
    page.clock.run_for(10)
    assert page.evaluate("cbPublishPending.active()") is False
    assert page.evaluate("CB_PF_RUNNING") is False
    assert _state(page) == IDLE


def test_a_gate_block_after_the_check_releases_the_buttons(page):
    page.evaluate("PRESS()")
    assert page.evaluate("cbPublishPending.active()") is True
    page.evaluate("BLOCK = true; RESOLVE()")
    page.clock.run_for(10)
    assert page.evaluate("cbPublishPending.active()") is False
    assert page.evaluate("PROCEEDED") == 0
    assert _state(page) == IDLE


def test_a_gate_repaint_does_not_reenable_while_pending_and_success_releases(page):
    page.evaluate("PRESS()")
    page.evaluate("document.querySelector('.cbd-publish-all').disabled = false; cbPublishPending.paint()")
    assert all(row[1] is True for row in _state(page))
    page.evaluate("RESOLVE()")
    page.clock.run_for(10)
    assert page.evaluate("PROCEEDED") == 1
    assert _state(page) == IDLE
    assert page.evaluate("AUD") >= 1


def test_the_state_before_the_press_is_restored_not_forced(page):
    page.evaluate("""() => {
        document.querySelector('.cb-publish-btn').disabled = true;
        document.querySelector('.cbd-publish-all').setAttribute('aria-busy', 'true'); }""")
    page.evaluate("cbPublishPending.start()")
    page.evaluate("cbPublishPending.end()")
    st = _state(page)
    assert st[1][1] is True                     # была выключена - осталась выключенной
    assert st[0][3] == "true"                   # aria-busy от очереди пережил отметку
    assert st[1][3] is None                     # у второй кнопки атрибута не было - не появился
    assert st[0][1] is False


def test_the_new_foot_label_written_while_pending_is_what_comes_back(page):
    page.evaluate("cbPublishPending.start()")
    page.evaluate("document.querySelector('.cbd-publish-all').dataset.cbIdleLabel = 'Publish all (3)'")
    page.evaluate("cbPublishPending.end()")
    assert page.text_content(".cbd-publish-all") == "Publish all (3)"
