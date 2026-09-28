"""ACL-1342 (аудит безопасности, S01): клик по шапке аккордеона аккаунта не
должен исполнять то, что лежит в имени аккаунта.

Раньше шапка аккордеона рисовалась как
`onclick="cbToggleAcc('{{ acc_id }}')"`, а `acc_id` — это имя аккаунта, в
котором заменены только пробелы и точки. Jinja экранирует `{{ acc_id }}` для
HTML-атрибута (кавычка -> `&#39;`), но браузер декодирует HTML-сущности
атрибута ДО того, как выполнит его как JS — то есть строка вида
`O'Brien'); window.x=1; //` в имени аккаунта возвращала себе кавычку и
вырывалась из JS-литерала прямо в обработчике клика.

Разметка (`<div class="cb-acc-row">...`) и код обработчика (`cbToggleAcc` +
делегирующий `addEventListener`) вырезаны из настоящего `report.html` —
проверяется исполнение, а не пересказ логики по памяти. Аккордеон в этом
срезе показан отдельно от вкладок и `<details>`, в которые он обычно вложен
на странице (`view-panel`, `manage-builder`) — их видимость к делу не
относится, а держать их открытыми в тесте пришлось бы через ту же логику,
которую тест не обязан знать.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

REPORT = ROOT / "src" / "web" / "templates" / "report.html"

# Кавычка + `');...//` — классический разрыв JS-строкового литерала внутри
# HTML-атрибута. `acc_id` дополнительно заменяет пробелы и точки на `_`
# (`acc_label.replace(' ', '_').replace('.', '_')`), поэтому у полезной
# нагрузки после разрыва нет ни пробелов, ни точек — иначе `window.aclPwned`
# исказился бы в `_window_aclPwned_` и присвоение бы не сработало.
# `window["aclPwned"]` — то же самое без точки; `//` в конце комментирует
# всё, что шаблон допишет следом (закрывающие `')`).
MALICIOUS_ACCOUNT_NAME = 'x\');window["aclPwned"]=1;//'


def _rendered_page(acc_label: str) -> str:
    from test_web_drill import _MOCK_REPORT, _render_html

    country = {**_MOCK_REPORT["countries"][0],
               "country": "Saudi Arabia",
               "open_recs": {acc_label: []},
               "close_recs": {}}
    return _render_html({**_MOCK_REPORT, "countries": [country]})


def _extract_acc_row(html: str) -> str:
    """Один `<div class="cb-acc-row">...</div>` — балансом вложенных div'ов."""
    start = html.index('<div class="cb-acc-row"')
    depth, i = 0, start
    while True:
        open_i = html.find("<div", i)
        close_i = html.find("</div>", i)
        assert close_i != -1, "не нашли закрывающий </div> у cb-acc-row"
        if open_i != -1 and open_i < close_i:
            depth += 1
            i = open_i + 4
        else:
            depth -= 1
            i = close_i + 6
            if depth == 0:
                return html[start:i]


def _accordion_js() -> str:
    """Настоящие `cbToggleAcc` + делегирующий `addEventListener` — не копия."""
    src = REPORT.read_text(encoding="utf-8")
    start = src.index("// ── Campaign Builder account accordion")
    end = src.index("\nfunction cbToggleAcc", start)
    end = src.index("\n}\n", end) + 2
    block = src[start:end]
    assert "document.addEventListener('click'" in block
    assert "function cbToggleAcc(accId)" in block
    return block


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        yield b
        b.close()


def _load(browser, fragment: str):
    ctx = browser.new_context()
    errors = []
    page = ctx.new_page()
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto("about:blank")
    page.set_content(fragment)
    page.add_script_tag(content=_accordion_js())
    return ctx, page, errors


def test_a_quote_breaking_account_name_renders_inert_and_does_not_run(browser):
    html = _rendered_page(MALICIOUS_ACCOUNT_NAME)
    fragment = _extract_acc_row(html)

    # Сама разметка не должна строить JS-обработчик из имени аккаунта — если
    # он вернулся, следующая проверка (клик) это всё равно поймает, но так
    # фикс задокументирован явно: data-* вместо инлайнового onclick.
    assert 'onclick="cbToggleAcc(' not in fragment
    assert 'data-acc-id="' in fragment

    ctx, page, errors = _load(browser, fragment)
    try:
        header = page.locator(".cb-acc-header").first
        assert header.count() == 1, "аккордеон аккаунта не отрисовался"

        header.click()

        pwned = page.evaluate("window.aclPwned || 0")
        assert pwned == 0, "имя аккаунта исполнилось как JS при клике на шапку"
        assert page.evaluate("typeof window.aclPwned") == "undefined", (
            "имя аккаунта исполнилось как JS при клике на шапку"
        )
        assert not errors, f"клик уронил страницу: {errors}"

        body = page.locator(".cb-acc-body").first
        assert "cb-acc-open" in (body.get_attribute("class") or ""), (
            "обычное поведение тумблера тоже должно работать — аккордеон не открылся"
        )

        # второй клик закрывает обратно — переключатель работает штатно
        header.click()
        assert "cb-acc-open" not in (body.get_attribute("class") or "")
    finally:
        ctx.close()


def test_a_normal_account_name_still_toggles_the_accordion(browser):
    html = _rendered_page("KLD_BFL_X84")
    fragment = _extract_acc_row(html)
    ctx, page, errors = _load(browser, fragment)
    try:
        header = page.locator(".cb-acc-header").first
        header.click()
        body = page.locator(".cb-acc-body").first
        assert "cb-acc-open" in (body.get_attribute("class") or ""), (
            "обычное поведение тумблера тоже должно работать — аккордеон не открылся"
        )
        assert not errors, errors
    finally:
        ctx.close()
