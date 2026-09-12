"""Чтение приносит не только число, но и то, из чего аккаунт состоит.

На это встанет переключатель (`acl-113`). Строки, которые дешборд рисует под
аккаунтом сегодня, приходят из книги статистики, и тумблеру там опереться не
на что: в книге нет id объектов Meta, она отстаёт на сутки, и столбца on/off
в ней нет вовсе. Всё три есть на экране, который чтение уже открывает.

Разметка здесь **снята с живого аккаунта** 11.09 (KLD_BFL_X84), а не
придумана. Первая версия этих проверок была придумана — искала `role="row"` и
`role="cell"`, — и живой прогон показал, что у грида Ads Manager размечен
ролями только заголовок: пятнадцать `columnheader` и ноль `role="cell"`, а
тело лежит под `role="presentation"`. Проверки при этом были зелёными: они
подтверждали ту же выдумку, из которой вырос разбор.

Поэтому разбор идёт по `data-surface` — собственной разметке Meta для её же
логов, — и гоняется на **настоящем браузере**: JS-блок это код, и подделка
возвращаемого значения проверяла бы подделку.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.analysis_agent import account_spend as sp  # noqa: E402


# ── адреса уровней ────────────────────────────────────────────────────────

def test_each_level_keeps_the_account_and_the_period():
    """Уровень меняется, вопрос — нет: тот же кабинет, те же дни."""
    for level in sp.TREE_LEVELS:
        url = sp._tree_url("act_123", level, "2026-09-01", "2026-09-10")
        assert f"/manage/{'adsets' if level == 'adset' else 'ads'}?" in url
        assert "act=123" in url, f"{level}: номер кабинета потерян"
        assert "date=2026-09-01_2026-09-11" in url, f"{level}: период потерян"


def test_an_unknown_level_is_refused():
    with pytest.raises(ValueError):
        sp._tree_url("act_123", "campaign", "2026-09-01")


def test_metas_own_campaign_level_is_not_read():
    """Остановка кампании гасит все её ad set'ы разом. Такого никто не просил."""
    assert sp.TREE_LEVELS == ("adset", "ad")
    assert "campaign" not in sp.ADS_MANAGER_LEVELS


# ── разбор таблицы, на настоящем браузере ─────────────────────────────────

@pytest.fixture(scope="module")
def browser():
    """Один браузер на модуль.

    Вложенные `sync_playwright()` в одном потоке уже ломали прогон: контекст
    нельзя открыть дважды. Поэтому фикстура модульная, а не пофункциональная.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        b = pw.chromium.launch()
        yield b
        b.close()


def _row(rid: str, kind: str, name: str, delivery: str, toggle: str | None) -> str:
    """Строка в разметке Meta: `data-surface` несёт id строки и назначение ячейки.

    Снято с живого аккаунта 11.09 (KLD_BFL_X84). Одна и та же разметка висит
    на нескольких вложенных узлах, и внутренние копии часто пустые — здесь это
    воспроизведено, потому что именно на этом разбор и мог бы споткнуться.
    """
    base = f"/am/table/table_row:{rid}unit/table_cell:"
    sw = (
        f'<div data-surface="{base}forObjectType(toggle,{kind})">'
        f'<input type="checkbox" role="switch" aria-checked="{toggle}" aria-label="On/off"></div>'
        if toggle is not None else ""
    )
    return (
        f'<div data-surface="/am/table/table_row:{rid}unit">'
        f'<div data-surface="{base}row_selector"><input type="checkbox" aria-checked="false"></div>'
        f'{sw}'
        f'<span data-surface="{base}forObjectType(name,{kind})">{name}'
        f'<span data-surface="{base}forObjectType(name,{kind})"></span></span>'
        f'<span data-surface="{base}forObjectType(delivery,{kind})">{delivery}</span>'
        f'<span data-surface="{base}spend">$412.02</span>'
        f'<span data-surface="{base}forAttributionWindow(results,default)">18 Leads (Form)</span>'
        f'<span data-surface="{base}forAttributionWindow(cost_per_result,default)">$22.89 Per Lead (Form)</span>'
        f"</div>"
    )


def _grid(rows: list[str]) -> str:
    """Грид Ads Manager: заголовки в `role=table`, тело — под `presentation`.

    Ровно то, что сломало первую версию: пятнадцать `columnheader` и **ноль**
    `role="cell"`. Разбор по ролям находил заголовки и ноль строк, то есть
    возвращал пустоту с видом работающего.
    """
    head = "".join(
        f'<div role="columnheader">{h}</div>'
        for h in ("Off / On", "Ad set", "Delivery", "Amount spent")
    )
    body = "".join(f'<div role="presentation">{r}</div>' for r in rows)
    return f'<div role="table"><div role="row">{head}</div>{body}</div>'


@pytest.fixture
def page(browser):
    p = browser.new_page()
    yield p
    p.close()


def test_a_row_gives_up_its_id_name_state_and_delivery(page):
    page.set_content(_grid([
        _row("120250178859040703", "CAMPAIGN", "SA_ARM_iOS_IMG_Reading_30_6.9", "Active", "true"),
    ]))
    rows, how, err = sp.read_level(page, "adset")
    assert not err, err
    assert how == "data-surface"
    (r,) = rows
    assert r.id == "120250178859040703"
    assert r.name == "SA_ARM_iOS_IMG_Reading_30_6.9"
    assert r.delivery == "Active"
    assert r.switch == "on"
    assert r.switch_label == "On/off"
    assert r.id_from == "data-surface"


def test_the_numbers_of_a_row_come_back_as_meta_wrote_them(page):
    """Не разбираем в числа намеренно: «$22.89 Per Lead (Form)» несёт и сумму,
    и за что она, а `22.89` — только половину. Считать по ним мы ничего не
    собираемся; они нужны, чтобы человек видел, что именно останавливает.
    """
    page.set_content(_grid([_row("120250178859040703", "CAMPAIGN", "SA_ARM", "Active", "true")]))
    (r,), _, err = sp.read_level(page, "adset")
    assert not err, err
    assert r.spend == "$412.02"
    assert r.results == "18 Leads (Form)"
    assert r.cost_per_result == "$22.89 Per Lead (Form)"


def test_a_row_without_numbers_says_nothing_rather_than_zero(page):
    """Черновик ещё ничего не потратил. «$0» — утверждение, которого нет."""
    base = "/am/table/table_row:120250178859040703unit/table_cell:"
    page.set_content(
        '<div role="table"><div role="presentation">'
        f'<div data-surface="/am/table/table_row:120250178859040703unit">'
        f'<span data-surface="{base}forObjectType(name,CAMPAIGN)">SA_draft</span>'
        f'<span data-surface="{base}forObjectType(delivery,CAMPAIGN)">In draft</span>'
        "</div></div></div>"
    )
    (r,), _, _ = sp.read_level(page, "adset")
    assert r.spend == "" and r.results == "" and r.cost_per_result == ""


def test_the_totals_row_is_not_an_object(page):
    """`table_row:-1` размечена как все, но переключать там нечего."""
    page.set_content(_grid([
        _row("120250178859040703", "CAMPAIGN", "SA_ARM", "Active", "true"),
        _row("-1", "CAMPAIGN", "Results from 6 ad sets", "", None),
    ]))
    rows, _, err = sp.read_level(page, "adset")
    assert not err, err
    assert [r.id for r in rows] == ["120250178859040703"]


def test_the_level_is_checked_by_what_meta_says_about_the_rows(page):
    """Словарь Meta здесь легаси: на странице ad set'ов строки помечены
    `CAMPAIGN`, на странице объявлений — `ADGROUP`. Так назывались эти уровни
    в старом Ads API.

    Прочитать `forObjectType(toggle,CAMPAIGN)` буквально значит ошибиться на
    целый уровень — а это разница между «остановить один ad set» и «погасить
    всё под кампанией».
    """
    assert sp._LEVEL_KIND == {"adset": "CAMPAIGN", "ad": "ADGROUP"}
    page.set_content(_grid([_row("1202501788590407", "ADGROUP", "ad_2_ar", "Active", "true")]))
    rows, _, err = sp.read_level(page, "adset")
    assert rows == [], "объявления приняты за ad set'ы"
    assert "ADGROUP" in err and "CAMPAIGN" in err


def test_the_ad_level_wants_adgroup(page):
    page.set_content(_grid([_row("1202501788509507", "ADGROUP", "1962_12- Officialy", "Active", "true")]))
    (r,), _, err = sp.read_level(page, "ad")
    assert not err, err
    assert r.level == "ad" and r.name == "1962_12- Officialy"


def test_rows_without_a_stated_kind_are_refused(page):
    """«Не сказано, что это» — не то же самое, что «это то, что мы просили»."""
    page.set_content(
        '<div role="table"><div role="presentation">'
        '<div data-surface="/am/table/table_row:120250178859040703unit/table_cell:mystery">x</div>'
        "</div></div>"
    )
    rows, _, err = sp.read_level(page, "adset")
    assert rows == [] and "что это за объекты" in err


def test_svg_paths_are_not_mistaken_for_ids(page):
    """Первая версия искала любое число в 15-18 цифр в атрибутах, и первыми
    находились `<path d="M 25.19238815542512 ...">`."""
    page.set_content(
        '<div role="table">'
        '<svg><path d="M 25.19238815542512 25.192388155425117"></path></svg>'
        '<div role="presentation">'
        + _row("120250178859040703", "CAMPAIGN", "SA_ARM", "Active", "true")
        + "</div></div>"
    )
    (r,), _, _ = sp.read_level(page, "adset")
    assert r.id == "120250178859040703"


def test_a_switched_off_row_says_off(page):
    page.set_content(_grid([_row("120250178850950703", "ADGROUP", "ad_2_ar", "Off", "false")]))
    (r,), _, _ = sp.read_level(page, "ad")
    assert r.switch == "off"
    assert r.level == "ad"


def test_a_row_without_a_switch_says_unknown_not_off(page):
    """«Тумблера не видно» и «тумблер выключен» — разные ответы.

    Спутать их значит показать человеку выключенным то, что тратит деньги.
    """
    page.set_content(_grid([
        _row("120250178859040703", "CAMPAIGN", "KW_broad", "In review", None),
    ]))
    (r,), _, _ = sp.read_level(page, "adset")
    assert r.switch == "unknown"
    assert r.delivery == "In review", "состояние Meta словами, а не сплющенное в булев"


def test_a_page_without_a_grid_is_an_error_not_an_empty_answer(page):
    """Пустой список и неоткрывшаяся страница неразличимы, если молчать."""
    page.set_content("<div>Something else entirely</div>")
    rows, how, err = sp.read_level(page, "adset")
    assert rows == []
    assert err, "страница без таблицы прошла как успешно прочитанная"
    assert how == "no-table"


def test_a_grid_with_a_header_and_no_rows_is_an_error_too(page):
    """Именно так выглядела поломка первой версии: заголовки нашлись, строк нет.

    Разбор по ролям возвращал пустоту с видом работающего, и понять это можно
    было только по тому, что объектов ноль.
    """
    page.set_content(_grid([]))
    rows, how, err = sp.read_level(page, "adset")
    assert rows == []
    assert how == "no-row-surfaces"
    assert err


def test_an_unknown_level_is_refused_before_the_page_is_touched():
    class _Explodes:
        def evaluate(self, *a, **k):
            raise AssertionError("страницу не должны были трогать")

    rows, _, err = sp.read_level(_Explodes(), "campaign")
    assert rows == [] and "campaign" in err


def test_a_page_that_throws_is_reported_not_swallowed():
    class _Broken:
        def evaluate(self, *a, **k):
            raise RuntimeError("target closed")

    rows, _, err = sp.read_level(_Broken(), "adset")
    assert rows == [] and "target closed" in err


# ── обход уровней: та же осторожность, что и со спендом ───────────────────

class _Page:
    """Страница, которая помнит, куда её водили."""

    def __init__(self):
        self.visited: list[str] = []

    def goto(self, url, **kw):
        self.visited.append(url)


@pytest.fixture
def flow(monkeypatch):
    """Проходимый обход с подменёнными сверками — проверяется порядок, не DOM."""
    monkeypatch.setattr(sp, "_wait_for_grid", lambda page: True)
    monkeypatch.setattr(sp, "_act_from_url", lambda page, tries=1: "act_123")
    monkeypatch.setattr(sp, "_shown_period", lambda page, tries=12: "Sep 1, 2026 - Sep 10, 2026")
    monkeypatch.setattr(sp, "_period_matches", lambda shown, a, b="": True)

    def _level(page, level):
        if level == "adset":
            return ([sp.AdObject(level="adset", id="11" * 8, name="set A"),
                     sp.AdObject(level="adset", id="22" * 8, name="set B")], "data-surface", "")
        return ([sp.AdObject(level="ad", id="99" * 8, name="an ad")], "data-surface", "")

    monkeypatch.setattr(sp, "read_level", _level)
    return monkeypatch


def test_ads_are_asked_for_one_ad_set_at_a_time(flow):
    """Общий список объявлений 12.09 отдал шесть строк при двадцати настоящих,
    и чем он был сужен, страница не сказала. По ad set'ам — по построению."""
    page = _Page()
    objects, how, err = sp._read_tree(page, "act_123", "act_123", "2026-09-01", "2026-09-10")
    assert not err, err
    assert len(page.visited) == 3, f"заходов {len(page.visited)}, а надо 1 + 2"
    assert "selected_adset_ids=" + "11" * 8 in page.visited[1]
    assert "selected_adset_ids=" + "22" * 8 in page.visited[2]
    assert how.startswith("adset:data-surface") and "ad:data-surface" in how


def test_every_ad_knows_whose_it_is(flow):
    objects, _, _ = sp._read_tree(_Page(), "act_123", "act_123", "2026-09-01")
    ads = [o for o in objects if o["level"] == "ad"]
    assert len(ads) == 2
    assert {a["parent_id"] for a in ads} == {"11" * 8, "22" * 8}
    sets = [o for o in objects if o["level"] == "adset"]
    assert all(s["parent_id"] == "" for s in sets), "у ad set'а родителя тут нет"


def test_a_wrong_account_stops_everything(flow):
    """Ads Manager умеет открыть кабинет профиля по умолчанию вместо запрошенного.

    Без сверки чужое дерево пришло бы с видом правильного ответа — ровно то,
    от чего уже защищён спенд.
    """
    flow.setattr(sp, "_act_from_url", lambda page, tries=1: "act_999")
    objects, _, err = sp._read_tree(_Page(), "act_123", "act_123", "2026-09-01")
    assert objects == []
    assert "не тот кабинет" in err


def test_a_wrong_period_stops_everything(flow):
    flow.setattr(sp, "_period_matches", lambda shown, a, b="": False)
    objects, _, err = sp._read_tree(_Page(), "act_123", "act_123", "2026-09-01")
    assert objects == []
    assert "другой период" in err


def test_the_account_is_rechecked_after_every_navigation(flow):
    """Переход — это новый адрес, и проверка до него ничего не говорит о том,
    что после. Заходов теперь три, и сверок должно быть три."""
    seen: list[int] = []
    flow.setattr(sp, "_act_from_url", lambda page, tries=1: (seen.append(1), "act_123")[1])
    sp._read_tree(_Page(), "act_123", "act_123", "2026-09-01")
    assert len(seen) == 3


def test_one_ad_set_failing_does_not_cost_the_others(flow):
    def _level(page, level):
        if level == "adset":
            return ([sp.AdObject(level="adset", id="11" * 8, name="A"),
                     sp.AdObject(level="adset", id="22" * 8, name="B")], "data-surface", "")
        # первый ad set отдаёт отказ, второй — объявление
        return ([], "", "сломалось") if len(page.visited) == 2 else (
            [sp.AdObject(level="ad", id="99" * 8, name="ad")], "data-surface", "")

    flow.setattr(sp, "read_level", _level)
    objects, _, err = sp._read_tree(_Page(), "act_123", "act_123", "2026-09-01")
    assert [o["level"] for o in objects] == ["adset", "adset", "ad"]
    assert "сломалось" in err


def test_no_ad_sets_means_no_ads_and_no_pretending(flow):
    flow.setattr(sp, "read_level", lambda page, level: ([], "no-row-surfaces", "пусто"))
    page = _Page()
    objects, _, err = sp._read_tree(page, "act_123", "act_123", "2026-09-01")
    assert objects == []
    assert len(page.visited) == 1, "пошли за объявлениями, не зная ни одного ad set'а"
    assert "пусто" in err


def test_a_long_account_says_what_it_did_not_read(flow):
    """Молчаливое усечение читается как «это всё»."""
    many = [sp.AdObject(level="adset", id=str(10 ** 17 + i), name=str(i))
            for i in range(sp.TREE_MAX_ADSETS + 3)]
    flow.setattr(sp, "read_level", lambda page, level:
                 (many, "data-surface", "") if level == "adset" else ([], "data-surface", ""))
    page = _Page()
    _, _, err = sp._read_tree(page, "act_123", "act_123", "2026-09-01")
    assert len(page.visited) == 1 + sp.TREE_MAX_ADSETS
    assert str(len(many)) in err and str(sp.TREE_MAX_ADSETS) in err


def test_the_ad_url_is_refused_for_the_wrong_level():
    with pytest.raises(ValueError):
        sp._tree_url("act_1", "adset", "2026-09-01", adset_id="123")


def test_a_made_up_ad_set_id_is_refused():
    """В адрес идёт то, что пришло со страницы. Проверяем, что это цифры."""
    with pytest.raises(ValueError):
        sp._tree_url("act_1", "ad", "2026-09-01", adset_id="123&act=999")


# ── красные линии ─────────────────────────────────────────────────────────

def _source(*names: str) -> str:
    """Тела названных функций — из файла, а не по памяти.

    Конец ищется с оглядкой на то, что функция может быть последней в файле:
    `str.index` на отсутствующий следующий `def` бросает ValueError, и сторож
    падает тем же красным, каким падал бы на настоящей поломке. Так уже
    случалось в проверке боковой панели — и едва не увело чинить работающий код.
    """
    src = (ROOT / "src" / "analysis_agent" / "account_spend.py").read_text(encoding="utf-8")
    out = []
    for name in names:
        start = src.index(f"def {name}")
        nxt = src.find("\ndef ", start + 10)
        body = src[start: nxt if nxt != -1 else len(src)]
        assert body.strip(), f"{name}: срез пустой"
        out.append(body)
    return "\n".join(out)


def test_reading_the_tree_clicks_nothing():
    """Это чтение. Единственное, что здесь делают со страницей, — спрашивают."""
    block = _source("read_level", "_read_tree")
    for forbidden in (".click(", ".fill(", ".press(", ".check(", ".select_option("):
        assert forbidden not in block, f"чтение дерева трогает страницу: {forbidden}"


def test_no_ad_editor_is_ever_opened():
    """Проверено на DQ204 02.08: Meta портит Leads-объявление одним открытием
    редактора — предвыбирает «Instant form (suggested)» и автосохраняет, и
    чистое объявление получает ошибку #100 от визита «посмотрел и ушёл».

    Поэтому всё читается со списка, и адреса уровней — списочные.
    """
    for url in sp.ADS_MANAGER_LEVELS.values():
        assert "/manage/" in url and "/edit" not in url, f"{url} — не список"
    assert "/edit" not in _source("read_level", "_read_tree")


def test_the_tree_never_starts_a_profile():
    """Перезапуск разлогинивает аккаунт и умеет терять прокси."""
    block = _source("read_level", "_read_tree")
    for forbidden in ("launch(", "connect_over_cdp", "new_context", "start_profile"):
        assert forbidden not in block, f"дерево трогает браузер: {forbidden}"


def test_a_broken_tree_does_not_lose_the_spend():
    """Разные ответы на разные вопросы. Потерять прочитанное число из-за
    второго — хуже, чем вернуть его без дерева."""
    src = (ROOT / "src" / "analysis_agent" / "account_spend.py").read_text(encoding="utf-8")
    start = src.index("def read_via_rpa")
    body = src[start: src.index("\ndef ", start + 10)]
    spend_set = body.index("out.state = \"ok\"")
    tree_call = body.index("_read_tree(")
    assert spend_set < tree_call, "дерево читается до того, как спенд принят"
    tail = body[tree_call:]
    assert "except Exception" in tail, "исключение в дереве уронит готовый результат"


# ── какой вид списка выбран ───────────────────────────────────────────────
#
# 12.09 выяснилось, что «All ads» и «Active ads» — разные списки, а какой из
# них открыт, Meta не пишет нигде: ни aria-selected, ни aria-current, ни
# tabindex. Единственное отличие выбранной вкладки — цвет подчёркивания:
# синий против серого у остальных.
#
# Значение этого не косметическое. Шесть строк в «Active ads» и шесть в «All
# ads» выглядят одинаково и значат разное, а человек по ним решает, что
# останавливать.

def _tabs(names, underline):
    """Вкладки видов: подчёркивание рисуется дочерним блоком, как у Meta."""
    cells = "".join(
        f'<div data-surface="/am/search_and_filter/quickviewTabGroup/quickviewTab">'
        f'<div style="border-bottom: 1px solid {c}">{n}</div></div>'
        for n, c in zip(names, underline)
    )
    return f'<div data-surface="/am/search_and_filter/quickviewTabGroup">{cells}</div>'


BLUE, GREY = "rgb(10, 120, 190)", "rgb(203, 210, 217)"
VIEWS = ("All ads", "Actions", "Had delivery", "Active ads")


def _with_tabs(rows_html, underline):
    return _tabs(VIEWS, underline) + rows_html


def test_the_open_view_is_named(page):
    page.set_content(_with_tabs(
        _grid([_row("120250178859040703", "CAMPAIGN", "SA_ARM", "Active", "true")]),
        (BLUE, GREY, GREY, GREY),
    ))
    _, how, err = sp.read_level(page, "adset")
    assert not err, err
    assert how == "data-surface(All ads)", f"вид не назван: {how}"


def test_another_view_is_named_too(page):
    """Тот случай, ради которого это и сделано: список неполон, и видно почему."""
    page.set_content(_with_tabs(
        _grid([_row("120250178859040703", "CAMPAIGN", "SA_ARM", "Active", "true")]),
        (GREY, GREY, GREY, BLUE),
    ))
    _, how, _ = sp.read_level(page, "adset")
    assert how == "data-surface(Active ads)"


def test_the_odd_one_out_is_found_by_difference_not_by_colour(page):
    """Тема у Meta меняется, а «выбранная одна и подчёркнута иначе» — нет."""
    page.set_content(_with_tabs(
        _grid([_row("120250178859040703", "CAMPAIGN", "SA_ARM", "Active", "true")]),
        ("rgb(255, 0, 0)", "rgb(1, 2, 3)", "rgb(1, 2, 3)", "rgb(1, 2, 3)"),
    ))
    _, how, _ = sp.read_level(page, "adset")
    assert how == "data-surface(All ads)"


def test_two_odd_ones_mean_the_rule_broke_and_it_says_nothing(page):
    """Назвать вид наугад хуже, чем не назвать: по нему будут решать."""
    page.set_content(_with_tabs(
        _grid([_row("120250178859040703", "CAMPAIGN", "SA_ARM", "Active", "true")]),
        (BLUE, "rgb(9, 9, 9)", GREY, GREY),
    ))
    _, how, _ = sp.read_level(page, "adset")
    assert how == "data-surface", f"вид назван, хотя правило не сработало: {how}"


def test_no_tabs_at_all_is_not_a_view(page):
    page.set_content(_grid([_row("120250178859040703", "CAMPAIGN", "SA_ARM", "Active", "true")]))
    _, how, _ = sp.read_level(page, "adset")
    assert how == "data-surface"
