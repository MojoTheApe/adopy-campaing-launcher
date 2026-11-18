"""ACL-2658: счёт и состав «Publish selected» идут за видимыми галочками.

Байер собрал 2 аккаунта x 4 кампании и отправил их, добавил ещё два аккаунта,
снял галочки с первых двух и стал отмечать новые кампании по одной: 1, 2, ... 7,
а на восьмой счёт прыгнул на 16. Когда отмечено всё, что можно отметить, кнопка
«Check ... with Meta» считала `total` (все кампании, в том числе те, что ещё
собираются и в запуск не едут), а запуск и проверка строились заново по формам
в момент клика, а не по отметкам. Теперь галочка аккаунта считается из галочек
его кампаний, счёт - из отмеченных и доступных, а уходит ровно отмеченное.

Сеть подменена тем же поддельным `/publish-jobs`, что и в тестах ACL-1355/1360:
в Meta ничего не уходит, проверяются тела запросов.
"""
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest
from playwright.sync_api import expect, sync_playwright
from test_builder_per_account import _render
from test_builder_multi_publish_browser import FORM_JS, _route_for, ready
from acl_dialog_capture import capture_dialogs

ACCOUNTS = ['A1', 'A2', 'A3', 'A4']


@pytest.fixture(scope='module')
def browser():
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get('ACL_BROWSER_EXECUTABLE') or None)
        yield b
        b.close()


@pytest.fixture(scope='module')
def html():
    return _render({a: [] for a in ACCOUNTS})


@pytest.fixture
def ui(browser, html):
    context = browser.new_context()
    page = context.new_page()
    errors, dialogs = [], []
    page.on('pageerror', lambda e: errors.append(str(e)))

    def on_dialog(d):
        dialogs.append(d.message)
        d.accept()
    page.on('dialog', on_dialog)
    capture_dialogs(page, dialogs)
    state = {'posts': [], 'jobs': {}, 'cancels': [], 'dialogs': dialogs}
    page.route('**/*', _route_for(html, state))
    page.goto('http://acl.test/')
    page.evaluate('cbDrawer.open()')
    yield page, state
    assert not errors, errors
    context.close()


def add(page, account, count):
    page.evaluate('''([account, count]) => {
      const f = (''' + FORM_JS + ''')(account);
      for (let i = 0; i < count; i++) {
        cbAddSlot(f); const s = f.querySelector('.cb-slot:last-child');
        const asset = s.querySelector('[name$="_asset"]');
        asset.value = account + '-' + (f.querySelectorAll('.cb-slot').length);
        asset.dispatchEvent(new Event('input', {bubbles: true}));
        s.querySelector('[name$="_ad_0_creative_file"]').value = 'offline-' + i + '.png';
      }
      f.dispatchEvent(new Event('input', {bubbles: true}));
    }''', [account, count])
    page.wait_for_function("document.querySelectorAll('.cbd-camp').length > 0")


def boxes(page, account):
    """Чекбоксы кампаний аккаунта - по карточке дерева."""
    ids = page.evaluate('a => Array.from((' + FORM_JS + ')(a).querySelectorAll(".cb-slot")).map(s => s.dataset.cbdId)', account)
    return [page.locator('.cbd-camp[data-camp="%s"] input.cbd-tick-box' % i) for i in ids]


def acc_box(page, account):
    return page.locator('.cbd-acc[data-acc$="|%s"] .cbd-acc-tick-box' % account)


def publish_button(page):
    return page.locator('.cbd-publish-all')


def ticked_names(page):
    """Что реально отмечено на странице: имена кампаний с включённым `_cbdPick`."""
    return sorted(page.evaluate('''() => Array.from(document.querySelectorAll('.cb-slot'))
        .filter(s => s._cbdPick).map(s => s.querySelector('.cb-name-hidden').value)'''))


def visible_checked(page):
    return page.locator('.cbd-camp input.cbd-tick-box:checked').count()


def names_of(page, account):
    return page.evaluate('a => Array.from((' + FORM_JS + ')(a).querySelectorAll(".cb-name-hidden")).map(i => i.value)', account)


def sent_names(post):
    return [post['camp_%d_name' % i] for i in range(int(post['camp_count']))]


def wait_posts(page, state, n):
    for _ in range(80):
        if len(state['posts']) >= n:
            break
        page.wait_for_timeout(100)
    page.wait_for_timeout(300)
    assert len(state['posts']) == n, [p['account_label'] for p in state['posts']]


def test_the_buyers_sequence_counts_one_to_eight_and_never_more(ui):
    p, state = ui
    add(p, 'A1', 4)
    add(p, 'A2', 4)
    ready(p)
    # Первый заход: «Select all» и Publish - оба аккаунта уходят в сборку.
    p.locator('.cbd-tick-all').click()
    expect(acc_box(p, 'A1')).to_be_checked()
    expect(acc_box(p, 'A2')).to_be_checked()
    publish_button(p).click()
    p.locator('#sfc-ok').click()
    wait_posts(p, state, 2)
    sent_first = {f['account_label']: sent_names(f) for f in state['posts']}
    assert sorted(sent_first) == ['A1', 'A2'] and all(len(v) == 4 for v in sent_first.values())

    # Новые аккаунты; сборка первых двух ещё идёт - они заперты.
    p.evaluate("window.ACL_FB_BACKEND = 'api'")   # кнопка «Check ... with Meta» есть только на Graph API
    add(p, 'A3', 4)
    add(p, 'A4', 4)
    ready(p)
    # Галочки первых двух аккаунтов сняты - и у их кампаний тоже: ничего не осталось.
    for a in ('A1', 'A2'):
        if acc_box(p, a).is_enabled() and acc_box(p, a).is_checked():
            acc_box(p, a).uncheck()
        expect(acc_box(p, a)).not_to_be_checked()
    assert ticked_names(p) == []
    assert visible_checked(p) == 0

    new_boxes = boxes(p, 'A3') + boxes(p, 'A4')
    assert len(new_boxes) == 8
    for i, b in enumerate(new_boxes, 1):
        b.click()
        s = '' if i == 1 else 's'
        expect(p.locator('.cbd-publication-scope strong')).to_have_text('%d campaign%s selected' % (i, s))
        expect(publish_button(p)).to_contain_text('(%d)' % i)
        # Счёт равен видимым галочкам - в кнопке, в строке «выбрано» и в проверке с Meta.
        assert visible_checked(p) == i
        assert len(ticked_names(p)) == i
        expect(p.locator('.cbd-plan')).to_have_text('Check %d campaign%s with Meta' % (i, s))
    # Всё доступное отмечено - «Publish all», но счёт всё равно 8, не 16.
    expect(publish_button(p)).to_contain_text('Publish all')
    expect(publish_button(p)).to_contain_text('(8)')
    assert ticked_names(p) == sorted(names_of(p, 'A3') + names_of(p, 'A4'))

    # Уходит ровно отмеченное: два новых задания, первые аккаунты не тронуты.
    p.evaluate("window.ACL_FB_BACKEND = 'rpa'")
    publish_button(p).click()
    p.locator('#sfc-ok').click()
    wait_posts(p, state, 4)
    second = {f['account_label']: sent_names(f) for f in state['posts'][2:]}
    assert sorted(second) == ['A3', 'A4']
    assert second['A3'] == names_of(p, 'A3') and second['A4'] == names_of(p, 'A4')
    assert state['dialogs'] == []


def test_account_tick_follows_and_clears_its_campaigns_only(ui):
    p, state = ui
    add(p, 'A1', 3)
    add(p, 'A2', 3)
    ready(p)
    a1, a2 = boxes(p, 'A1'), boxes(p, 'A2')

    acc_box(p, 'A1').check()
    for b in a1:
        expect(b).to_be_checked()
    for b in a2:
        expect(b).not_to_be_checked()
    assert ticked_names(p) == sorted(names_of(p, 'A1'))
    expect(publish_button(p)).to_have_text('Publish selected → drafts (3)')

    # Снял галочку аккаунта - сняты все его кампании, у соседа ничего не изменилось.
    a2[0].click()
    acc_box(p, 'A1').uncheck()
    for b in a1:
        expect(b).not_to_be_checked()
    assert ticked_names(p) == [names_of(p, 'A2')[0]]
    expect(publish_button(p)).to_have_text('Publish selected → drafts (1)')
    expect(acc_box(p, 'A2')).not_to_be_checked()
    assert acc_box(p, 'A2').evaluate('b => b.indeterminate') is True

    # Последняя кампания аккаунта: галочка аккаунта встаёт, за его пределами - ничего.
    a2[1].click()
    a2[2].click()
    expect(acc_box(p, 'A2')).to_be_checked()
    assert acc_box(p, 'A2').evaluate('b => b.indeterminate') is False
    expect(acc_box(p, 'A1')).not_to_be_checked()
    assert ticked_names(p) == sorted(names_of(p, 'A2'))
    assert visible_checked(p) == 3
    expect(publish_button(p)).to_have_text('Publish selected → drafts (3)')

    # Снята одна - галочка аккаунта «частично», остальные на месте.
    a2[1].click()
    assert acc_box(p, 'A2').evaluate('b => b.indeterminate') is True
    expect(publish_button(p)).to_have_text('Publish selected → drafts (2)')


def test_publish_sends_exactly_the_ticked_campaigns(ui):
    p, state = ui
    add(p, 'A1', 3)
    add(p, 'A2', 3)
    ready(p)
    a1n, a2n = names_of(p, 'A1'), names_of(p, 'A2')
    boxes(p, 'A1')[1].click()
    boxes(p, 'A2')[0].click()
    boxes(p, 'A2')[2].click()
    expect(publish_button(p)).to_have_text('Publish selected → drafts (3)')
    publish_button(p).click()
    p.locator('#sfc-ok').click()
    wait_posts(p, state, 2)
    by = {f['account_label']: sent_names(f) for f in state['posts']}
    assert by == {'A1': [a1n[1]], 'A2': [a2n[0], a2n[2]]}, by

    # Отправленное снято с галочек; аккаунты в сборке второй раз не берутся.
    assert ticked_names(p) == []
    assert visible_checked(p) == 0
    expect(publish_button(p)).to_have_text('Publish all → drafts (0)')
    assert len(state['posts']) == 2
    assert state['dialogs'] == []
