"""Страну, у которой есть расход и нет аккаунтов, можно наполнить на месте.

В карте такая страна появляется потому, что книга знает про её деньги, а
связей у неё нет. Раньше вместо формы подключения там стоял текст:
«подключите аккаунт из раздела Accounts этой страны, или заведите страну
сверху и поместите её» — то есть человека отправляли искать другое место,
чтобы сделать то, что он уже открыл.

Привязка заводит страну сама — `account_links.link` просто добавляет строку,
и страна начинает существовать. Значит отдельный шаг «создать» не нужен, и
форма может быть той же самой.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TPL = ROOT / "src" / "web" / "templates"
MAP = TPL / "_account_map.html"
COUNTRY = TPL / "_account_map_country.html"
ADD = TPL / "_account_map_add.html"


def test_the_form_exists_as_one_piece():
    """Две копии разошлись бы: их правят в разное время и по разным поводам."""
    assert ADD.is_file(), "общего куска с формой нет"
    body = ADD.read_text(encoding="utf-8")
    for control in ("amap-known", "amap-inst", "amap-find", "amapLink", "amapFind"):
        assert control in body, f"в форме нет {control}"


def test_both_places_use_it():
    assert '{% include "_account_map_add.html" %}' in MAP.read_text(encoding="utf-8")
    assert '{% include "_account_map_add.html" %}' in COUNTRY.read_text(encoding="utf-8")


def test_neither_place_keeps_its_own_copy():
    """Сторож: вернуть копию — вернуть расхождение.

    Ищется именно форма — `div.amap-add` со ссылкой на страну. Класс
    `amap-add` носит ещё и верхняя кнопка «Add country»: она про раскладку,
    а не про подключение, и трогать её незачем.
    """
    for f in (MAP, COUNTRY):
        body = f.read_text(encoding="utf-8")
        assert '<div class="amap-add" data-amap-country=' not in body, (
            f"{f.name} снова несёт свою копию формы"
        )


def test_a_country_from_the_map_links_by_its_key():
    """Ключ и имя различаются, когда в карте лежит код («KW»), а в отчёте имя."""
    assert "{% with amap_value = mc.key %}" in COUNTRY.read_text(encoding="utf-8")


def test_a_country_from_the_workbook_links_by_its_name():
    """У неё ключа ещё нет: она существует только расходом в книге."""
    assert "{% with amap_value = mc.country %}" in MAP.read_text(encoding="utf-8")


def test_the_dead_end_text_is_gone():
    """Текст отправлял в другое место вместо того, чтобы дать сделать здесь.

    Проверяется только ветка страны из книги. Про раздел Accounts упоминает
    ещё и сообщение «No countries yet» — там совет уместен: подключать
    действительно негде, пока нет ни одной страны.
    """
    # Render the virtual-country branch instead of slicing at source-specific prose.
    branch = _render(virtual=True)
    assert "Accounts section" not in branch, "совет идти в другой раздел остался"
    assert "Connect one below" in branch, "не сказано, что подключить можно тут же"
    assert 'data-amap-country="Guatemala"' in branch and 'class="amap-add"' in branch


# ── отрисовка ─────────────────────────────────────────────────────────────

def _render(virtual: bool):
    """Отрисовать карту с одной страной — настоящей или только из книги."""
    from jinja2 import Environment, FileSystemLoader

    # Фильтры берём настоящие, а не свои похожие: карта форматирует суммы
    # по валютам, и окружение, знающее меньше фильтров, чем приложение,
    # падает на шаблоне, который на экране работает.
    from src.analysis_agent.money_format import fmt_usd, fmt_currency, fmt_summary_amount

    env = Environment(loader=FileSystemLoader(str(TPL)), autoescape=True)
    env.filters["fmt_usd"] = fmt_usd
    env.filters["fmt_currency"] = fmt_currency
    env.filters["fmt_summary_amount"] = fmt_summary_amount
    country = {
        "country": "Guatemala", "key": "" if virtual else "GT", "virtual": virtual,
        "rows": [], "spend": 1894, "deposits": 4, "leads": 82,
        "live_sums": [], "live_read": 0, "book": {},
    }
    return env.get_template("_account_map.html").render(
        amap={"countries": [country], "available": [], "all_accounts": ["FacebookDQ438"],
              "dates": [], "date_from": "", "date_to": "", "span": ""},
        mlx={"A": "Inhouse", "B": "Agency"},
    )


def test_the_workbook_only_country_gets_the_form():
    """То, ради чего правка: в Гватемале есть чем подключить аккаунт."""
    out = _render(virtual=True)
    assert 'class="amap-add"' in out, "формы нет — подключить по-прежнему негде"
    assert 'data-amap-country="Guatemala"' in out, "форма не знает, к чему привязывать"
    assert "already connected" in out


def test_a_real_country_still_gets_its_own():
    out = _render(virtual=False)
    assert 'data-amap-country="GT"' in out, "страна из карты привязывается не по ключу"


def test_this_check_would_notice_the_old_behaviour():
    """Обратная проверка: до правки формы во второй ветке не было вовсе."""
    body = MAP.read_text(encoding="utf-8")
    without = body.replace(
        '{% with amap_value = mc.country %}{% include "_account_map_add.html" %}{% endwith %}', ""
    )
    assert 'amap_value = mc.country' not in without


# ── вложенность ───────────────────────────────────────────────────────────
#
# Форму вынесли в отдельный кусок, а её закрывающий `</div>` остался в
# _account_map_country.html: вырезали только открывающий тег и содержимое,
# последняя строка хунка не попала в правку. Проверки выше остались зелёными —
# они спрашивали, есть ли в выводе нужные поля, а не цел ли он.
#
# В браузере лишний `</div>` не игнорируется: парсер выкидывает элементы,
# пока не выкинет какой-нибудь div, и вместе с ним уносит `<main>`. Карта
# вывалилась из своей секции, все страновые разделы стали детьми `<body>` —
# страница показывала одну строку Колумбии и пустоту под ней.

def _div_depth_low_and_final(html: str):
    """Наименьшая глубина и конечная — по настоящему разбору, не по счёту строк."""
    from html.parser import HTMLParser

    class _Depth(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.depth = 0
            self.low = 0

        def handle_starttag(self, tag, attrs):
            if tag == "div":
                self.depth += 1

        def handle_endtag(self, tag):
            if tag == "div":
                self.depth -= 1
                self.low = min(self.low, self.depth)

    p = _Depth()
    p.feed(html)
    return p.low, p.depth


def test_the_rendered_map_closes_exactly_what_it_opens():
    for virtual in (False, True):
        low, final = _div_depth_low_and_final(_render(virtual=virtual))
        assert low == 0, (
            f"virtual={virtual}: лишний </div> — браузер вынесет карту из её секции"
        )
        assert final == 0, f"virtual={virtual}: незакрытый <div> — секция съест следующую"


def test_the_form_stays_inside_the_country_it_belongs_to():
    """Тот самый признак поломки: форма оказывалась снаружи своей строки."""
    out = _render(virtual=False)
    block = out[out.index("<details"): out.index("</details>")]
    assert 'data-amap-country="GT"' in block, "форма вывалилась из своей страны"
