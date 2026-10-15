"""Чтение приносит не только число, но и то, из чего аккаунт состоит.

На это встанет переключатель (`acl-113`). Строки, которые дешборд рисует под
аккаунтом сегодня, приходят из книги статистики, и тумблеру там опереться не
на что: в книге нет id объектов Meta, она отстаёт на сутки, и столбца on/off
в ней нет вовсе. Всё три есть на экране, который чтение уже открывает.

Разметка здесь **снята с живого аккаунта** 11.09 (identifiers and names sanitized), а не
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
        assert f"/manage/{'campaigns' if level == 'campaign' else 'ads'}?" in url
        assert "act=123" in url, f"{level}: номер кабинета потерян"
        assert "date=2026-09-01_2026-09-11" in url, f"{level}: период потерян"


def test_an_unknown_level_is_refused():
    with pytest.raises(ValueError):
        sp._tree_url("act_123", "audience", "2026-09-01")


def test_the_two_levels_are_campaign_and_ad():
    """Решение владельца 12.09, поправляющее прежнее «ad set и объявление».

    Кампания и ad set здесь идут один к одному, а деньги останавливает
    кампания: живой аккаунт показал строки с тумблером `on` и доставкой
    `Campaign off` — ad set включён, а не крутится ничего.
    """
    assert sp.TREE_LEVELS == ("campaign", "ad")
    assert sp._LEVEL_KIND["campaign"] == "CAMPAIGN_GROUP"


def test_the_ad_set_level_is_still_known_but_not_walked():
    """`read_level` его умеет; лишний заход на живой аккаунт ради строки,
    которую никто не трогает, — нет."""
    assert "adset" in sp.ADS_MANAGER_LEVELS and "adset" not in sp.TREE_LEVELS


# ── разбор таблицы, на настоящем браузере ─────────────────────────────────

@pytest.fixture(scope="module")
def browser():
    """Один браузер на модуль.

    Вложенные `sync_playwright()` в одном потоке уже ломали прогон: контекст
    нельзя открыть дважды. Поэтому фикстура модульная, а не пофункциональная.
    """
    import os
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        yield b
        b.close()


def _row(rid: str, kind: str, name: str, delivery: str, toggle: str | None) -> str:
    """Строка в разметке Meta: `data-surface` несёт id строки и назначение ячейки.

    Снято с живого аккаунта 11.09 (identifiers and names sanitized). Одна и та же разметка висит
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
    p.route("**/*", lambda route: route.abort())
    yield p
    p.close()


def test_a_row_gives_up_its_id_name_state_and_delivery(page):
    page.set_content(_grid([
        _row("12345678900000000303", "CAMPAIGN_GROUP", "Example campaign", "Active", "true"),
    ]))
    rows, how, err = sp.read_level(page, "campaign")
    assert not err, err
    assert how == "data-surface"
    (r,) = rows
    assert r.id == "12345678900000000303"
    assert r.name == "Example campaign"
    assert r.delivery == "Active"
    assert r.switch == "on"
    assert r.switch_label == "On/off"
    assert r.id_from == "data-surface"


def test_the_numbers_of_a_row_come_back_as_meta_wrote_them(page):
    """Не разбираем в числа намеренно: «$22.89 Per Lead (Form)» несёт и сумму,
    и за что она, а `22.89` — только половину. Считать по ним мы ничего не
    собираемся; они нужны, чтобы человек видел, что именно останавливает.
    """
    page.set_content(_grid([_row("12345678900000000303", "CAMPAIGN_GROUP", "SA_ARM", "Active", "true")]))
    (r,), _, err = sp.read_level(page, "campaign")
    assert not err, err
    assert r.spend == "$412.02"
    assert r.results == "18 Leads (Form)"
    assert r.cost_per_result == "$22.89 Per Lead (Form)"


def test_a_row_without_numbers_says_nothing_rather_than_zero(page):
    """Черновик ещё ничего не потратил. «$0» — утверждение, которого нет."""
    base = "/am/table/table_row:12345678900000000303unit/table_cell:"
    page.set_content(
        '<div role="table"><div role="presentation">'
        f'<div data-surface="/am/table/table_row:12345678900000000303unit">'
        f'<span data-surface="{base}forObjectType(name,CAMPAIGN_GROUP)">SA_draft</span>'
        f'<span data-surface="{base}forObjectType(delivery,CAMPAIGN_GROUP)">In draft</span>'
        "</div></div></div>"
    )
    (r,), _, _ = sp.read_level(page, "campaign")
    assert r.spend == "" and r.results == "" and r.cost_per_result == ""


def test_the_totals_row_is_not_an_object(page):
    """`table_row:-1` размечена как все, но переключать там нечего."""
    page.set_content(_grid([
        _row("12345678900000000303", "CAMPAIGN_GROUP", "SA_ARM", "Active", "true"),
        _row("-1", "CAMPAIGN_GROUP", "Results from 6 ad sets", "", None),
    ]))
    rows, _, err = sp.read_level(page, "campaign")
    assert not err, err
    assert [r.id for r in rows] == ["12345678900000000303"]


def test_the_level_is_checked_by_what_meta_says_about_the_rows(page):
    """Словарь Meta здесь легаси: на странице ad set'ов строки помечены
    `CAMPAIGN`, на странице объявлений — `ADGROUP`. Так назывались эти уровни
    в старом Ads API.

    Прочитать `forObjectType(toggle,CAMPAIGN)` буквально значит ошибиться на
    целый уровень — а это разница между «остановить один ad set» и «погасить
    всё под кампанией».
    """
    assert sp._LEVEL_KIND == {"campaign": "CAMPAIGN_GROUP", "adset": "CAMPAIGN", "ad": "ADGROUP"}
    page.set_content(_grid([_row("123456789000000003", "ADGROUP", "ad_2_ar", "Active", "true")]))
    rows, _, err = sp.read_level(page, "campaign")
    assert rows == [], "объявления приняты за кампании"
    assert "CAMPAIGN_GROUP" in err


def test_the_ad_level_wants_adgroup(page):
    page.set_content(_grid([_row("123456789000000001", "ADGROUP", "1962_12- Officialy", "Active", "true")]))
    (r,), _, err = sp.read_level(page, "ad")
    assert not err, err
    assert r.level == "ad" and r.name == "1962_12- Officialy"


def test_rows_without_a_stated_kind_are_refused(page):
    """«Не сказано, что это» — не то же самое, что «это то, что мы просили»."""
    page.set_content(
        '<div role="table"><div role="presentation">'
        '<div data-surface="/am/table/table_row:12345678900000000303unit/table_cell:mystery">x</div>'
        "</div></div>"
    )
    rows, _, err = sp.read_level(page, "campaign")
    assert rows == [] and "object type" in err


def test_svg_paths_are_not_mistaken_for_ids(page):
    """Первая версия искала любое число в 15-18 цифр в атрибутах, и первыми
    находились `<path d="M 25.19238815542512 ...">`."""
    page.set_content(
        '<div role="table">'
        '<svg><path d="M 25.19238815542512 25.192388155425117"></path></svg>'
        '<div role="presentation">'
        + _row("12345678900000000303", "CAMPAIGN_GROUP", "SA_ARM", "Active", "true")
        + "</div></div>"
    )
    (r,), _, _ = sp.read_level(page, "campaign")
    assert r.id == "12345678900000000303"


def test_a_switched_off_row_says_off(page):
    page.set_content(_grid([_row("12345678900000000103", "ADGROUP", "ad_2_ar", "Off", "false")]))
    (r,), _, _ = sp.read_level(page, "ad")
    assert r.switch == "off"
    assert r.level == "ad"


def test_a_row_without_a_switch_says_unknown_not_off(page):
    """«Тумблера не видно» и «тумблер выключен» — разные ответы.

    Спутать их значит показать человеку выключенным то, что тратит деньги.
    """
    page.set_content(_grid([
        _row("12345678900000000303", "CAMPAIGN_GROUP", "KW_broad", "In review", None),
    ]))
    (r,), _, _ = sp.read_level(page, "campaign")
    assert r.switch == "unknown"
    assert r.delivery == "In review", "состояние Meta словами, а не сплющенное в булев"


def test_a_page_without_a_grid_is_an_error_not_an_empty_answer(page):
    """Пустой список и неоткрывшаяся страница неразличимы, если молчать."""
    page.set_content("<div>Something else entirely</div>")
    rows, how, err = sp.read_level(page, "campaign")
    assert rows == []
    assert err, "страница без таблицы прошла как успешно прочитанная"
    assert how == "no-table"


def test_a_grid_with_a_header_and_no_rows_is_an_error_too(page):
    """Именно так выглядела поломка первой версии: заголовки нашлись, строк нет.

    Разбор по ролям возвращал пустоту с видом работающего, и понять это можно
    было только по тому, что объектов ноль.
    """
    page.set_content(_grid([]))
    rows, how, err = sp.read_level(page, "campaign")
    assert rows == []
    assert how == "no-row-surfaces"
    assert err


def test_an_unknown_level_is_refused_before_the_page_is_touched():
    class _Explodes:
        def evaluate(self, *a, **k):
            raise AssertionError("страницу не должны были трогать")

    rows, _, err = sp.read_level(_Explodes(), "audience")
    assert rows == [] and "audience" in err


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
        self.url = url


@pytest.fixture
def flow(monkeypatch):
    """Проходимый обход с подменёнными сверками — проверяется порядок, не DOM."""
    monkeypatch.setattr(sp, "_wait_for_grid", lambda page: True)
    monkeypatch.setattr(sp, "_act_from_url", lambda page, tries=1: "act_123")
    monkeypatch.setattr(sp, "_shown_period", lambda page, tries=12: "Sep 1, 2026 - Sep 10, 2026")
    monkeypatch.setattr(sp, "_period_matches", lambda shown, a, b="": True)

    def _level(page, level):
        if level == "campaign":
            return ([sp.AdObject(level="campaign", id="11" * 8, name="camp A"),
                     sp.AdObject(level="campaign", id="22" * 8, name="camp B")], "data-surface", "")
        return ([sp.AdObject(level="ad", id=str(9900 + len(page.visited)), name="an ad")], "data-surface", "")

    monkeypatch.setattr(sp, "read_level", _level)
    return monkeypatch


def test_ads_are_asked_for_one_ad_set_at_a_time(flow):
    """Общий список объявлений 12.09 отдал шесть строк при двадцати настоящих,
    и чем он был сужен, страница не сказала. По ad set'ам — по построению."""
    page = _Page()
    objects, how, err = sp._read_tree(page, "act_123", "act_123", "2026-09-01", "2026-09-10")
    assert not err, err
    assert len(page.visited) == 3, f"заходов {len(page.visited)}, а надо 1 + 2"
    assert "selected_campaign_ids=" + "11" * 8 in page.visited[1]
    assert "selected_campaign_ids=" + "22" * 8 in page.visited[2]
    assert how.startswith("campaign:data-surface") and "ad:data-surface" in how


def test_every_ad_knows_whose_it_is(flow):
    objects, _, _ = sp._read_tree(_Page(), "act_123", "act_123", "2026-09-01")
    ads = [o for o in objects if o["level"] == "ad"]
    assert len(ads) == 2
    assert {a["parent_id"] for a in ads} == {"11" * 8, "22" * 8}
    camps = [o for o in objects if o["level"] == "campaign"]
    assert all(c["parent_id"] == "" for c in camps), "у кампании родителя тут нет"


def test_a_wrong_account_stops_everything(flow):
    """Ads Manager умеет открыть кабинет профиля по умолчанию вместо запрошенного.

    Без сверки чужое дерево пришло бы с видом правильного ответа — ровно то,
    от чего уже защищён спенд.
    """
    flow.setattr(sp, "_act_from_url", lambda page, tries=1: "act_999")
    objects, _, err = sp._read_tree(_Page(), "act_123", "act_123", "2026-09-01")
    assert objects == []
    assert "a different ad account" in err


def test_a_wrong_period_stops_everything(flow):
    flow.setattr(sp, "_period_matches", lambda shown, a, b="": False)
    objects, _, err = sp._read_tree(_Page(), "act_123", "act_123", "2026-09-01")
    assert objects == []
    assert "a different period" in err


def test_the_account_is_rechecked_after_every_navigation(flow):
    """Переход — это новый адрес, и проверка до него ничего не говорит о том,
    что после. Заходов теперь три, и сверок должно быть три."""
    seen: list[int] = []
    flow.setattr(sp, "_act_from_url", lambda page, tries=1: (seen.append(1), "act_123")[1])
    sp._read_tree(_Page(), "act_123", "act_123", "2026-09-01")
    assert len(seen) == 6


def test_one_campaign_failing_does_not_cost_the_others(flow):
    def _level(page, level):
        if level == "campaign":
            return ([sp.AdObject(level="campaign", id="11" * 8, name="A"),
                     sp.AdObject(level="campaign", id="22" * 8, name="B")], "data-surface", "")
        # первая кампания отдаёт отказ, вторая — объявление
        return ([], "", "сломалось") if len(page.visited) == 2 else (
            [sp.AdObject(level="ad", id="99" * 8, name="ad")], "data-surface", "")

    flow.setattr(sp, "read_level", _level)
    objects, _, err = sp._read_tree(_Page(), "act_123", "act_123", "2026-09-01")
    assert [o["level"] for o in objects] == ["campaign", "campaign", "ad"]
    assert "сломалось" in err


def test_no_campaigns_means_no_ads_and_no_pretending(flow):
    flow.setattr(sp, "read_level", lambda page, level: ([], "no-row-surfaces", "пусто"))
    page = _Page()
    objects, _, err = sp._read_tree(page, "act_123", "act_123", "2026-09-01")
    assert objects == []
    assert len(page.visited) == 1, "пошли за объявлениями, не зная ни одной кампании"
    assert "пусто" in err


def test_a_long_account_says_what_it_did_not_read(flow):
    """Молчаливое усечение читается как «это всё»."""
    many = [sp.AdObject(level="campaign", id=str(10 ** 17 + i), name=str(i))
            for i in range(sp.TREE_MAX_PARENTS + 3)]
    flow.setattr(sp, "read_level", lambda page, level:
                 (many, "data-surface", "") if level == "campaign" else ([], "data-surface", ""))
    page = _Page()
    _, _, err = sp._read_tree(page, "act_123", "act_123", "2026-09-01")
    assert len(page.visited) == 1 + sp.TREE_MAX_PARENTS
    assert str(len(many)) in err and str(sp.TREE_MAX_PARENTS) in err


def test_the_ad_url_is_refused_for_the_wrong_level():
    with pytest.raises(ValueError):
        sp._tree_url("act_1", "campaign", "2026-09-01", parent_level="campaign", parent_id="123")


def test_a_made_up_ad_set_id_is_refused():
    """В адрес идёт то, что пришло со страницы. Проверяем, что это цифры."""
    with pytest.raises(ValueError):
        sp._tree_url("act_1", "ad", "2026-09-01", parent_level="campaign", parent_id="123&act=999")


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
        _grid([_row("12345678900000000303", "CAMPAIGN_GROUP", "SA_ARM", "Active", "true")]),
        (BLUE, GREY, GREY, GREY),
    ))
    _, how, err = sp.read_level(page, "campaign")
    assert not err, err
    assert how == "data-surface(All ads)", f"вид не назван: {how}"


def test_another_view_is_named_too(page):
    """Тот случай, ради которого это и сделано: список неполон, и видно почему."""
    page.set_content(_with_tabs(
        _grid([_row("12345678900000000303", "CAMPAIGN_GROUP", "SA_ARM", "Active", "true")]),
        (GREY, GREY, GREY, BLUE),
    ))
    _, how, _ = sp.read_level(page, "campaign")
    assert how == "data-surface(Active ads)"


def test_the_odd_one_out_is_found_by_difference_not_by_colour(page):
    """Тема у Meta меняется, а «выбранная одна и подчёркнута иначе» — нет."""
    page.set_content(_with_tabs(
        _grid([_row("12345678900000000303", "CAMPAIGN_GROUP", "SA_ARM", "Active", "true")]),
        ("rgb(255, 0, 0)", "rgb(1, 2, 3)", "rgb(1, 2, 3)", "rgb(1, 2, 3)"),
    ))
    _, how, _ = sp.read_level(page, "campaign")
    assert how == "data-surface(All ads)"


def test_two_odd_ones_mean_the_rule_broke_and_it_says_nothing(page):
    """Назвать вид наугад хуже, чем не назвать: по нему будут решать."""
    page.set_content(_with_tabs(
        _grid([_row("12345678900000000303", "CAMPAIGN_GROUP", "SA_ARM", "Active", "true")]),
        (BLUE, "rgb(9, 9, 9)", GREY, GREY),
    ))
    _, how, _ = sp.read_level(page, "campaign")
    assert how == "data-surface", f"вид назван, хотя правило не сработало: {how}"


def test_no_tabs_at_all_is_not_a_view(page):
    page.set_content(_grid([_row("12345678900000000303", "CAMPAIGN_GROUP", "SA_ARM", "Active", "true")]))
    _, how, _ = sp.read_level(page, "campaign")
    assert how == "data-surface"


# ══ сверка периода: сорок процентов времени чтения ════════════════════════
#
# Замер 12.09 на живом аккаунте: сверка периода стоила 22 секунды из 54 — при
# шести переходах по 3.7 с. Запасной путь ходил по всем `div[role="button"]`
# и спрашивал `inner_text()` у каждого **из Python**: на странице Ads Manager
# их сотни, и каждый вопрос — обращение в браузер.
#
# Ровно так же однажды тормозило чтение итогов (175 с против 40). Лечение то
# же: спросить страницу один раз.

def test_the_period_comes_from_what_meta_marked(page):
    page.set_content('<div role="button" aria-label="Date range: Sep 11, 2026">calendar</div>')
    assert "Sep 11, 2026" in sp._period_once(page)


def test_a_plain_button_with_a_date_is_the_fallback(page):
    """У кнопки периода часто нет aria-label, и опознаётся она по дате внутри."""
    page.set_content(
        '<div role="button">Campaigns</div>'
        '<div role="button">Sep 11, 2026</div>'
    )
    assert sp._period_once(page) == "Sep 11, 2026"


def test_a_wall_of_text_with_a_date_in_it_is_not_the_period(page):
    """Иначе периодом окажется первый же длинный блок, куда попала дата."""
    page.set_content(
        '<div role="button">' + ("Some long explanation about billing. " * 4)
        + 'Sep 11, 2026</div>'
    )
    assert sp._period_once(page) == ""


def test_a_page_without_a_date_says_nothing(page):
    page.set_content('<div role="button">Campaigns</div>')
    assert sp._period_once(page) == ""


def test_a_broken_page_is_silence_not_a_crash():
    class _Broken:
        def evaluate(self, *a, **k):
            raise RuntimeError("target closed")

    assert sp._period_once(_Broken()) == ""


def test_the_search_happens_inside_the_page(page):
    """Сторож на возврат медленного приёма: обход по узлу за раз стоил сорок
    процентов времени чтения."""
    src = (ROOT / "src" / "analysis_agent" / "account_spend.py").read_text(encoding="utf-8")
    start = src.index("def _period_once(page)")
    nxt = src.find("\ndef ", start + 10)
    body = src[start: nxt if nxt != -1 else len(src)]
    assert "page.evaluate" in body
    for forbidden in ("query_selector_all", "inner_text()", "get_attribute"):
        assert forbidden not in body, f"вернулся обход по узлу за раз: {forbidden}"


def test_one_look_is_one_round_trip(page):
    """Один взгляд — один вопрос браузеру, а не сотни."""
    calls = []
    real = page.evaluate

    class _Counting:
        def evaluate(self, js, arg=None):
            calls.append(1)
            return real(js, arg)

    page.set_content('<div role="button">Sep 11, 2026</div>')
    sp._period_once(_Counting())
    assert calls == [1]


@pytest.mark.parametrize("mode", ["hidden", "ambiguous", "loading", "untyped", "hidden-row"])
def test_only_visible_identified_ready_rows_are_accepted(page, mode):
    good = _grid([_row("111", "CAMPAIGN_GROUP", "current", "Active", "true")])
    old = _grid([_row("222", "CAMPAIGN_GROUP", "old", "Off", "false")])
    markup = {
        "hidden": '<div hidden>' + old + '</div>' + good,
        "ambiguous": old + good,
        "loading": good.replace('role="table"', 'role="table" aria-busy="true"'),
        "untyped": _grid([_row("111", "CAMPAIGN_GROUP", "current", "Active", "true"),
                           '<div data-surface="/am/table/table_row:222unit/table_cell:spend">$10</div>']),
        "hidden-row": _grid([_row("111", "CAMPAIGN_GROUP", "current", "Active", "true"),
                              '<div hidden>' + _row("222", "CAMPAIGN_GROUP", "old", "Off", "false") + '</div>']),
    }[mode]
    page.set_content(markup)
    rows, _, err = sp.read_level(page, "campaign")
    if mode in ("hidden", "hidden-row"):
        assert not err and [(r.id, r.name) for r in rows] == [("111", "current")]
    else:
        assert rows == [] and err


def test_a_parent_filter_lost_on_navigation_refuses_the_ads(flow):
    class Redirected(_Page):
        def goto(self, url, **kwargs):
            super().goto(url, **kwargs)
            self.url = url.split('&selected_campaign_ids=')[0]
    objects, _, err = sp._read_tree(Redirected(), "act_123", "act_123", "2026-09-01")
    assert objects and all(o["level"] == "campaign" for o in objects)
    assert "filter" in err


@pytest.mark.parametrize("change", ["account", "period", "parent"])
def test_context_changed_during_parse_is_refused(flow, change):
    original = sp.read_level
    def changing(page, level):
        result = original(page, level)
        if level == "ad":
            if change == "account":
                flow.setattr(sp, "_act_from_url", lambda *a, **kw: "act_999")
            elif change == "period":
                flow.setattr(sp, "_period_matches", lambda *a, **kw: False)
            else:
                page.url = page.url.split('&selected_campaign_ids=')[0]
        return result
    flow.setattr(sp, "read_level", changing)
    objects, _, err = sp._read_tree(_Page(), "act_123", "act_123", "2026-09-01")
    assert objects and all(o["level"] == "campaign" for o in objects)
    assert err


def test_the_same_ad_under_different_campaigns_has_no_guessed_parent(flow):
    original = sp.read_level
    def duplicated(page, level):
        if level == "ad":
            return [sp.AdObject(level="ad", id="999", name="stale ad")], "data-surface", ""
        return original(page, level)
    flow.setattr(sp, "read_level", duplicated)
    objects, _, err = sp._read_tree(_Page(), "act_123", "act_123", "2026-09-01")
    assert objects and all(o["level"] == "campaign" for o in objects)
    assert "different campaigns" in err


def test_a_scrollable_snapshot_does_not_claim_a_complete_tree(page):
    page.set_content('<div style="height: 30px; overflow-y: auto">' + _grid([
        _row("111", "CAMPAIGN_GROUP", "first", "Active", "true"),
        _row("222", "CAMPAIGN_GROUP", "second", "Active", "true"),
    ]) + '</div>')
    objects, _, err = sp.read_level(page, "campaign")
    assert objects and "completeness is not confirmed" in err


# ── таблица без id: id кампаний из данных самой страницы (ACL-1255) ───────
#
# Doc19 с 18.09 показывает таблицу без id: кампании читались по имени, а
# объявления не читались вовсе — их спрашивают по id кампании. 22.09 в
# сохранённой странице нашёлся `campaign_structure_tree`: Meta вшивает в неё
# id каждой кампании и имена её объявлений.

import json as _json
from collections import Counter as _Counter


def _structure_script(*campaigns) -> str:
    """Вшитый ответ Meta в той же вложенности, что на живой странице Doc19."""
    nodes = [{"__typename": "AdCampaignGroup", "campaign_structure_tree": {
        "campaign_id": cid, "name": name, "status": "ACTIVE",
        "children": [{"adset_id": "7" + cid, "name": name,
                      "children": [{"adgroup_id": "8" + cid, "name": a} for a in ads]}],
    }} for cid, name, ads in campaigns]
    payload = {"require": [["ScheduledServerJS", "handle", None, [{"__bbox": {"require": [
        ["RelayPrefetchedStreamCache", "next", [], ["adp_q", {"__bbox": {"result": {"data": {
            "campaign_groups_dynamic_tier_1_query_context": {"campaign_groups": {"nodes": nodes}},
        }}}}]],
    ]}}]]]}
    return ('<script type="application/json" data-sjs="">'
            + _json.dumps(payload) + '</script>')


def test_campaign_ids_are_found_in_the_page_data(page):
    page.set_content(_structure_script(("123", "SA camp", ["ad one", "ad one", "ad two"]),
                                       ("456", "Warmup", ["Warmup"])))
    got = sp._campaigns_from_page_data(page)
    assert got == {"SA camp": {"id": "123", "ads": {"ad one": 2, "ad two": 1}},
                   "Warmup": {"id": "456", "ads": {"Warmup": 1}}}


def test_names_are_cleaned_like_the_table_cleans_them(page):
    """Таблица схлопывает пробелы; данные страницы — нет. Сравнивать надо одинаково."""
    page.set_content(_structure_script(("123", "  SA\u00a0 camp ", ["ad  one"])))
    assert sp._campaigns_from_page_data(page) == {"SA camp": {"id": "123", "ads": {"ad one": 1}}}


def test_two_campaigns_with_one_name_get_no_id(page):
    """По имени их не различить — угадывать нельзя."""
    page.set_content(_structure_script(("123", "Twin", ["a"]), ("456", "Twin", ["b"]),
                                       ("789", "Single", ["c"])))
    got = sp._campaigns_from_page_data(page)
    assert got["Single"]["id"] == "789"
    assert got["Twin"] == {"id": "", "ads": {}}


def test_a_page_without_the_data_gives_nothing(page):
    page.set_content('<script type="application/json">{"campaign_structure_tree": 1}</script>'
                     '<script type="application/json">not json campaign_structure_tree</script>')
    assert sp._campaigns_from_page_data(page) == {}


@pytest.fixture
def nameless(flow):
    """Таблица без id: кампании и объявления пришли по имени, без id."""
    state = {"structure": {"camp A": {"id": "111", "ads": _Counter(["ad A1", "ad A2"])},
                           "camp B": {"id": "222", "ads": _Counter(["ad B1"])}},
             "campaigns": ["camp A", "camp B"],
             "ads": {"111": ["ad A1", "ad A2"], "222": ["ad B1"]}}

    def _level(page, level):
        if level == "campaign":
            return ([sp.AdObject(level="campaign", name=n) for n in state["campaigns"]],
                    "by-name", "read by name: this account's table has no campaign ids")
        cid = page.url.split("selected_campaign_ids=")[1].split("&")[0]
        return ([sp.AdObject(level="ad", name=n, spend=state.get("spend", {}).get(cid, "$1.00"))
                 for n in state["ads"][cid]],
                "by-name", "read by name: this account's table has no ad ids")

    flow.setattr(sp, "read_level", _level)
    flow.setattr(sp, "_campaigns_from_page_data", lambda page: state["structure"])
    return state


def test_ads_of_a_nameless_table_are_read_under_their_campaigns(nameless):
    page = _Page()
    objects, _, err = sp._read_tree(page, "act_123", "act_123", "2026-09-01")
    assert len(page.visited) == 3
    assert "selected_campaign_ids=111" in page.visited[1]
    assert "selected_campaign_ids=222" in page.visited[2]
    camps = [o for o in objects if o["level"] == "campaign"]
    ads = [o for o in objects if o["level"] == "ad"]
    assert [(c["name"], c["id"], c["data_id"]) for c in camps] == [
        ("camp A", "", "111"), ("camp B", "", "222")]
    assert sorted((a["name"], a["parent_id"]) for a in ads) == [
        ("ad A1", "111"), ("ad A2", "111"), ("ad B1", "222")]
    assert err.count("has no ad ids") == 1, "пометка по разу на кампанию — шум"


def test_the_page_id_is_not_a_switchable_id(nameless):
    """Id из данных страницы в строке таблицы не найти — выключать по нему нечего."""
    objects, _, _ = sp._read_tree(_Page(), "act_123", "act_123", "2026-09-01")
    assert all(o["id"] == "" for o in objects)


def test_ads_that_are_not_in_the_campaign_structure_are_not_attributed(nameless):
    """Фильтр не сработал — пришли чужие объявления. Приписывать их нельзя."""
    nameless["ads"]["111"] = ["someone else's ad"]
    objects, _, err = sp._read_tree(_Page(), "act_123", "act_123", "2026-09-01")
    ads = [o for o in objects if o["level"] == "ad"]
    assert [a["name"] for a in ads] == ["ad B1"]
    assert "did not match this campaign" in err


def test_a_campaign_missing_from_the_page_data_is_said_out_loud(nameless):
    del nameless["structure"]["camp B"]
    page = _Page()
    objects, _, err = sp._read_tree(page, "act_123", "act_123", "2026-09-01")
    assert len(page.visited) == 2, "за объявлениями без id не ходят"
    assert "ads were not read for 1 of 2 campaigns" in err
    assert {o["parent_id"] for o in objects if o["level"] == "ad"} == {"111"}


def test_nameless_ads_are_not_mistaken_for_one_ad_under_many_campaigns(nameless):
    """Пустой id у всех объявлений — не «одно объявление под разными кампаниями»."""
    objects, _, err = sp._read_tree(_Page(), "act_123", "act_123", "2026-09-01")
    assert len([o for o in objects if o["level"] == "ad"]) == 3
    assert "different campaigns" not in err


def test_an_id_table_does_not_look_at_the_page_data(flow):
    """Аккаунты с id работают как раньше: данные страницы им не нужны."""
    called = []
    flow.setattr(sp, "_campaigns_from_page_data", lambda page: called.append(1) or {})
    sp._read_tree(_Page(), "act_123", "act_123", "2026-09-01")
    assert called == []


def test_a_name_twice_in_the_table_gets_no_borrowed_id(nameless):
    """В данных страницы «camp A» одна, а в таблице две. Второй строке достался
    бы чужой id и чужие объявления."""
    nameless["campaigns"] = ["camp A", "camp A", "camp B"]
    page = _Page()
    objects, _, err = sp._read_tree(page, "act_123", "act_123", "2026-09-01")
    assert len(page.visited) == 2, "за объявлениями «camp A» ходить нельзя"
    assert [c["data_id"] for c in objects if c["level"] == "campaign"] == ["", "", "222"]
    assert "share a name" in err
    assert "did not give their ids" not in err


def test_an_ignored_filter_is_caught_even_for_clones(nameless):
    """Клоны: одинаковые имена объявлений. Meta проигнорировала фильтр и
    отдала все объявления аккаунта — каждое имя «знакомо», но их больше."""
    nameless["structure"] = {"camp A": {"id": "111", "ads": _Counter(["clone ad"])},
                             "camp B": {"id": "222", "ads": _Counter(["clone ad"])}}
    nameless["ads"] = {"111": ["clone ad", "clone ad"], "222": ["clone ad", "clone ad"]}
    objects, _, err = sp._read_tree(_Page(), "act_123", "act_123", "2026-09-01")
    assert [o for o in objects if o["level"] == "ad"] == []
    assert err.count("did not match this campaign") == 2


def test_clones_with_a_working_filter_are_kept(nameless):
    nameless["structure"] = {"camp A": {"id": "111", "ads": _Counter(["clone ad"])},
                             "camp B": {"id": "222", "ads": _Counter(["clone ad"])}}
    nameless["ads"] = {"111": ["clone ad"], "222": ["clone ad"]}
    # Разные деньги — разные строки: фильтр сработал, каждая под своей кампанией.
    nameless["spend"] = {"111": "$10.00", "222": "$20.00"}
    objects, _, err = sp._read_tree(_Page(), "act_123", "act_123", "2026-09-01")
    assert sorted(o["parent_id"] for o in objects if o["level"] == "ad") == ["111", "222"]
    assert "did not match" not in err


def test_a_campaign_whose_page_data_lists_no_ads_takes_none(nameless):
    nameless["structure"]["camp B"] = {"id": "222", "ads": _Counter()}
    objects, _, err = sp._read_tree(_Page(), "act_123", "act_123", "2026-09-01")
    assert {o["parent_id"] for o in objects if o["level"] == "ad"} == {"111"}
    assert "did not match this campaign" in err


def test_an_id_table_with_nameless_ads_behaves_as_before(flow):
    """Кампании с id, объявления без id: прежнее правило неоднозначности на
    месте, и пометка «по имени» остаётся при своей кампании."""
    def _level(page, level):
        if level == "campaign":
            return ([sp.AdObject(level="campaign", id="11" * 8, name="camp A"),
                     sp.AdObject(level="campaign", id="22" * 8, name="camp B")], "data-surface", "")
        return ([sp.AdObject(level="ad", name="an ad")], "by-name",
                "read by name: this account's table has no ad ids")

    flow.setattr(sp, "read_level", _level)
    objects, _, err = sp._read_tree(_Page(), "act_123", "act_123", "2026-09-01")
    assert all(o["level"] == "campaign" for o in objects)
    assert "different campaigns" in err
    assert err.count("has no ad ids") == 2


def test_a_twin_in_the_page_data_is_not_called_missing(nameless):
    """Два id под одним именем в данных страницы — «не различить», а не «нет данных»."""
    nameless["structure"]["camp B"] = {"id": "", "ads": _Counter()}
    _, _, err = sp._read_tree(_Page(), "act_123", "act_123", "2026-09-01")
    assert "share a name" in err and "did not give their ids" not in err


def test_a_stale_selection_between_clones_is_not_attributed(nameless):
    """При заходе в B на экране осталась строка A: то же имя, те же деньги.
    Сверку по структуре она проходит — ловит её то, что строка одна и та же."""
    nameless["structure"] = {"camp A": {"id": "111", "ads": _Counter(["clone ad"])},
                             "camp B": {"id": "222", "ads": _Counter(["clone ad"])}}
    nameless["ads"] = {"111": ["clone ad"], "222": ["clone ad"]}
    nameless["spend"] = {"111": "$42.00", "222": "$42.00"}
    objects, _, err = sp._read_tree(_Page(), "act_123", "act_123", "2026-09-01")
    assert [o for o in objects if o["level"] == "ad"] == []
    assert "different campaigns" in err


def test_clones_that_never_spent_are_kept(nameless):
    nameless["structure"] = {"camp A": {"id": "111", "ads": _Counter(["clone ad"])},
                             "camp B": {"id": "222", "ads": _Counter(["clone ad"])}}
    nameless["ads"] = {"111": ["clone ad"], "222": ["clone ad"]}
    nameless["spend"] = {"111": "$0.00", "222": "$0.00"}
    objects, _, err = sp._read_tree(_Page(), "act_123", "act_123", "2026-09-01")
    assert len([o for o in objects if o["level"] == "ad"]) == 2
    assert "different campaigns" not in err
