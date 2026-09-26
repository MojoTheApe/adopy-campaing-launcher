# -*- coding: utf-8 -*-
"""«Копии нет» должно сопровождаться тем, что шаг видел (acl-1171).

15.09 на живом прогоне поиск по медиатеке трижды подряд ответил «копии нет» —
в том числе через полторы минуты после того, как **сам же её туда загрузил**.
Разобрать это было нечем: ни числа плиток, ни содержимого поля поиска, ни
подписей соседей. Весь вечер ушёл на догадки о том, что можно было прочитать.

Это оказалось не мелочью. В том же прогоне выяснилось, что Meta **не берёт**
вторую заливку того же файла в тот же кабинет: первое объявление загрузилось
за 120 секунд, второе застыло на `progress: 0` и отказало через 8.5 минут.
То есть повторное использование — не экономия трафика, как считалось, а
**единственный способ** дать креатив второму и третьему объявлению. Пока
поиск не находит, кампанию целиком не собрать.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import meta_dom as md  # noqa: E402


class _EmptyGrid:
    def count(self) -> int:
        return 0

    def nth(self, i):  # pragma: no cover — до плиток дело не доходит
        raise AssertionError("плиток нет, брать нечего")


@pytest.fixture
def blind(monkeypatch):
    """Сетка пуста, а страница отвечает на вопросы о себе."""
    monkeypatch.setattr(md, "_library_tiles", lambda page, name: _EmptyGrid())
    monkeypatch.setattr(md, "_library_search", lambda page, name: True)
    monkeypatch.setattr(md, "_library_selected_count", lambda page: 0)
    monkeypatch.setattr(md, "_library_any_tile_marked", lambda page: False)
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    said: list[dict] = []
    monkeypatch.setattr(md._telemetry, "record",
                        lambda step, ok, **kw: said.append({"step": step, "ok": ok, **kw}))
    return said


class _Page:
    """Страница, которая умеет отвечать про свои вкладки и подписи."""

    def __init__(self, captions=None, tabs=None, box_value="1_es_2292_post.mp4"):
        self._captions = captions if captions is not None else ["other_file.mp4"]
        self._tabs = tabs if tabs is not None else ["Images", "Videos *", "Upload"]
        self._box = box_value

    def get_by_placeholder(self, *_a, **_k):
        page = self

        class _Box:
            first = None

            def count(self_inner):
                return 1

            def input_value(self_inner, timeout=0):
                return page._box

        box = _Box()
        box.first = box
        return box

    def evaluate(self, script):
        return self._captions if "mp4|mov" in script else self._tabs


def test_an_empty_library_is_reported_with_what_was_on_screen(blind):
    page = _Page()
    assert md._try_reuse_library_media(page, "1_es_2292_post.mp4") == md.REUSE_ABSENT

    assert len(blind) == 1, "ровно одна запись: дубль в телеметрии хуже молчания"
    rec = blind[0]
    assert rec["step"] == "ad_creative_reuse"
    assert rec["note"] == "not_in_library"
    sight = rec.get("diag") or {}
    assert sight.get("looked_for") == "1_es_2292_post.mp4"
    assert "captions" in sight, "подписи соседей — по ним видно, под каким именем файл лежит"
    assert "tabs" in sight, "вкладка — поиск по чужой объяснил бы всё разом"
    assert "search_box" in sight, "что реально стоит в поиске, а не что мы туда слали"


def test_the_grid_is_asked_more_than_once(monkeypatch):
    """Свежезагруженный файл появляется не мгновенно — переспрашиваем.

    Но недолго: 15.09 он не нашёлся и через полторы минуты, так что если
    задержка индексации такая, повторное использование всё равно не спасёт.
    """
    tries = {"n": 0}

    class _Late:
        def count(self_inner):
            tries["n"] += 1
            return 0

        def nth(self_inner, i):  # pragma: no cover
            raise AssertionError

    monkeypatch.setattr(md, "_library_tiles", lambda page, name: _Late())
    monkeypatch.setattr(md, "_library_search", lambda page, name: True)
    monkeypatch.setattr(md, "_library_selected_count", lambda page: 0)
    monkeypatch.setattr(md, "_library_any_tile_marked", lambda page: False)
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(md._telemetry, "record", lambda *a, **k: None)

    md._try_reuse_library_media(_Page(), "x.mp4")
    assert tries["n"] == md._LIBRARY_APPEAR_TRIES > 1


def test_waiting_is_bounded(monkeypatch):
    """Ждать без конца нельзя: три объявления на кампанию и шесть кампаний в
    круге, и каждая лишняя секунда умножается на восемнадцать."""
    assert md._LIBRARY_APPEAR_TRIES <= 8


def test_a_broken_page_does_not_break_the_report(blind):
    """Страница может не ответить на вопрос о себе. Это не повод уронить шаг —
    записываем, что смогли, и идём грузить."""

    class _Mute(_Page):
        def evaluate(self, script):
            raise RuntimeError("страница перерисовывается")

    assert md._try_reuse_library_media(_Mute(), "x.mp4") == md.REUSE_ABSENT
    sight = blind[0].get("diag") or {}
    assert "captions_error" in sight or "captions" in sight


# ── «нашли, но не взяли» тоже обязано рассказать ─────────────────────────


class _StuckGrid:
    """Плитки есть, клики проходят, а счётчик не двигается.

    Ровно то, что случилось 15.09 во второй кампании: копия в медиатеке уже
    лежала — её загрузила первая кампания, — поиск её нашёл, а взять не смог.
    """

    def __init__(self, tiles: int = 3, sticky: bool = True):
        self._tiles = tiles
        self.sticky = sticky
        self.selected: set[int] = set()
        #: Счётчик «просыпается» только когда мы пошли снимать за собой —
        #: то есть клик регистрируется ПОЗЖЕ, чем мы перестали его ждать.
        #: Это не выдумка: так описано в самом коде и измерено 24.08.
        self.awake = False
        #: После скольких опросов счётчика правда всплывает.
        #: Больше, чем длится ожидание клика, и меньше, чем пауза в снятии.
        self.wakes_after = tiles * 11 + 2

    def count(self) -> int:
        return self._tiles

    def nth(self, i: int):
        grid = self

        class _Tile:
            def scroll_into_view_if_needed(self_inner, timeout=0):
                pass

            def click(self_inner, timeout=0):
                if i in grid.selected:
                    grid.awake = True  # второй клик по той же плитке — это снятие
                    if not grid.sticky:
                        grid.selected.discard(i)
                else:
                    grid.selected.add(i)

        return _Tile()


def _wire(monkeypatch, grid):
    """Счётчик сетки, который «просыпается» поздно.

    Пока мы ждём подтверждения клика, он отвечает нулём — значит выбор
    считается несостоявшимся. А на экране он уже есть, и обнаруживается это
    только когда мы идём снимать за собой. Ровно этим и опасно повторное
    использование: загрузка поверх такого невидимого выбора даёт объявление
    с двумя креативами.
    """
    reads = {"n": 0}

    def count(_page):
        # Счётчик просыпается сам, после того как мы перестали ждать клик:
        # `_library_undo_selection` начинается с паузы именно на этот случай.
        reads["n"] += 1
        if reads["n"] > grid.wakes_after:
            grid.awake = True
        return len(grid.selected) if grid.awake else 0

    said: list[dict] = []
    monkeypatch.setattr(md, "_library_tiles", lambda page, name: grid)
    monkeypatch.setattr(md, "_library_search", lambda page, name: True)
    monkeypatch.setattr(md, "_library_selected_count", count)
    monkeypatch.setattr(md, "_library_any_tile_marked", lambda page: bool(grid.selected))
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(md._telemetry, "record",
                        lambda step, ok, **kw: said.append({"step": step, "ok": ok, **kw}))
    return said


def test_a_pick_that_did_not_take_reports_the_attempts(monkeypatch):
    """Что кликали и что показывал счётчик — иначе чинить нечего."""
    said = _wire(monkeypatch, _StuckGrid(tiles=3, sticky=False))
    md._try_reuse_library_media(_Page(), "1_es_2292_post.mp4")

    assert len(said) == 1, "одна запись на событие"
    diag = said[0].get("diag") or {}
    assert diag.get("tiles_found") == 3, "сколько копий нашлось"
    picks = diag.get("picks") or []
    assert picks, "дневник выбора обязан быть непустым"
    assert {"tile", "before"} <= set(picks[0]), picks[0]
    assert "after" in picks[0], "счётчик после клика — главное число здесь"
    assert diag.get("undone") is True


def test_a_stuck_selection_is_reported_as_a_failure(monkeypatch):
    """Снять не удалось — это отказ, и он обязан нести тот же дневник."""
    said = _wire(monkeypatch, _StuckGrid(tiles=2, sticky=True))
    out = md._try_reuse_library_media(_Page(), "x.mp4")

    assert out == md.REUSE_DIRTY
    assert said[0]["ok"] is False
    assert said[0]["note"] == "library_pick_left_selection"
    assert (said[0].get("diag") or {}).get("undone") is False


def test_the_two_outcomes_are_named_apart(monkeypatch):
    """«Кликнул и снял» и «кликнул и застрял» — разные беды, и в телеметрии
    они не должны выглядеть одинаково."""
    clean = _wire(monkeypatch, _StuckGrid(sticky=False))
    md._try_reuse_library_media(_Page(), "x.mp4")
    stuck = _wire(monkeypatch, _StuckGrid(sticky=True))
    md._try_reuse_library_media(_Page(), "x.mp4")
    assert clean[0]["note"] != stuck[0]["note"]
