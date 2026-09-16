# -*- coding: utf-8 -*-
"""Подпункт плейсментов не теряется из-за заглавной буквы (acl-1162).

15.09, живой прогон на кувейтском кабинете. Sentry привёз тикет, в котором
рядом лежали две строки — «что искали» и «что видели»:

    subitems_missing: ["Instagram feed", "Facebook Notifications"]
    subitems_seen:    [… "Instagram Feed" … "Facebook notifications" …]

Разница в одной букве. Подпункт объявлялся отсутствующим, шаг плейсментов
отказывал, и без этого тикета найти это было бы нечем: в логе видно только
«не нашёл».

Сводить регистр безопасно: названия плейсментов различаются словами, а не
регистром — «Facebook Feed» и «Facebook profile feed» живут в одном списке и
не путаются.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import meta_dom as md  # noqa: E402

#: Ровно то, что прислал живой кабинет 15.09.
SEEN_LIVE = [
    "Placements", "Facebook Feed", "Facebook profile feed", "Instagram Feed",
    "Instagram profile feed", "Facebook Marketplace", "Facebook right column",
    "Instagram Explore home", "Facebook Business Explore", "Threads feed",
    "Facebook notifications", "Allow limited spending to excluded placements",
    "Instagram Stories", "Facebook Stories", "Messenger Stories",
    "Instagram Reels", "Facebook Reels", "WhatsApp Status",
    "Facebook in-stream reels", "Ads on Facebook Reels",
]


@pytest.mark.parametrize("wanted, why", [
    ("Instagram feed", "страница пишет Instagram Feed"),
    ("Facebook Notifications", "страница пишет Facebook notifications"),
])
def test_the_two_that_were_lost_are_found_now(wanted, why):
    assert md._label_on_page(wanted, SEEN_LIVE), why


def test_what_already_matched_keeps_matching():
    for label in ("Facebook Feed", "Instagram Stories", "WhatsApp Status"):
        assert md._label_on_page(label, SEEN_LIVE)


def test_a_name_that_is_really_absent_stays_absent():
    """Смягчили регистр — не смягчили смысл."""
    assert not md._label_on_page("Twitter Feed", SEEN_LIVE)
    assert not md._label_on_page("", SEEN_LIVE)


def test_close_names_are_not_confused():
    """Главный риск сведения регистра: слипание соседей. Его здесь нет —
    названия различаются словами."""
    assert md._label_on_page("Facebook feed", SEEN_LIVE)
    assert md._label_on_page("Facebook profile feed", SEEN_LIVE)
    # И это разные строки: одна не должна «находиться» вместо другой.
    assert "Facebook Feed" in SEEN_LIVE and "Facebook profile feed" in SEEN_LIVE


def test_surrounding_spaces_do_not_matter():
    assert md._label_on_page("  Instagram Feed  ", SEEN_LIVE)


def test_the_browser_side_ignores_case_too():
    """Сверка в JS — вторая половина той же ошибки.

    Проверяется текстом: выполнить этот скрипт без страницы нельзя, а
    оставить одну половину исправленной значило бы починить наполовину.
    """
    js = md.PLACEMENTS_SUBITEM_JS
    assert "toLowerCase()" in js, "сверка в браузере снова учитывает регистр"
    assert "norm(e.textContent) === label" not in js, "вернулось точное сравнение"
