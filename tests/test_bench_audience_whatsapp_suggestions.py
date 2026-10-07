# -*- coding: utf-8 -*-
"""Advantage+ audience на WhatsApp-раскладках классического редактора (ACL-1638).

06.10: итоговый шаг `advantage_plus_audience` отказал `still_on` на двух
аккаунтах, и это два разных отказа (снимки в `audience_whatsapp_suggestions_
2026_10_06.html`):

* `whatsapp_suggestion_collapsed` — FacebookDoc120, 573ab2cf3646/00 (и
  4285247f763a 05.10): карточка в режиме контролов, поле Age свёрнуто в
  «35 - 65+ · Unknown age on WhatsApp: Excluded» с меткой «Your suggestion».
  Сводку с пометкой после границ читали только без пометки (`$` в
  `_AGE_SUMMARY_RE`), поэтому строка не раскрывалась (`age_row_opened:
  false`, `age: []`), «Use as a suggestion» не снималась, бейдж оставался «on».
* `whatsapp_age_hard_badge_on` — Facebook_9403A, d4f9d5f016c1/03 (и
  327c0b934cb3): возраст уже жёсткий (35 - 65+, галочка снята), а бейдж
  всё равно «Advantage+ on». До шага интересов (02_audience_location) тот же
  бейдж читал «off». Какой шаг его перевернул, снимки не говорят (успешный
  прогон 8c6496e50815 держит «off» и с интересами-подсказкой), поэтому робот
  нажать здесь ничего не может: отказ остаётся отказом, в `diag` — состояние
  галочки и режим карточки, а после каждого шага пишется `audience_badge_probe`.

Эмуляция Meta (раскрытие строки Age, галочка, бейдж) — `_META_AGE` из
`test_suggest_an_audience_layout`.
"""
from __future__ import annotations

import re

import pytest

from src.fb_ads_rpa import meta_dom as md
from tests.bench_support import FIXTURES, fragment
from tests.test_suggest_an_audience_layout import (  # noqa: F401  (fixtures)
    FRAG as SUGGEST_FRAG,
    _META_AGE,
    _RIGHT_PANEL,
    _checkbox,
    _clicked,
    _never_the_forbidden_buttons,
    browser,
    page,
)

FIXTURE = FIXTURES / "audience_whatsapp_suggestions_2026_10_06.html"


def _card(name: str) -> str:
    return f'<main style="width:640px">{fragment(FIXTURE, name)}</main>{_RIGHT_PANEL}'


def _row(page):
    return [r for r in page.rows if r["step"] == "advantage_plus_audience"][-1]


# ── Doc120: свёрнутый Age «35 - 65+ · Unknown age on WhatsApp: Excluded» ───

@pytest.mark.parametrize("text, expected", [
    ("35 - 65+ · Unknown age on WhatsApp: Excluded", True),
    ("35 – 65+", True),
    ("18 - 65+", True),
    ("35 - 65+ ·", False),
    ("35 - 65+ extra words", False),
    ("Budget 35 - 65+", False),
])
def test_the_age_summary_accepts_a_note_after_the_bounds(text, expected):
    assert bool(re.search(md._AGE_SUMMARY_RE, text)) is expected


def test_the_collapsed_age_with_a_note_is_read_as_a_range(page):
    page.set_content(_card("whatsapp_suggestion_collapsed"))
    assert md._age_range_read(page) == ["35", "65+"]
    # Свёрнутое поле не считается открытым: по этому признаку решается,
    # раскрывать ли его.
    assert md._age_triggers_read(page) == []


def test_the_collapsed_age_row_with_a_note_is_opened_unchecked_and_the_badge_goes_off(page):
    page.set_content(_card("whatsapp_suggestion_collapsed"))
    page.evaluate(_META_AGE, [SUGGEST_FRAG["age_open_suggestion"], True])
    assert md._audience_badge_on(page) is True
    assert md.verify_audience_advantage_off(page) is True
    assert md._audience_badge_on(page) is False
    assert _checkbox(page) == "false"
    row = _row(page)
    assert row["ok"] is True and row["note"] == "off"
    assert row["diag"]["your_suggestion"] is True
    assert row["diag"]["age_row_opened"] is True and row["diag"]["unchecked"] is True
    assert row["diag"]["age"] == ["35", "65+"]
    _never_the_forbidden_buttons(page)


def test_the_suggestion_that_meta_keeps_on_is_still_a_failure(page):
    """Нет ложного «ok»: галочка снята, бейдж остался «on» — отказ."""
    page.set_content(_card("whatsapp_suggestion_collapsed"))
    page.evaluate(_META_AGE, [SUGGEST_FRAG["age_open_suggestion"], False])
    assert md.verify_audience_advantage_off(page) is False
    row = _row(page)
    assert row["ok"] is False and row["note"] == "still_on"
    assert row["diag"]["age_row_opened"] is True and row["diag"]["unchecked"] is True
    _never_the_forbidden_buttons(page)


# ── 9403A: возраст жёсткий, бейдж «on», интересы — подсказка ──────────────

def test_a_hard_age_with_the_badge_on_is_blocked_with_the_cause_in_the_diag(page):
    page.set_content(_card("whatsapp_age_hard_badge_on"))
    page.evaluate("window.clicked = []; document.addEventListener('click', e => window.clicked.push("
                  "(e.target.closest('[role=button], button, a, label') || e.target).textContent.trim().slice(0, 60)), true)")
    assert md.verify_audience_advantage_off(page) is False
    row = _row(page)
    assert row["ok"] is False and row["note"] == "still_on"
    diag = row["diag"]
    assert diag["age"] == ["35", "65+"]
    assert diag["age_suggestion_checked"] is False
    assert diag["mode"] == "controls"
    # Робот только читает: ни «Use recommended setup», ни «Switch to
    # recommended setup», ни галочку, которой нечего снимать.
    assert _clicked(page) == []


def test_both_layouts_are_in_controls_mode_so_the_pre_age_check_is_not_the_cause(page):
    for name in ("whatsapp_suggestion_collapsed", "whatsapp_age_hard_badge_on"):
        page.set_content(_card(name))
        mode = md._audience_mode(page)
        assert mode["mode"] == "controls", name
        assert {"Locations", "Age", "Gender", "Detailed targeting", "Languages"} <= set(mode["controls"])


def test_the_badge_probe_only_reads_and_says_on_off_or_unknown(page):
    page.evaluate("window.clicked = []; document.addEventListener('click', e => window.clicked.push(1), true)")
    for html, note in ((_card("whatsapp_age_hard_badge_on"), "on"),
                       (SUGGEST_FRAG["controls_off"], "off"),
                       ("<div>no audience card</div>", "unknown")):
        page.set_content(html)
        md._probe_audience_badge(page, "targeting")
        row = [r for r in page.rows if r["step"] == "audience_badge_probe"][-1]
        assert row["note"] == note and row["ok"] is None and row["diag"] == {"after": "targeting"}
    assert _clicked(page) == []
