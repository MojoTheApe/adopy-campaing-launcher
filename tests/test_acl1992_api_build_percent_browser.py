"""ACL-1992: у API-задания процент у точки — done / total объектов, а не модель робота.

Тот же настоящий шаблон и подменённая сеть, что в
test_campaign_build_percent_browser. Шаг телеметрии у API-пути всегда
`api_create_step` — модель ACL-1347 его не знает, поэтому раньше процент
стоял на одной цифре.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import expect
from test_campaign_build_dot_browser import OK, camp, dot, progress, show
from test_campaign_build_percent_browser import browser, html, pct, publish, ui  # noqa: F401 — фикстуры

LINES = [
    "preflight: creatives read",
    "campaign: created PAUSED, tag kept",
    "ad set 'S0': created PAUSED, tag kept",
    "ad 1: image uploaded",
    "ad 1: creative created",
    "ad 1: created PAUSED",
]


def api_step(page, state, name, done, total, last, idle=2.0):
    c = camp(name, step='api_create_step', idle_s=idle, units={'done': done, 'total': total, 'last': last})
    show(page, state, progress('running', [c], current=name, idle_s=idle))
    expect(dot(page, name)).to_have_attribute('title', 'Building: ' + last)
    return pct(page, name)


def test_api_percent_follows_done_over_total_and_never_says_100_before_the_result(ui):
    p, state = ui
    (a,) = publish(p)
    seen = [api_step(p, state, a, i + 1, 7, line) for i, line in enumerate(LINES)]
    assert seen == [14, 28, 42, 57, 71, 85]
    # Все объекты созданы, итога ещё нет — 99, не 100.
    assert api_step(p, state, a, 7, 7, 'readback: all fields match') == 99
    show(p, state, progress('done', [camp(a, result=OK)]))
    expect(dot(p, a)).to_have_class('cbd-ind cbd-ind-build-ok')
    assert dot(p, a).get_attribute('data-pct') is None


def test_api_percent_does_not_go_back_when_a_form_grows_the_total(ui):
    p, state = ui
    (a,) = publish(p)
    assert api_step(p, state, a, 3, 7, "ad set 'S0': created PAUSED, tag kept") == 42
    # Форма добавила единицу в total — доля меньше, но процент держит достигнутое.
    assert api_step(p, state, a, 3, 8, "form 'Lead': created") == 42
    assert api_step(p, state, a, 4, 8, 'ad 1: image uploaded') == 50
