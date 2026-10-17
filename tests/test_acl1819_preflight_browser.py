"""ACL-1819: pre-flight audit в настоящем ящике билдера (офлайн-Chromium).

Страница рисуется с реальной таблицей правил; Meta и дешборд подменены маршрутами.
Проверяется глазами пользователя: карточка и значок errors: N, клик по строке ставит
фокус в поле, Publish all выключен с причиной, пока у выбранной кампании есть ошибка,
кнопка Check with Meta берёт вердикты кабинета из /publish-plan (и только по нажатию), а под RPA
серверных строк нет. ACL-1990: проверки сложены в разделы; раздел с ошибкой раскрыт сам,
щелчок раскрывает и сворачивает; «не удалось проверить» (профиль занят) Publish не выключает. Браузерный тест — при свободной памяти больше 6 ГБ (CI).
С переменной ACL1819_SHOTS=<папка> тот же сценарий снимает экраны в светлой и тёмной теме.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_builder_duplicate_buttons import browser  # noqa: E402,F401
from test_builder_per_account import _render  # noqa: E402
from test_drawer_review_browser import add  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.fb_api import preflight_rules as pf  # noqa: E402

LIBRARY = [{"ad_title": "Text A", "headline": "Short headline", "body": "A body that fits.",
            "description": "", "asset": "", "language": ""}]


def _page_html(backend: str) -> str:
    html = _render({"Demo": [], "Other": []})
    # Общая отрисовка пропускает ворота ради чужих тестов; здесь нужны настоящие.
    html = html.replace("<script>/*ACL1819_TEST_GATE*/window.cbPreflightGate = function (items, go) { go(); };</script>", "")
    assert "ACL1819_TEST_GATE" not in html
    html = html.replace("window.CB_PREFLIGHT_RULES = {};", "window.CB_PREFLIGHT_RULES = "
                        + json.dumps(pf.export_rules()) + ";", 1)
    assert "CB_PREFLIGHT_RULES = {\"version\"" in html, "таблица правил не попала на страницу"
    return html.replace("window.ACL_FB_BACKEND = \"rpa\";", f"window.ACL_FB_BACKEND = \"{backend}\";", 1)


class Ui:
    def __init__(self, page, state):
        self.p, self.state = page, state


def _open(browser, backend, plan_reply=None, theme="light"):
    ctx = browser.new_context(color_scheme=theme, viewport={"width": 1280, "height": 1100})
    p = ctx.new_page()
    errors: list[str] = []
    p.on("pageerror", lambda e: errors.append(str(e)))
    state = {"plan_posts": 0, "errors": errors, "ctx": ctx}
    html = _page_html(backend)

    def route(r):
        url = r.request.url
        if r.request.resource_type == "document":
            r.fulfill(content_type="text/html", body=html)
        elif "/publish-plan" in url:
            state["plan_posts"] += 1
            reply = plan_reply() if callable(plan_reply) else (plan_reply or {"results": []})
            r.fulfill(json=reply)
        elif "/api/ad-copy" in url:
            r.fulfill(json={"rows": LIBRARY})
        elif "/preview-publish" in url:
            r.fulfill(json={"plan": []})
        else:
            r.fulfill(json={"files": [], "texts": [], "rows": LIBRARY if "ad-copy" in url else [],
                            "profiles": [], "waiting": False})

    p.route("**/*", route)
    p.goto("http://acl.test/")
    p.evaluate("cbDrawer.open()")
    return p, state


def _first(p):
    add(p)
    p.locator(".cbd-camp-row").first.click()
    p.wait_for_selector(".cbd-pf:not([hidden])")


def _fill_valid(p):
    """Всё, что требуют клиентские правила: ссылка с метками, бюджет, текст из библиотеки."""
    p.evaluate("""() => {
      const f = document.querySelector('.cbd-host form.open-account-form');
      const set = (el, v) => { el.value = v; el.dispatchEvent(new Event('input', {bubbles: true})); };
      set(f.querySelector('input[name="tracking_link"]'), 'https://track.example.com/c?sub=1');
      const s = f.querySelector('.cb-slot:not([hidden])');
      set(s.querySelector('[name$="_budget"]'), '20');
      set(s.querySelector('input[type="hidden"][name$="_ad_0_copy"]'), 'Text A');
    }""")
    p.evaluate("() => cbTree.refreshEditor()")


def _settled_name(p) -> str:
    """Имя кампании дописывается асинхронно (оффер приезжает позже): ждём, пока оно
    не меняется полсекунды, — вердикты /publish-plan лежат под этим именем."""
    get = "() => document.querySelector('.cbd-host .cb-slot:not([hidden]) .cb-name-hidden').value"
    name, same = p.evaluate(get), 0
    for _ in range(40):
        p.wait_for_timeout(100)
        now = p.evaluate(get)
        same = same + 1 if now == name else 0
        name = now
        if same >= 5:
            break
    return name


def _badge(p) -> str:
    return p.locator(".cbd-pf .cbd-pf-badge").inner_text()


def _topic(p, tid) -> dict:
    """Раздел карточки: статус, раскрыт ли, строка-итог."""
    return p.evaluate("""(tid) => { const li = document.querySelector('.cbd-pf-topic[data-topic="' + tid + '"]');
      if (!li) return null;
      return { status: li.dataset.status, open: !li.querySelector('.cbd-pf-list').hidden,
               summary: li.querySelector('.cbd-pf-tsum').textContent }; }""", tid)


def _shots(p, name):
    out = os.environ.get("ACL1819_SHOTS")
    if out:
        Path(out).mkdir(parents=True, exist_ok=True)
        # Карточка стоит под формой: прокручиваем к ней и берём панель на всю высоту.
        p.evaluate("() => { const c = document.querySelector('.cbd-pf'); if (c) c.scrollIntoView({block: 'center'}); }")
        p.wait_for_timeout(150)
        p.screenshot(path=str(Path(out) / f"{name}.png"))


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_errors_block_publish_and_clicking_a_line_focuses_the_field(browser, theme):
    p, state = _open(browser, "rpa", theme=theme)
    try:
        if theme == "dark":
            p.evaluate("() => document.documentElement.setAttribute('data-theme', 'dark')")
        _first(p)
        assert _badge(p).startswith("errors: ") and int(_badge(p).split(": ")[1]) >= 3
        assert p.locator(".cbd-pf-tree").first.inner_text().startswith("errors: "), "значка нет в дереве"
        assert "Account checks need the API backend" in p.locator(".cbd-pf").inner_text()
        assert p.locator(".cbd-pf [data-act='pf-check']").count() == 0
        # Разделы, а не двадцать строк; под RPA проверки с Meta нет.
        ids = p.evaluate("() => [...document.querySelectorAll('.cbd-pf-topic')].map(t => t.dataset.topic)")
        assert ids == ["audience", "budget", "destination", "creative", "names"], ids
        # Раздел с ошибкой раскрыт сам, чистый — свёрнут.
        assert _topic(p, "creative")["status"] == "error" and _topic(p, "creative")["open"]
        assert "Primary text" in _topic(p, "creative")["summary"]
        assert _topic(p, "audience") == {"status": "ok", "open": False, "summary": "2 checks ok"}
        btn = p.locator(".cbd-publish-all")
        assert btn.is_disabled(), "Publish all включён при ошибках"
        assert "Publish is off" in p.locator(".cbd-gate-reason").inner_text()
        _shots(p, f"01-errors-rpa-{theme}")
        # Клик по строке «Primary text» — вкладка Ad и фокус в поле текста.
        p.locator(".cbd-pf-line[data-rule='primary_text']").click()
        assert p.evaluate("() => document.activeElement.classList.contains('cb-adtitle-search')")
        # Клик по «Link https://» — вкладка Campaign и фокус в поле ссылки.
        p.locator(".cbd-pf-line[data-rule='link_https']").click()
        assert p.evaluate("() => document.activeElement.name") == "tracking_link"
        # Исправили всё — ошибок нет, Publish включён, причина исчезла.
        _fill_valid(p)
        assert _badge(p) == "no errors", p.locator(".cbd-pf").inner_text()
        assert btn.is_enabled() and p.locator(".cbd-gate-reason").count() == 0
        # Исправлено — раздел свернулся сам; щелчок по нему раскрывает и сворачивает.
        assert _topic(p, "creative") == {"status": "ok", "open": False, "summary": "6 checks ok"}
        p.locator(".cbd-pf-tbtn[data-topic='creative']").click()
        assert _topic(p, "creative")["open"] and p.locator(".cbd-pf-line[data-rule='headline']").is_visible()
        p.evaluate("() => cbTree.refreshEditor()")
        assert _topic(p, "creative")["open"], "перерисовка свернула раскрытый человеком раздел"
        p.locator(".cbd-pf-tbtn[data-topic='creative']").click()
        assert not _topic(p, "creative")["open"]
        _shots(p, f"02-valid-rpa-{theme}")
        # Предупреждение (ссылка без меток) показано, но не блокирует.
        p.evaluate("""() => { const i = document.querySelector('.cbd-host input[name="tracking_link"]');
          i.value = 'https://track.example.com/c'; i.dispatchEvent(new Event('input', {bubbles: true})); }""")
        p.evaluate("() => cbTree.refreshEditor()")
        assert _badge(p).startswith("warnings: ") and btn.is_enabled()
        assert _topic(p, "destination")["status"] == "warning" and _topic(p, "destination")["open"]
        assert state["plan_posts"] == 0, "под RPA ушёл запрос аккаунта"
        assert not state["errors"], state["errors"]
    finally:
        state["ctx"].close()


def test_under_api_the_check_button_loads_the_account_verdicts_and_only_on_press(browser):
    reply = {"account": "Demo", "results": [{"campaign_name": "x", "error": None, "audit": [
        {"rule": "account_active", "status": "ok", "detail": "", "ads": []},
        {"rule": "page_ok", "status": "error", "ads": [],
         "detail": "The page from the account map cannot advertise from this ad account."},
        {"rule": "pixel_ok", "status": "ok", "detail": "", "ads": []}]}], "error": "", "account_audit": None}

    def make_reply():
        name = p.evaluate("() => document.querySelector('.cbd-host .cb-slot:not([hidden]) .cb-name-hidden').value")
        reply["results"][0]["campaign_name"] = name
        return reply

    p, state = _open(browser, "api", make_reply)
    try:
        _first(p)
        _fill_valid(p)
        # Правки клавишами сеть не трогают.
        p.locator(".cbd-pf-line[data-rule='budget_set']")      # карточка есть
        assert state["plan_posts"] == 0
        acc = _topic(p, "account")
        assert acc["status"] == "unknown" and not acc["open"] and "not checked yet" in acc["summary"], acc
        assert _topic(p, "meta")["summary"] == "Not checked yet"
        assert p.locator(".cbd-pf-check").inner_text() == "Check with Meta"
        assert "30 s" in p.locator(".cbd-pf-meta").inner_text(), "не сказано, сколько идёт проверка"
        assert p.locator(".cbd-publish-all").is_enabled(), "не проверенный кабинет не блокирует"
        assert p.locator(".cbd-plan").inner_text() == "Check 1 campaign with Meta"
        _shots(p, "03-api-before-check-light")
        # Пока проверка идёт: «Checking… 0:42», серверные разделы «checking», кнопка выключена.
        p.evaluate("""() => { const f = document.querySelector('.cbd-host form.open-account-form');
          f._pfBusy = true; f._pfStarted = Date.now() - 42000; cbTree.refreshEditor(); }""")
        assert p.locator(".cbd-pf-check").is_disabled() and "Checking… 0:42" in p.locator(".cbd-pf-check").inner_text()
        assert _topic(p, "account")["status"] == "busy" and _topic(p, "budget")["status"] == "busy"
        assert _topic(p, "destination")["status"] == "ok", "клиентский раздел не ждёт Meta"
        _shots(p, "03b-api-checking-light")
        p.evaluate("""() => { const f = document.querySelector('.cbd-host form.open-account-form');
          f._pfBusy = false; cbTree.refreshEditor(); }""")
        p.locator(".cbd-pf [data-act='pf-check']").click()
        p.wait_for_function("document.querySelector('.cbd-pf-line[data-rule=\"page_ok\"]').classList.contains('cbd-pf-error')")
        assert state["plan_posts"] == 1
        assert _badge(p) == "errors: 1"
        assert _topic(p, "account")["status"] == "error" and _topic(p, "account")["open"]
        assert "Checked with Meta" in p.locator(".cbd-pf-meta").inner_text()
        assert p.locator(".cbd-pf-check").inner_text() == "Check again"
        assert p.locator(".cbd-publish-all").is_disabled()
        assert "Facebook Page" in p.locator(".cbd-gate-reason").inner_text()
        _shots(p, "04-api-after-check-light")
        # Правка кампании делает вердикты кабинета устаревшими: снова «press Check», Publish не заперт.
        p.evaluate("""() => { const e = document.querySelector('.cbd-host [name$="_budget"]');
          e.value = '25'; e.dispatchEvent(new Event('input', {bubbles: true})); }""")
        p.wait_for_function("document.querySelector('.cbd-pf-line[data-rule=\"page_ok\"]').classList.contains('cbd-pf-unknown')")
        assert "Edited since the check" in p.locator(".cbd-pf-meta").inner_text()
        assert p.locator(".cbd-pf-check").inner_text() == "Check again"
        assert p.locator(".cbd-publish-all").is_enabled()
        assert state["plan_posts"] == 1, "правка вызвала запрос"
        # Publish с устаревшими вердиктами сначала обновляет их — и ошибка кабинета не пускает.
        p.locator(".cbd-publish-all").click()
        p.wait_for_function("document.querySelector('.cbd-pf-line[data-rule=\"page_ok\"]').classList.contains('cbd-pf-error')")
        assert state["plan_posts"] == 2
        assert p.locator("#sfc-ok").count() == 0, "окно подтверждения открылось при серверной ошибке"
        assert not state["errors"], state["errors"]
    finally:
        state["ctx"].close()


def test_a_check_that_could_not_reach_the_account_offers_retry_and_keeps_publish_on(browser):
    """ACL-1990: профиль занят / сброс соединения — «Could not check», а не ошибка кампании."""
    replies = [{"account": "Demo", "results": [], "error": "The Demo profile is busy with another job.",
                "error_kind": "not_checked", "account_audit": None}]

    p, state = _open(browser, "api", lambda: replies[min(state["plan_posts"], len(replies)) - 1])
    try:
        _first(p)
        _fill_valid(p)
        _settled_name(p)
        p.locator(".cbd-pf .cbd-pf-check").click()
        p.wait_for_function("document.querySelector('.cbd-pf-meta').textContent.includes('Could not check')")
        assert "busy with another job" in p.locator(".cbd-pf-meta").inner_text()
        assert p.locator(".cbd-pf-check").inner_text() == "Retry"
        assert _badge(p) == "no errors", p.locator(".cbd-pf").inner_text()
        meta = _topic(p, "meta")
        assert meta["status"] == "unknown" and meta["open"] and meta["summary"] == "Could not check", meta
        assert p.locator(".cbd-pf-retry").is_visible()
        assert p.locator(".cbd-publish-all").is_enabled() and p.locator(".cbd-gate-reason").count() == 0
        _shots(p, "06-api-could-not-check-light")
        # Retry из раздела — новая проверка.
        p.locator(".cbd-pf-retry").click()
        p.wait_for_function("document.querySelector('.cbd-pf-check') && !document.querySelector('.cbd-pf-check').disabled")
        assert state["plan_posts"] == 2
        assert not state["errors"], state["errors"]
    finally:
        state["ctx"].close()


def test_warnings_are_listed_in_the_confirm_dialog(browser):
    p, state = _open(browser, "rpa")
    try:
        _first(p)
        _fill_valid(p)
        p.evaluate("""() => { const i = document.querySelector('.cbd-host input[name="tracking_link"]');
          i.value = 'https://track.example.com/c'; i.dispatchEvent(new Event('input', {bubbles: true})); }""")
        p.evaluate("() => cbTree.refreshEditor()")
        p.locator(".cbd-publish-all").click()
        p.wait_for_selector("#safe-confirm-modal")
        text = p.locator("#safe-confirm-modal").inner_text()
        assert "Pre-flight warnings" in text and "Tracking parameters in the link" in text, text
        _shots(p, "05-confirm-with-warning-light")
        assert not state["errors"], state["errors"]
    finally:
        state["ctx"].close()
