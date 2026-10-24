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
    browser, html, ui, add, ready, publish_one, finish, camp_name, FORM_JS,
    two_country_html, TWIN_JS, _route_for, _seeded_two_country_page,
)
from acl_dialog_capture import capture_dialogs

ALL_RE = r'^Publish all → drafts \((\d+)\)$'


def box(page, account, slot=0):
    """Чекбокс строки кампании в ящике — по карточке, а не по имени: имена
    у кампаний разных аккаунтов бывают одинаковыми."""
    cid = page.evaluate('([a,i]) => (' + FORM_JS + ')(a).querySelectorAll(".cb-slot")[i].dataset.cbdId',
                        [['Demo', 'Other'][account], slot])
    return page.locator('.cbd-camp[data-camp="%s"] input[type="checkbox"]' % cid)


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
    # ACL-1628: подпись берётся из отрисовки ящика — ждём её, а не читаем сразу.
    expect(box(p, 1, 0)).to_have_attribute('aria-label', 'Select %s to publish' % other_a)

    publish_button(p).click()
    modal = p.locator('#safe-confirm-modal')
    expect(modal).to_contain_text('Build 2 campaigns on 2 accounts')
    # Живые аккаунты: в подтверждении — какие именно кампании уйдут.
    listed = modal.locator('.cb-selected-list')
    expect(listed).to_contain_text('Demo: ' + demo_b)
    expect(listed).to_contain_text('Other: ' + other_a)
    expect(listed).not_to_contain_text(camp_name(p, 0, 0))
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
    expect(publish_button(p)).to_have_text('Publish all → drafts (0)')
    expect(publish_button(p)).to_be_disabled()
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
    # Страница узнаёт о конце сборки из опроса: раз в POLL_MS (3 с), после
    # сбоя опроса — через 6 с. 8 с не хватало на занятой машине
    # (параллельный прогон, ACL-1397); ждём два полных цикла со сбоем.
    # Проверка та же: после конца сборки галочка снова доступна.
    expect(box(p, 0, 0)).to_be_enabled(timeout=15000)

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


def test_one_account_in_two_countries_is_ticked_and_locked_per_country(browser, two_country_html):
    """Один аккаунт в двух странах — две формы билдера. Отметки, «одно
    задание на аккаунт» и запрет отмечать идущий — по паре страна+аккаунт."""
    context = browser.new_context()
    page = context.new_page()
    errors, dialogs = [], []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.on('dialog', lambda d: (dialogs.append(d.message), d.accept()))
    capture_dialogs(page, dialogs)
    state = {'posts': [], 'jobs': {}, 'cancels': [], 'dialogs': dialogs}
    page.route('**/*', _route_for(two_country_html, state))
    page.goto('http://acl.test/')

    def twin_box(country, slot):
        cid = page.evaluate('([c,i]) => (' + TWIN_JS + ')(c).querySelectorAll(".cb-slot")[i].dataset.cbdId',
                            [country, slot])
        return page.locator('.cbd-camp[data-camp="%s"] input[type="checkbox"]' % cid)

    def twin_name(country, slot):
        return page.evaluate('([c,i]) => (' + TWIN_JS + ')(c).querySelectorAll(".cb-name-hidden")[i].value',
                             [country, slot])

    try:
        page.evaluate('cbDrawer.open()')
        for i, c in enumerate(('Chile', 'Peru')):
            page.evaluate('''([c,i]) => { const f=(''' + TWIN_JS + ''')(c);
              for (const a of [['Gold','Crude'],['Silver','Wheat']][i]) {
                cbAddSlot(f); const s=f.querySelector('.cb-slot:last-child');
                s.querySelector('[name$="_asset"]').value=a;
                s.querySelector('[name$="_ad_0_creative_file"]').value='offline.png';
              }
              f.dispatchEvent(new Event('input',{bubbles:true})); }''', [c, i])
        ready(page)
        expect(page.locator('.cbd-publish-all')).to_have_text('Publish all → drafts (4)')

        twin_box('Chile', 1).click()
        expect(page.locator('.cbd-publish-all')).to_have_text('Publish selected → drafts (1)')
        want = twin_name('Chile', 1)
        page.locator('.cbd-publish-all').click()
        modal = page.locator('#safe-confirm-modal')
        expect(modal).to_contain_text('Build 1 campaign on 1 account')
        expect(modal).to_contain_text('UL869 (Chile)')
        expect(modal).not_to_contain_text('UL869 (Peru)')
        page.locator('#sfc-ok').click()
        wait_posts(state, 1, page)
        assert state['posts'][0]['country'] == 'Chile'
        assert sent_names(state['posts'][0]) == [want]

        # Идёт Chile — заперта только её форма; Peru того же аккаунта свободна.
        expect(twin_box('Chile', 0)).to_be_disabled(timeout=6000)
        expect(twin_box('Peru', 0)).to_be_enabled()
        twin_box('Peru', 0).click()
        want = twin_name('Peru', 0)
        expect(page.locator('.cbd-publish-all')).to_have_text('Publish selected → drafts (1)')
        page.locator('.cbd-publish-all').click()
        page.locator('#sfc-ok').click()
        wait_posts(state, 2, page)
        assert state['posts'][1]['country'] == 'Peru'
        assert sent_names(state['posts'][1]) == [want]
        assert dialogs == [], dialogs
        assert not errors, errors
    finally:
        context.close()


@pytest.mark.parametrize('saved, locked', [
    ({}, {'Chile': True, 'Peru': True}),                  # задание без страны — занят везде
    ({'country': 'Peru'}, {'Chile': False, 'Peru': True}),
])
def test_a_job_restored_after_reload_locks_ticks_by_its_own_country(browser, two_country_html, saved, locked):
    """Задание, запомненное до F5: без страны оно запирает галочки аккаунта во
    всех странах, со страной — только в своей (ревью v2 PR #363)."""
    context, page, state, dialogs, errors = _seeded_two_country_page(browser, two_country_html, saved)
    try:
        for c in ('Chile', 'Peru'):
            page.evaluate('''c => { const f=(''' + TWIN_JS + ''')(c);
              cbAddSlot(f); const s=f.querySelector('.cb-slot:last-child');
              s.querySelector('[name$="_asset"]').value='Gold';
              s.querySelector('[name$="_ad_0_creative_file"]').value='offline.png';
              f.dispatchEvent(new Event('input',{bubbles:true})); }''', c)
        for c, want in locked.items():
            cid = page.evaluate('c => (' + TWIN_JS + ')(c).querySelector(".cb-slot").dataset.cbdId', c)
            tick = page.locator('.cbd-camp[data-camp="%s"] input[type="checkbox"]' % cid)
            if want:
                expect(tick).to_be_disabled(timeout=6000)
            else:
                expect(tick).to_be_enabled(timeout=6000)
        assert state['posts'] == []
        assert not errors, errors
    finally:
        context.close()


# ── ревью PR #369 ──────────────────────────────────────────────────────────

def picks(page, account):
    return page.evaluate('a => Array.from((' + FORM_JS + ')(a).querySelectorAll(".cb-slot")).map(s => !!s._cbdPick)',
                         ['Demo', 'Other'][account])


def test_the_count_is_what_goes_and_a_ticked_card_of_a_running_form_can_be_unticked(ui):
    p, state = ui
    add(p, 0, count=2)
    add(p, 1, count=1)
    ready(p)
    box(p, 0, 1).click()
    box(p, 1, 0).click()
    expect(publish_button(p)).to_have_text('Publish selected → drafts (2)')

    publish_one(p, state, 0, slot=0)          # Demo собирается; её отмеченная карточка 1 — нет
    expect(box(p, 0, 0)).to_be_disabled(timeout=6000)
    # K — только то, что уйдёт: отмеченная карточка идущей Demo не считается.
    expect(publish_button(p)).to_have_text('Publish selected → drafts (1)')
    expect(box(p, 0, 1)).to_be_checked()
    expect(box(p, 0, 1)).to_be_enabled()      # снять можно, серой-отмеченной не висит
    assert 'untick' in box(p, 0, 1).evaluate('b => b.closest("label").title')

    publish_button(p).click()
    modal = p.locator('#safe-confirm-modal')
    expect(modal).to_contain_text('Build 1 campaign on 1 account')
    expect(modal.locator('.cb-selected-list')).to_contain_text('Other: ' + camp_name(p, 1, 0))
    expect(modal.locator('.cb-selected-list')).not_to_contain_text('Demo')
    expect(modal).to_contain_text('Already being built, skipped: Demo')
    p.locator('#sfc-ok').click()
    wait_posts(state, 2, p)
    assert state['posts'][1]['account_label'] == 'Other'
    assert sent_names(state['posts'][1]) == [camp_name(p, 1, 0)]

    box(p, 0, 1).click()                      # снять отметку у идущей формы
    expect(box(p, 0, 1)).not_to_be_checked()
    expect(box(p, 0, 1)).to_be_disabled()     # а поставить снова — нет
    assert picks(p, 0) == [False, False]


def test_account_publish_on_the_country_page_sends_only_its_ticked(ui):
    p, state = ui
    add(p, 0, count=3)
    add(p, 1, count=2)
    ready(p)
    names = [camp_name(p, 0, i) for i in range(3)]
    box(p, 0, 0).click()
    box(p, 0, 2).click()
    p.evaluate('cbTree.close()')
    p.evaluate('cbPublish((' + FORM_JS + ')("Demo"))')
    modal = p.locator('#safe-confirm-modal')
    expect(modal).to_contain_text('only the selected ones, 2 of 3')
    expect(modal.locator('.cb-selected-list')).to_contain_text(names[0] + ', ' + names[2])
    p.locator('#sfc-ok').click()
    wait_posts(state, 1, p)
    assert state['posts'][0]['account_label'] == 'Demo'
    assert sent_names(state['posts'][0]) == [names[0], names[2]]

    # Без отметок — вся форма, как раньше.
    p.evaluate('cbPublish((' + FORM_JS + ')("Other"))')
    expect(modal).not_to_contain_text('only the selected ones')
    p.locator('#sfc-ok').click()
    wait_posts(state, 2, p)
    assert state['posts'][1]['camp_count'] == '2'


def test_deleting_a_neighbour_keeps_the_tick_on_its_card(ui):
    p, state = ui
    add(p, 0, count=3)
    ready(p)
    want = camp_name(p, 0, 2)
    box(p, 0, 2).click()
    p.evaluate("""() => { const f=(""" + FORM_JS + """)('Demo');
      cbRemoveSlot(f.querySelectorAll('.cb-slot')[0]); cbTree.paint(); }""")
    expect(p.locator('.cbd-camp')).to_have_count(2)
    ready(p)
    assert camp_name(p, 0, 1) == want
    expect(box(p, 0, 1)).to_be_checked()
    expect(box(p, 0, 0)).not_to_be_checked()
    expect(publish_button(p)).to_have_text('Publish selected → drafts (1)')
    publish_button(p).click()
    p.locator('#sfc-ok').click()
    wait_posts(state, 1, p)
    assert sent_names(state['posts'][0]) == [want]


def test_duplicates_are_unticked_and_the_tick_survives_reload(ui):
    p, state = ui
    add(p, 0, count=2)
    ready(p)
    want = camp_name(p, 0, 1)
    box(p, 0, 1).click()
    p.evaluate("""() => { const f=(""" + FORM_JS + """)('Demo');
      cbDuplicateSlot(f.querySelectorAll('.cb-slot')[1].querySelector('.cb-dup-slot-btn'));
      cbDuplicateSlot(f.querySelectorAll('.cb-slot')[0].querySelector('.cb-dup-slot-btn'));
      cbTree.paint(); }""")
    p.wait_for_function("document.querySelectorAll('.cbd-camp').length === 4")
    assert picks(p, 0) == [False, False, True, False]
    p.evaluate('cbKeep.save()')
    p.reload()
    p.evaluate('cbDrawer.open()')
    p.wait_for_function("document.querySelectorAll('.cbd-camp').length === 4")
    assert picks(p, 0) == [False, False, True, False]
    ready(p)
    expect(publish_button(p)).to_have_text('Publish selected → drafts (1)')
    publish_button(p).click()
    p.locator('#sfc-ok').click()
    wait_posts(state, 1, p)
    post = state['posts'][0]
    assert post['camp_count'] == '1'
    assert sent_names(post) == [want]
    assert post['camp_0_ad_0_creative_file'] == 'offline-1.png', post


def test_of_two_cards_with_one_name_only_the_ticked_goes(ui):
    p, state = ui
    add(p, 0, count=2)
    p.evaluate("""() => { const f=(""" + FORM_JS + """)('Demo'); const s=f.querySelectorAll('.cb-slot');
      s[1].querySelector('[name$="_asset"]').value = s[0].querySelector('[name$="_asset"]').value;
      f.dispatchEvent(new Event('input',{bubbles:true})); }""")
    p.wait_for_function("""() => { const n=(""" + FORM_JS + """)('Demo').querySelectorAll('.cb-name-hidden');
      return n[0].value === n[1].value; }""")
    p.wait_for_function("Array.from(document.querySelectorAll('.cb-slot')).every(s => s._drawerResult && s._drawerResult.state !== 'wait')")
    p.locator('.cbd-camp input[type=checkbox]').nth(1).click()
    expect(publish_button(p)).to_have_text('Publish selected → drafts (1)')
    publish_button(p).click()
    p.locator('#sfc-ok').click()
    wait_posts(state, 1, p)
    post = state['posts'][0]
    assert post['camp_count'] == '1', post
    assert post['camp_0_ad_0_creative_file'] == 'offline-1.png', post
