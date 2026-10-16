"""Аккаунт подключается одинаково в обоих местах — и это место сильное.

Было два разных способа. В разделе страны — поиск профиля по последним буквам
через Multilogin, и у каждой находки две кнопки: Open (запустить профиль) и
Connect. Так человек и работает: нашёл, открыл, посмотрел в самом кабинете что
нужно, подключил.

Во вкладке Accounts — выпадающий список, и в нём лежали ТОЛЬКО открытые
профили. Не открыт — выбрать нечем, остаётся вписать имя и номер кабинета
руками. Открыть профиль оттуда было нельзя.

На этом встал аккаунт KLD_BFL_X84: в карте его нет, панель билдера пишет
«needs setup», а подключить неоткуда — профиль не запущен.

Решение владельца развернуло прежнее направление (задача #151 предлагала
обратное — чтобы карта равнялась на вкладку). Равняться надо на сильное место,
а сильное здесь — раздел страны.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TPL = ROOT / "src" / "web" / "templates"
REPORT = TPL / "report.html"


def _section() -> str:
    page = REPORT.read_text(encoding="utf-8")
    start = page.index('<section class="acc-connect">')
    return page[start: page.index("</section>", start)]


def _render(amap_value: str) -> str:
    from jinja2 import Environment, FileSystemLoader

    env = Environment(loader=FileSystemLoader(str(TPL)), autoescape=True)
    return env.get_template("_account_map_add.html").render(
        amap_value=amap_value,
        amap={"all_accounts": ["FacebookDQ438", "KLD_BFL_Y65"]},
        mlx={"A": "Inhouse", "B": "KLD"},
    )


# ── одна форма на два места ───────────────────────────────────────────

def test_the_accounts_tab_uses_the_same_form_as_the_map():
    """Третий вариант формы разошёлся бы с двумя первыми — два похожих
    места уже однажды разошлись."""
    section = _section()
    assert '{% include "_account_map_add.html" %}' in section, (
        "вкладка Accounts снова рисует собственную форму подключения"
    )
    assert "amap_value = ''" in section, "форма отдана со страной, которой здесь нет"


def test_the_old_open_profiles_only_picker_is_gone():
    """Выпадающий список открытых профилей и был поломкой: профиль, который
    не запущен, выбрать нечем, а открыть его оттуда нельзя."""
    section = _section()
    assert 'id="ac-new-prof"' not in section
    assert "Pick an open profile" not in section


def test_finding_and_opening_are_offered_where_connecting_happens():
    out = _render("")
    assert "amap-find" in out, "поиска профиля нет"
    assert "amap-hits" in out, "находкам негде появиться"
    # Кнопку рисует общий обработчик — проверяем, что он тот самый. С ACL-1457
    # она одна: Connect сам открывает профиль и подключает.
    page = REPORT.read_text(encoding="utf-8")
    hits = page[page.index("function amapRenderHits"):]
    hits = hits[: hits.index("\n/* Открыть найденный профиль")]
    assert "'Connect'" in hits and "amapOpenAndConnect" in hits
    assert "'Open'" not in hits, "отдельная кнопка Open вернулась"


def test_the_manual_path_keeps_its_own_button():
    """Запасной путь без кнопки — это не запасной путь."""
    section = _section()
    manual = section[section.index('id="ac-manual"'):]
    assert 'id="ac-add-btn"' in manual, "кнопка ручного ввода потерялась"
    assert "Connect by hand" in manual


# ── страна ────────────────────────────────────────────────────────────

def test_without_a_country_there_is_nothing_to_attach_to():
    """Выпадающий список из тринадцати имён с кнопкой Add означал бы во
    вкладке Accounts действие, которого не существует: привязывать не к чему.
    """
    out = _render("")
    assert "already connected" not in out
    assert 'data-amap-country=""' in out
    assert "amap-find" in out, "поиск профиля к стране не привязан и должен остаться"


def test_inside_a_country_the_attachment_stays():
    out = _render("Mexico")
    assert "already connected" in out
    assert 'data-amap-country="Mexico"' in out
    assert "FacebookDQ438" in out


def test_the_optional_country_reaches_the_shared_form():
    """У общей формы один источник страны — её атрибут. Второй путь передачи
    означал бы, что в карте и здесь она однажды начнёт браться по-разному."""
    page = REPORT.read_text(encoding="utf-8")
    fn = page[page.index("window.acCountryChanged"):]
    fn = fn[: fn.index("\n  };") + 5]
    assert "#view-accounts .amap-add" in fn, "поле страны пишет не в ту форму"
    assert "setAttribute('data-amap-country'" in fn

    section = _section()
    assert 'oninput="acCountryChanged(this)"' in section, "поле страны ни с чем не связано"


def test_connect_reads_the_country_at_the_moment_of_the_click():
    """Иначе набранное после отрисовки формы не доедет."""
    page = REPORT.read_text(encoding="utf-8")
    fn = page[page.index("async function amapConnect"):]
    fn = fn[: fn.index("\nasync function ", 10)] if "\nasync function " in fn[10:] else fn[:4000]
    assert "box.getAttribute('data-amap-country')" in fn


# ── что человеку сказано ──────────────────────────────────────────────

def test_the_page_says_why_a_new_profile_has_nothing_to_read():
    """Ad account читается из истории профиля на этой машине. У профиля,
    который здесь не открывали, её нет — и это надо сказать до нажатия, а не
    оставить человека гадать, почему поле пустое."""
    # Пробелы схлопываем: фраза переносится по строкам в шаблоне, и проверка
    # по точной строке ловила бы вёрстку, а не смысл.
    flat = " ".join(_section().split())
    assert "never been opened on this machine has no history to read" in flat
    assert "open it, and it will" in flat


def test_the_heading_describes_the_new_way():
    section = _section()
    # ACL-1151: один выбор — профиль; «последних букв» больше нет.
    assert re.search(r"Pick a profile", section), "подпись не говорит, что выбирают профиль"
    assert "last letters" not in section, "подпись всё ещё просит последние буквы"
