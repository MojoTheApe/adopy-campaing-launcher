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
    said: list[tuple] = []
    monkeypatch.setattr(md._telemetry, "record", lambda step, ok=None, **k: said.append((step, ok, k.get("note"))))
    uploads: list[bool] = []

    def fake_upload(page, ct, cp, *, edit_url="", reuse_only=False):
        uploads.append(reuse_only)
        return not reuse_only

    monkeypatch.setattr(md, "upload_ad_creative", fake_upload)
    md.upload_ad_creative_with_retry(object(), "video", FILE)
    assert md.upload_ad_creative_with_retry(object(), "video", FILE) is False
    assert uploads == [False, True], "после отказа медиатеки не должно быть настоящей заливки"
    assert ("ad_creative_upload", False, "same_file_already_uploaded_this_run") in said


# ── reuse_only внутри самого шага ─────────────────────────────────────────

def test_reuse_only_stops_before_the_upload_and_closes_the_wizard(monkeypatch):
    """Мастер открыт, копии нет — закрыть и выйти, ничего не заливая."""
    closed: list[bool] = []
    src = __import__("inspect").getsource(md.upload_ad_creative)
    assert "if reuse_only:" in src and "_close_creative_setup_modal(page)" in src.split("if reuse_only:")[1][:400]
    assert "library_has_no_copy" in src


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
    monkeypatch.setattr(md, "_library_selected_count", lambda page: 1)   # всегда 1: эксклюзивно
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(md, "_close_creative_setup_modal", lambda page: True)
    monkeypatch.setattr(md, "_verify_ad_media_present", lambda page: True)
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
