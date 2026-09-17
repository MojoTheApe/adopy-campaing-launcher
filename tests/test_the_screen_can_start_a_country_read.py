# -*- coding: utf-8 -*-
"""Пострановое чтение можно запустить с экрана, а не только запросом (acl-357).

Замечание ревьюера 14.09: «чтение по странам не подключено к кнопкам
дашборда». Так и было — маршруты в `app.py` существовали, разметка для показа
результата существовала, а **нажать было нечего**. Полный набор тестов при
этом был зелёный: каждая половина проверялась отдельно, и ровно на стыке
проверки не было.

Поэтому проверки здесь идут по цепочке целиком: кнопка → функция → маршрут.
Разорвать её можно в любом из трёх мест, и каждое выглядит безобидно.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TEMPLATES = ROOT / "src" / "web" / "templates"
ROW = (TEMPLATES / "_account_map_country.html").read_text(encoding="utf-8")
PAGE = (TEMPLATES / "report.html").read_text(encoding="utf-8")
APP = (ROOT / "src" / "web" / "app.py").read_text(encoding="utf-8")


def test_the_account_row_has_a_button_that_starts_it():
    assert "amapReadCountries(this)" in ROW, (
        "в строке аккаунта нет кнопки, запускающей пострановое чтение"
    )


def test_the_button_calls_the_route_that_exists():
    fn = PAGE.split("function amapReadCountries(", 1)
    assert len(fn) == 2, "функция amapReadCountries не найдена на странице"
    body = fn[1][:2000]
    assert "/account-spend/read-countries" in body, (
        "кнопка есть, но ничего не запрашивает"
    )
    assert '@app.post("/account-spend/read-countries")' in APP, (
        "страница зовёт маршрут, которого нет на сервере"
    )


def test_the_screen_learns_when_the_reading_is_done():
    """Запустить мало: без опроса кнопка осталась бы в «countries…» навсегда."""
    assert "function amapPollCountries(" in PAGE
    assert "/account-spend/status?account=" in PAGE
    assert '"split": country_split.get_split(' in APP, (
        "опрос спрашивает состояние, а сервер его не отдаёт"
    )


def test_a_failed_reading_returns_the_button_to_the_person():
    """Отказ обязан вернуть кнопку в работу — иначе строку нельзя перечитать."""
    assert "function amapCountriesFailed(" in PAGE
    block = PAGE.split("function amapCountriesFailed(", 1)[1][:400]
    assert "btn.disabled = false" in block


def test_the_result_is_drawn_by_the_server_not_a_second_time_by_us():
    """Разметку строк собирает `_account_map.html`; клиент её только забирает.

    Рядом в шаблоне уже стоит предупреждение о том, чем кончается вторая
    сборка той же разметки на клиенте: строка замирает на отрисованном
    сервером и молчит.
    """
    block = PAGE.split("function amapRefreshCountryLines(", 1)
    assert len(block) == 2, "обновление строк стран не найдено"
    body = block[1][:2500]
    assert "/account-map?date_from=" in body, (
        "строки собираются на клиенте вместо запроса готового фрагмента"
    )
    assert "amap-country-read" in body


def test_without_dates_the_button_says_so_instead_of_failing():
    """Период обязателен: чтение за «всё время» отчёт не строит."""
    assert re.search(r"amap-countries\"?\s+disabled", ROW) or "disabled" in ROW
    assert "Select dates to read the country split" in ROW


# ── место для третьей кнопки ──────────────────────────────────────────────
#
# Кнопку мало добавить: колонка действий фиксированной ширины, и первая
# попытка вытолкнула `Remove` на 49px за край. В разметке он оставался, в
# тестах — тоже, а нажать его было нельзя. Поймано только в браузере.


def test_the_actions_column_has_room_for_three_buttons():
    """Три кнопки — это 161px; прежние 13% ширины таблицы их не вмещали."""
    cols = re.findall(r'<col style="width:(\d+)%"', ROW)
    assert cols, "колонки таблицы аккаунтов не найдены"
    assert sum(int(c) for c in cols) == 100, "доли колонок должны давать ровно 100%"
    assert int(cols[-1]) >= 18, (
        "колонка с кнопками сузилась — третья кнопка снова уедет за край"
    )


def test_the_wrap_rule_is_written_strongly_enough_to_win():
    """Страховка на случай, если кнопок станет четыре.

    Перенос в этой ячейке запрещает `.amap table.amap-grid td` — правило
    сильнее любого одиночного класса. Первая версия починки была написана
    как `.amap-actions { white-space: normal }` и не сработала вовсе:
    браузер показывал прежний `nowrap`. Короткая запись здесь выглядит
    правильной и молча ничего не делает.
    """
    rule = re.search(
        r"\.amap table\.amap-grid td\.amap-actions\s*\{[^}]*\}", PAGE, re.S)
    assert rule, "правило переноса для ячейки с кнопками потеряло силу селектора"
    assert "white-space: normal" in rule.group(0)
