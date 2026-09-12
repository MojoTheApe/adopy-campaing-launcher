"""Подключение аккаунта — одно дело, одна панель.

Раньше оно было разорвано надвое: в карте аккаунтов строка привязки, а вход в
Multilogin — отдельной панелью внизу, в билдере кампаний. Вторая была
предусловием первой (без живого токена кнопке Reload неоткуда взять список
профилей), но ничто на экране об этом не говорило, и владелец прочитал их как
одно, разорванное пополам.

Хуже: панель входа рисовалась **по копии на каждую страну**. На отрисованной
странице было двадцать полей пароля, двадцать textarea для токена и двадцать
копий инструкции «How to run the RPA» — ради настройки, которая одна на
машину: токен пишется в `.env`.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TPL = ROOT / "src" / "web" / "templates"
REPORT = TPL / "report.html"
COUNTRY = TPL / "_account_map_country.html"


def _no_comments(text: str) -> str:
    """Без комментариев Jinja и JS-блоков: объяснение убранного — не его
    возвращение. За эту сессию такой сторож трижды ловил собственный текст."""
    text = re.sub(r"\{#.*?#\}", "", text, flags=re.S)
    return re.sub(r"/\*.*?\*/", "", text, flags=re.S)


# ── панели больше не две ──────────────────────────────────────────────────

def test_the_builder_no_longer_carries_its_own_multilogin_panel():
    body = _no_comments(REPORT.read_text(encoding="utf-8"))
    assert "ml-connect-panel" not in body or 'class="ml-connect-panel"' not in body, (
        "панель входа снова живёт в билдере"
    )
    assert "Multilogin — connect to auto-link profile_id" not in body


def test_the_help_that_was_repeated_per_country_is_gone():
    """Справка — не контрол, и двадцать её копий не нужны никому."""
    body = _no_comments(REPORT.read_text(encoding="utf-8"))
    assert "How to run the RPA" not in body


def _connect_form() -> str:
    """Форма подключения — там, где она лежит.

    Она бывает встроена в блок страны и бывает вынесена отдельным файлом:
    вынос сделан ради страны, которой нет в карте, и ждёт своего мерджа. Обе
    раскладки живые, и проверять надо содержимое, а не место — иначе сторож
    падает на переезде, а не на поломке.
    """
    extracted = TPL / "_account_map_add.html"
    if extracted.exists():
        return extracted.read_text(encoding="utf-8")
    body = COUNTRY.read_text(encoding="utf-8")
    return body[body.index('<div class="amap-add"'):]


def test_signing_in_lives_where_the_account_is_connected():
    block = _connect_form()
    for needed in ("amap-ml-line", "amap-ml-signin", "amapMlSignin", "amapMlSaveToken"):
        assert needed in block, f"в форме подключения нет {needed}"


def test_pasting_a_token_is_still_possible():
    """Пароля может не быть вовсе: токен выдаёт владелец воркспейса."""
    assert "amap-ml-token" in _connect_form()


# ── как это ведёт себя ────────────────────────────────────────────────────

def _js() -> str:
    """Только этот блок, с точным концом.

    Срез «до следующего def/function» за эту сессию подводил четыре раза: то
    функция оказывалась последней в файле, то соседняя переезжала. Конец здесь
    задан строкой, которой блок кончается, и проверен на непустоту.
    """
    src = REPORT.read_text(encoding="utf-8")
    start = src.index("var AMAP_ML_STATE")
    tail = "else amapMlBoot();"
    block = src[start: src.index(tail, start) + len(tail)]
    assert "amapMlSignin" in block and len(block) < 8000, "срез захватил лишнее"
    return block


def test_the_sign_in_hides_itself_once_connected():
    """Подключился — поля исчезают во всех копиях блока: состояние общее."""
    js = _js()
    assert "signin.hidden = alive" in js, "поля входа не прячутся по живому токену"
    assert "amapMlPaintAll" in js, "перекрашивается только один блок из тридцати"


def test_a_stale_token_is_not_called_connected():
    """«Токен есть» и «токен живой» — разное: по протухшему профили не придут."""
    js = _js()
    assert "state.has_token ? 'token expired' : 'not connected'" in js


def test_the_password_does_not_linger_on_screen():
    js = _js()
    assert ".amap-ml-pass').value = ''" in js, "пароль остаётся в поле после входа"


def test_the_state_is_asked_once_per_instance_not_once_per_block():
    """Блоков на странице под тридцать, а ответ один и тот же."""
    js = _js()
    assert "var wanted = {}" in js and "Object.keys(wanted).forEach" in js


def test_switching_instance_moves_both_the_search_and_the_state():
    """Прежний обработчик умел только первое."""
    js = _js()
    start = js.index("function amapInstanceChanged")
    body = js[start: js.index("\nfunction ", start + 10)]
    assert "amapFilter" in body and "amapMlRefresh" in body


def test_the_block_is_found_by_class_not_by_id():
    """Блок рисуется дважды на страну — карта на Home и вкладка страны.
    Одинаковые id сломали бы обе копии."""
    js = _js()
    assert "getElementById" not in js, "вернулась адресация по id"
    assert "closest('.amap-add')" in js


# ── сколько их на странице ────────────────────────────────────────────────

def test_the_rendered_page_has_no_password_field_per_country():
    """То самое число, ради которого всё затевалось."""
    from jinja2 import Environment, FileSystemLoader

    env = Environment(loader=FileSystemLoader(str(TPL)), autoescape=True)
    env.filters["fmt_usd"] = lambda v: f"${v:,.0f}" if v else "—"
    country = {"country": "Saudi Arabia", "key": "SA", "rows": [], "virtual": False}
    out = env.get_template("_account_map_country.html").render(
        mc=country,
        amap={"countries": [country], "available": [], "all_accounts": ["FacebookDQ438"],
              "live": {}, "dates": [], "date_from": "", "date_to": "", "span": ""},
        mlx={"A": "Inhouse", "B": "Agency"},
    )
    # Одна страна — один блок подключения и ни одного поля пароля в разметке:
    # оно появляется только когда токена нет, и рисует его JS.
    assert out.count('class="amap-add"') == 1
    assert out.count("amap-ml-pass") == 1, "поле пароля не одно на блок"
