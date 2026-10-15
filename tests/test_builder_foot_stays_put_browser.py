"""ACL-1728: нижние поля ящика билдера не пересоздаются, пока идут задания.

Владелец: «страна» внизу ящика закрывалась сама через пару секунд, пока
собирались черновики. Причина: опрос заданий перерисовывал нижнюю панель
(`innerHTML`), и открытый <select> заменялся новым узлом. Проверяется на
настоящем шаблоне: при идущем задании и нескольких перерисовках панели
узлы те же, фокус на месте, выбранное и набранное не потеряно.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from playwright.sync_api import expect
from test_builder_multi_publish_browser import (  # noqa: F401 — фикстуры
    browser, html, ui, add, ready, publish_one,
)



def mark_nodes(page):
    page.evaluate("""() => {
      window.__country = document.querySelector('#cbd-foot .cbd-pick-country');
      window.__account = document.querySelector('#cbd-foot .cbd-pick-account');
      window.__typed = document.querySelector('#cbd-foot .cbd-new-country');
    }""")


def same_nodes(page):
    return page.evaluate("""() => ({
      country: window.__country === document.querySelector('#cbd-foot .cbd-pick-country'),
      account: window.__account === document.querySelector('#cbd-foot .cbd-pick-account'),
      typed: window.__typed === document.querySelector('#cbd-foot .cbd-new-country'),
    })""")


def repaint_foot(page, times=5):
    """Опрос задания зовёт перерисовку ящика — повторяем её и даём пройти настоящим опросам."""
    for _ in range(times):
        page.evaluate('cbTree.paint()')
        page.wait_for_timeout(150)
    page.wait_for_timeout(2500)   # хотя бы один настоящий круг опроса


def test_country_select_survives_progress_updates(ui):
    p, state = ui
    add(p, 0)
    ready(p)
    publish_one(p, state, 0)          # задание идёт, опрос работает
    mark_nodes(p)
    country = p.locator('#cbd-foot .cbd-pick-country')
    country.focus()
    value = country.input_value()
    repaint_foot(p)
    assert same_nodes(p)['country']
    expect(country).to_be_focused()
    assert country.input_value() == value


def test_typed_new_country_survives_progress_updates(ui):
    p, state = ui
    add(p, 0)
    ready(p)
    publish_one(p, state, 0)
    typed = p.locator('#cbd-foot .cbd-new-country')
    typed.click()
    typed.type('Narnia')
    mark_nodes(p)
    repaint_foot(p)
    assert same_nodes(p)['typed']
    expect(typed).to_be_focused()
    expect(typed).to_have_value('Narnia')


def test_deferred_repaint_runs_after_focus_leaves_and_keeps_input(ui):
    p, state = ui
    add(p, 0)
    ready(p)
    publish_one(p, state, 0)
    typed = p.locator('#cbd-foot .cbd-new-country')
    typed.click()
    typed.type('Narnia')
    # Пока фокус в поле, панель не трогают; ушёл фокус — перерисовка догоняет,
    # а набранное остаётся в новом узле.
    p.evaluate('cbTree.paint()')
    p.locator('#cbd-foot .cbd-publish-all').focus()
    p.wait_for_timeout(300)
    expect(p.locator('#cbd-foot .cbd-new-country')).to_have_value('Narnia')
