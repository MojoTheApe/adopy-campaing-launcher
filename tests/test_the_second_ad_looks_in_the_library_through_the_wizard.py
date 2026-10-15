# -*- coding: utf-8 -*-
"""Второе объявление ищет копию там, где она есть, и верит плитке (acl-1192).

Три параллельных батча 16.09, каждая кампания с двумя-тремя объявлениями:
первое объявление получало медиа, второе и третье — нет. Телеметрия второго:

    ad_creative_reuse   not_in_library   search_box: null, captions: []
    ad_creative_upload  False            same_file_already_uploaded_this_run

`search_box: null` — медиатека не открыта. Мемо «файл уже загружен в этом
прогоне» шло за копией **на голую страницу объявления** (снимок: кнопка «Set up
creative», «Please specify the media»), а медиатека живёт на шаге Media
мастера. Копия была — диалога не было.

Второе: сам выбор. Условие «счётчик вырос» писалось для пустой сетки; на живой
странице он уже 1 до клика (acl-1192, шесть кликов `before: 1, after: 1`), а
выбор эксклюзивен — вырасти ему некуда. Теперь считается и слово самой плитки.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import meta_dom as md  # noqa: E402

FILE = "C:/creatives/1_es_2292_post.mp4"


# ── мемо идёт в медиатеку через мастер ────────────────────────────────────

@pytest.fixture
def quiet(monkeypatch):
    monkeypatch.setattr(md, "_close_creative_setup_modal", lambda page: True)
    monkeypatch.setattr(md, "_verify_ad_media_present", lambda page: False)
    monkeypatch.setattr(md, "_upload_stalled", lambda page: False)
    monkeypatch.setattr(md._telemetry, "record", lambda *a, **k: None)
    md._telemetry.set_current_run("run-x", "camp")
    md._uploaded_ok.clear(); md._failed_uploads.clear()
    yield
    md._set_upload_why(None)
    md._uploaded_ok.clear(); md._failed_uploads.clear()


def test_the_memo_path_goes_through_the_wizard_not_the_bare_page(quiet, monkeypatch):
    calls: list[dict] = []

    def fake_upload(page, creative_type, creative_path, *, edit_url="", reuse_only=False):
        calls.append({"reuse_only": reuse_only})
        return True

    monkeypatch.setattr(md, "upload_ad_creative", fake_upload)
    assert md.upload_ad_creative_with_retry(object(), "video", FILE) is True
    assert md.upload_ad_creative_with_retry(object(), "video", FILE) is True
    assert calls == [{"reuse_only": False}, {"reuse_only": True}]


def test_a_copy_found_through_the_wizard_attaches_the_second_ad(quiet, monkeypatch):
    monkeypatch.setattr(md, "upload_ad_creative",
                        lambda page, ct, cp, *, edit_url="", reuse_only=False: True)
    md.upload_ad_creative_with_retry(object(), "video", FILE)
    assert md.upload_ad_creative_with_retry(object(), "video", FILE) is True


def test_no_copy_is_a_named_refusal_not_an_upload(quiet, monkeypatch):
    """ACL-1637: «копии нет» ведёт к одной свежей заливке, но только если файл
    до Meta не доходил. Файл, который Meta уже отвергла, второй раз не грузим:
    объявление отказывает именованной причиной."""
    said: list[tuple] = []
    monkeypatch.setattr(md._telemetry, "record", lambda step, ok=None, **k: said.append((step, ok, k.get("note"))))
    uploads: list[bool] = []

    def fake_upload(page, ct, cp, *, edit_url="", reuse_only=False):
        uploads.append(reuse_only)
        if reuse_only:
            md._set_upload_why(md.LIBRARY_HAS_NO_COPY)
        return not reuse_only

    monkeypatch.setattr(md, "upload_ad_creative", fake_upload)
    md.upload_ad_creative_with_retry(object(), "video", FILE)
    # Файл уже отвергнут Meta на соседнем объявлении.
    md._failed_uploads[md._upload_memo_key(FILE)] = "media_refused_on_ad"
    assert md.upload_ad_creative_with_retry(object(), "video", FILE) is False
    assert uploads == [False, True], "отвергнутый Meta файл заново не грузим"
    assert ("ad_creative_upload", False, "same_file_already_uploaded_this_run") in said


# ── reuse_only внутри самого шага ─────────────────────────────────────────

def _reuse_only_block():
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(md.upload_ad_creative)))
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and isinstance(node.test, ast.Name) and node.test.id == "reuse_only":
            return node
    raise AssertionError("в upload_ad_creative нет ветки `if reuse_only:`")


def _calls_in(nodes) -> list[str]:
    import ast

    names = []
    for top in nodes:
        for n in ast.walk(top):
            if isinstance(n, ast.Call):
                f = n.func
                names.append(f.id if isinstance(f, ast.Name) else getattr(f, "attr", ""))
    return names


def test_reuse_only_stops_before_the_upload_and_closes_the_wizard():
    """Правило ACL-1637: в режиме reuse_only копию ищем и по имени сжатого
    файла, при её отсутствии закрываем мастер и называем причину; заливки в
    этой ветке нет — её решает вызывающий."""
    import ast

    block = _reuse_only_block()
    calls = _calls_in(block.body)
    assert "_reuse_known_copy" in calls, "медиатека ищется и по имени сжатой копии"
    assert "_close_creative_setup_modal" in calls, "мастер закрыт"
    assert block.body and isinstance(block.body[-1], ast.Return), "ветка выходит, не заливая"
    assert not {"set_input_files", "_upload_in_progress"} & set(calls)
    dumped = ast.dump(block)
    assert "LIBRARY_HAS_NO_COPY" in dumped or "library_has_no_copy" in dumped
    assert calls.index("_reuse_known_copy") < calls.index("_close_creative_setup_modal")


def test_a_failed_library_pick_is_named_left_selection():
    """`known is False` (выбор не снялся) — это `library_pick_left_selection`."""
    import ast

    block = _reuse_only_block()
    found = False
    for n in ast.walk(block):
        if (isinstance(n, ast.If) and isinstance(n.test, ast.Compare)
                and isinstance(n.test.left, ast.Name) and n.test.left.id == "known"
                and isinstance(n.test.ops[0], ast.Is)
                and getattr(n.test.comparators[0], "value", None) is False):
            found = "library_pick_left_selection" in ast.dump(n)
    assert found


# ── выбор плитки при непустом счётчике ────────────────────────────────────

class _Tile:
    def __init__(self, grid, index):
        self._grid, self._index = grid, index

    def scroll_into_view_if_needed(self, timeout=0):
        pass

    def click(self, timeout=0):
        self._grid.chosen = self._index

    def get_attribute(self, name):
        if name == "aria-pressed" and self._grid.chosen == self._index:
            return "true"
        return None

    def locator(self, sel):
        class _Box:
            def count(self_inner):
                return 0

        return _Box()


class _Grid:
    """Живая сетка: что-то уже выбрано (счётчик 1), выбор эксклюзивен."""

    def __init__(self, tiles=3):
        self._tiles = tiles
        self.chosen = -1  # чужой выбор, счётчик уже 1

    def count(self):
        return self._tiles

    def nth(self, i):
        return _Tile(self, i)


def _wire(monkeypatch, grid):
    monkeypatch.setattr(md, "_library_tiles", lambda page, filename: grid)
    monkeypatch.setattr(md, "_library_search", lambda page, filename: True)
    monkeypatch.setattr(md, "_tile_thumb_ids", lambda tile: ["111111_2222222222_3333333333_n"])
    monkeypatch.setattr(md, "_library_selected_count", lambda page: 1)   # всегда 1: эксклюзивно
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(md, "_close_creative_setup_modal", lambda page: True)
    monkeypatch.setattr(md, "_verify_ad_media_present", lambda page: True)
    # These grid tests isolate selection; attachment identity has its own
    # browser tests and must be explicitly confirmed by this fixture.
    monkeypatch.setattr(md, "_verify_ad_media_is_the_file", lambda page, filename, picked=None: (True, {}))
    monkeypatch.setattr(md._telemetry, "record", lambda *a, **k: None)

    class _Next:
        def count(self): return 1
        def is_disabled(self): return False
        def click(self, timeout=0): pass

    class _Dialog:
        def is_visible(self, timeout=0): return True
        def get_by_role(self, role, name=""): return type("L", (), {"first": _Next()})()

    class _Root:
        def get_by_role(self, role): return type("L", (), {"first": _Dialog()})()

    monkeypatch.setattr(md, "_roots", lambda page: [_Root()])
    monkeypatch.setattr(md, "_LIBRARY_APPEAR_TRIES", 1)
    page = type("P", (), {"url": "https://adsmanager.facebook.com/adsmanager/manage/ads/edit/standalone"})()
    return page


def test_a_tile_that_says_it_is_selected_is_a_pick_even_when_the_counter_stays_at_one(monkeypatch):
    grid = _Grid()
    page = _wire(monkeypatch, grid)
    assert md._try_reuse_library_media(page, "1_es_2292_post.mp4") == md.REUSE_ATTACHED
    assert grid.chosen == 0, "выбрана не первая подходящая плитка"


def test_the_counter_test_still_works_on_an_empty_grid(monkeypatch):
    grid = _Grid()
    page = _wire(monkeypatch, grid)
    counter = {"n": 0}
    monkeypatch.setattr(md, "_library_selected_count", lambda page: counter["n"])
    orig_click = _Tile.click

    def click(self, timeout=0):
        orig_click(self, timeout); counter["n"] = 1

    monkeypatch.setattr(_Tile, "click", click)
    monkeypatch.setattr(_Tile, "get_attribute", lambda self, name: None)
    assert md._try_reuse_library_media(page, "1_es_2292_post.mp4") == md.REUSE_ATTACHED


def test_a_tile_without_a_word_of_its_own_does_not_pass_on_its_own(monkeypatch):
    """Двойник без ARIA и без роста счётчика — по-прежнему «не взялось»."""
    grid = _Grid()
    page = _wire(monkeypatch, grid)
    monkeypatch.setattr(_Tile, "get_attribute", lambda self, name: None)
    monkeypatch.setattr(md, "_library_undo_selection", lambda page, tiles, clicked, before: True)
    assert md._try_reuse_library_media(page, "1_es_2292_post.mp4") == md.REUSE_ABSENT


def test_a_copy_already_selected_is_taken_without_a_click(monkeypatch):
    """16.09 23:28 (S15): «1 selected», у плитки `data-test-decoration="selected"`
    на обёртке. Клик по ней снимал выбор, и шаг решал, что копии нет."""
    grid = _Grid()
    page = _wire(monkeypatch, grid)
    monkeypatch.setattr(_Tile, "get_attribute", lambda self, name: None)
    monkeypatch.setattr(_Tile, "evaluate", lambda self, js: self._index == 1, raising=False)
    clicks = []
    monkeypatch.setattr(_Tile, "click", lambda self, timeout=0: clicks.append(self._index))
    assert md._try_reuse_library_media(page, "2046_1.mp4") == md.REUSE_ATTACHED
    assert clicks == []


def test_the_decoration_on_the_wrapper_marks_a_real_tile_selected():
    from playwright.sync_api import sync_playwright
    import os
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        p = b.new_page()
        p.set_content(
            '<div data-test-decoration="selected"><div role="button" tabindex="0" id="a"><div>2046_1.mp4</div></div></div>'
            '<div><div role="button" tabindex="0" id="b"><div>2046_1.mp4</div></div></div>'
        )
        assert md._library_tile_selected(p.locator("#a")) is True
        assert md._library_tile_selected(p.locator("#b")) is False
        b.close()


def test_media_already_on_the_ad_is_recognised_by_todays_labels():
    """17.09 (S15, 3-е объявление): «Uploaded media · 1 selected · Change selections»."""
    from playwright.sync_api import sync_playwright
    import os
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        p = b.new_page()
        p.set_content("<div>Media</div><div>Uploaded media</div><div>1 selected</div>"
                      "<div role='button'>Change selections</div><div role='button'>Customize media</div>")
        assert md._media_block_filled_now(p) is True
        p.set_content("<div>Media</div><div>Please specify the media to run with this ad.</div>")
        assert md._media_block_filled_now(p) is False
        b.close()


def test_a_different_thumbnail_after_selection_stays_dirty(monkeypatch):
    """Чужая миниатюра на объявлении после выбора — отказ без загрузки.

    Порт ACL-1284, вариант A (решение владельца 24.09): раньше здесь стояло
    «нечитаемое медиа после выбора — dirty» (строгая проверка #238). В живом
    коде нечитаемое медиа отказом не является — наличие медиа проверено
    отдельно, и сверка миниатюр пишет `thumb: unconfirmed`. Инвариант, который
    остаётся: другой файл на объявлении — никогда не наш креатив.
    """
    verify = md._verify_ad_media_is_the_file
    grid = _Grid()
    page = _wire(monkeypatch, grid)
    monkeypatch.setattr(md, "_verify_ad_media_is_the_file", verify)
    monkeypatch.setattr(md, "_ad_media_thumb_ids", lambda page: ["999999_8888888888_7777777777_n"])
    assert md._try_reuse_library_media(page, "2012.mp4") == md.REUSE_DIRTY
