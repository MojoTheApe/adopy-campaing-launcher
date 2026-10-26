"""ACL-2124: шапка и календарь доступны поверх открытого немодального Builder."""
import os
import sys
from pathlib import Path

from playwright.sync_api import expect

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_builder_duplicate_buttons import browser
from test_builder_per_account import _render


def _hit(locator):
    return locator.evaluate("""el => {
        const r = el.getBoundingClientRect();
        const x = r.left + r.width / 2, y = r.top + r.height / 2;
        const hit = document.elementFromPoint(x, y);
        return {ok: r.width > 0 && r.height > 0 && x >= 0 && x < innerWidth &&
                    y >= 0 && y < innerHeight && !!hit && (hit === el || el.contains(hit)),
                coveredBy: hit && (hit.id || hit.className), rect: [r.x, r.y, r.width, r.height]};
    }""")


def test_header_controls_and_calendar_remain_clickable_across_drawer_layouts(browser, tmp_path):
    ctx = browser.new_context(viewport={'width': 390, 'height': 900})
    p = ctx.new_page()
    errors = []
    p.on('pageerror', lambda error: errors.append(str(error)))
    html = _render({'Demo': []})
    shots = Path(os.environ.get('ACL_SHOTS_DIR') or tmp_path / 'header-shots')
    shots.mkdir(parents=True, exist_ok=True)

    def route(r):
        if r.request.resource_type == 'document':
            r.fulfill(content_type='text/html', body=html)
        elif r.request.url.endswith('/static/logo.svg'):
            logo = Path(__file__).resolve().parents[1] / 'src/web/static/logo.svg'
            r.fulfill(content_type='image/svg+xml', body=logo.read_text(encoding='utf-8'))
        else:
            r.fulfill(json={'files': [], 'texts': [], 'rows': [], 'profiles': [], 'waiting': False})

    p.route('**/*', route)
    try:
        p.goto('http://acl.test/')
        p.evaluate("""() => {
            document.getElementById('amap-dates').textContent = JSON.stringify(['2026-10-01', '2026-10-02']);
            document.querySelector('#buyer-select option').textContent = 'Demo Buyer';
        }""")
        for width in (390, 1024, 1440):
            p.set_viewport_size({'width': width, 'height': 900})
            for theme in ('light', 'dark'):
                p.evaluate('theme => document.documentElement.dataset.theme = theme', theme)
                for opened in (False, True):
                    p.evaluate('opened => opened ? cbDrawer.open() : cbDrawer.close()', opened)
                    p.wait_for_timeout(300)
                    label = f'{width}-{theme}-' + ('open' if opened else 'closed')
                    p.screenshot(path=str(shots / (label + '.png')))
                    if opened:
                        bounds = p.locator('.cbd-root').bounding_box()
                        bottom = p.locator('header.page-header').evaluate('el => el.getBoundingClientRect().bottom')
                        assert abs(bounds['y'] - bottom) <= 1, (label, bounds, bottom)
                        assert abs(bounds['y'] + bounds['height'] - 900) <= 1, (label, bounds)
                        assert _hit(p.locator('#cbd-close'))['ok'], (label, 'close button blocked')
                    for selector in ('#period-btn', '#buyer-select', '#theme-toggle', '#dash-refresh-btn'):
                        result = _hit(p.locator(selector))
                        assert result['ok'], (label, selector, result)
                    p.locator('#period-btn').click(timeout=2000)
                    expect(p.locator('.amap-cal')).to_be_visible()
                    # Проверяем не только кнопку шапки: видимая часть календаря также получает клики.
                    results = p.locator('.amap-cal button').evaluate_all("""buttons => buttons.map(el => {
                        const r = el.getBoundingClientRect(), x = r.left + r.width / 2, y = r.top + r.height / 2;
                        if (x < 0 || x >= innerWidth || y < 0 || y >= innerHeight || el.disabled) return true;
                        const hit = document.elementFromPoint(x, y);
                        return hit === el || el.contains(hit);
                    })""")
                    assert results and all(results), (label, 'calendar blocked')
                    p.keyboard.press('Escape')
                    expect(p.locator('.amap-cal')).to_have_count(0)
        # Длинный отчёт: уехавшая за экран шапка освобождает место ящику.
        p.evaluate("document.body.style.minHeight = '1800px'; scrollTo(0, 500)")
        p.wait_for_function("document.querySelector('.cbd-root').getBoundingClientRect().top === 0")
        p.evaluate('scrollTo(0, 0)')
        p.wait_for_function("Math.abs(document.querySelector('.cbd-root').getBoundingClientRect().top - document.querySelector('header.page-header').getBoundingClientRect().bottom) <= 1")
        p.locator('#cbd-close').click()
        expect(p.locator('.cbd-root')).to_have_attribute('data-open', '0')
        assert not errors, errors
    finally:
        ctx.close()
