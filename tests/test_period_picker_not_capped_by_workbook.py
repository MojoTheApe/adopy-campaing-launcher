"""Книга не должна ограничивать выбор периода.

Ошибка, ради которой написан этот файл: 8 сентября в календаре нельзя было
выбрать «Today», «Yesterday» и «Today and yesterday» — они гасли, потому что
книга статистики заканчивалась 6-м. Кнопка Read при этом ходит не в книгу, а
в Ads Manager, где вчера и сегодня есть всегда.

Два источника, одно ограничение — вот в чём была ошибка. Книга говорит, что
знает дешборд, а не что можно спросить у аккаунта. Тест держит границу между
ними: «нет строк» разрешено помечать, но не запрещать.
"""
from __future__ import annotations

import re
from pathlib import Path

TPL = Path(__file__).resolve().parent.parent / "src" / "web" / "templates" / "report.html"


def _picker() -> str:
    """Кусок шаблона от пресетов до конца рисовки календаря."""
    t = TPL.read_text(encoding="utf-8")
    start = t.index("function amapPresets(")
    end = t.index("[data-apply]", start)
    return t[start:end]


def test_no_preset_or_day_is_disabled_by_the_workbook():
    """Ни один атрибут disabled не попадает в разметку календаря.

    Проверка идёт по строкам, а не по окну в N символов вокруг слова: у
    прошлого сторожа (drill) было именно окно, и он молчал, потому что до
    искомого слова не дотягивался. Отсутствие срабатывания легко принять за
    отсутствие ошибки — поэтому здесь ищется сам выводимый атрибут.
    """
    bad = [ln.strip() for ln in _picker().splitlines() if "' disabled" in ln]
    assert not bad, (
        "период снова ограничен книгой — а спенд читается с аккаунта:\n"
        + "\n".join(bad)
    )


def test_presets_are_not_clipped_to_the_last_day_of_the_workbook():
    """`cut` когда-то подрезал конец пресета последним днём книги.

    Из-за этого «This month» на отставшей книге кончался позавчера, а
    «Today» превращался в пустой период и гас.
    """
    body = _picker()
    m = re.search(r"var cut = function \(iso\) \{ return ([^;]+); \};", body)
    assert m, "функция cut исчезла — проверьте, чем её заменили"
    assert m.group(1).strip() == "iso", (
        "cut снова что-то подрезает: " + m.group(1)
    )


def test_a_day_without_rows_is_marked_not_forbidden():
    """Пометка нужна: человек должен видеть, что дешборд про день не знает."""
    body = _picker()
    assert "' thin'" in body, "исчезла пометка дней и пресетов без данных в книге"


def test_an_empty_period_still_shows_the_account_map():
    """Пустой период не должен прятать то, что от книги не зависит.

    Выбрав сегодняшний день, человек получал «No country data found. Check
    Excel path and filters» — диагноз про здоровый файл — и вместе с отчётом
    терял карту аккаунтов, а на ней кнопку Read. Именно она читает Ads
    Manager, а не книгу, и именно ради неё сегодняшний день и выбирают: экран
    прятал ровно то, что продолжало работать.
    """
    # Проверяется отрисовка, а не текст шаблона. Прежняя версия искала строки
    # в файле — и осталась зелёной, когда блок перестал отрисовываться вовсе:
    # страна из связей заполнила список, условие блока стало ложным, а строки
    # в файле никуда не делись. Условия этого блока живут в
    # tests/test_sidebar_without_workbook.py, где их отрисовывают.
    t = TPL.read_text(encoding="utf-8")
    start = t.index("{%- set book_covers_period")
    empty = t[start : t.index("<script>", start)]
    assert "_account_map.html" in empty, "карта аккаунтов снова спрятана пустым периодом"
    assert "account_map.date_from" in empty, (
        "пустой отчёт снова не называет период — а человек пойдёт чинить файл"
    )


def test_a_failed_read_names_its_reason_in_the_cell():
    """Неудача должна выглядеть неудачей с первого взгляда.

    8 сентября чтение за 5–7 сентября сорвалось: строка итогов ещё не успела
    появиться. На экране осталась кнопка «failed · retry», причина — только в
    подсказке, а про сохранённый снимок узнать было неоткуда. Это читалось как
    «ничего не произошло».
    """
    t = TPL.read_text(encoding="utf-8")
    fn = t[t.index("function amapSpendFailed"):]
    fn = fn[: fn.index("\nfunction ")]
    assert "cell.innerHTML" in fn, "причина снова только в подсказке к кнопке"
    assert "снимок" in fn, "снимок экрана снова негде увидеть"


def test_country_blocks_are_collapsed_by_default():
    """Открытость блока не должна зависеть от места страны в списке.

    Было `{% if loop.index <= 3 %}open{% endif %}`: первые три страны всегда
    раскрыты, остальные нет. На экране это выглядит как случайность — правила
    не видно, а порядок стран им управляет. Сводка в заголовке отвечает на
    вопрос «что здесь» без раскрытия.
    """
    t = (TPL.parent / "_account_map.html").read_text(encoding="utf-8")
    # Ищем без закрывающей кавычки: в классе появилось условие
    # (`amap-country{% if not mc.rows %} amap-nocov{% endif %}`), и точная
    # строка перестала находиться. Смысл проверки от этого не изменился.
    block = t[t.index("<details class=\"amap-country"):]
    block = block[: block.index("</summary>")]
    assert "open" not in block.replace("amap-openprof", ""), (
        "блок страны снова раскрыт разметкой: " + block[:200]
    )
    assert "data-country" in block, "исчез ключ, по которому помнится раскрытое"
