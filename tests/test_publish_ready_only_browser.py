"""Publish popup: «Publish N ready» и убрать не готовое из этой публикации (×).

Владелец 17.11: выбрал несколько кампаний, часть готова, часть нет. Не готовые
перечислены в окне; готовые он хочет отправить прямо оттуда, а лишние —
по желанию убрать крестиком. Крестик только снимает галочку и выводит
кампанию из этой публикации: черновик НЕ стирается (чёрновики стирает
только Remove человека в билдере). Окно не закрывается, страница не
перезагружается, второго подтверждения нет. Не готовое никогда не уходит.

Стенд — тот же, что у ACL-2353: настоящий шаблон, подменённые маршруты.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_acl2353_review_browser import (  # noqa: E402,F401
    Fake, _open, _ready, _publish, _settle, _names, _body, _real, browser,
)

PIXEL_ERROR = {"audit": [{"rule": "pixel_ok", "status": "error", "ads": [], "detail": "The ad account has no pixel."}]}


def _hidden_names(p):
    return p.evaluate("() => [...document.querySelectorAll('form.open-account-form .cb-name-hidden')].map(x => x.value)")


def _setup(browser, count=3, bad_idx=(1,), tick=False):
    fake = Fake()
    p, fake, errors, ctx = _open(browser, fake=fake)
    _ready(p, count=count)
    names = _hidden_names(p)
    bad = [names[i] for i in bad_idx]
    fake.check_extra = lambda n: dict(PIXEL_ERROR) if n in bad else {}
    if tick:
        for i in range(count):
            p.locator('.cbd-camp input[type="checkbox"]').nth(i).check()
    return p, fake, errors, ctx, names, bad


def _jobs_names(fake):
    out = []
    for body in fake.jobs:
        out += _names(body)
    return out


def test_ready_and_not_ready_are_listed_apart_and_the_button_counts_only_ready(browser):
    p, fake, errors, ctx, names, bad = _setup(browser)
    try:
        _publish(p)
        _settle(p)
        left, right = p.locator(".pc-left").inner_text(), p.locator(".pc-right").inner_text()
        for ok_name in (names[0], names[2]):
            assert ok_name in left, left
        assert "Will not be created (1)" in right and bad[0] in right
        assert "The ad account has no pixel." in right
        assert p.locator("#sfc-ok").inner_text() == "Publish 2 ready"
        assert p.locator("#sfc-ok").is_enabled()
        # Один шаг: без второго подтверждения.
        p.locator("#sfc-ok").click()
        p.wait_for_function("document.querySelector('#safe-confirm-modal') === null")
        p.wait_for_timeout(300)
        assert len(fake.jobs) == 1
        sent = _jobs_names(fake)
        assert sorted(sent) == sorted([names[0], names[2]]), sent   # не готовое не уходит, и крестик не нужен
        assert not _real(errors), errors
    finally:
        ctx.close()


def test_x_takes_the_campaign_out_keeps_the_popup_and_the_draft_survives_reload(browser):
    p, fake, errors, ctx, names, bad = _setup(browser, tick=True)
    try:
        p.evaluate("() => { window.__same_page = 'yes'; }")
        _publish(p)
        _settle(p)
        x = p.locator("#safe-confirm-modal .rv-blocked [data-rv-drop]")
        assert x.count() == 1
        x.click()
        assert p.locator("#safe-confirm-modal").count() == 1            # окно открыто
        assert p.evaluate("window.__same_page") == "yes"               # страница не перезагружалась
        assert "Will not be created" not in _body(p)
        assert p.locator("#sfc-ok").inner_text() == "Publish 2 ready"
        assert p.locator("#sfc-ok").is_enabled()
        assert fake.jobs == []
        # В билдере кампания снята с галочки, но осталась.
        assert p.evaluate("() => [...document.querySelectorAll('.cb-slot')].map(s => !!s._cbdPick)") == [True, False, True]
        assert p.evaluate("() => document.querySelectorAll('form.open-account-form .cb-slot').length") == 3
        # Подтверждение после удаления из списка не считается «изменённым».
        p.locator("#sfc-ok").click()
        p.wait_for_function("document.querySelector('#safe-confirm-modal') === null")
        p.wait_for_timeout(300)
        assert len(fake.jobs) == 1 and bad[0] not in _jobs_names(fake)
        assert sorted(_jobs_names(fake)) == sorted([names[0], names[2]])
        # Черновик жив после перезагрузки.
        saved = p.evaluate("() => JSON.stringify(cbKeep.all())")
        assert bad[0] in saved
        p.reload()
        p.evaluate("cbDrawer.open()")
        p.wait_for_function("document.querySelectorAll('form.open-account-form .cb-slot').length === 3")
        assert bad[0] in _hidden_names(p)
        assert not _real(errors), errors
    finally:
        ctx.close()


def test_x_works_when_nothing_was_ticked_publish_all(browser):
    p, fake, errors, ctx, names, bad = _setup(browser)
    try:
        _publish(p)
        _settle(p)
        p.locator("#safe-confirm-modal .rv-blocked [data-rv-drop]").click()
        assert p.locator("#sfc-ok").inner_text() == "Publish 2 ready"
        assert "Campaigns changed since this review" not in _body(p)
        p.locator("#sfc-ok").click()
        p.wait_for_function("document.querySelector('#safe-confirm-modal') === null")
        p.wait_for_timeout(300)
        assert sorted(_jobs_names(fake)) == sorted([names[0], names[2]])
    finally:
        ctx.close()


def test_nothing_ready_disables_the_button_and_shows_the_reason(browser):
    p, fake, errors, ctx, names, bad = _setup(browser, count=1, bad_idx=(0,))
    try:
        _publish(p)
        _settle(p)
        assert p.locator("#sfc-ok").inner_text() == "Publish 0 ready"
        assert p.locator("#sfc-ok").is_disabled()
        assert "Nothing can be created" in _body(p) and "The ad account has no pixel." in _body(p)
        p.locator("#sfc-ok").click(force=True)
        assert fake.jobs == []
    finally:
        ctx.close()


def test_not_ready_account_can_be_taken_out_and_is_never_sent(browser):
    fake = Fake()
    fake.ready = False
    p, fake, errors, ctx = _open(browser, fake=fake)
    try:
        _ready(p, count=2)
        _publish(p)
        _settle(p)
        assert "Not ready (1)" in _body(p)
        assert p.locator("#sfc-ok").is_disabled()
        p.locator("#safe-confirm-modal [data-rv-drop-acct]").click()
        assert p.locator("#safe-confirm-modal").count() == 1
        assert "Not ready" not in _body(p)
        assert p.locator("#sfc-ok").is_disabled()
        assert fake.jobs == []
        assert p.evaluate("() => document.querySelectorAll('form.open-account-form .cb-slot').length") == 2
    finally:
        ctx.close()
