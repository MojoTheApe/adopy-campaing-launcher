"""Country reads update summaries without discarding in-progress account setup.

ACL-1296: фикстура резала скрипт по `amapMoney`, и `amapSplitNote` с
`amapApplyReading` в неё не попадали. `amapRefreshSplitNote` падал внутри
теста с `ReferenceError`, свой же `.catch` это проглатывал, тест оставался
зелёным — а путь, ради которого он написан, не выполнялся ни разу. Срез
расширен до `amapPaintTree`, и подпись про пострановую разбивку проверяется
исполнением, а не поиском слов в исходнике.
"""
import json
import os
from pathlib import Path
import pytest

SOURCE = Path('src/web/templates/report.html').read_text(encoding='utf-8')

#: Заглушки того, что лежит за срезом. Их две, и обе ничего не делают: в этом
#: файле проверяют клетку, а не дерево и не разметку отказа.
STUBS = """
var amapReadAllBusy = false;
var period = {from: '2026-09-01', to: '2026-09-15', span: ''};
function amapPeriod() { return period; }
function amapPaintTree() {}
// Заглушка на будущее: ни один тест здесь не доводит чтение до отказа. Если
// такой появится, настоящая функция ещё и перевешивает пометку об упавших
// странах — заглушка этого не делает, и проверять придётся по-настоящему.
function amapSpendFailed(btn, cell, why) { window._failed = [btn, cell, why]; }
"""


def _script():
    start = SOURCE.index('function amapSplitMark(')
    end = SOURCE.index('function amapPaintTree(')
    return SOURCE[start:end]


ROW = ('<table><tr id="row">'
       '<td class="amap-live" id="cell" data-account="Demo">'
       '<span class="amap-live-ok" title="2026-09-01…2026-09-15 · read 2026-09-22 18:00:00 · rpa">$41.50</span>'
       '</td>'
       '<td><button id="read" class="amap-btn amap-read">Read spend</button></td>'
       '</tr></table>')

SLOT = ('<div id="amap-slot"><div class="amap"><div class="amap-head">'
        '<span class="amap-note">1 country</span>'
        '<input id="amap-new-country" value="unsaved setup"></div>'
        '<details class="amap-country" data-country="Peru" open><summary>'
        '<span class="amap-live-summary"><b class="amap-live-summary-values"></b>'
        '<span class="amap-live-summary-count"></span>'
        '<i class="amap-country-read">old reading</i></span>'
        '</summary><input id="account-setup" value="pending profile"></details>'
        '<details class="amap-available"></details></div></div>')


@pytest.fixture
def page():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get('ACL_BROWSER_EXECUTABLE') or None)
        context = browser.new_context()
        context.route('**/*', lambda route: route.fulfill(content_type='text/html', body=''))
        pg = context.new_page()
        pg.goto('http://acl.test/')
        pg.set_content(SLOT + ROW)
        pg.add_script_tag(content=STUBS + "\n" + _script())
        pg.evaluate('window.savedInput=document.querySelector("#account-setup")')
        yield pg
        browser.close()


def _status(page, reading, split):
    """Ответ опроса состояния — тот же, что отдаёт сервер."""
    page.route('**/account-spend/status?*', lambda route: route.fulfill(
        content_type='application/json',
        body=json.dumps({'ok': True, 'reading': reading, 'split': split})))


OK_READING = {'account': 'Demo', 'date_from': '2026-09-01', 'date_to': '2026-09-15',
              'state': 'ok', 'spend': 41.5, 'currency': '$', 'how': 'rpa',
              'finished_at': '2026-09-22 18:00:00'}


def _title(page):
    return page.locator('#cell .amap-live-ok').get_attribute('title')


def test_new_country_keeps_existing_setup_nodes_and_text(page):
    html = '''<div class="amap"><div class="amap-head"><span class="amap-note">2 countries</span><input id="amap-new-country"></div><details class="amap-country" data-country="Peru"><summary><span class="amap-live-summary"><i class="amap-country-read">fresh reading</i></span></summary><input id="account-setup"></details><details class="amap-country" data-country="Chile"><summary><span class="amap-live-summary"><i class="amap-country-read">Chile $44.17</i></span></summary></details><details class="amap-available"></details></div>'''
    page.route('**/account-map?*', lambda route: route.fulfill(content_type='text/html', body=html))
    page.evaluate('amapRefreshCountryLines(period)')
    assert page.locator('#account-setup').input_value() == 'pending profile'
    assert page.locator('#amap-new-country').input_value() == 'unsaved setup'
    assert page.evaluate('savedInput === document.querySelector("#account-setup")')
    assert 'fresh reading' in page.locator('[data-country="Peru"]').inner_text()
    assert page.locator('[data-country="Chile"]').count() == 1


def test_failed_retry_removes_the_previous_country_total(page):
    """Неудача разбивки: страновой итог снимается, спенд и кнопка остаются.

    ACL-1282: кнопка одна на обе половины чтения, поэтому «failed · retry» на
    ней означало бы «спенд не прочитан». Отказ разбивки живёт в клетке — там
    же, где прочитанное число, которое он не отменяет.
    """
    from playwright.sync_api import expect
    page.route('**/account-spend/read-countries', lambda route: route.fulfill(
        content_type='application/json', body=json.dumps({'ok': True})))
    _status(page, OK_READING, {'state': 'failed', 'error': 'synthetic read failure'})
    page.route('**/account-map?*', lambda route: route.fulfill(content_type='text/html', body='<div class="amap"><details class="amap-country" data-country="Peru"><summary><span class="amap-live-summary"></span></summary></details></div>'))
    page.evaluate('amapRunSplit("Demo", document.querySelector("#read"), period)')
    expect(page.locator('#cell .amap-split-bad')).to_have_text('countries failed', timeout=10000)
    assert 'synthetic read failure' in page.locator('#cell .amap-split-bad').get_attribute('title')
    expect(page.locator('#read')).to_have_text('Read spend')
    assert page.locator('#cell .amap-live-ok').inner_text() == '$41.50'
    expect(page.locator('.amap-country-read')).to_have_count(0)


def test_a_second_attempt_clears_the_previous_country_failure(page):
    """Пометка об отказе — про текущее чтение, а не вечная надпись в клетке."""
    from playwright.sync_api import expect
    page.evaluate('amapSplitMark("Demo", "first failure")')
    assert page.locator('#cell .amap-split-bad').count() == 1
    page.route('**/account-spend/read-countries', lambda route: route.fulfill(
        content_type='application/json', body=json.dumps({'ok': True})))
    _status(page, OK_READING, {'state': 'ok', 'rows': [], 'spend': 41.5, 'spend_currency': '$',
                               'spend_at': '2026-09-22 18:00:00'})
    page.route('**/account-map?*', lambda route: route.fulfill(content_type='text/html', body='<div class="amap"><details class="amap-country" data-country="Peru"><summary><span class="amap-live-summary"><i class="amap-country-read">Peru $12.00</i></span></summary></details></div>'))
    page.evaluate('amapRunSplit("Demo", document.querySelector("#read"), period)')
    expect(page.locator('#cell .amap-split-bad')).to_have_count(0, timeout=10000)
    expect(page.locator('#read')).to_have_text('Read spend')
    # И подпись про объяснённую сумму — весь путь целиком, от чтения до
    # клетки. Ревью 23.09: без этого можно выкинуть вызов обновления, и
    # подсказка объясняла бы прежние деньги вечно, ничем себя не выдав.
    expect(page.locator('#cell .amap-live-ok')).to_have_attribute(
        'title', '2026-09-01…2026-09-15 · read 2026-09-22 18:00:00 · rpa'
                 ' · countries read for $41.50', timeout=10000)


# ── Какую сумму объясняет разбивка (ACL-1288, проверено исполнением) ──────
#
# Раньше эти же свойства проверялись поиском слов в исходнике: «есть ли в
# тексте функции `theirs === ours`». Такая проверка зеленеет от правильно
# написанного кода и ничего не говорит о том, что он делает; в этом PR она
# трижды краснела на здоровых правках. Здесь тот же код выполняется.


def test_the_note_survives_the_repaint_after_a_resumed_read(page):
    """Ровно та последовательность, ради которой подпись писалась: нажал,
    перезагрузил страницу посреди чтения, восстановленный опрос дочитал спенд
    и — намеренно — не тронул страны. Новая сумма на экране, а разбивка
    объясняет прежнюю, и сказать об этом больше нечему."""
    page.evaluate("""(s) => amapApplyReading('Demo', s.reading, s.split)""", {
        'reading': OK_READING,
        'split': {'state': 'ok', 'spend': 10.0, 'spend_currency': '$',
                  'spend_at': '2026-09-22 12:00:00'}})
    title = _title(page)
    assert 'countries read for $10.00, not the $41.50 above' in title, title
    assert 'read 2026-09-22 18:00:00 · rpa' in title, "подсказка потеряла прежнюю половину"


def test_the_same_money_is_not_called_a_difference(page):
    page.evaluate("""(s) => amapApplyReading('Demo', s.reading, s.split)""", {
        'reading': OK_READING,
        'split': {'state': 'ok', 'spend': 41.5, 'spend_currency': '$',
                  'spend_at': '2026-09-22 18:00:00'}})
    title = _title(page)
    assert 'countries read for $41.50' in title and 'not the' not in title, title


def test_half_a_cent_is_not_a_difference_either(page):
    """`_same_money` считает 41.502 и 41.50 одними деньгами и страны не
    перечитывает. Подсказка обязана молчать о том же."""
    page.evaluate("""(s) => amapApplyReading('Demo', s.reading, s.split)""", {
        'reading': OK_READING,
        'split': {'state': 'ok', 'spend': 41.502, 'spend_currency': '$',
                  'spend_at': '2026-09-22 18:00:00'}})
    assert 'not the' not in _title(page), _title(page)


@pytest.mark.parametrize('split', [
    None,
    {'state': 'running'},
    {'state': 'failed', 'error': 'profile is closed'},
])
def test_a_split_that_is_not_an_answer_says_nothing(page, split):
    """Нечитанная, идущая и упавшая разбивка ничего не объясняют."""
    page.evaluate("""(s) => amapApplyReading('Demo', s.reading, s.split)""",
                  {'reading': OK_READING, 'split': split})
    assert 'countries' not in _title(page), _title(page)


def test_a_split_that_does_not_remember_its_spend_says_so(page):
    """Записи до ACL-1282 суммы не помнят. Молчать о них — значит выдать их
    за свежие."""
    page.evaluate("""(s) => amapApplyReading('Demo', s.reading, s.split)""", {
        'reading': OK_READING, 'split': {'state': 'ok', 'spend': None}})
    assert 'countries read before this spend was known' in _title(page)


def test_a_fresh_split_rewrites_the_note_once(page):
    """После своего чтения разбивка объясняет уже новую сумму."""
    from playwright.sync_api import expect
    page.evaluate("""(s) => amapApplyReading('Demo', s.reading, s.split)""", {
        'reading': OK_READING,
        'split': {'state': 'ok', 'spend': 10.0, 'spend_currency': '$',
                  'spend_at': '2026-09-22 12:00:00'}})
    assert 'not the $41.50 above' in _title(page)
    _status(page, OK_READING, {'state': 'ok', 'spend': 41.5, 'spend_currency': '$',
                               'spend_at': '2026-09-22 18:00:00'})
    page.evaluate('amapRefreshSplitNote("Demo", period)')
    expect(page.locator('#cell .amap-live-ok')).to_have_attribute(
        'title', '2026-09-01…2026-09-15 · read 2026-09-22 18:00:00 · rpa'
                 ' · countries read for $41.50', timeout=10000)
    assert _title(page).count(' · countries ') == 1


def test_a_reading_that_is_running_leaves_the_note_alone(page):
    """Обновление подписи не должно писать поверх чтения, которое идёт."""
    page.evaluate("""(s) => amapApplyReading('Demo', s.reading, s.split)""", {
        'reading': OK_READING,
        'split': {'state': 'ok', 'spend': 10.0, 'spend_currency': '$',
                  'spend_at': '2026-09-22 12:00:00'}})
    before = _title(page)
    _status(page, dict(OK_READING, state='running', spend=None), None)
    page.evaluate('amapRefreshSplitNote("Demo", period)')
    assert _title(page) == before
    # Молчание обязано быть решением, а не падением: `amapRefreshSplitNote`
    # глотает свои ошибки, и «ничего не изменилось» выглядит одинаково у
    # правильного пропуска и у сломанного кода (ревью 23.09). Тот же вызов
    # на дочитанном показании обязан подпись переписать.
    _status(page, OK_READING, {'state': 'ok', 'spend': 41.5, 'spend_currency': '$',
                               'spend_at': '2026-09-22 18:00:00'})
    page.evaluate('amapRefreshSplitNote("Demo", period)')
    assert _title(page) != before and 'countries read for $41.50' in _title(page)


def test_the_refresh_writes_every_row_of_this_account_and_no_other(page):
    """Аккаунт в двух странах стоит в двух блоках, а соседний — чужой.

    Перебор строк в `amapRefreshSplitNote` проверяется отдельно от
    `amapApplyReading`: это разные переборы, и сломать их можно порознь.
    """
    page.evaluate("""() => {
      const row = document.querySelector('#row');
      const mine = document.querySelector('#cell').cloneNode(true);
      mine.id = 'mine2';
      const other = document.querySelector('#cell').cloneNode(true);
      other.id = 'other';
      other.dataset.account = 'Someone else';
      other.querySelector('.amap-live-ok').title = 'do not touch';
      row.appendChild(mine); row.appendChild(other);
    }""")
    _status(page, OK_READING, {'state': 'ok', 'spend': 41.5, 'spend_currency': '$',
                               'spend_at': '2026-09-22 18:00:00'})
    page.evaluate('amapRefreshSplitNote("Demo", period)')
    got = page.evaluate("""() => ({
      mine: document.querySelector('#cell .amap-live-ok').title,
      mine2: document.querySelector('#mine2 .amap-live-ok').title,
      other: document.querySelector('#other .amap-live-ok').title})""")
    assert 'countries read for $41.50' in got['mine']
    assert got['mine2'] == got['mine'], "вторая строка того же аккаунта осталась прежней"
    assert got['other'] == 'do not touch', "переписали чужую строку"


def test_a_one_day_reading_keeps_the_way_the_server_writes_it(page):
    """Сервер пишет один день одним днём. Клиент собирает подсказку заново и
    не имеет права превращать её в «2026-09-01…2026-09-01»: человек видит,
    как подсказка меняется на чтении тех же дат."""
    one_day = dict(OK_READING, date_from='2026-09-01', date_to='2026-09-01')
    page.evaluate("""(s) => amapApplyReading('Demo', s.reading, s.split)""",
                  {'reading': one_day, 'split': None})
    assert _title(page).startswith('2026-09-01 · read '), _title(page)


def test_the_note_is_written_on_every_row_of_the_account(page):
    """Аккаунт в двух странах стоит в двух блоках: сервер при перезагрузке
    подпишет обе, и расходиться они не должны."""
    page.evaluate("""() => {
      const copy = document.querySelector('#cell').cloneNode(true);
      copy.id = 'cell2';
      document.querySelector('#row').appendChild(copy);
    }""")
    page.evaluate("""(s) => amapApplyReading('Demo', s.reading, s.split)""", {
        'reading': OK_READING,
        'split': {'state': 'ok', 'spend': 10.0, 'spend_currency': '$',
                  'spend_at': '2026-09-22 12:00:00'}})
    titles = page.evaluate("""() => Array.from(
      document.querySelectorAll('.amap-live .amap-live-ok')).map(n => n.title)""")
    assert len(titles) == 2 and titles[0] == titles[1], titles
    assert 'not the $41.50 above' in titles[1]


def test_an_empty_report_is_an_answer_and_explains_its_spend(page):
    """«За период ничего не крутилось» — прочитано, а не пусто: `amapRunSplit`
    считает `empty` успехом, и подпись обязана говорить про ту же сумму."""
    page.evaluate("""(s) => amapApplyReading('Demo', s.reading, s.split)""", {
        'reading': OK_READING,
        'split': {'state': 'empty', 'rows': [], 'spend': 41.5, 'spend_currency': '$',
                  'spend_at': '2026-09-22 18:00:00'}})
    assert 'countries read for $41.50' in _title(page)
