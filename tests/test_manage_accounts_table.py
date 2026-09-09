"""Одна строка на аккаунт, одно место подключения.

Под картой аккаунтов жила вторая секция, где тот же аккаунт описывался ещё
раз: свои числа (`Cost` — тот же спенд), своя колонка Ad Asset, которую
просили убрать, и своя форма подключения. Форма была хуже той, что на карте:
та ищет профиль по последним буквам и читает номер кабинета из его же истории,
а эта просила номер руками.

Уникальными были только «что сделать» и «готов» — они переехали наверх.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ROW = ROOT / "src" / "web" / "templates" / "_account_map_country.html"
PAGE = ROOT / "src" / "web" / "templates" / "report.html"


def test_the_duplicate_section_is_gone():
    t = PAGE.read_text(encoding="utf-8")
    assert "Account Statuses</h2>" not in t, "секция вернулась — аккаунт снова описан дважды"
    assert "to_do_display" not in t, "старая таблица вернулась"


def test_only_one_place_connects_an_account():
    """Форма на карте читает номер кабинета сама; вторая просила его руками."""
    t = PAGE.read_text(encoding="utf-8")
    assert "Connect an account to" not in t, "второе место подключения вернулось"


def test_the_state_of_the_account_is_written_not_ticked():
    """Галочка отвечала на вопрос, которого никто не задаёт вслух.

    ✅/❌ говорило «готов ли», а спрашивают другое: что с аккаунтом. Слово
    отвечает на второй вопрос и, когда аккаунт не готов, называет причину —
    ради неё и держали отдельную колонку «To do».
    """
    t = ROW.read_text(encoding="utf-8")
    assert "r.ready" in t, "состояние аккаунта негде посмотреть"
    assert ">ready<" in t, "состояние не написано словом"
    assert "needs setup" in t, "неготовый аккаунт ничего о себе не говорит"
    # Смотрим только в строки таблицы: в комментарии рядом галочка названа
    # нарочно — там объясняется, почему её убрали.
    body = t[t.index("<tbody>"): t.index("</tbody>")]
    assert "✅" not in body and "❌" not in body, "галочка вернулась"


def test_the_reason_is_written_when_the_account_is_not_ready():
    """Иначе «needs setup» — это тупик: непонятно, что именно сделать."""
    t = ROW.read_text(encoding="utf-8")
    setup = t[t.index('class="amap-setup"'): t.index("</td>", t.index('class="amap-setup"'))]
    assert "r.to_do" in setup, "причина неготовности потерялась вместе с колонкой"


def test_the_open_recommendations_column_is_gone():
    """Список рекомендаций у готового аккаунта — шум, а не задача.

    Он занимал шестую часть ширины таблицы и повторял то, что уже сказано в
    разделе оптимизации.
    """
    t = ROW.read_text(encoding="utf-8")
    head = t[t.index("<thead>"): t.index("</thead>")]
    assert ">To do<" not in head, "колонка To do вернулась"
    assert "amap-todo" not in t, "ячейка To do осталась в разметке"


def test_the_column_count_still_matches_itself():
    """Ошибка ровно этого рода однажды выпихнула кнопки за правый край."""
    t = ROW.read_text(encoding="utf-8")
    head = t[t.index("<thead>"): t.index("</thead>")]
    body = t[t.index("<tbody>"): t.index("</tbody>")]
    cg = t[t.index("<colgroup>"): t.index("</colgroup>")]
    cols = len(re.findall(r"<th[ >]", head))
    assert len(re.findall(r"<td[ >]", body)) == cols, "ячеек не столько же, сколько колонок"
    assert cg.count("<col ") == cols, "долей не столько же, сколько колонок"
    assert sum(int(x) for x in re.findall(r"width:(\d+)%", cg)) == 100
    assert int(re.search(r"ncols=(\d+)", t).group(1)) == cols, "строка раскрытия поедет"


def test_a_failed_read_shows_its_reason_after_a_reload():
    """Живой обработчик рисует причину только после попытки.

    После перезагрузки человек видел «failed» без объяснения — в том числе
    когда причина успела смениться.
    """
    t = ROW.read_text(encoding="utf-8")
    cell = t[t.index("amap-live"): t.index("amap-status")]
    assert "state == 'failed'" in cell, "отказ снова показывается прочерком"
    assert "sr.error" in cell, "причина не выводится"
    assert "sr.shot" in cell, "снимок опять негде найти"


def test_an_account_the_workbook_never_heard_of_is_not_called_unfinished():
    """«Нет в книге» и «в книге, но не готов» — разные вещи.

    Аккаунт, заведённый на дешборде для тестов, в рабочей книге статистики не
    появится никогда. Красное «needs setup» на нём означало бы задачу, которой
    не существует, — а предупреждение, которое горит всегда, перестаёт что-либо
    значить, и настоящую нехватку рядом уже никто не заметит.
    """
    t = ROW.read_text(encoding="utf-8")
    setup = t[t.index('class="amap-setup"'): t.index("</td>", t.index('class="amap-setup"'))]
    assert "not in the workbook" in setup, "аккаунт вне книги снова назван недонастроенным"
    assert "r.in_workbook" in setup, "неоткуда узнать, знает ли книга этот аккаунт"
    # Настоящая недонастройка никуда не делась: книга знает и не считает готовым.
    assert "needs setup" in setup


def test_the_row_carries_whether_the_workbook_knows_the_account():
    """Признак считается на сервере: шаблон не должен гадать по прочерку.

    Пустые числа бывают и у аккаунта из книги — просто за выбранные дни.
    """
    source = (ROOT / "src" / "web" / "app.py").read_text(encoding="utf-8")
    assert '"in_workbook"' in source and "_workbook_has_account(df, country_name, label)" in source
