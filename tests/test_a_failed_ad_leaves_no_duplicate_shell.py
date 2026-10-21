# -*- coding: utf-8 -*-
"""ACL-1675: повтор создания объявления после перезагрузки не плодит пустые.

Первое `create_additional_ad` могло упасть, уже оставив в дереве заготовку
«New Leads Ad»; `recover_stuck_page` перезагружал редактор, и второй create
добавлял ещё одну — пустое объявление-дубликат. Теперь заготовку опознаём
по id строки дерева (`ads_campaign_structure_item_<id>`): «своя» — только id,
которого не было в снимке перед созданием. Место строки в дереве ничего не
значит (живые снимки: свежая то сверху, то посередине, то снизу), поэтому
выбор строки проверяется настоящим JS на сохранённом дереве
(fixtures/ads_manager/editor_tree_new_shell_between_*, снимок
4e4969867d28/12_ad4_create). Заготовку упавшего объявления N следующее не
берёт и не удаляет: она остаётся в телеметрии `adN_leftover_shell`.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import meta_dom as md  # noqa: E402
from tests.test_additional_ad_without_creative_has_no_text import flow  # noqa: E402,F401

FIX = Path(__file__).parent / "fixtures" / "ads_manager"
TREE = (FIX / "editor_tree_new_shell_between_2026_10_07.html").read_text(encoding="utf-8")

P = md._SHELL_ROW_PREFIX
FRESH = P + "100000000000005"        # свежая заготовка: третья из четырёх объявлений, посередине
ABOVE = P + "100000000000003"        # заготовка упавшего объявления выше свежей
BELOW = P + "100000000000006"        # заготовка упавшего объявления ниже свежей


def _tree(*, above: bool = False, below: bool = False, spacer: int = 0) -> str:
    html = TREE
    if above:
        html = html.replace("Sample Ad 2", "New Leads Ad")
    if below:
        html = html.replace("Sample Ad 3", "New Leads Ad")
    return (f"<div style='height:{spacer}px'></div>" if spacer else "") + html


# ── настоящий JS на дереве ──────────────────────────────────────────────────

@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        try:
            yield b
        finally:
            b.close()


@pytest.fixture
def page(browser, monkeypatch):
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(md, "wait_editor_saved", lambda p: True)
    monkeypatch.setattr(md, "_tree_open_refused", lambda *a, **k: False)
    monkeypatch.setattr(md, "_close_creative_setup_modal", lambda p: None)
    monkeypatch.setattr(md._telemetry, "record", lambda *a, **k: None)
    md._ADOPTED_SHELL_IDS.clear()
    ctx = browser.new_context(viewport={"width": 1500, "height": 800})
    p = ctx.new_page()
    clicks: list[str | None] = []

    def click(pg, x, y):
        clicks.append(pg.evaluate(
            "([x, y]) => { const e = document.elementFromPoint(x, y);"
            " const r = e && e.closest('[id^=\"ads_campaign_structure_item_\"]'); return r ? r.id : null; }",
            [x, y]))

    monkeypatch.setattr(md, "_cdp_mouse_click", click)
    p.clicks = clicks
    try:
        yield p
    finally:
        ctx.close()


def test_the_tree_read_gives_the_row_ids_of_every_shell(page):
    page.set_content(_tree(above=True, below=True))
    assert md._tree_shell_ids(page) == frozenset({ABOVE, FRESH, BELOW})


def test_a_page_without_the_tree_reads_as_no_shells_not_unreadable(page):
    page.set_content("<div>nothing</div>")
    assert md._tree_shell_ids(page) == frozenset()


@pytest.mark.parametrize("above,below", [(False, True), (True, False), (True, True)])
def test_the_fresh_shell_is_taken_whatever_its_place_among_leftovers(page, monkeypatch, above, below):
    """Утёкшие заготовки выше и/или ниже свежей: открывается именно свежая."""
    page.set_content(_tree(above=above, below=below))
    before = frozenset(i for i, on in ((ABOVE, above), (BELOW, below)) if on)
    created = []
    monkeypatch.setattr(md, "create_additional_ad", lambda p, n: created.append(1) or True)
    assert md._adopt_shell_or_recreate(page, "set", before) is True
    assert page.clicks == [FRESH]
    assert created == [] and FRESH in md._ADOPTED_SHELL_IDS
    assert not ({ABOVE, BELOW} & md._ADOPTED_SHELL_IDS)


def test_the_fresh_shell_is_scrolled_into_view_before_the_click(page, monkeypatch):
    page.set_content(_tree(below=True, spacer=3000))
    monkeypatch.setattr(md, "create_additional_ad", lambda p, n: pytest.fail("created"))
    assert md._adopt_shell_or_recreate(page, "set", frozenset({BELOW})) is True
    assert page.clicks == [FRESH], "клик ушёл мимо строки за краем экрана"


def test_rows_marked_adgroup_are_read_without_the_aria_fallback(page):
    """Живая страница: `data-objecttype="ADGROUP"` — основной признак строки
    объявления (сохранённые снимки его теряют, там работает aria-label)."""
    html = (_tree(above=True, below=True)
            .replace('aria-label="Ad" id=', 'data-objecttype="ADGROUP" id=')
            .replace('aria-label="Ad set" id=', 'data-objecttype="CAMPAIGN" id='))
    assert 'aria-label="Ad"' not in html and 'data-objecttype="ADGROUP"' in html
    page.set_content(html)
    assert md._tree_shell_ids(page) == frozenset({ABOVE, FRESH, BELOW})


def test_only_leftovers_in_the_tree_means_nothing_to_adopt_and_a_clean_create(page, monkeypatch):
    page.set_content(_tree(above=True, below=True))
    created = []
    monkeypatch.setattr(md, "create_additional_ad", lambda p, n: created.append(1) or True)
    assert md._adopt_shell_or_recreate(page, "set", frozenset({ABOVE, FRESH, BELOW})) is True
    assert page.clicks == [] and created == [1]


def test_a_row_that_opens_another_ad_is_not_a_success(page, monkeypatch):
    page.set_content(_tree() + "<input placeholder='Enter your ad name here...' value='Sample Ad 1'>")
    monkeypatch.setattr(md, "create_additional_ad", lambda p, n: pytest.fail("created"))
    assert md._adopt_shell_or_recreate(page, "set", frozenset()) is False


# ── цикл ожидания в create_additional_ad (45 с) ─────────────────────────────

def test_the_wait_loop_adopts_only_a_new_id_when_leftovers_exist(monkeypatch):
    shown = {"name": False}
    reads = iter([frozenset({ABOVE, BELOW}),                 # снимок до «Create ad»
                  frozenset({ABOVE, BELOW}),                 # первая проверка цикла: новой нет
                  frozenset({ABOVE, BELOW, FRESH})])         # заготовка появилась поздно
    last = {"v": frozenset({ABOVE, BELOW, FRESH})}

    def ids(p):
        last["v"] = next(reads, last["v"])
        return last["v"]

    opened = []

    def open_by_id(p, rid):
        opened.append(rid)
        shown["name"] = True
        return True

    box = SimpleNamespace(is_visible=lambda timeout=None: shown["name"])
    page = SimpleNamespace(url="https://adsmanager.facebook.com/adsmanager/manage/ads/edit",
                           get_by_placeholder=lambda t: SimpleNamespace(first=box))
    monkeypatch.setattr(md, "_tab_frozen", lambda p: False)
    monkeypatch.setattr(md, "_wait_tree_ready", lambda p, *a, **k: True)
    monkeypatch.setattr(md, "_click_adset_action_menu", lambda p: True)
    monkeypatch.setattr(md, "_click_create_menu_item", lambda p: True)
    monkeypatch.setattr(md, "_creative_modal_open", lambda p: False)
    monkeypatch.setattr(md, "_close_creative_setup_modal", lambda p: None)
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(md, "_eval_bounded", lambda *a, **k: None)
    monkeypatch.setattr(md, "_tree_shell_ids", ids)
    monkeypatch.setattr(md, "_open_shell_by_id", open_by_id)
    monkeypatch.setattr(md, "_open_tree_ad", lambda *a, **k: pytest.fail("открытие по имени — самая левая строка"))
    md._ADOPTED_SHELL_IDS.clear()
    assert md.create_additional_ad(page, "set") is True
    assert opened == [FRESH]


# ── в потоке ────────────────────────────────────────────────────────────────

def _wire(monkeypatch, snapshots, create_results):
    """snapshots — очередь ответов `_tree_shell_ids` (последний повторяется)."""
    seen = {"create": 0, "open": []}
    queue = list(snapshots)

    def tree(p):
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def create(p, name):
        seen["create"] += 1
        return create_results[min(seen["create"], len(create_results)) - 1]

    def open_by_id(p, rid):
        seen["open"].append(rid)
        return True

    monkeypatch.setattr(md, "_tree_shell_ids", tree)
    monkeypatch.setattr(md, "create_additional_ad", create)
    monkeypatch.setattr(md, "_open_shell_by_id", open_by_id)
    monkeypatch.setattr(md, "recover_stuck_page", lambda *a, **k: True)
    monkeypatch.setattr(md, "_close_creative_setup_modal", lambda p: None)
    md._ADOPTED_SHELL_IDS.clear()
    return seen


def test_first_create_leaves_a_shell_then_reload_gives_exactly_one_ad(flow, monkeypatch):
    # до create дерево пусто; первый create упал, оставив заготовку; после перезагрузки она видна
    seen = _wire(monkeypatch, [frozenset(), frozenset({FRESH})], [False])
    flow["fail_upload"] = set()
    _result, soft = flow["run"]()
    assert seen["create"] == 1, "второй create добавил бы пустое объявление"
    assert seen["open"] == [FRESH] and "ad2_create" not in soft


def test_an_unreadable_tree_after_the_reload_does_not_create(flow, monkeypatch):
    seen = _wire(monkeypatch, [frozenset(), None], [False])
    flow["fail_upload"] = set()
    _result, soft = flow["run"]()
    assert seen["create"] == 1
    assert any(s.startswith("ad2_create") for s in soft)


def test_an_unreadable_snapshot_before_the_create_does_not_create_a_retry(flow, monkeypatch):
    seen = _wire(monkeypatch, [None], [False])
    flow["fail_upload"] = set()
    _result, soft = flow["run"]()
    assert seen["create"] == 1
    assert any(s.startswith("ad2_create") for s in soft)


def test_a_clean_tree_after_the_reload_still_retries_the_create(flow, monkeypatch):
    seen = _wire(monkeypatch, [frozenset(), frozenset()], [False, True])
    flow["fail_upload"] = set()
    flow["run"]()
    assert seen["create"] == 2


def test_a_shell_that_shows_up_late_never_gets_a_third_create(flow, monkeypatch):
    """Повтор после перезагрузки создавал (дерево было чистым), и тоже упал;
    заготовка появилась позже — усыновляем её, третьего create нет."""
    seen = _wire(monkeypatch, [frozenset(), frozenset(), frozenset({FRESH})], [False, False])
    flow["fail_upload"] = set()
    flow["run"]()
    assert seen["create"] == 2
    assert seen["open"] == [FRESH]


def test_a_clean_tree_at_the_retry_and_at_the_outer_adopt_creates_exactly_twice(flow, monkeypatch):
    """Повтор после перезагрузки создал (дерево чистое) и упал; снаружи дерево
    снова чистое — третьего create нет (`may_create=not retried`): всего два."""
    seen = _wire(monkeypatch, [frozenset()], [False, False, True])
    flow["fail_upload"] = set()
    _result, soft = flow["run"]()
    assert seen["create"] == 2
    assert any(s.startswith("ad2_create") for s in soft)


def test_the_next_ad_does_not_take_the_leftover_shell(monkeypatch):
    seen = _wire(monkeypatch, [frozenset({ABOVE})], [True])
    # заготовка одна и она старая (в снимке): новой нет — создаём, не открываем
    assert md._adopt_shell_or_recreate(object(), "set", frozenset({ABOVE})) is True
    assert seen["open"] == [] and seen["create"] == 1


def test_the_leftover_is_marked_for_the_audit_and_not_deleted(flow, monkeypatch):
    _wire(monkeypatch, [frozenset({ABOVE})], [True])
    records = []
    monkeypatch.setattr(md._telemetry, "record",
                        lambda step, ok=None, **kw: records.append((step, ok, kw.get("note"))))
    flow["fail_upload"] = set()
    flow["run"]()
    assert ("ad2_leftover_shell", None, "leftover_shell") in records
    assert not [r for r in records if r[0] == "ad2_create" and r[2] == "leftover_shell"]
