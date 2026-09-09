"""`meta_dom`: то, что проверяется без страницы.

Из 136 функций модуля 103 требуют DOM и ждут стенда с записанными страницами
(`acl-340`). Эти четыре не требуют ничего — и на них опирается всё остальное:
как сравниваются подписи, какой пункт меню считается нужным и как выглядят
паузы между действиями.

`acl-340c`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import meta_dom as md  # noqa: E402


# ─────────────── приведение подписи ───────────────


@pytest.mark.parametrize("raw,expected", [
    ("Website", "website"),
    ("  Website  ", "website"),
    ("WEBSITE", "website"),
    ("Website  and\n instant   forms", "website and instant forms"),
    ("", ""),
    (None, ""),
])
def test_a_label_is_compared_by_its_words_not_its_spacing(raw, expected):
    """Meta переносит подписи по-разному в зависимости от ширины окна.

    Сравнивать их посимвольно значило бы не находить кнопку на узком экране.
    """
    assert md._norm_label(raw) == expected


def test_normalising_is_idempotent():
    """Повторное приведение ничего не меняет — иначе сравнение зависит от того,
    сколько раз строку успели обработать по дороге."""
    once = md._norm_label("  Website   and\tinstant forms ")
    assert md._norm_label(once) == once


# ─────────────── «Website» и не совсем «Website» ───────────────


@pytest.mark.parametrize("label", ["Website", "website", "  WEBSITE ", "Sitio web", "sitio  web"])
def test_the_plain_website_option_is_recognised(label):
    """Интерфейс бывает и на испанском — обе подписи означают одно."""
    assert md._website_only_label(label) is True


@pytest.mark.parametrize("label", [
    "Website and instant forms",
    "Website and Instant Forms",
    "Instant forms",
    "Website leads",
    "Sitio web y formularios instantáneos",
    "",
    None,
])
def test_a_longer_option_is_not_the_plain_website_option(label):
    """Это разные пункты меню, и выбор между ними меняет саму кампанию.

    «Website» и «Website and instant forms» — разные места назначения
    конверсии. Совпадение по вхождению вместо точного дало бы второй пункт
    там, где нужен первый, и кампания собралась бы не та.
    """
    assert md._website_only_label(label) is False


# ─────────────── первая строка пункта меню ───────────────


def test_a_menu_row_is_identified_by_its_first_line():
    """У пункта меню под названием идёт описание — оно к делу не относится."""
    assert md._menu_first_line("Website\nSend traffic to your site") == "website"


@pytest.mark.parametrize("raw,expected", [
    ("  Website  \n  описание  ", "website"),
    ("Website", "website"),
    # Обрезка идёт до деления на строки, поэтому ведущий перенос не обнуляет
    # ответ. Я ожидал обратного и ошибся — код прав.
    ("\nWebsite", "website"),
    ("", ""),
    (None, ""),
])
def test_the_first_line_survives_odd_spacing(raw, expected):
    assert md._menu_first_line(raw) == expected


# ─────────────── паузы между действиями ───────────────


def test_a_pause_stays_within_its_spread():
    """Разброс задан долей от основы, и выходить за него пауза не должна."""
    for _ in range(200):
        v = md._human_jitter(1.0, 0.3)
        assert 0.7 <= v <= 1.3


def test_pauses_do_not_repeat():
    """Ради этого функция и существует.

    В разборе обнаруживаемости 2026-07-31 одинаковые паузы оказались самым
    считаемым признаком: 33 паузы ровно по 0.5 с, 24 по 0.6 с, 21 по 0.3 с в
    одном файле. Живой человек не повторяет промежуток до миллисекунды.
    """
    values = {md._human_jitter(0.5) for _ in range(200)}
    assert len(values) > 190, (
        "паузы начали повторяться — именно по этому признаку автоматизацию и "
        "отличают от человека"
    )


def test_no_spread_means_exactly_the_base():
    """Нулевой разброс — осознанный выбор, а не повод для случайности."""
    assert md._human_jitter(0.5, 0.0) == 0.5


def test_a_zero_pause_stays_zero():
    assert md._human_jitter(0.0) == 0.0


def test_the_pause_is_centred_on_its_base():
    """Среднее должно держаться основы: смещённые паузы копили бы задержку
    на длинном прогоне и меняли бы его продолжительность."""
    n = 2000
    mean = sum(md._human_jitter(1.0, 0.3) for _ in range(n)) / n
    assert 0.97 <= mean <= 1.03, f"среднее {mean:.3f} ушло от основы"
