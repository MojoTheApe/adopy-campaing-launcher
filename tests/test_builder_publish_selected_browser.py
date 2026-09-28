"""ACL-1360: галочки у кампаний билдера и «Publish selected» — на настоящем шаблоне.

Владелец хочет выбирать, что собирать, а не собирать всё. У каждой строки
кампании в ящике — настоящий чекбокс; отмечено что-то — нижняя кнопка
читается «Publish selected → drafts (K)» и шлёт только отмеченное, по
заданию на аккаунт; «Select all» сверху отмечает всё, и кнопка снова
«Publish all → drafts (N)», а сам переключатель — «Clear selection».
Кампании аккаунта, который сейчас собирается, отметить нельзя. Отметки
переживают F5 и снимаются с кампаний, ушедших роботу.

Сеть подменена тем же поддельным `/publish-jobs`, что и в тесте ACL-1355:
проверяются сами тела запросов, а не надписи.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest
from playwright.sync_api import expect
from test_builder_multi_publish_browser import (  # noqa: F401 — фикстуры
    browser, html, ui, add, ready, publish_one, finish, camp_name,
)

ALL_RE = r'^Publish all → drafts \((\d+)\)$'


def box(page, account, slot=0):
    """Чекбокс строки кампании в ящике — по имени кампании, не по номеру."""
    name = camp_name(page, account, slot)
    return page.locator('.cbd-camp').filter(
        has=page.locator('.cbd-camp-name', has_text=name)).locator('input[type="checkbox"]')


def publish_button(page):
    return page.locator('.cbd-publish-all')


def wait_posts(state, n, page):
    for _ in range(80):
        if len(state['posts']) >= n:
            break
        page.wait_for_timeout(100)
    page.wait_for_timeout(300)   # лишнее задание успело бы уйти
    assert len(state['posts']) == n, [p['account_label'] for p in state['posts']]


def sent_names(post):
    return [post['camp_%d_name' % i] for i in range(int(post['camp_count']))]


def test_ticked_campaigns_only_are_sent_one_job_per_account(ui):
    p, state = ui
    add(p, 0, count=2)
    add(p, 1, count=2)
    ready(p)
    demo_b, other_a = camp_name(p, 0, 1), camp_name(p, 1, 0)
    assert len({camp_name(p, 0, 0), demo_b, other_a, camp_name(p, 1, 1)}) == 4

    # Ничего не отмечено — всё как раньше.
    expect(p.locator('.cbd-camp input[type="checkbox"]')).to_have_count(4)
    for a, s in ((0, 0), (0, 1), (1, 0), (1, 1)):
        expect(box(p, a, s)).not_to_be_checked()
    expect(publish_button(p)).to_have_text('Publish all → drafts (4)')
    # Галочки только у кампаний — не у аккаунтов и не у объявлений.
    expect(p.locator('.cbd-account input[type="checkbox"]')).to_have_count(0)

    # Одна — мышью, вторая — с клавиатуры: настоящий чекбокс, Space его ставит.
    box(p, 0, 1).click()
    expect(publish_button(p)).to_have_text('Publish selected → drafts (1)')
    box(p, 1, 0).focus()
    p.keyboard.press('Space')
    expect(box(p, 1, 0)).to_be_checked()
    expect(publish_button(p)).to_have_text('Publish selected → drafts (2)')
    label = box(p, 1, 0).get_attribute('aria-label') or ''
    assert other_a in label, label

    publish_button(p).click()
    modal = p.locator('#safe-confirm-modal')
    expect(modal).to_contain_text('Campaigns: 2')
    expect(modal).to_contain_text('Demo')
    expect(modal).to_contain_text('Other')
    p.locator('#sfc-ok').click()
    wait_posts(state, 2, p)

    by_account = {f['account_label']: f for f in state['posts']}
    assert set(by_account) == {'Demo', 'Other'}
    assert sent_names(by_account['Demo']) == [demo_b], sent_names(by_account['Demo'])
    assert sent_names(by_account['Other']) == [other_a], sent_names(by_account['Other'])
    assert by_account['Demo']['hand_off'] == '1' and by_account['Demo']['activate'] == '0'

    # Ушедшие роботу отметки сняты; аккаунты собираются — их не отметить.
    for a, s in ((0, 1), (1, 0)):
        expect(box(p, a, s)).not_to_be_checked(timeout=6000)
    for a, s in ((0, 0), (0, 1), (1, 0), (1, 1)):
        expect(box(p, a, s)).to_be_disabled()
    title = box(p, 0, 0).evaluate('b => b.closest("label").title')
    assert 'being built' in title, title
    expect(publish_button(p)).to_have_text('Publish all → drafts (4)')
    assert p.evaluate("JSON.stringify(cbKeep.all())").count('"picked":true') == 0

    finish(p, state, 'Demo')
    finish(p, state, 'Other')
    expect(p.locator('.cbd-queue')).to_contain_text('Done — 2 of 2 accounts', timeout=8000)
    expect(box(p, 0, 0)).to_be_enabled(timeout=6000)
    assert state['dialogs'] == []


def test_select_all_returns_publish_all_and_clear_selection_resets(ui):
    p, state = ui
    add(p, 0, count=2)
    add(p, 1, count=1)
    ready(p)
    control = p.locator('.cbd-tick-all')
    expect(control).to_have_text('Select all')
    control.click()
    for a, s in ((0, 0), (0, 1), (1, 0)):
        expect(box(p, a, s)).to_be_checked()
    expect(publish_button(p)).to_have_text('Publish all → drafts (3)')
    expect(control).to_have_text('Clear selection')

    control.click()
    for a, s in ((0, 0), (0, 1), (1, 0)):
        expect(box(p, a, s)).not_to_be_checked()
    expect(control).to_have_text('Select all')
    expect(publish_button(p)).to_have_text('Publish all → drafts (3)')

    # Отмечено не всё — «Select all» остаётся, рядом — «Clear selection».
    box(p, 0, 0).click()
    expect(publish_button(p)).to_have_text('Publish selected → drafts (1)')
    expect(control).to_have_text('Select all')
    clear = p.locator('.cbd-tick-none')
    expect(clear).to_have_text('Clear selection')
    clear.click()
    expect(box(p, 0, 0)).not_to_be_checked()
    expect(publish_button(p)).to_have_text('Publish all → drafts (3)')

    # Всё отмечено — «Publish all» шлёт все кампании, как раньше.
    control.click()
    publish_button(p).click()
    expect(p.locator('#safe-confirm-modal')).to_contain_text('Build every campaign as a draft?')
    p.locator('#sfc-ok').click()
    wait_posts(state, 2, p)
    by_account = {f['account_label']: f for f in state['posts']}
    assert by_account['Demo']['camp_count'] == '2'
    assert by_account['Other']['camp_count'] == '1'


def test_running_account_cannot_be_ticked_and_selection_survives_reload(ui):
    p, state = ui
    add(p, 0, count=1)
    add(p, 1, count=2)
    ready(p)
    box(p, 1, 1).click()
    expect(publish_button(p)).to_have_text('Publish selected → drafts (1)')

    publish_one(p, state, 0)          # Publish у карточки Demo — аккаунт собирается
    expect(box(p, 0, 0)).to_be_disabled(timeout=6000)
    assert 'being built' in box(p, 0, 0).evaluate('b => b.closest("label").title')
    expect(box(p, 1, 0)).to_be_enabled()
    expect(box(p, 1, 1)).to_be_checked()

    # «Select all» не трогает собирающийся аккаунт: всё, что можно, отмечено.
    p.locator('.cbd-tick-all').click()
    expect(box(p, 1, 0)).to_be_checked()
    expect(box(p, 0, 0)).not_to_be_checked()
    expect(p.locator('.cbd-tick-all')).to_have_text('Clear selection')
    p.locator('.cbd-tick-all').click()
    box(p, 1, 1).click()
    expect(publish_button(p)).to_have_text('Publish selected → drafts (1)')

    p.reload()
    p.evaluate('cbDrawer.open()')
    expect(box(p, 1, 1)).to_be_checked(timeout=6000)
    expect(box(p, 1, 0)).not_to_be_checked()
    expect(box(p, 0, 0)).to_be_disabled(timeout=6000)
    expect(publish_button(p)).to_have_text('Publish selected → drafts (1)')

    finish(p, state, 'Demo')
    expect(box(p, 0, 0)).to_be_enabled(timeout=8000)

    # После F5 «Publish selected» шлёт только отмеченное.
    ready(p)
    want = camp_name(p, 1, 1)
    publish_button(p).click()
    p.locator('#sfc-ok').click()
    wait_posts(state, 2, p)
    assert state['posts'][-1]['account_label'] == 'Other'
    assert sent_names(state['posts'][-1]) == [want]


def test_selection_storage_failure_never_breaks_the_drawer(ui):
    """Хранилище недоступно (приватное окно) — галочки работают, страница живёт."""
    p, state = ui
    add(p, 0, count=2)
    ready(p)
    p.evaluate('''() => { Storage.prototype.setItem = () => { throw new Error('quota'); }; }''')
    box(p, 0, 0).click()
    expect(box(p, 0, 0)).to_be_checked()
    expect(publish_button(p)).to_have_text('Publish selected → drafts (1)')
