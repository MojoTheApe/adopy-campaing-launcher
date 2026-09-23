# -*- coding: utf-8 -*-
"""Пострановое чтение можно запустить с экрана, а не только запросом (acl-357).

Замечание ревьюера 14.09: «чтение по странам не подключено к кнопкам
дашборда». Так и было — маршруты в `app.py` существовали, разметка для показа
результата существовала, а **нажать было нечего**. Полный набор тестов при
этом был зелёный: каждая половина проверялась отдельно, и ровно на стыке
проверки не было.

Поэтому проверки здесь идут по цепочке целиком: кнопка → функция → маршрут.
Разорвать её можно в любом из трёх мест, и каждое выглядит безобидно.

ACL-1282: кнопок стало не три, а две. «Countries» больше нет — разбивку
ведёт то же нажатие, что и спенд, и цепочка стала длиннее на одно звено:
кнопка → чтение спенда → решение сервера → чтение стран.
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
    """Одна кнопка на строку — и она запускает обе половины чтения."""
    assert "amapReadSpend(this)" in ROW, (
        "в строке аккаунта нет кнопки, запускающей чтение"
    )
    assert "amapReadCountries" not in ROW and "Countries</button>" not in ROW, (
        "вторая кнопка вернулась: ACL-1282 свёл оба чтения в одно нажатие"
    )


def test_the_button_calls_the_route_that_exists():
    fn = PAGE.split("function amapRunSplit(", 1)
    assert len(fn) == 2, "функция amapRunSplit не найдена на странице"
    body = fn[1][:2000]
    assert "/account-spend/read-countries" in body, (
        "чтение стран есть, но ничего не запрашивает"
    )
    assert '@app.post("/account-spend/read-countries")' in APP, (
        "страница зовёт маршрут, которого нет на сервере"
    )


def test_the_spend_reading_leads_to_the_split_when_it_is_needed():
    """Звено, которого раньше не было: спенд прочитан → запускаем страны.

    Разорвать его можно молча — разбивка просто перестанет читаться вовсе, а
    кнопка будет отрабатывать как прежде.
    """
    poll = PAGE.split("function amapPollSpend(", 1)
    assert len(poll) == 2, "опрос чтения спенда не найден"
    body = poll[1][:1800]
    assert "j.plan && j.plan.needed" in body and "amapRunSplit(" in body, (
        "после удачного спенда разбивка больше не запускается"
    )
    assert "s.state === 'ok'" in body, (
        "разбивку спрашивают и после неудачного спенда — кабинет не открылся"
    )


def test_the_server_says_whether_the_split_is_needed():
    """Решение принимает сервер: у клиента нет ни привязок, ни хранилища.

    И приезжает оно вместе с дочитанным спендом, а не с его запуском
    (ACL-1288): решение сравнивает суммы, а на запуске новой суммы ещё нет.
    """
    assert "def _split_plan(" in APP
    assert 'answer["plan"] = _split_plan(' in APP, (
        "опрос состояния не отдаёт решение о странах"
    )
    assert "why_split_is_needed" in APP


def test_the_screen_learns_when_the_reading_is_done():
    """Запустить мало: без опроса кнопка осталась бы в «countries…» навсегда."""
    assert "function amapAwaitSplit(" in PAGE
    assert "/account-spend/status?account=" in PAGE
    assert '"split": country_split.get_split(' in APP, (
        "опрос спрашивает состояние, а сервер его не отдаёт"
    )


def test_a_failed_reading_returns_the_button_to_the_person():
    """Отказ обязан вернуть кнопку в работу — иначе строку нельзя перечитать."""
    assert "function amapSplitDone(" in PAGE
    block = PAGE.split("function amapSplitDone(", 1)[1][:400]
    assert "btn.disabled = !!amapReadAllBusy" in block
    assert "'Read spend'" in block, (
        "кнопка должна вернуться к своей обычной подписи, а не остаться «countries…»"
    )


def test_the_failed_split_marks_every_row_of_the_account():
    """Аккаунт в двух странах стоит в двух блоках, и сервер при перезагрузке
    пометит обе. Пометить одну значило бы менять экран под человеком."""
    block = PAGE.split("function amapSplitMark(", 1)[1][:900]
    assert "querySelectorAll('.amap-live')" in block and "dataset.account" in block


def test_a_stored_split_failure_survives_a_failed_spend_reading():
    """Отказ разбивки относится к тем же датам: неудача спенда его не
    опровергает, а `amapSpendFailed` пересобирает клетку целиком."""
    block = PAGE.split("function amapSpendFailed(", 1)[1][:1400]
    assert "amap-split-bad" in block, (
        "пометка об упавших странах исчезает при неудаче спенда до перезагрузки"
    )


def test_a_failed_split_does_not_look_like_a_failed_spend():
    """Кнопка общая, значит «failed · retry» на ней читалось бы как «нет числа».

    Отказ разбивки живёт в клетке, рядом с прочитанным спендом, и приходит
    с сервера — иначе он исчезал бы при перезагрузке страницы.
    """
    assert "function amapSplitMark(" in PAGE
    assert "amap-split-bad" in PAGE and "amap-split-bad" in ROW
    assert "amap.splits" in ROW, "клетка не знает о пострановом чтении с сервера"
    assert "def _live_splits(" in APP and '"splits": splits' in APP


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
    assert "Select dates to read account spend; Maximum is not supported" in ROW


def test_read_all_follows_the_same_rule():
    """Очередь Read all читает страны тем же правилом и в том же заходе.

    Иначе получилось бы два разных ответа на один вопрос: строка читает
    страны, а «Read all» — нет, и человек не знает, что у него на экране.
    """
    block = PAGE.split("async function amapQueueRead(", 1)
    assert len(block) == 2, "очередь Read all не найдена"
    # Тело функции целиком, а не первые N символов: последняя строка тела —
    # часть проверки, и срез «сколько-нибудь» её бы не поймал.
    body = block[1][:block[1].index(chr(10) + "}" + chr(10))]
    assert "done.plan && done.plan.needed" in body, (
        "очередь не спрашивает сервер, нужна ли разбивка"
    )
    assert "await amapRunSplit(" in body, (
        "разбивка должна идти внутри очереди: аккаунт открыт сейчас"
    )
    assert "return await amapRunSplit(" not in body, (
        "ответ разбивки стал ответом строки: упавшие страны превратили бы "
        "прочитанный спенд в неудачу и испортили счёт «N read, M failed»"
    )
    tail = body[body.index("await amapRunSplit("):]
    tail = tail[:tail.index("} catch")]
    assert "return done.state;" in tail and "return " not in tail.split("return done.state;")[0], (
        "строка обязана вернуть состояние спенда, а не разбивки"
    )


# ── место для кнопок ──────────────────────────────────────────────────────
#
# Кнопку мало добавить: колонка действий фиксированной ширины, и первая
# попытка вытолкнула `Remove` на 49px за край. В разметке он оставался, в
# тестах — тоже, а нажать его было нельзя. Поймано только в браузере.


def test_the_actions_column_has_room_for_its_buttons():
    """Две кнопки — «Read spend» и «×» — это ~120px на таблице 982px."""
    cols = re.findall(r'<col style="width:(\d+)%"', ROW)
    assert cols, "колонки таблицы аккаунтов не найдены"
    assert sum(int(c) for c in cols) == 100, "доли колонок должны давать ровно 100%"
    assert int(cols[-1]) >= 12, (
        "колонка с кнопками сузилась — кнопки снова уедут за край"
    )
    assert int(cols[0]) >= 28, (
        "освободившаяся ширина должна была уйти имени (ACL-1277)"
    )


def test_the_wrap_rule_is_written_strongly_enough_to_win():
    """Страховка на случай, если кнопок снова станет больше.

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
