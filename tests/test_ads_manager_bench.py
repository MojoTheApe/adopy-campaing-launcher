"""Стенд: настоящая разметка Ads Manager против нашего кода поиска элементов.

`meta_dom` — 3175 строк при 6% покрытия, и 103 из 136 его функций принимают
страницу. Подделка тут не годится: ломается не наш код сам по себе, а
**совпадение наших селекторов с разметкой Meta**, а двойник соглашается с чем
угодно, что мы написали. Проверять надо настоящую страницу.

Разметка взята из снимка живого прогона 9 сентября — того самого, где
`placements` упал пятый раз подряд. Вычищена и вырезана до раздела: 106 КБ
вместо четырёх мегабайт.

**Чего стенд не проверяет.** Снимок статический: скриптов Meta в нём нет,
галочка не вызовет обработчиков и состояние не изменится. То есть здесь
проверяется **нахождение** контролов, а не их поведение — и это ровно тот вид
поломки, который у нас случается: пять отказов подряд, потому что искали
формулировки, которых на странице больше нет.

Стареет ли фикстура. Да, и это её работа: когда Meta переделает раздел снова,
проверка останется зелёной, а живой прогон упадёт. Поэтому снимок датирован
именем файла, и рядом с каждым отказом на живой машине теперь ложится свежий —
заменить фикстуру дешевле, чем разбираться заново.
"""
from __future__ import annotations

import sys
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ads_manager"


def _chromium():
    """Локальный Chromium. К рекламному аккаунту отношения не имеет.

    Это не профиль Multilogin и не живая страница: обычный браузер открывает
    файл с диска. Правила про прокси сюда не относятся — открывать нечего,
    кроме сохранённой разметки.
    """
    from src.fb_ads_rpa.config import ensure_playwright_browsers_path

    ensure_playwright_browsers_path()
    from playwright.sync_api import sync_playwright

    return sync_playwright()


@pytest.fixture(scope="module")
def browser():
    """Один браузер на весь файл.

    Синхронный Playwright не терпит двух обёрток в одном потоке — вторая
    падает прямо в `sync_playwright()`. Поэтому обёртка тут одна, а страницы
    от неё раздаются.
    """
    with _chromium() as pw:
        b = pw.chromium.launch(headless=True, executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        try:
            yield b
        finally:
            b.close()


def _open(browser, name="placements_2026_09_09.html"):
    pg = browser.new_page()
    # No fixture can load remote images, styles or scripts from a real account.
    pg.route("**/*", lambda route: route.continue_() if route.request.url.startswith("file:") else route.abort())
    pg.goto((FIXTURES / name).as_uri())
    return pg


@pytest.fixture(scope="module")
def page(browser):
    """Страница только для чтения — её состояние никто не меняет."""
    return _open(browser)


@pytest.fixture(scope="module")
def ad_page(browser):
    """Раздел объявления: назначение и креатив, из того же снимка."""
    return _open(browser, "ad_copy_2026_09_09.html")


# ── что на странице есть на самом деле ────────────────────────────────────

def test_the_placements_section_is_there():
    """Сначала убеждаемся, что фикстура вообще про то, о чём мы говорим."""
    html = (FIXTURES / "placements_2026_09_09.html").read_text(encoding="utf-8")
    assert "Placements" in html and "Placement controls" in html


def test_the_old_wording_is_gone(page):
    """То, что искал наш код до 9 сентября.

    Пять отказов подряд объясняются этой строкой: раздел искали по
    формулировкам, которых на странице нет ни одной.
    """
    for gone in ("Advantage+ placements", "Manual placements"):
        assert page.get_by_text(gone, exact=True).count() == 0, (
            f"{gone!r} снова на странице — фикстура устарела, а проверка врёт"
        )


def test_the_section_is_already_expanded(page):
    """«Hide settings» значит, что раздел открыт.

    Наш код первым делом жмёт «Show more settings». Обе надписи есть в
    разметке, и это первое, что надо развести: клик по «показать» на уже
    открытом разделе его закроет.
    """
    assert page.get_by_text("Hide settings", exact=True).count() >= 1


def test_the_category_ids_we_relied_on_do_not_exist(page):
    """`ads_placement_expandable_list_item_{feed,story,stream}`.

    По ним код разворачивал три категории. Meta перешла на порождаемые
    идентификаторы — `js_14p`, `_r_d2_`, — и они меняются от отрисовки к
    отрисовке. Цепляться за них нельзя больше никогда.
    """
    html = (FIXTURES / "placements_2026_09_09.html").read_text(encoding="utf-8")
    assert "ads_placement_expandable_list_item" not in html


def test_the_categories_are_findable_by_their_words(page):
    """Единственная опора, которая осталась, — текст рядом с контролом.

    Так ищет человек, и так должен искать код.
    """
    for label in ("Feeds", "Stories, Status, Reels", "Search results", "Apps and sites"):
        assert page.get_by_text(label, exact=True).count() >= 1, f"{label!r} не найден по тексту"


def test_each_category_has_a_checkbox_next_to_its_words(page):
    """Найти надпись мало — нужен контрол, который она подписывает."""
    for label in ("Feeds", "Search results", "Apps and sites"):
        box = page.locator("label").filter(has_text=label).locator("input[type=checkbox]")
        assert box.count() >= 1, f"у {label!r} не нашлось галочки внутри подписи"


def test_the_allow_limited_spending_checkbox_is_gone(page):
    """Последний шаг нашего кода снимал именно её.

    В разметке её нет. Значит шаг либо лишний, либо переехал — и оставлять
    его молча падающим нельзя.
    """
    assert page.get_by_text("Allow limited spending", exact=False).count() == 0


def test_the_categories_are_collapsible_buttons(page):
    """Раскрыватель категории — сама строка: `role=button` с `aria-expanded`.

    Кнопок «Expand» с подписью на странице нет ни одной — по ним код
    разворачивал категории раньше. Открывается строка целиком.
    """
    assert page.locator('[aria-label="Expand"]').count() == 0
    for name in ("Feeds", "Stories, Status, Reels", "Search results"):
        row = page.locator('[role="button"][aria-expanded]').filter(has_text=name)
        assert row.count() >= 1, f"у категории {name!r} нет раскрывателя"


def test_the_subitems_are_not_in_the_page_until_a_category_is_opened(page):
    """Восемнадцать подпунктов, которые проставлял наш код, в разметке отсутствуют.

    Проверено по полному снимку в 4 МБ: «Facebook Feed» там встречается, но
    **только внутри словаря переводов** в скрипте, не как строка формы.
    Значит категории свёрнуты, и до раскрытия ставить в них нечего.
    """
    for gone in ("Instagram feed", "Facebook Marketplace", "Facebook profile feed"):
        assert page.get_by_text(gone, exact=True).count() == 0


# ── наш код против этой разметки ──────────────────────────────────────────

@pytest.fixture
def fresh_page(browser):
    """Своя страница на тест: код ниже жмёт галочки и меняет состояние."""
    pg = _open(browser)
    try:
        yield pg
    finally:
        pg.close()


@pytest.fixture
def dom(monkeypatch):
    """`meta_dom` без человеческих пауз — на стенде ждать нечего."""
    from src.fb_ads_rpa import meta_dom

    monkeypatch.setattr(meta_dom, "_sleep_h", lambda *a, **k: None)
    return meta_dom


def test_the_five_categories_end_up_in_the_target_configuration(fresh_page, dom):
    """Главное: после прохода наши три категории включены, две выключены.

    Галочка в снимке — настоящий `<input type=checkbox>`, и браузер
    переключает её сам, без скриптов Meta. То есть состояние здесь
    проверяется по-настоящему, а не по факту «клик не бросил исключение».
    """
    report: dict = {}
    dom.fill_placements(fresh_page, report)

    def checked(name):
        box = fresh_page.locator("label").filter(has_text=name).locator("input[type=checkbox]")
        return box.first.is_checked()

    assert checked("Feeds") is True
    assert checked("Stories, Status, Reels") is True
    assert checked("In-stream ads for reels") is True
    assert checked("Search results") is False, "Search results остались включены"
    assert checked("Apps and sites") is False, "Apps and sites остались включены"


def test_an_already_open_section_is_not_closed_again(fresh_page, dom):
    """Раздел приходит раскрытым. Клик по «показать» его бы закрыл.

    Именно этим начинался прежний код, и это первое, что надо было развести.
    """
    before = fresh_page.locator("label").filter(has_text="Feeds").count()
    dom.fill_placements(fresh_page, report={})
    after = fresh_page.locator("label").filter(has_text="Feeds").count()
    assert after >= before >= 1, "категории пропали со страницы — раздел свернули"


def test_the_report_names_every_category_it_touched(fresh_page, dom):
    """Отказ должен говорить, что именно не нашлось.

    Голое `False` в телеметрии стоило пяти прогонов вслепую: шаг падал, а
    почему — приходилось выяснять заново каждый раз.
    """
    report: dict = {}
    dom.fill_placements(fresh_page, report)
    assert set(report.get("categories", {})) == {
        "Feeds",
        "Stories, Status, Reels",
        "In-stream ads for reels",
        "Search results",
        "Apps and sites",
    }


def test_each_kept_category_gets_its_expander_clicked(fresh_page, dom):
    """Подпункты не появятся, пока категория не раскрыта.

    Раскрыватель лежит **на седьмом уровне** над подписью — первая версия
    искала в шести и молча не находила ничего: `was: null` в отчёте, ноль
    проставленных подпунктов и никакого следа о причине.
    """
    report: dict = {}
    dom.fill_placements(fresh_page, report)
    for name in ("Feeds", "Stories, Status, Reels", "In-stream ads for reels"):
        got = report["expanded"][name]
        assert got.get("was") is not None, f"у {name!r} не нашёлся раскрыватель: {got}"


def test_the_report_lists_the_subitem_names_the_page_actually_has(fresh_page, dom):
    """Своими именами подпунктов страница расскажет то, чего мы не знаем.

    Названия восемнадцати подпунктов взяты из прогона в июле. Живы они или
    переименованы — проверить по свёрнутой разметке нельзя. Поэтому код
    переписывает в отчёт то, что нашёл на месте: одного упавшего прогона
    хватит, чтобы узнать настоящий список вместо гадания.
    """
    report: dict = {}
    dom.fill_placements(fresh_page, report)
    assert "subitems_seen" in report, "код не рассказывает, какие подпункты видит"


def _code_strings(source: str, func: str | None = None) -> str:
    """Строки, которые код **использует**, — без описаний в докстрингах.

    Разница существенная: удалённые имена в докстринге нужны — иначе через
    полгода их вернут, не зная, что они уже отваливались.
    """
    import ast

    tree: ast.AST = ast.parse(source)
    if func:
        tree = next(
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == func
        )
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                docs.add(doc)
    return "\n".join(
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value not in docs
    )


def test_no_removed_name_is_used_by_fill_placements_any_more():
    """Сторож: за порождаемые идентификаторы нельзя зацепиться даже случайно.

    Проверяется именно `fill_placements`. Соседний `select_manual_placements`
    ищет «Manual placements» до сих пор — это отдельный, более старый путь,
    и трогать его здесь не к месту.
    """
    source = (ROOT / "src" / "fb_ads_rpa" / "meta_dom.py").read_text(encoding="utf-8")
    used = _code_strings(source, "fill_placements")
    for gone in ("ads_placement_expandable_list_item", "Advantage+ placements", "Manual placements"):
        assert gone not in used, f"код снова ищет {gone!r} — на странице этого нет"


def test_the_guard_above_would_notice_a_real_use():
    """Обратная проверка: сторож, который ничего не ловит, — хуже отсутствующего."""
    probe = '''
def fill_placements(page):
    """Упоминание в описании: ads_placement_expandable_list_item."""
    return page.query_selector("#ads_placement_expandable_list_item_feed")
'''
    used = _code_strings(probe, "fill_placements")
    assert "ads_placement_expandable_list_item_feed" in used, "сторож не видит настоящего обращения"
    assert used.count("ads_placement_expandable_list_item") == 1, "описание всё-таки посчитано"


# ══ поля объявления: Primary text, Headline, ссылка ═══════════════════════
#
# Три отказа из шести в прогоне 9 сентября — `ad_copy`, `ad_destination` и их
# двойники на втором и третьем объявлении. Разметка ниже из того же снимка.

def test_the_old_ad_copy_locators_find_nothing(ad_page):
    """Ровно та причина, по которой шаг падал.

    Поле заголовка искали по `placeholder`, а основной текст — по роли
    `combobox` с именем. На странице нет ни одного и ни другого: подпись
    лежит **соседним div-ом**, а само поле — голая `<textarea>` без
    атрибутов.
    """
    assert ad_page.get_by_placeholder("Write a short headline").count() == 0
    assert ad_page.get_by_role("combobox", name="Tell people what your ad is about").count() == 0


def test_meta_marks_each_field_with_its_own_surface_name(ad_page):
    """Опора, которой у нас не было: Meta подписывает поля сама.

    `data-surface` — её собственная разметка для логов, и в отличие от
    классов и идентификаторов она осмысленная и не порождается заново.
    """
    for key in ("message_field", "headline_field", "link_description_field", "pce_website_url_field"):
        assert ad_page.locator(f'[data-surface$="/{key}"]').count() == 1, f"нет {key}"


def test_the_copy_fields_are_bare_textareas(ad_page):
    """Ни placeholder, ни role, ни aria-label — цепляться не за что.

    Поэтому запасной ход ищет по подписи рядом, а не по атрибуту поля.
    """
    for key in ("message_field", "headline_field"):
        ta = ad_page.locator(f'[data-surface$="/{key}"] textarea')
        assert ta.count() == 1
        for attr in ("placeholder", "role", "aria-label"):
            assert ta.first.get_attribute(attr) is None, f"{key}: у поля появился {attr}"


def test_the_destination_section_is_still_hidden_while_the_copy_is_filled(ad_page):
    """Порядок разделов, а не поломка: на шаге текстов назначение ещё скрыто.

    Здесь легко ошибиться, и я ошибся: по этому снимку я решил, что раздел
    назначения свёрнут и в этом причина отказа `ad_destination`. Снимок
    самого отказа показал обратное — там поле разложено и пусто (см.
    `test_the_destination_field_is_there_and_visible_when_its_turn_comes`).
    Раздел раскрывается позже, и `set_ad_destination_website` сам снимает с
    него `display: none` перед работой.

    Проверка остаётся: она держит порядок, на который опирается код, — на
    шаге текстов в поле ссылки писать нечего.
    """
    got = ad_page.evaluate(
        """() => {
          const one = (sel) => {
            const el = document.querySelector(sel);
            const r = el.getBoundingClientRect();
            let n = el, hidden = false;
            for (let i = 0; i < 60 && n; i++) {
              if (getComputedStyle(n).display === 'none') { hidden = true; break; }
              n = n.parentElement;
            }
            return {w: r.width, h: r.height, hidden};
          };
          return {
            url: one('[placeholder="Enter the URL people visit after your ad"]'),
            headline: one('[data-surface$="/headline_field"] textarea'),
          };
        }"""
    )
    assert got["url"]["hidden"] is True, "раздел назначения раскрылся — причина отказа была другой"
    assert got["url"]["w"] == 0
    assert got["headline"]["hidden"] is False and got["headline"]["h"] > 0, (
        "поля текста тоже не разложены — тогда виноват снимок, а не Meta"
    )


def test_the_code_finds_the_headline_and_the_primary_text(ad_page, dom):
    """То, ради чего всё: наш код находит оба поля в этой разметке."""
    for key, mark in (("message_field", "body"), ("headline_field", "title")):
        assert dom.mark_ad_copy_field(ad_page, key, None, mark), f"{key} не найден"
        found = ad_page.locator(f'[data-rpa-field="{mark}"]')
        assert found.count() == 1
        assert found.first.evaluate("el => el.tagName") == "TEXTAREA"


def test_the_field_is_still_found_when_meta_drops_its_surface_names(ad_page, dom):
    """Запасной ход по подписи — на случай, если `data-surface` уедет.

    Атрибут служебный: он для логов Meta, не для нас, и обещаний нам не
    давал. Один способ поиска на такое поле — это тот же самый отказ,
    отложенный на несколько месяцев.
    """
    ad_page.evaluate("""() => {
      for (const e of document.querySelectorAll('[data-surface]')) e.removeAttribute('data-surface');
    }""")
    try:
        assert ad_page.locator('[data-surface$="/headline_field"]').count() == 0
        assert dom.mark_ad_copy_field(ad_page, "headline_field", "Write a short headline", "title2")
        assert ad_page.locator('[data-rpa-field="title2"]').first.evaluate("el => el.tagName") == "TEXTAREA"
    finally:
        ad_page.reload()


# ══ ссылка объявления ═════════════════════════════════════════════════════
#
# Разметка из снимка момента, когда `ad_destination` упал, — не из снимка
# шага текстов: разделы раскрываются по очереди, и путать их моменты нельзя.

@pytest.fixture(scope="module")
def dest_page(browser):
    return _open(browser, "ad_destination_2026_09_09.html")


def test_the_old_destination_placeholder_is_gone(dest_page):
    """Причина отказа `ad_destination`, и она того же рода, что у соседей.

    Код искал `placeholder="http://www.example.com/page"`. На странице таких
    ноль: Meta заменила подсказку на «Enter the URL people visit after your
    ad». Третий шаг подряд, упавший из-за переписанной формулировки.
    """
    assert dest_page.locator('[placeholder="http://www.example.com/page"]').count() == 0
    assert dest_page.locator('[placeholder="Enter the URL people visit after your ad"]').count() == 1


def test_the_destination_field_is_there_and_visible_when_its_turn_comes(dest_page):
    """В момент своего отказа поле разложено и пусто — искать было что."""
    got = dest_page.evaluate(
        """() => {
          const el = document.querySelector('[placeholder="Enter the URL people visit after your ad"]');
          const r = el.getBoundingClientRect();
          return {w: r.width, h: r.height, value: el.value};
        }"""
    )
    assert got["w"] > 0 and got["h"] > 0, "поле не разложено — тогда причина отказа другая"
    assert got["value"] == "", "в поле уже что-то есть — снимок не с пустого шага"


def test_the_code_finds_the_destination_field(dest_page, dom):
    """То же, что для текстов: сначала разметочное имя, потом подпись."""
    assert dom.mark_ad_copy_field(dest_page, "pce_website_url_field", None, "dest")
    found = dest_page.locator('[data-rpa-field="dest"]')
    assert found.count() == 1
    assert found.first.get_attribute("placeholder") == "Enter the URL people visit after your ad"


def test_the_destination_code_does_not_use_the_removed_placeholder():
    """Сторож: старая подсказка не должна вернуться в код."""
    source = (ROOT / "src" / "fb_ads_rpa" / "meta_dom.py").read_text(encoding="utf-8")
    used = _code_strings(source, "set_ad_destination_website")
    assert "http://www.example.com/page" not in used


def test_the_destination_falls_back_to_the_placeholder(dest_page, dom):
    """Запасной ход для ссылки — подсказка, а не подпись.

    У поля текста подписи нет вовсе, и там ищем по соседнему div. У ссылки
    наоборот: `placeholder` на месте, и он ближе к контролу, чем надпись
    «Website URL», которая в разметке продублирована.
    """
    dest_page.evaluate("""() => {
      for (const e of document.querySelectorAll('[data-surface]')) e.removeAttribute('data-surface');
    }""")
    try:
        assert dom.mark_ad_copy_field(
            dest_page, "pce_website_url_field", None, "dest2",
            placeholder="Enter the URL people visit after your ad",
        )
        assert dest_page.locator('[data-rpa-field="dest2"]').count() == 1
    finally:
        dest_page.reload()


def test_the_reveal_check_can_actually_say_yes(ad_page, dom):
    """Проверка «поле готово» должна проходить на настоящей разметке.

    Прежняя искала `role=combobox` с именем «Tell people what your ad is
    about». Таких на странице ноль, то есть она не могла ответить «да» ни
    разу — и `fill_ad_copy` выходил ещё до полей, не записав, почему.
    """
    assert ad_page.locator('[role="combobox"]').count() == 0, (
        "combobox вернулся — тогда прежняя проверка была не так уж неправа"
    )
    assert dom.mark_ad_copy_field(ad_page, "message_field", None, "probe"), (
        "поле основного текста не находится — раскрытие снова скажет «нет»"
    )


def test_the_reveal_check_is_the_one_the_step_uses():
    """Сторож: проверку легко починить в одном месте и забыть про второе."""
    source = (ROOT / "src" / "fb_ads_rpa" / "meta_dom.py").read_text(encoding="utf-8")
    block = source[source.index("def _reveal_ad_copy_fields"):]
    block = block[: block.index("\ndef ", 10)]
    assert "mark_ad_copy_field" in block, "раскрытие ищет поле своим способом"
    assert "Tell people what your ad is about'" not in block or "combobox" not in block


def test_every_destination_lookup_goes_through_one_helper():
    """Поле ссылки искали в четырёх местах — и все четыре по мёртвой подсказке.

    Хуже отказа самого шага была проверка «записалось ли»: она искала то же
    самое, всегда отвечала «нет» и запускала ремонт поверх правильно
    заполненного поля.
    """
    source = (ROOT / "src" / "fb_ads_rpa" / "meta_dom.py").read_text(encoding="utf-8")
    for func in ("set_ad_destination_website", "_ad_destination_persisted"):
        used = _code_strings(source, func)
        assert "http://www.example.com/page" not in used, f"{func}: мёртвая подсказка вернулась"
        assert "placeholder" not in used.lower() or "destination_url_box" in source


# ══ сторож загрузки против настоящей страницы ═════════════════════════════

def test_the_loading_matcher_does_not_catch_fast_loading(ad_page, dom):
    """Одно отсутствующее ограничение слова стоило тридцати секунд на действие.

    `re.compile(r"Loading", re.I)` находил на странице редактора постоянную
    видимую фразу «Send people to a fast-loading, mobile-optimized
    experience» — описание Instant Experience. Скрыться она не может, а
    ожидание «пока исчезнет» звалось перед каждой попыткой каждого клика.
    Отсюда плоский пол `ad3_name` в 31.7с при восьми прогонах.
    """
    import re as _re

    loose = ad_page.get_by_text(_re.compile(r"Loading", _re.I))
    assert loose.count() >= 1, "фраза-ловушка исчезла со страницы — проверка больше ничего не значит"
    assert "fast-loading" in loose.first.inner_text().lower()

    tight = ad_page.get_by_text(dom._LOADING_TEXT)
    assert tight.count() == 0, "новый образец всё ещё ловит постоянный текст страницы"


def test_the_loading_matcher_still_catches_the_real_overlay():
    """Сузили так, что перестало ловить настоящее, — это было бы хуже."""
    for real in ("Loading...", "loading creation", "  Loading"):
        assert dom_module()._LOADING_TEXT.match(real), f"{real!r} перестал считаться накладкой"
    for not_overlay in ("Send people to a fast-loading, mobile-optimized experience.",
                        "Downloading report", "Reloading"):
        assert not dom_module()._LOADING_TEXT.match(not_overlay), f"{not_overlay!r} принят за накладку"


def dom_module():
    from src.fb_ads_rpa import meta_dom

    return meta_dom


def test_the_wait_returns_at_once_when_nothing_is_loading():
    """Сторож: без этой проверки ожидание снова выберет весь таймаут."""
    source = (ROOT / "src" / "fb_ads_rpa" / "meta_dom.py").read_text(encoding="utf-8")
    block = source[source.index("def _wait_loading"):]
    block = block[: block.index("def _click(")]
    assert "is_visible" in block, "ожидание не спрашивает, висит ли накладка сейчас"
    assert "timeout=30000" not in block, "вернулся тридцатисекундный таймаут"
    assert "_LOADING_TEXT" in block, "образец снова задан на месте"


@pytest.mark.parametrize('extra', [
    '<div data-surface="x/headline_field"><textarea></textarea></div>',
    '<div aria-hidden="true" data-surface="x/headline_field"><textarea></textarea></div>',
])
def test_field_lookup_refuses_ambiguous_visible_fields_but_ignores_hidden_decoys(browser, dom, extra):
    pg = browser.new_page()
    try:
        pg.set_content('<div data-surface="x/headline_field"><textarea></textarea></div>' + extra)
        got = dom.mark_ad_copy_field(pg, 'headline_field', None, 'title')
        assert got is ('aria-hidden' in extra)
        assert pg.locator('[data-rpa-field="title"]').count() == int(got)
    finally: pg.close()


def test_field_lookup_does_not_reuse_a_stale_marker(browser, dom):
    pg = browser.new_page()
    try:
        pg.set_content('<div data-surface="x/headline_field"><textarea></textarea></div>')
        assert dom.mark_ad_copy_field(pg, 'headline_field', None, 'title')
        pg.locator('textarea').evaluate('el => el.disabled = true')
        assert not dom.mark_ad_copy_field(pg, 'headline_field', None, 'title')
        assert pg.locator('[data-rpa-field="title"]').count() == 0
    finally: pg.close()


def test_copy_field_lookup_keeps_the_previous_accessible_layout(browser, dom):
    pg = browser.new_page()
    try:
        pg.set_content('<div contenteditable="true" role="combobox" aria-label="Tell people what your ad is about">Text</div>')
        assert dom.mark_ad_copy_field(pg, 'message_field', 'Tell people what your ad is about', 'body')
    finally: pg.close()


def test_category_checkbox_rejected_by_page_is_not_success(fresh_page, dom):
    fresh_page.locator('label').filter(has_text='Search results').locator('input').first.evaluate(
        "el => { el.checked = true; el.addEventListener('click', e => e.preventDefault()); }")
    report = {}
    assert not dom.fill_placements(fresh_page, report)
    assert report['categories']['Search results']['checked'] is True


def test_broken_recorded_selector_is_detected_by_real_lookup(browser, dom):
    pg = _open(browser, 'ad_copy_2026_09_09.html')
    try:
        assert dom.mark_ad_copy_field(pg, 'headline_field', None, 'title')
        pg.locator('[data-surface$="/headline_field"]').evaluate('el => el.removeAttribute("data-surface")')
        assert not dom.mark_ad_copy_field(pg, 'headline_field', None, 'title')
    finally: pg.close()


def test_upload_progress_ignores_hidden_bars_and_refuses_ambiguity(browser, dom):
    pg = browser.new_page()
    try:
        pg.set_content('<div role="progressbar" aria-valuenow="0" hidden></div><div role="progressbar" aria-valuenow="45" style="width:20px;height:20px"></div>')
        assert dom._upload_progress(pg) == 45
        pg.evaluate("document.body.insertAdjacentHTML('beforeend', '<div role=progressbar aria-valuenow=10 style=height:20px></div>')")
        assert dom._upload_progress(pg) is None
    finally: pg.close()


# ── разметка без обёртки <label> (DQ500/DQ501, 14.09) ─────────────────────
#
# Фикстуры на неё нет: строки категорий приходят свёрнутыми, и снять их можно
# только живым прогоном. Форму строки взяли из отладки круга 11 — там у всех
# пяти категорий настоящий `input[type=checkbox]`, но подписи `<label>` нет
# ни одной. Поэтому здесь не снимок, а та же форма в двух строках.

_ROW_WITHOUT_LABEL = """
  <ul><li>
    <div><div><div><span>Feeds</span></div>
    <span>Get high visibility for your business with ads in feeds</span></div>
    <input type="checkbox" checked>
  </div></li></ul>
"""


def _row_hit(pg, name):
    from src.fb_ads_rpa.meta_dom import PLACEMENTS_ROW_JS

    return pg.evaluate(
        "(name) => {" + PLACEMENTS_ROW_JS + """
          const hit = rowFor(document, name);
          return hit ? { tag: hit.row.tagName.toLowerCase(), checked: hit.cb.checked } : null;
        }""",
        name,
    )


def test_the_row_search_works_without_a_label_wrapper(browser):
    """Опора — галочка в строке, а не элемент `<label>` вокруг слов.

    Прежний код требовал обёртку, и на DQ500/DQ501 не находил ничего при всех
    пяти категориях на экране: восемь кругов ушло на «видно 0 категорий из 5».
    """
    pg = browser.new_page()
    try:
        pg.set_content(_ROW_WITHOUT_LABEL)
        hit = _row_hit(pg, "Feeds")
        assert hit is not None, "строка без <label> снова не находится"
        assert hit["checked"] is True
    finally:
        pg.close()


def test_the_row_search_needs_the_checkbox_not_just_the_words(browser):
    """Слова без контрола — не строка.

    Иначе шаг отчитается «нашёл» там, где переключать нечего, и отказ снова
    придётся выяснять по посмертному снимку.
    """
    pg = browser.new_page()
    try:
        pg.set_content(_ROW_WITHOUT_LABEL)
        pg.evaluate("document.querySelector('input[type=checkbox]').remove()")
        assert _row_hit(pg, "Feeds") is None, "нашлась строка, в которой нечего переключать"
    finally:
        pg.close()


def test_the_row_search_does_not_reach_into_a_neighbouring_row(browser):
    """Подъём упирается в `li`: у соседней строки своя галочка.

    Без этой границы «Apps and sites» без собственной галочки подцепил бы
    чужую и отчитался бы об успехе, выключив не ту категорию.
    """
    pg = browser.new_page()
    try:
        pg.set_content(
            "<ul>"
            "<li><div><span>Feeds</span></div><input type='checkbox' checked></li>"
            "<li><div><span>Apps and sites</span></div></li>"
            "</ul>"
        )
        assert _row_hit(pg, "Feeds") is not None
        assert _row_hit(pg, "Apps and sites") is None, "дотянулся до галочки соседней строки"
    finally:
        pg.close()


def test_the_row_search_skips_a_disabled_twin(browser):
    """Одно имя может встретиться дважды, и первое — чужое.

    14.09 на DQ501 «Search results» нашлась и как категория, и как
    недоступный контрол внутри `<label>`. Шаг кликал в недоступный, читал
    «всё ещё включена» и отчитывался об успехе: категория уезжала в кампанию
    включённой, хотя должна была быть выключена.
    """
    pg = browser.new_page()
    try:
        pg.set_content(
            "<ul>"
            "<li><label><span>Search results</span>"
            "<input type='checkbox' checked disabled></label></li>"
            "<li><div><span>Search results</span></div>"
            "<input type='checkbox' checked></li>"
            "</ul>"
        )
        hit = _row_hit(pg, "Search results")
        assert hit is not None
        assert hit["tag"] == "li", "взяли не строку категории"
        assert (
            pg.evaluate(
                "() => document.querySelectorAll('input')[1].disabled"
            )
            is False
        )
        disabled_first = pg.evaluate(
            "() => document.querySelectorAll('input')[0].disabled"
        )
        assert disabled_first is True, "стенд перестал воспроизводить случай"
    finally:
        pg.close()


def test_the_september_markup_has_no_expand_buttons(page):
    """Обратная сторона: в старой разметке этих кнопок нет ни одной.

    Значит добавленный способ её не трогает, и обе разметки живут в одном
    коде без ветвления по аккаунту.
    """
    assert page.locator('[aria-label="Expand"]').count() == 0


def _expand(pg, name):
    from src.fb_ads_rpa.meta_dom import PLACEMENTS_EXPAND_JS

    return pg.evaluate(PLACEMENTS_EXPAND_JS, [name])


def test_the_expander_is_found_by_its_reader_label(browser):
    """Раскрыватель без `aria-expanded`: состояние — в подписи для чталки.

    14.09 на DQ501 в `li` каждой категории лежит `button[aria-label=Expand]`
    и никакого `aria-expanded` рядом. Поиск шёл по атрибуту и не находил
    ничего: подпункты не проставились ни у одной из трёх категорий.
    """
    pg = browser.new_page()
    try:
        pg.set_content(
            "<div data-rpa-placements-marker>"
            "<ul><li><div><span>Feeds</span></div><input type='checkbox' checked>"
            "<button aria-label='Expand'>+</button></li></ul></div>"
        )
        pg.evaluate(
            "() => { window.__hits = 0;"
            " document.querySelector('button').addEventListener('click', () => window.__hits++) }"
        )
        res = _expand(pg, "Feeds")
        assert res["where"] == "кнопка Expand", res
        assert pg.evaluate("() => window.__hits") == 1, "кнопку не нажали"
    finally:
        pg.close()


def test_an_already_open_category_is_not_collapsed_again(browser):
    """Раскрытая кнопка называется «Collapse». Нажать её — свернуть категорию.

    Тот же класс ошибки, что и «Show more settings» на уже открытом разделе:
    шаг своим же кликом отменял то, чего добивался.
    """
    pg = browser.new_page()
    try:
        pg.set_content(
            "<div data-rpa-placements-marker>"
            "<ul><li><div><span>Feeds</span></div><input type='checkbox' checked>"
            "<button aria-label='Collapse'>-</button></li></ul></div>"
        )
        pg.evaluate(
            "() => { window.__hits = 0;"
            " document.querySelector('button').addEventListener('click', () => window.__hits++) }"
        )
        res = _expand(pg, "Feeds")
        assert res["where"] == "уже раскрыта", res
        assert pg.evaluate("() => window.__hits") == 0, "свернули уже раскрытую категорию"
    finally:
        pg.close()


def _set_subitem(pg, label, want):
    """Тот же вызов, что делает шаг для подпункта."""
    from src.fb_ads_rpa import meta_dom

    return pg.evaluate(meta_dom.PLACEMENTS_SUBITEM_JS, [label, want])


def test_a_frozen_subitem_already_in_the_wanted_state_is_not_a_failure(browser):
    """«Facebook Business Explore» Meta держит выключенной и трогать не даёт.

    14.09, круги 15-16: подпункт оставался единственной причиной отказа шага,
    хотя стоял ровно так, как нам нужно. Требовать клика от недоступной
    галочки бессмысленно — но только когда состояние уже верное.
    """
    pg = browser.new_page()
    try:
        pg.set_content(
            "<div data-rpa-placements-marker><ul>"
            "<li><div><span>Facebook Business Explore</span></div>"
            "<input type='checkbox' disabled></li>"
            "</ul></div>"
        )
        assert _set_subitem(pg, "Facebook Business Explore", False)["ok"] is True
        res = _set_subitem(pg, "Facebook Business Explore", True)
        assert res["ok"] is False and res["why"] == "недоступна и не та", res
    finally:
        pg.close()


def test_a_subitem_with_a_live_checkbox_is_still_clicked(browser):
    """Обычный подпункт должен переключаться, а не «считаться подходящим»."""
    pg = browser.new_page()
    try:
        pg.set_content(
            "<div data-rpa-placements-marker><ul>"
            "<li><div><span>Threads feed</span></div>"
            "<input type='checkbox' checked></li>"
            "</ul></div>"
        )
        assert _set_subitem(pg, "Threads feed", False)["ok"] is True
        assert pg.evaluate("() => document.querySelector('input').checked") is False
    finally:
        pg.close()
