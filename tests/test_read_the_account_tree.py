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
    assert rows == [] and "что это за объекты" in err


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
    assert "фильтр" in err


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
    assert "разных кампаний" in err


def test_a_scrollable_snapshot_does_not_claim_a_complete_tree(page):
    page.set_content('<div style="height: 30px; overflow-y: auto">' + _grid([
        _row("111", "CAMPAIGN_GROUP", "first", "Active", "true"),
        _row("222", "CAMPAIGN_GROUP", "second", "Active", "true"),
    ]) + '</div>')
    objects, _, err = sp.read_level(page, "campaign")
    assert objects and "полнота не подтверждена" in err


# ── acl-113d: не ходить туда, где смотреть нечего ─────────────────────────
#
# Чтение аккаунта занимало 1,5-3 минуты: один заход за списком кампаний плюс
# по одному на каждую. Общий список объявлений доверия не заслужил (12.09 он
# отдал шесть строк на $661 при спенде аккаунта $2 117), так что заходы
# остаются — но выключенной кампании без спенда среди них делать нечего.


@pytest.fixture
def flow_mixed(monkeypatch):
    """Четыре кампании: тратившая, включённая пустая, выключенная пустая, без тумблера."""
    monkeypatch.setattr(sp, "_wait_for_grid", lambda page: True)
    monkeypatch.setattr(sp, "_act_from_url", lambda page, tries=1: "act_123")
    monkeypatch.setattr(sp, "_shown_period", lambda page, tries=12: "Sep 1, 2026 - Sep 10, 2026")
    monkeypatch.setattr(sp, "_period_matches", lambda shown, a, b="": True)

    camps = [
        sp.AdObject(level="campaign", id="1" * 8, name="потратила", switch="off", spend="$12.30"),
        sp.AdObject(level="campaign", id="2" * 8, name="включена пустая", switch="on", spend="$0.00"),
        sp.AdObject(level="campaign", id="3" * 8, name="выключена пустая", switch="off", spend="$0.00"),
        sp.AdObject(level="campaign", id="4" * 8, name="тумблер неизвестен", switch="unknown", spend=""),
    ]

    def _level(page, level):
        if level == "campaign":
            return (camps, "data-surface", "")
        return ([sp.AdObject(level="ad", id=str(9900 + len(page.visited)), name="an ad")], "data-surface", "")

    monkeypatch.setattr(sp, "read_level", _level)
    return monkeypatch


def test_an_idle_closed_campaign_costs_no_visit(flow_mixed):
    """Выключена и ничего не потратила — заходить незачем."""
    page = _Page()
    _, _, err = sp._read_tree(page, "act_123", "act_123", "2026-09-01", "2026-09-10")
    assert len(page.visited) == 4, f"заходов {len(page.visited)}, а надо 1 + 3"
    assert "3" * 8 not in " ".join(page.visited), "сходили в выключенную пустую"
    assert "не спрашивались у 1" in err, err


def test_a_closed_campaign_that_spent_is_still_read(flow_mixed):
    """Деньги за период потрачены — человек вправе увидеть, на что."""
    page = _Page()
    sp._read_tree(page, "act_123", "act_123", "2026-09-01", "2026-09-10")
    assert "selected_campaign_ids=" + "1" * 8 in " ".join(page.visited)


def test_a_live_campaign_without_spend_is_still_read(flow_mixed):
    """Включена и пуста — может быть запущена сегодня."""
    page = _Page()
    sp._read_tree(page, "act_123", "act_123", "2026-09-01", "2026-09-10")
    assert "selected_campaign_ids=" + "2" * 8 in " ".join(page.visited)


def test_an_unknown_switch_is_not_taken_for_off(flow_mixed):
    """Не знаем положения тумблера — значит не пропускаем."""
    page = _Page()
    sp._read_tree(page, "act_123", "act_123", "2026-09-01", "2026-09-10")
    assert "selected_campaign_ids=" + "4" * 8 in " ".join(page.visited)


def test_skipping_is_said_out_loud_not_silently(flow_mixed):
    """Дерево без этих объявлений выглядит как полное — значит надо сказать."""
    _, _, err = sp._read_tree(_Page(), "act_123", "act_123", "2026-09-01", "2026-09-10")
    assert err, "пропуск прошёл молча"


@pytest.mark.parametrize(
    "text, spent",
    [
        ("$12.30", True), ("$0.00", False), ("", False), ("—", False),
        ("0,00 ₪", False), ("1 957,42 ₪", True), ("$0.14", True), ("0", False),
    ],
)
def test_what_counts_as_spent(text, spent):
    """Число не разбираем: у Meta своя валюта, свои разделители и своя локаль."""
    assert sp._looks_spent(text) is spent


# ── таблица, которая ещё грузится ─────────────────────────────────────────
#
# `read_level` отказывается, пока таблица помечена занятой, — и правильно:
# читать недогруженную значит получить половину строк с видом целого. Но
# звали её ровно один раз, и отказ был окончательным. 15.09 на живом кабинете
# дерево не прочиталось ни разу из четырёх: «таблица campaign: table-loading»
# при исправном спенде рядом. Выглядело это как «дерево пустое».

def test_a_loading_table_is_read_again_not_given_up_on(flow, monkeypatch):
    """Пока таблица грузится — ждём и спрашиваем снова."""
    monkeypatch.setattr(sp, "_sleep", lambda *a, **k: None)
    calls = {"n": 0}

    def _level(page, level):
        calls["n"] += 1
        if level == "campaign" and calls["n"] == 1:
            return ([], "", "таблица campaign: table-loading")
        if level == "campaign":
            return ([sp.AdObject(level="campaign", id="11" * 8, name="camp A")], "data-surface", "")
        return ([sp.AdObject(level="ad", id="9901", name="an ad")], "data-surface", "")

    flow.setattr(sp, "read_level", _level)
    objects, _how, err = sp._read_tree(_Page(), "act_123", "act_123", "2026-09-01", "2026-09-10")
    assert not err, err
    assert [o["level"] for o in objects] == ["campaign", "ad"]
    assert calls["n"] >= 2, "сдались после первого отказа"


def test_a_table_that_never_settles_is_a_named_failure(flow, monkeypatch):
    """Не дождались — так и сказано, а не «дерево пустое»."""
    monkeypatch.setattr(sp, "_sleep", lambda *a, **k: None)
    flow.setattr(sp, "read_level",
                 lambda page, level: ([], "", "таблица campaign: table-loading"))
    objects, _how, err = sp._read_tree(_Page(), "act_123", "act_123", "2026-09-01", "2026-09-10")
    assert objects == []
    assert "table-loading" in err


# ── обёртки без собственной рамки (`display: contents`) ───────────────────
#
# 15.09, живой кабинет KLD_BFL_X84 со спендом $3 344 за период: элементов с
# меткой строки 163, под образец подходят 155, **видимых — ноль**. Все до
# одной `display: contents`: у обёртки нет своей рамки, а содержимое видно, и
# Meta вешает свою разметку `data-surface` именно на такие. Проверка «есть ли
# рамка» отбрасывала их все, дерево читалось пустым, и выглядело это как «на
# аккаунте ничего нет» — при исправно прочитанном спенде рядом.


def _boxless_row(obj_id: str, kind: str, name: str) -> str:
    """Строка, размеченная так, как её рисует Meta сегодня.

    Обёртки с `data-surface` — `display: contents`: своей рамки нет, а текст
    лежит глубже, в узлах с рамками. Поэтому `innerText` у обёртки работает,
    хотя `getClientRects()` у неё пуст.
    """
    base = f"/am/table/table_row:{obj_id}unit/table_cell:"
    box = 'style="display:block"'
    return (
        f'<div role="presentation" data-surface="/am/table/table_row:{obj_id}unit">'
        f'<span data-surface="{base}forObjectType(toggle,{kind})">'
        f'<span {box}><span role="switch" aria-checked="true" aria-label="On/off"></span></span></span>'
        f'<span data-surface="{base}forObjectType(name,{kind})"><span {box}>{name}</span></span>'
        f'<span data-surface="{base}forObjectType(delivery,{kind})"><span {box}>Active</span></span>'
        f'<span data-surface="{base}spend"><span {box}>$412.02</span></span>'
        f"</div>"
    )


def test_rows_marked_on_boxless_wrappers_are_still_read(page):
    """`display: contents` — не «невидимо», а «нет своей рамки»."""
    page.set_content(
        "<style>[data-surface]{display:contents}</style>"
        + _grid([_boxless_row("12345678900000000303", "CAMPAIGN_GROUP", "Live campaign")])
    )
    rows, how, err = sp.read_level(page, "campaign")
    assert not err, err
    assert [r.name for r in rows] == ["Live campaign"]
    assert rows[0].switch == "on" and rows[0].delivery == "Active"


def test_a_truly_hidden_wrapper_is_still_skipped(page):
    """Спрятанное остаётся спрятанным: послабление только про рамку."""
    page.set_content(
        "<style>[data-surface]{display:contents;visibility:hidden}</style>"
        + _grid([_boxless_row("12345678900000000303", "CAMPAIGN_GROUP", "Hidden")])
    )
    rows, _how, err = sp.read_level(page, "campaign")
    assert rows == []
    assert err, "пустой ответ без объяснения читается как «строк нет»"
