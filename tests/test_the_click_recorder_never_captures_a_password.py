"""ACL-1342 (аудит безопасности, S05): рекордер кликов не должен записывать
значение поля пароля или одноразового кода.

`click_recorder.py` слушает `change` на странице человека, который вручную
проходит вход в Multilogin/Meta, и до этого фикса писал `el.value` для
ЛЮБОГО поля — включая `<input type="password">` — прямо в JSON рецепта, а
он читается через `/rpa-record/status` (`recent_actions`). Значение пароля
и код 2FA оказывались в файле рецепта и в ответе статус-эндпоинта в
открытом виде.

Проверяется настоящий инжектируемый JS (`click_recorder._INJECT_JS` /
`_DRAIN_JS`), исполненный в offline Chromium на синтетической странице —
не пересказ по памяти.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import click_recorder as cr  # noqa: E402

_PAGE = """
<form>
  <input id="login" name="login" placeholder="Email" value="">
  <input id="pw" name="password" type="password" placeholder="Password" value="">
  <input id="otp" name="otp_code" type="text" autocomplete="one-time-code" placeholder="6-digit code" value="">
  <input id="approvals" name="approvals_code" type="text" placeholder="Enter the code you received" value="">
</form>
"""


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        yield b
        b.close()


@pytest.fixture
def page(browser):
    ctx = browser.new_context()
    pg = ctx.new_page()
    pg.set_content(_PAGE)
    pg.evaluate(cr._INJECT_JS)
    yield pg
    ctx.close()


def _fill_and_blur(page, selector: str, value: str):
    page.fill(selector, value)
    # change fires on blur — move focus elsewhere, same as a real login flow.
    page.locator("body").click()


def _drain(page) -> list[dict]:
    return page.evaluate(cr._DRAIN_JS)


def test_a_password_fields_value_never_reaches_the_recorded_actions(page):
    _fill_and_blur(page, "#pw", "hunter2-real-password")

    actions = _drain(page)
    changes = [a for a in actions if a.get("type") == "change"]
    assert changes, "поле пароля не породило записи change"

    dump = str(actions)
    assert "hunter2-real-password" not in dump, "пароль утёк в записанные действия"
    assert changes[0]["value"] == "<hidden>"
    assert changes[0].get("secret") is True


def test_a_one_time_code_field_is_also_masked_by_autocomplete(page):
    _fill_and_blur(page, "#otp", "482913")

    actions = _drain(page)
    changes = [a for a in actions if a.get("type") == "change"]
    assert changes, "поле кода не породило записи change"

    dump = str(actions)
    assert "482913" not in dump, "одноразовый код утёк в записанные действия"
    assert changes[0]["value"] == "<hidden>"


def test_metas_approvals_code_checkpoint_field_is_masked_by_name_alone(page):
    """PR #343 ревью: Meta 2FA-чекпоинт называет своё поле `approvals_code` —
    `type="text"`, без `autocomplete="one-time-code"` (легаси-форма чекпоинта
    не всегда его проставляет). Ни "otp", ни "verification", ни "security",
    ни "auth", ни "passcode" в этом имени нет — маскировать обязано только
    распознавание имени поля."""
    _fill_and_blur(page, "#approvals", "913482")

    actions = _drain(page)
    changes = [a for a in actions if a.get("type") == "change"]
    assert changes, "поле approvals_code не породило записи change"

    dump = str(actions)
    assert "913482" not in dump, "код чекпоинта Meta (approvals_code) утёк в записанные действия"
    assert changes[0]["value"] == "<hidden>"
    assert changes[0].get("secret") is True


def test_an_ordinary_text_field_is_still_captured_in_full(page):
    """Фикс не должен маскировать всё подряд — обычное поле логина как было."""
    _fill_and_blur(page, "#login", "buyer@example.test")

    actions = _drain(page)
    changes = [a for a in actions if a.get("type") == "change"]
    assert changes, "обычное поле не породило записи change"
    assert changes[0]["value"] == "buyer@example.test"
    assert not changes[0].get("secret")


def test_get_status_exposes_whatever_the_recorder_captured_unchanged():
    """`get_status()` (эндпоинт `/rpa-record/status`) — просто витрина
    `_state.actions`; если JS замаскировал значение, витрина не должна его
    распаковывать обратно."""
    with cr._lock:
        original = list(cr._state.actions)
    try:
        with cr._lock:
            cr._state.actions = [
                {"type": "change", "name": "password", "value": "<hidden>", "secret": True},
                {"type": "change", "name": "login", "value": "buyer@example.test"},
            ]
        status = cr.get_status()
        dump = str(status)
        assert "<hidden>" in dump
        assert "buyer@example.test" in dump
    finally:
        with cr._lock:
            cr._state.actions = original
