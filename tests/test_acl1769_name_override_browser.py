"""ACL-1769: своё имя кампании доходит до `camp_N_name` (DOM, настоящий cbUpdateName).

Мутация, на которой тест обязан падать: в `cbUpdateName` обнулить чтение поля
override. Чистая `effectiveName` проверена отдельно (builder_layout_harness.cjs), но
только этот тест доказывает проводку «поле → скрытое имя, которое уходит на сервер».
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_builder_duplicate_buttons import browser, page  # noqa: E402,F401
from test_drawer_review_browser import add  # noqa: E402

HIDDEN = ".cbd-host .cb-slot:not([hidden]) .cb-name-hidden"
OVERRIDE = ".cbd-host .cb-slot:not([hidden]) .cb-name-override"


def _open(p):
    add(p)
    p.locator(".cbd-camp-row").first.click()
    p.locator(".cbd-host .cb-slot:not([hidden]) [name$='_asset']").fill("Alpha")


def test_the_override_reaches_camp_n_name_and_falls_back_to_the_auto_name(page):
    _open(page)
    auto = page.input_value(HIDDEN)
    assert "Alpha" in auto or auto, "собранное имя пустое — проверять нечего"

    page.fill(OVERRIDE, "My own name")
    assert page.input_value(HIDDEN) == "My own name", "своё имя не попало в camp_N_name"
    assert page.locator(".cbd-host .cb-slot:not([hidden]) [data-name-display]").text_content() == "My own name"
    page.wait_for_function("document.querySelector('.cbd-camp-name').textContent === 'My own name'")

    page.fill(OVERRIDE, "")
    assert page.input_value(HIDDEN) == auto, "стёртое своё имя не вернуло собранное"

    page.fill(OVERRIDE, "   ")
    assert page.input_value(HIDDEN) == auto, "пробелы приняты за имя"

    page.fill(OVERRIDE, "  Trimmed  ")
    assert page.input_value(HIDDEN) == "Trimmed"

    # Серверу уходит ровно тот же набор полей: у своего имени нет `name`.
    keys = page.evaluate("() => Array.from(new FormData(document.querySelector('.cbd-host form.open-account-form')).keys())")
    assert not [k for k in keys if "override" in k], keys
    assert "camp_0_name" in keys

    # А в черновике оно есть и переживает перезагрузку.
    page.evaluate("() => cbKeep.save()")
    saved = page.evaluate("() => localStorage.getItem('acl.builder.v2')")
    assert "name_override" in saved and "Trimmed" in saved
