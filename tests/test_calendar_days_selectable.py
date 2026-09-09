"""День без строк в книге должен выглядеть выбираемым — он и есть выбираемый.

9 сентября: «почему-то в дате нельзя проставить вчерашний день». Проверено на
живом дешборде — можно: кнопка не `disabled`, клик применяется, адрес
становится `?date_from=2026-09-08&date_to=2026-09-08`. Мешал только вид.

Дни и пресеты без строк в книге красились приглушённым цветом с `opacity: .55`
— тем же языком, каким рядом говорит `:disabled`. Человек прочёл ровно то, что
было написано, и был прав: так выглядит запрет.

Разница по существу. Книга статистики говорит, **что знает дешборд**, а не что
можно спросить у аккаунта: кнопка Read ходит в Ads Manager, где вчера и сегодня
есть всегда, а книга отстаёт на день-два почти каждый день. Гасить выбор по
книге — значит связывать два источника одним ограничением.

Проверяется здесь стиль, потому что сломано было именно в нём. Что день
кликабелен, держит соседняя проверка периода.
"""
from __future__ import annotations

import re
from pathlib import Path

TPL = Path(__file__).resolve().parent.parent / "src" / "web" / "templates" / "report.html"


def _css(selector: str) -> str:
    """Тело правила для селектора — или пустая строка."""
    t = TPL.read_text(encoding="utf-8")
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", t)
    return m.group(1) if m else ""


def test_a_day_without_workbook_rows_is_not_dimmed():
    """Приглушение — язык запрета. Здесь запрета нет."""
    body = _css(".amap-cal-grid button.thin")
    assert "opacity" not in body, "день без данных снова притушен — читается как выключенный"
    assert "color:" not in body, "цвет текста снова отличается от обычного дня"


def test_a_preset_without_workbook_rows_is_not_dimmed():
    body = _css(".amap-cal-presets button.thin")
    assert "color:" not in body, "пресет снова красится как выключенный"


def test_the_absence_of_data_is_still_marked():
    """Убрать приглушение — не значит скрыть, что данных нет.

    Иначе человек выберет день и будет искать пропавшие числа, вместо того
    чтобы знать заранее: их не будет, а спенд прочитается.
    """
    assert _css(".amap-cal-grid button.thin::after"), "у дня без данных не осталось никакой пометки"
    assert "dashed" in _css(".amap-cal-presets button.thin i"), "у пресета пропал пунктир"


def test_the_mark_stays_visible_on_the_selected_day():
    """Выбранный день красится акцентным фоном.

    Точка постоянного цвета на нём исчезла бы, и метка пропала бы ровно тогда,
    когда на день смотрят.
    """
    assert "currentColor" in _css(".amap-cal-grid button.thin::after")


def test_only_disabled_looks_disabled():
    """Единственный язык запрета — зачёркнутый серый. Он остаётся за `:disabled`."""
    for selector in (".amap-cal-grid button:disabled", ".amap-cal-presets button:disabled"):
        assert "line-through" in _css(selector), f"{selector} перестал выглядеть запретом"
    for selector in (".amap-cal-grid button.thin", ".amap-cal-presets button.thin"):
        assert "line-through" not in _css(selector), f"{selector} заговорил языком запрета"


def test_no_preset_or_day_is_rendered_disabled():
    """Тот же вопрос, но про разметку: гасить по книге нельзя вовсе.

    Подрезку по книге уже убирали однажды; проверка держит её убранной.
    """
    t = TPL.read_text(encoding="utf-8")
    block = t[t.index("function amapPresets"): t.index("function amapCalendar")]
    assert "disabled" not in block, "пресеты снова гасятся"
    grid = t[t.index("function monthHtml"): t.index("function draw()")]
    assert "disabled" not in grid, "дни снова гасятся"
