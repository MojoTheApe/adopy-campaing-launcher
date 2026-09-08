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


def test_what_is_left_to_do_moved_into_the_row():
    t = ROW.read_text(encoding="utf-8")
    assert "r.to_do" in t, "«что сделать» негде посмотреть"
    assert "r.ready" in t, "готовность аккаунта негде посмотреть"


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
    cell = t[t.index("amap-live"): t.index("amap-todo")]
    assert "state == 'failed'" in cell, "отказ снова показывается прочерком"
    assert "sr.error" in cell, "причина не выводится"
    assert "sr.shot" in cell, "снимок опять негде найти"
