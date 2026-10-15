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
    """Форма на карте читает номер кабинета сама; вторая просила его руками.

    Стережётся возвращение той формы, а не всякое упоминание подключения.
    Ящик билдера показывает у себя кнопку «+ Account» — но за ней стоит эта
    же форма, физически перенесённая из карты и возвращаемая на место; своих
    полей у ящика нет, и проверяется это здесь же, ниже.
    """
    # Без комментариев: объяснение убранного — не его возвращение. Сторож,
    # читающий комментарий вместо кода, ловит собственный текст.
    import re as _re
    t = PAGE.read_text(encoding="utf-8")
    t = _re.sub(r"\{#.*?#\}", "", t, flags=_re.S)
    t = _re.sub(r"/\*.*?\*/", "", t, flags=_re.S)
    assert "Connect an account to" not in t, "второе место подключения вернулось"
    # Поля той формы — по одному экземпляру на страницу: она одна.
    for field in ("amap-ml-email", "amap-known", "amap-find"):
        assert t.count('class="' + field) + t.count(" " + field + '"') <= 2, (
            f"поле {field} встречается больше одного раза — форм подключения снова несколько"
        )


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
    # Проверка `ncols == cols` убрана вместе с книжной строкой раскрытия
    # (12.09): у живого дерева colspan берётся из самой строки и разойтись
    # ему не с чем.


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


def test_the_row_carries_whether_the_workbook_knows_the_account():
    """Признак считается на сервере: шаблон не должен гадать по прочерку.

    Пустые числа бывают и у аккаунта из книги — просто за выбранные дни.
    """
    source = (ROOT / "src" / "web" / "app.py").read_text(encoding="utf-8")
    assert '"in_workbook"' in source and "_workbook_has_account(df, country_name, label)" in source


def test_the_setup_column_is_replaced_by_a_note_column():
    """ACL-1620: «ready / needs setup» убрано, на его месте заметка менеджера.

    Поля r.ready / r.to_do остаются в данных, но в таблицу больше не выводятся.
    """
    t = ROW.read_text(encoding="utf-8")
    head = t[t.index("<thead>"): t.index("</thead>")]
    body = t[t.index("<tbody>"): t.index("</tbody>")]
    assert ">Setup<" not in head and "amap-setup" not in t
    assert ">Note<" in head and 'class="amap-acct-note' in body
    assert "needs setup" not in body and "r.to_do" not in body
