"""Страница за период, которого книга ещё не знает, обязана это сказать.

Книгу статистики заполняют на следующий день, поэтому за сегодня и вчера
строк в ней нет.

Раньше в этом случае исчезала вся левая панель вместе с билдерами: весь блок
лежит в шаблоне под `if report.countries`, а список приходит из книги. Это
починено — страна с подключённым аккаунтом попадает в отчёт своей пустой
секцией, и панель на месте.

Но починка забрала с собой предупреждение: оно стояло под тем же условием, а
пустым список больше не бывает. Страница стала рисоваться целиком —
семнадцать стран, все числа нули и ни слова о том, почему. Такой экран
неотличим от «всё посчитано, у вас ноль продаж», и человек идёт искать
поломку в выгрузке.

Признак у страницы уже был, только его не спрашивали: собственные даты книги.
Список стран отвечает на «есть ли кому показывать», а не на «есть ли числа».
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.web.app import templates  # noqa: E402

TPL = ROOT / "src" / "web" / "templates" / "report.html"


def _piece() -> str:
    """Кусок шаблона, отвечающий за предупреждение, — из файла, а не по памяти.

    Границы ищутся **после** начала блока: `id="amap-slot"` встречается в
    шаблоне и раньше — у карты своё место ниже по потоку, — и срез по первому
    вхождению давал пустую строку. Пустой шаблон отрисовывается в пустой
    вывод, то есть проверка падала ровно так же, как упала бы на настоящей
    поломке. Это едва не увело чинить работающий код.
    """
    src = TPL.read_text(encoding="utf-8")
    start = src.index("{%- set book_covers_period")
    slot = src.index('id="amap-slot"', start)
    end = src.index("{% endif %}", src.index("{% endif %}", slot) + 1) + len("{% endif %}")
    piece = src[start:end]
    assert piece.count("{% if") == piece.count("{% endif %}"), "срез разрезал условие пополам"
    return piece.replace('{% include "_account_map.html" %}', "MAP")


def _render(countries, *, date_from="2026-09-09", date_to=None, dates=("2026-09-07", "2026-09-08"), source_info=None):
    account_map = {"date_from": date_from, "date_to": date_from if date_to is None else date_to, "dates": list(dates)}
    if source_info is not None:
        account_map["source_info"] = source_info
    return templates.env.from_string(_piece()).render(
        report={"countries": list(countries)},
        account_map=account_map,
    )


ONE_COUNTRY = [{"country": "Saudi Arabia"}]


@pytest.mark.parametrize("sources,caption,advice", [
    (["workbook"], "The workbook has no rows", "the workbook may need updating"),
    (["bi"], "The BI data have no rows", "the statistics source may need updating"),
    (["bi", "workbook"], "The selected statistics have no rows", "the statistics source may need updating"),
    ([], "The selected statistics have no rows", "the statistics source may need updating"),
])
@pytest.mark.parametrize("dates", [("2026-09-07", "2026-09-08"), ()])
def test_empty_period_names_its_actual_source_without_losing_dates_or_map(sources, caption, advice, dates):
    out = _render([], source_info={"sources": sources}, dates=dates)
    assert caption in out and advice in out
    assert "2026-09-09" in out
    if dates:
        assert "It runs to 2026-09-08" in out
    else:
        assert "readable dates" in out and "It runs to" not in out
    assert out.count('id="workbook-period-notice"') == 1 and out.count("MAP") == 1
    if sources != ["workbook"]:
        assert "The workbook has no rows" not in out and "the workbook may need updating" not in out


@pytest.mark.parametrize("sources", [["bi"], ["bi", "workbook"]])
@pytest.mark.parametrize("start,end", [("2026-09-08", "2026-09-08"), ("2026-09-06", ""), ("", "2026-09-08")])
def test_source_aware_covered_and_open_periods_do_not_show_a_false_empty_notice(sources, start, end):
    out = _render(ONE_COUNTRY, date_from=start, date_to=end, source_info={"sources": sources})
    assert 'id="workbook-period-notice"' not in out


# ── период, которого книга не знает ───────────────────────────────────────

def test_a_period_outside_the_workbook_names_itself():
    """Тот самый случай: страны на странице есть, чисел за период нет."""
    out = _render(ONE_COUNTRY)
    assert "The workbook has no rows" in out
    assert "2026-09-09" in out, "период не назван — человек пойдёт чинить файл"
    assert "It runs to 2026-09-08" in out, "не сказано, докуда книга дошла"


def test_a_period_the_workbook_covers_says_nothing():
    out = _render(ONE_COUNTRY, date_from="2026-09-08")
    assert 'id="workbook-period-notice"' not in out


def test_all_dates_says_nothing():
    """У «всех дат» периода нет, и жаловаться не на что."""
    assert 'id="workbook-period-notice"' not in _render(ONE_COUNTRY, date_from="")


def test_a_workbook_with_no_dates_at_all_still_speaks():
    """Книга нечитаема — это тоже ответ, и молчать про него нельзя."""
    out = _render(ONE_COUNTRY, dates=())
    assert "The workbook has no rows" in out
    assert "It runs to" not in out, "названа граница, которой нет"


# ── карта аккаунтов ───────────────────────────────────────────────────────

def test_the_account_map_appears_when_there_is_nothing_else():
    """Кнопка Read читает Ads Manager, а не книгу — ради неё дату и выбирают."""
    assert "MAP" in _render([])


def test_the_account_map_is_not_drawn_twice():
    """Ниже по потоку у карты своё место."""
    assert "MAP" not in _render(ONE_COUNTRY)


# ── обратная проверка ─────────────────────────────────────────────────────

def test_this_check_would_notice_the_old_behaviour():
    """Сторож, повторяющий шаблон, ничего не стоит.

    Здесь это вдвойне важно: прежний сторож на этот блок искал строки в тексте
    шаблона — и остался зелёным, когда блок перестал отрисовываться вовсе.
    """
    old = _piece().replace(
        "{% if not report.countries or not book_covers_period.value %}",
        "{% if not report.countries %}",
    )
    out = templates.env.from_string(old).render(
        report={"countries": ONE_COUNTRY},
        account_map={"date_from": "2026-09-09", "dates": ["2026-09-08"]},
    )
    assert 'id="workbook-period-notice"' not in out, (
        "прежнее условие сообщение показывает — значит проверка ничего не ловит"
    )


def test_a_range_with_rows_after_its_first_day_is_not_empty():
    out = _render(ONE_COUNTRY, date_from="2026-09-06", date_to="2026-09-08")
    assert 'id="workbook-period-notice"' not in out


def test_a_gap_inside_the_workbook_is_empty():
    out = _render(ONE_COUNTRY, date_from="2026-09-08", dates=("2026-09-07", "2026-09-09"))
    assert "The workbook has no rows" in out


def test_open_ended_periods_match_the_report_filter():
    assert 'id="workbook-period-notice"' not in _render(ONE_COUNTRY, date_from="2026-09-06", date_to="")
    assert 'id="workbook-period-notice"' not in _render(ONE_COUNTRY, date_from="", date_to="2026-09-08")
    out = _render(ONE_COUNTRY, date_from="", date_to="2026-09-06")
    assert "The workbook has no rows" in out and "dates through 2026-09-06" in out


def test_a_partial_overlap_is_not_described_as_no_rows():
    assert 'id="workbook-period-notice"' not in _render(ONE_COUNTRY, date_from="2026-09-08", date_to="2026-09-12")


def _full_page(countries=True):
    from unittest.mock import MagicMock
    from tests.test_web_build_upload import _minimal_report
    from src.web.app import _enrich_country_overview, mlx_instance_labels
    report = _minimal_report()
    if not countries:
        report['countries'] = []
    _enrich_country_overview(report)
    amap = dict(countries=[], available=[], all_accounts=[], live={}, span='',
                date_from='2026-09-09', date_to='2026-09-09', dates=['2026-09-08'])
    return templates.env.get_template('report.html').render(
        request=MagicMock(), report=report, mlx_labels=mlx_instance_labels(),
        warnings=[], built_at='2026-09-12', upload_errors={}, account_map=amap,
        account_preflight={}, account_preflight_json={}, ref_options={},
        ref_options_json={}, account_alias_map_json={}, draft_status={},
    )


def test_full_page_places_notice_before_dashboard_and_keeps_one_map():
    html = _full_page()
    assert html.index('id="workbook-period-notice"') < html.index('<div class="dashboard-shell">')
    assert html.count('id="amap-slot"') == 1
    assert 'Read all' in html and 'manage-builder-' in html


def test_full_empty_page_still_has_the_account_map():
    html = _full_page(countries=False)
    assert 'The workbook has no rows' in html
    assert html.count('id="amap-slot"') == 1 and 'Read all' in html
