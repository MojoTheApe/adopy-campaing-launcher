"""Процент сборки у дышащей точки кампании (ACL-1347) — на настоящем шаблоне.

Страница рендерится целиком, сеть подменена (как в
test_campaign_build_dot_browser): `/publish-jobs/{id}/progress` отдаёт
записанные последовательности шагов робота — классический редактор Meta и
редактор «в один экран» (`simple_*`), одно и три объявления, зависание,
повтор кампании, F5. Проверяется: процент растёт и не идёт назад, стоит в
границах этапа (зависший шаг не доползает до следующего), ждущие, готовые,
упавшие и потерянные кампании процента не показывают, лишних запросов нет.
Границы этапов берутся из той же таблицы шаблона, что и у страницы.
"""
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from playwright.sync_api import expect, sync_playwright
from test_builder_per_account import _render
from test_campaign_build_dot_browser import (
    BLOCKED, JOB, OK, _route_for, add, camp, dot, names, progress, queued, show,
)

from scripts import acl_build_progress_model as model_script

MODEL = model_script.read_model()
STAGES = [s for s, _ in MODEL['stages']]


def bounds(ads=1):
    """Границы этапов в процентах — как считает страница (доли времени)."""
    secs = {s: v * (ads if s == 'ad' else 1) for s, v in MODEL['stages']}
    total = sum(secs.values())
    out, lo = {}, 0.0
    for s in STAGES:
        out[s] = (100 * lo / total, 100 * (lo + secs[s]) / total)
        lo += secs[s]
    return out


def stage(step):
    return model_script.stage_of(step, MODEL['rules'])


@pytest.fixture(scope='module')
def browser():
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get('ACL_BROWSER_EXECUTABLE') or None)
        yield b
        b.close()


@pytest.fixture(scope='module')
def html():
    return _render({'Demo': [], 'Other': []})


def _page(browser, html, **ctx):
    context = browser.new_context(**ctx)
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    state = {'posts': 0, 'progress_calls': 0, 'progress': {'status': 'queued', 'campaigns': []}, 'warnings': []}
    page.on('console', lambda m: m.type == 'warning' and state['warnings'].append(m.text))
    page.route('**/*', _route_for(html, state))
    page.goto('http://acl.test/')
    page.evaluate('cbDrawer.open()')
    return context, page, state, errors


@pytest.fixture
def ui(browser, html):
    context, page, state, errors = _page(browser, html)
    yield page, state
    assert not errors, errors
    context.close()


def publish(page, *, ads=1, count=1):
    """Кампании аккаунта Demo (у первой — `ads` объявлений) уходят в задание."""
    add(page, count=count)
    if ads > 1:
        page.evaluate('''ads => {
          const slot = document.querySelector('form.open-account-form .cb-slot');
          for (let i = 1; i < ads; i++) {
            cbAddAd(slot.querySelector('.cb-ad-rows'));
            slot.querySelector('[name$="_ad_' + i + '_creative_file"]').value = 'offline-ad-' + i + '.png';
          }
          slot.closest('form').dispatchEvent(new Event('input', {bubbles: true}));
        }''', ads)
        page.wait_for_function(f"document.querySelector('.cb-slot').querySelectorAll('.cb-ad-row').length === {ads}")
    page.wait_for_function("Array.from(document.querySelectorAll('.cb-slot')).every(s => s._drawerResult && s._drawerResult.state === 'ok')")
    page.locator('.cbd-publish-all').click()
    page.locator('#sfc-ok').click()
    page.wait_for_function('Object.keys(cbBuild.jobs()).length === 1')
    got = names(page)
    assert len(got) == count and all(got)
    return got


def title_of(step, ok=True):
    if not step:
        return 'Building: starting'
    return 'Building: ' + step.replace('_', ' ') + (' — failed, retrying' if ok is False else '')


def after(page, name, prop='content'):
    """Стиль ::after индикатора. Ящик перерисовывается сам (repaintSoon):
    узел, найденный за миг до перерисовки, уже вне документа и отдаёт
    пустой стиль — спросить снова (как animation() в тесте точки)."""
    got = ''
    for _ in range(20):
        got = dot(page, name).evaluate(
            f'e => e.isConnected ? getComputedStyle(e, "::after").{prop} : ""')
        if got:
            return got
        page.wait_for_timeout(100)
    return got


def _settled_boxes(page, name):
    """Прямоугольники точки и имени кампании — из одного кадра, после того
    как раскладка устоялась (ACL-1397).

    Раньше два `bounding_box()` читались по очереди, сразу после смены
    процента: ящик ещё перерисовывался (repaintSoon) и доезжал, и точка
    с именем мерились в разных кадрах — тест изредка падал под нагрузкой
    (параллельный прогон, занятая машина). Здесь оба прямоугольника берутся
    одним вызовом в одном кадре, и только когда два кадра подряд дали
    одинаковую картину; проверка расположения остаётся прежней."""
    expect(dot(page, name)).to_be_visible()
    got = page.evaluate('''async name => {
      const frame = () => new Promise(r => requestAnimationFrame(() => r()));
      const read = () => {
        const row = Array.from(document.querySelectorAll('.cbd-camp')).find(
          r => (r.querySelector('.cbd-camp-name') || {}).textContent?.includes(name));
        if (!row) return null;
        const ind = row.querySelector('.cbd-ind'), label = row.querySelector('.cbd-camp-name');
        if (!ind || !label) return null;
        const a = ind.getBoundingClientRect(), b = label.getBoundingClientRect();
        if (!a.width || !b.width) return null;
        return [{x: a.x, width: a.width}, {x: b.x, width: b.width}];
      };
      let last = null;
      for (let i = 0; i < 120; i++) {
        await frame();
        const now = read();
        if (now && last && JSON.stringify(now) === JSON.stringify(last)) return now;
        last = now;
      }
      return last;
    }''', name)
    assert got, f'no laid-out row for {name}'
    return got


def pct(page, name):
    value = dot(page, name).get_attribute('data-pct')
    assert value and value.endswith('%'), value
    return int(value[:-1])


def step(page, state, name, step_name, *, idle=1.0, note='', ok=True, others=(), stall_after=180):
    """Показать шаг робота и вернуть процент у точки."""
    payload = progress('running', [camp(name, step=step_name, note=note, ok=ok, idle_s=idle), *others],
                       current=name, idle_s=idle)
    payload['stall_after_s'] = stall_after
    show(page, state, payload)
    if idle < stall_after:
        expect(dot(page, name)).to_have_attribute('title', title_of(step_name, ok))
    else:
        expect(dot(page, name)).to_have_class('cbd-ind cbd-ind-build-attn')
        expect(dot(page, name)).to_have_attribute('title', f'No new step for {max(1, round(idle / 60))} min'
                                                  f' (last: {step_name.replace("_", " ")}) — check the Multilogin window')
    return pct(page, name)


# Записанные последовательности: шаг, заметка, секунды тишины к опросу, ok.
# Взяты из телеметрии 28.09 (прогоны 8058b54f78d0 и 961252633112).
CLASSIC = [
    ('', '', 4, True), ('job_phase', 'browser_attach', 3, True), ('campaigns_list_ready', 'ready', 1, True),
    ('window_bounds', '', 0.5, True), ('create', '', 5, True), ('create_took', '', 20, True),
    ('campaign_open_budget', 'opened', 2, True), ('leads', '', 1, True), ('onboarding_modal', '', 4, True),
    ('campaign_name', '', 2, True), ('campaign_fields', '', 1, True), ('next_to_adset', '', 0.1, True),
    ('window_bounds', '', 6, True), ('adset_name', '', 3, True), ('conversion_menu_open', '', 15, True),
    ('conversion_website_pick', 'cdp', 1, True), ('conversion_event', '', 2, True),
    ('further_limit_continue', '', 4, True), ('advantage_plus_off', 'still_on', 8, False),
    ('audience_age', '', 0.1, True), ('audience_location', '', 12, True), ('languages', '', 10, True),
    ('targeting', '', 0.2, True), ('advantage_plus_audience', 'off', 2, True), ('placements', '', 2, True),
    ('next_to_ad', '', 0.1, True), ('window_bounds', '', 1.5, True), ('ad_name', '', 20, True),
    ('ad_creative_reuse', 'not_in_library', 30, True), ('ad_creative_upload', '', 25, True),
    ('ad_destination', '', 0.1, True), ('window_bounds', '', 14, True), ('text_variations_off', 'label_not_found', 9, True),
    ('ad_copy', '', 5, True), ('destination_sweep_skipped', '', 0.1, True), ('ad_level_summary', '', 2, True),
]
SIMPLE = [
    ('job_phase', 'open_campaigns', 2, True), ('campaigns_list_ready', 'ready', 1, True), ('create', '', 10, True),
    ('campaign_open_budget', 'opened', 2, True), ('leads', '', 1, True), ('continue', '', 1, True),
    ('simple_create', 'after_continue', 20, True), ('campaign_name', '', 0.2, True),
    ('simple_objective', 'leads', 3, True), ('campaign_fields', '', 1, True), ('simple_adset_section', '', 0.1, True),
    ('window_bounds', '', 5, True), ('adset_name', '', 7, True), ('conversion_menu_open', '', 19, True),
    ('click_target_covered', '', 1, False), ('conversion_website_pick', 'cdp', 1, True),
    ('conversion_event', '', 4, True), ('simple_card', 'Audience:opened', 2, True),
    ('further_limit_continue', '', 4, True), ('audience_age', '', 0.1, True), ('audience_location', '', 10, True),
    ('languages', '', 14, True), ('targeting', '', 0.2, True), ('simple_card', 'Placements:opened', 40, True),
    ('placements', '', 1, True), ('simple_ad_section', '', 0.1, True), ('window_bounds', '', 1.4, True),
    ('ad_name', '', 17, True), ('ad_creative_reuse', 'not_in_library', 60, True),
    ('ad_creative_upload', '', 30, True), ('ad_destination', '', 1, True), ('ad_copy', '', 0.1, True),
    ('simple_create_more_ads', 'not_created:2', 2, False), ('destination_sweep_skipped', '', 5, True),
    ('simple_create_draft', 'all_edits_saved', 0.1, True), ('ad_level_summary', '', 1, True),
]


def _play(page, state, name, sequence, ads=1):
    b = bounds(ads)
    seen, known = [], 'profile'
    for step_name, note, idle, ok in sequence:
        got = step(page, state, name, step_name, idle=idle, note=note, ok=ok)
        st = stage(step_name) if step_name else 'profile'
        assert st is not None, step_name
        if st != 'hold':
            known = st
        lo, hi = b[known]
        assert lo - 1 <= got < hi, (step_name, got, known, b[known])
        assert not seen or got >= seen[-1], f'{step_name}: {got} after {seen[-1]} — went backwards'
        seen.append(got)
    return seen


@pytest.mark.parametrize('sequence', [CLASSIC, SIMPLE], ids=['classic', 'single_screen'])
def test_percent_grows_through_a_recorded_build_and_clears_when_done(ui, sequence):
    p, state = ui
    (a,) = publish(p)
    seen = _play(p, state, a, sequence)
    assert len(set(seen)) >= 12, f'percent barely moves: {seen}'
    assert seen[-1] >= bounds()['final'][0] - 1 and seen[-1] <= 99
    expect(dot(p, a)).to_have_class('cbd-ind cbd-ind-build-run')

    show(p, state, progress('done', [camp(a, result=OK)]))
    expect(dot(p, a)).to_have_class('cbd-ind cbd-ind-build-ok')
    assert dot(p, a).get_attribute('data-pct') is None


def test_the_percent_sits_next_to_the_dot_and_keeps_the_tooltip(ui):
    p, state = ui
    (a,) = publish(p)
    got = step(p, state, a, 'ad_copy', idle=12)
    ind = dot(p, a)
    expect(ind).to_have_attribute('title', 'Building: ad copy')
    assert after(p, a) == f'"{got}%"'
    # Процент — справа от точки, в той же строке, не поверх имени.
    box, name_box = _settled_boxes(p, a)
    assert box['x'] + box['width'] <= name_box['x'] + 1
    expect(ind.locator('span')).to_have_css('animation-name', 'cbd-build-pulse')


def test_ads_split_the_ad_stage_and_ad2_ad3_steps_advance_it(ui):
    p, state = ui
    (a,) = publish(p, ads=3)
    b3, b1 = bounds(3), bounds(1)
    lo, hi = b3['ad']
    width = (hi - lo) / 3
    # Три объявления: этап объявлений — большая часть полосы.
    assert hi - lo > (b1['ad'][1] - b1['ad'][0]) + 15

    at_ad_name = step(p, state, a, 'ad_name', idle=1)
    assert lo - 1 <= at_ad_name < lo + width
    first_done = step(p, state, a, 'ad_copy', idle=1)
    assert lo + width - 2 <= first_done < lo + width, 'first of three ads ends at a third of the stage'
    ad2 = step(p, state, a, 'ad2_name', idle=1)
    assert lo + width <= ad2 + 1 < lo + 2 * width
    # ad_creative_reuse второго объявления пишется без номера — наследует 2.
    reuse = step(p, state, a, 'ad_creative_reuse', note='not_in_library', idle=20)
    assert ad2 < reuse < lo + 2 * width
    ad2_done = step(p, state, a, 'ad2_copy', idle=1)
    assert lo + 2 * width - 2 <= ad2_done < lo + 2 * width
    ad3 = step(p, state, a, 'ad3_name', idle=1)
    ad3_done = step(p, state, a, 'ad3_copy', idle=1)
    assert ad2_done <= ad3 < ad3_done < hi
    assert step(p, state, a, 'ad_level_summary', idle=1) <= 99


def test_one_ad_card_finishes_the_ad_stage_with_its_first_ad(ui):
    p, state = ui
    (a,) = publish(p)
    lo, hi = bounds(1)['ad']
    assert hi - 2 <= step(p, state, a, 'ad_copy', idle=1) < hi


def test_a_slow_step_never_reaches_the_next_stage(ui):
    p, state = ui
    (a,) = publish(p)
    # Последний шаг этапа кампании стоит в его конце: дальше — только
    # следующий этап, сколько бы робот ни молчал.
    campaign_hi = bounds()['campaign'][1]
    assert step(p, state, a, 'next_to_adset', idle=5000, stall_after=420) < campaign_hi
    adset_hi = bounds()['adset'][1]
    first = step(p, state, a, 'placements', idle=5)
    stuck = step(p, state, a, 'placements', idle=5000, stall_after=420)
    assert first <= stuck < adset_hi, 'stalled ad set crept into the ad stage'

    lo, hi = bounds()['ad']
    before = step(p, state, a, 'ad_creative_reuse', note='not_in_library', idle=20)
    slow = step(p, state, a, 'ad_creative_reuse', note='not_in_library', idle=200, stall_after=420)
    stalled = step(p, state, a, 'ad_creative_reuse', note='not_in_library', idle=500, stall_after=420)
    later = step(p, state, a, 'ad_creative_reuse', note='not_in_library', idle=5000, stall_after=420)
    assert before <= slow <= stalled == later < hi - 5, 'a stuck creative step creeps toward 99%'
    # Встала — точка оранжевая, причина та же, процент остался.
    expect(dot(p, a)).to_have_class('cbd-ind cbd-ind-build-attn')


def test_a_retry_and_an_unknown_step_keep_the_reached_value(ui):
    p, state = ui
    (a,) = publish(p)
    reached = step(p, state, a, 'languages', idle=5)
    assert reached > bounds()['adset'][0]
    assert step(p, state, a, 'create', ok=False, idle=2) == reached
    assert step(p, state, a, 'campaign_name', idle=2) == reached
    # Незнакомый шаг — этап последнего знакомого, не 0 и не назад.
    assert step(p, state, a, 'brand_new_robot_step', idle=2) >= reached
    assert step(p, state, a, 'brand_new_robot_step', idle=900, stall_after=420) < bounds()['adset'][1]


def test_a_service_step_keeps_growing_from_the_last_real_step(ui):
    """window_bounds сразу за переходом (и незнакомый шаг) — не повод
    замереть: рост идёт от последнего настоящего шага."""
    p, state = ui
    (a,) = publish(p)
    real = step(p, state, a, 'ad_creative_reuse', note='not_in_library', idle=0.1)
    assert step(p, state, a, 'brand_new_robot_step', idle=15) > real
    later = step(p, state, a, 'ad_destination', idle=0.1)
    assert step(p, state, a, 'window_bounds', idle=14) > later


def test_the_reached_percent_survives_a_reload(ui):
    p, state = ui
    (a,) = publish(p)
    reached = step(p, state, a, 'ad_creative_upload', idle=5)
    # Робот повторяет кампанию с начала — после F5 процент не падает.
    state['progress'] = progress('running', [camp(a, step='create', ok=False, idle_s=1)], current=a, idle_s=1)
    p.reload()
    p.evaluate('cbDrawer.open()')
    expect(dot(p, a)).to_have_attribute('title', title_of('create', False))
    assert pct(p, a) == reached
    stored = p.evaluate("JSON.parse(localStorage.getItem('acl.builder.jobs'))")
    assert int(stored[JOB]['track'][a]['pct']) == reached


def test_states_without_progress_show_no_percent(ui):
    p, state = ui
    a, b = publish(p, count=2)
    show(p, state, queued([camp(a), camp(b)], ahead=1))
    expect(dot(p, a)).to_have_class('cbd-ind cbd-ind-build-wait')
    assert dot(p, a).get_attribute('data-pct') is None

    step(p, state, a, 'adset_name', idle=3, others=[camp(b)])
    assert dot(p, a).get_attribute('data-pct') is not None
    expect(dot(p, b)).to_have_attribute('title', 'Waiting for its turn')
    assert dot(p, b).get_attribute('data-pct') is None, 'a campaign waiting its turn shows a percent'

    why = 'Account restricted by Meta — complete the verification in Ads Manager'
    failed = {'success': False, 'publishable': False, 'error': 'x', 'blocking_failures': ['account_blocked']}
    show(p, state, progress('running', [camp(a, result=failed, blocker=why), camp(b, step='create', idle_s=2)],
                            current=b, idle_s=2))
    expect(dot(p, a)).to_have_attribute('title', why)
    assert dot(p, a).get_attribute('data-pct') is None
    expect(dot(p, b)).to_have_attribute('title', 'Building: create')
    assert dot(p, b).get_attribute('data-pct') is not None

    show(p, state, progress('done', [camp(a, result=failed), camp(b, result=BLOCKED)]))
    expect(dot(p, b)).to_have_class('cbd-ind cbd-ind-build-bad')
    assert dot(p, a).get_attribute('data-pct') is None and dot(p, b).get_attribute('data-pct') is None


def test_a_lost_job_shows_no_percent(ui):
    p, state = ui
    (a,) = publish(p)
    step(p, state, a, 'ad_name', idle=3)
    show(p, state, None)
    expect(dot(p, a)).to_have_class('cbd-ind cbd-ind-build-lost')
    assert dot(p, a).get_attribute('data-pct') is None


def _requests_while_building(browser, html):
    context, p, state, errors = _page(browser, html)
    try:
        (a,) = publish(p)
        seen = []
        p.on('request', lambda r: seen.append(urlparse(r.url).path))
        show(p, state, progress('running', [camp(a, step='ad_creative_reuse', idle_s=10)], current=a, idle_s=10))
        expect(dot(p, a)).to_have_attribute('title', 'Building: ad creative reuse')
        p.wait_for_timeout(6500)   # два обычных опроса
        shown = dot(p, a).get_attribute('data-pct')
        assert not errors, errors
        return seen, shown
    finally:
        context.close()


def test_no_extra_requests_for_the_percent(browser, html):
    """Та же сборка на странице без таблицы процента (процента нет) и с ней:
    запросы те же — процент считается из ответа уже идущего опроса."""
    without = model_script._BLOCK.sub('', html)
    assert without != html
    base, base_shown = _requests_while_building(browser, without)
    got, shown = _requests_while_building(browser, html)
    assert base_shown is None and shown and shown.endswith('%')
    progress_path = f'/publish-jobs/{JOB}/progress'
    assert set(got) <= set(base), sorted(set(got) - set(base))
    assert 2 <= got.count(progress_path) <= base.count(progress_path) + 1, (got, base)


def test_reduced_motion_keeps_a_still_percent(browser, html):
    context, p, state, errors = _page(browser, html, reduced_motion='reduce')
    try:
        (a,) = publish(p)
        got = step(p, state, a, 'languages', idle=4)
        ind = dot(p, a)
        assert after(p, a) == f'"{got}%"'
        assert after(p, a, 'animationName') == 'none'
        expect(ind.locator('span')).to_have_css('animation-name', 'none')
        expect(ind.locator('span')).not_to_have_css('box-shadow', 'none')
        assert not errors, errors
    finally:
        context.close()


def test_the_page_maps_steps_exactly_like_the_script(ui):
    """Одна таблица — два исполнителя: JS страницы и скрипт пересчёта
    раскладывают шаги по этапам одинаково."""
    from test_build_progress_model import STEPS_SEEN_IN_TELEMETRY

    p, _state = ui
    probe = sorted(STEPS_SEEN_IN_TELEMETRY | {'ad3_copy', 'ad12_name', 'brand_new_robot_step', ''})
    got = p.evaluate('names => names.map(n => cbBuild.stageOf(n))', probe)
    assert dict(zip(probe, got)) == {n: stage(n) for n in probe}


# ── ревью PR #364: поломанная таблица отключает процент, а не точку ──────

def _broken(html, old, new):
    assert html.count(old) >= 1, old
    return html.replace(old, new, 1)


@pytest.mark.parametrize('breakage', ['bad_rule', 'bad_json'])
def test_a_broken_model_table_never_breaks_the_build_dot(browser, html, breakage):
    """Правила пишутся руками. Кривой регэксп пропускается (остальные
    правила работают, одна строка в консоли); битый JSON выключает только
    процент — точка сборки живёт как до ACL-1347."""
    if breakage == 'bad_rule':
        page_html = _broken(html, '["^(window_|', '["^((window_|')
    else:
        page_html = _broken(html, '"stages": [', '"stages": [[')
    context, p, state, errors = _page(browser, page_html)
    warnings = state['warnings']
    try:
        assert p.evaluate('typeof window.cbBuild') == 'object'
        (a,) = publish(p)
        show(p, state, progress('running', [camp(a, step='ad_copy', idle_s=3)], current=a, idle_s=3))
        expect(dot(p, a)).to_have_class('cbd-ind cbd-ind-build-run')
        expect(dot(p, a)).to_have_attribute('title', 'Building: ad copy')
        if breakage == 'bad_rule':
            # Остальные правила на месте — процент есть; кривое правило названо.
            assert pct(p, a) > bounds()['ad'][0] - 1
            assert p.evaluate('cbBuild.stageOf("window_bounds")') is None
            assert p.evaluate('cbBuild.stageOf("adset_name")') == 'adset'
        else:
            assert dot(p, a).get_attribute('data-pct') is None
        show(p, state, progress('done', [camp(a, result=OK)]))
        expect(dot(p, a)).to_have_class('cbd-ind cbd-ind-build-ok')
        assert not errors, errors
    finally:
        context.close()
    if breakage == 'bad_rule':
        assert len([w for w in warnings if 'cb-build-model' in w]) == 1, warnings
