"""Шаг засчитывал успех по соседнему полю.

`_select_leads_dataset` отвечает True, как только заполнено поле «Dataset».
Пиксель у этих аккаунтов один, Meta подставляет его сама — значит поле
непусто всегда. `select_conversion_event_lead` возвращал True, ни разу не
тронув Conversion event.

10 сентября обе кампании ушли в черновик без события конверсии, при зелёном
`conversion_event` в телеметрии. В снимке ад-сета, снятом уже после «успеха»,
стоит пустое состояние поля: «Select an event or search by name».

Посылка «с 24.08 поля события нет, его заменил Dataset» была верна когда-то.
Проверка осталась на ней, а поле вернулось.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import meta_dom  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ads_manager"


def test_the_dataset_alone_is_not_the_conversion_event():
    """Главное правило шага, записанное так, чтобы его нельзя было потерять."""
    source = (ROOT / "src" / "fb_ads_rpa" / "meta_dom.py").read_text(encoding="utf-8")
    start = source.index("def select_conversion_event_lead")
    body = source[start : source.index("\ndef ", start + 10)]
    code = "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("#"))
    assert "conversion_event_field_present(page)" in code, (
        "успех снова засчитывается без проверки, есть ли поле события"
    )
    assert "if _select_leads_dataset(page):\n        return True" not in code, (
        "датасет снова закрывает шаг в одиночку"
    )


def test_the_old_layout_still_passes_on_the_dataset():
    """Там, где поля события действительно нет, датасет — единственный ответ.

    Иначе правка превратила бы починку в новый отказ на старой раскладке.
    """
    source = (ROOT / "src" / "fb_ads_rpa" / "meta_dom.py").read_text(encoding="utf-8")
    start = source.index("def select_conversion_event_lead")
    body = source[start : source.index("\ndef ", start + 10)]
    assert "return dataset_ok" in body


# ── поле есть или нет: по настоящей разметке ──────────────────────────────
#
# Фикстура — раздел Conversion из снимка ад-сета, снятого 9 сентября уже
# ПОСЛЕ того, как шаг отчитался успехом.

import pytest  # noqa: E402


def _chromium():
    from src.fb_ads_rpa.config import ensure_playwright_browsers_path

    ensure_playwright_browsers_path()
    from playwright.sync_api import sync_playwright

    return sync_playwright()


@pytest.fixture(scope="module")
def page():
    with _chromium() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            pg = browser.new_page()
            pg.goto((FIXTURES / "conversion_event_2026_09_09.html").as_uri())
            yield pg
        finally:
            browser.close()


def test_the_field_is_on_the_page_after_the_step_reported_success(page):
    """Посылка «поле убрали» разметкой не подтверждается.

    Снимок сделан после зелёного `conversion_event` — и метка на месте.
    """
    assert meta_dom.conversion_event_field_present(page) is True


def test_and_it_was_empty(page):
    """Пустое состояние поля — прямая улика: событие так и не выбрали."""
    assert page.get_by_text("Select an event or search by name").count() == 1


def test_a_page_without_the_field_says_so(page):
    """Обратная проверка: иначе функция отвечала бы «есть» на чём угодно."""
    page.evaluate(
        """() => {
          for (const e of document.querySelectorAll('*')) {
            if (/conversion event/i.test(e.textContent || '') && e.children.length <= 2) {
              e.textContent = 'Performance goal';
            }
          }
        }"""
    )
    try:
        assert meta_dom.conversion_event_field_present(page) is False
    finally:
        page.reload()
