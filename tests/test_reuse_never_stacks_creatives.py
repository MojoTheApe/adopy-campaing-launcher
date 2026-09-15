# -*- coding: utf-8 -*-
"""Повторное использование медиатеки не может дать объявление с шестью креативами.

Возможность искать уже загруженный файл вместо повторной заливки включали
трижды и дважды выключали. Второе отключение (24.08) стоило того, что до сих
пор каждый прогон гонит один и тот же файл заново: 15.09 живой креатив весил
247 МБ, и за круг из шести кампаний он уезжал в Meta восемнадцать раз.

Причина отключения была не в поиске, а в том, что **исходов было два на три
случая**. «Копии нет» и «выбрал, но не довёл» возвращали одинаковое `False`,
и вызывающий в обоих случаях грузил файл — во втором поверх уже выбранного.
Объявления выходили с 3-6 креативами (прогоны 5bff134ea745, 770bd7e5fb7b),
Meta делала из лишних отдельные вариации, и публиковалось не то, что собрал
оператор.

Проверки ниже держат ровно это различие. Живого браузера здесь нет: сетка
медиатеки подменяется двойником, который считает клики и умеет вести себя как
та, что «не отпускает» выбор.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import meta_dom as md  # noqa: E402


class _Tile:
    def __init__(self, grid: "_Grid", index: int):
        self._grid = grid
        self._index = index

    def scroll_into_view_if_needed(self, timeout: int = 0) -> None:
        pass

    def click(self, timeout: int = 0) -> None:
        self._grid.click(self._index)


class _Grid:
    """Двойник сетки медиатеки.

    `sticky` — плитки, которые нельзя снять обратно: именно так ведёт себя
    настоящая сетка, когда мастер уже ушёл вперёд.
    """

    def __init__(self, tiles: int = 3, *, sticky: bool = False):
        self._tiles = tiles
        self.selected: set[int] = set()
        self.sticky = sticky
        self.clicks: list[int] = []

    def count(self) -> int:
        return self._tiles

    def nth(self, i: int) -> _Tile:
        return _Tile(self, i)

    def click(self, i: int) -> None:
        self.clicks.append(i)
        if i in self.selected:
            if not self.sticky:
                self.selected.discard(i)
        else:
            self.selected.add(i)


@pytest.fixture
def grid(monkeypatch):
    g = _Grid()
    monkeypatch.setattr(md, "_library_tiles", lambda page, filename: g)
    monkeypatch.setattr(md, "_library_search", lambda page, filename: True)
    monkeypatch.setattr(md, "_library_selected_count", lambda page: len(g.selected))
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(md, "_close_creative_setup_modal", lambda page: True)
    monkeypatch.setattr(md, "_roots", lambda page: [])
    return g


def test_nothing_in_the_library_is_not_a_failure(grid, monkeypatch):
    """Обычный случай: файла нет — грузим, и сетка не тронута."""
    grid._tiles = 0
    assert md._try_reuse_library_media(object(), "x.mp4") == md.REUSE_ABSENT
    assert grid.clicks == []


def test_a_pick_that_cannot_finish_puts_the_grid_back(grid):
    """`_roots` пуст — мастер не продвинулся. Выбор обязан быть снят."""
    out = md._try_reuse_library_media(object(), "x.mp4")
    assert out == md.REUSE_ABSENT, "отступление должно разрешать загрузку"
    assert grid.selected == set(), "после отступления выбор обязан быть пуст"


def test_a_selection_that_will_not_come_off_forbids_uploading(grid):
    """Та самая поломка 24.08, теперь названная своим именем.

    Сетка не отпускает выбор. Раньше это было неотличимо от «ничего не
    нашли», и файл грузился поверх — объявление выходило с двумя креативами.
    """
    grid.sticky = True
    out = md._try_reuse_library_media(object(), "x.mp4")
    assert out == md.REUSE_DIRTY
    assert grid.selected, "предусловие теста: выбор действительно остался"


def test_a_dirty_grid_makes_the_step_refuse(monkeypatch):
    """`False` значит «шаг отказал», а не «иди грузи».

    Это и есть та развилка, из-за которой возможность выключали: отказ шага
    видно в телеметрии, а лишний креатив в объявлении не видно нигде, кроме
    глаз в кабинете.
    """
    monkeypatch.setattr(md, "get_reuse_library_media", lambda: True)
    monkeypatch.setattr(md, "_try_reuse_library_media", lambda page, name: md.REUSE_DIRTY)
    assert md.reuse_before_upload(object(), "x.mp4") is False


def test_an_attached_copy_ends_the_step(monkeypatch):
    """Ради чего всё: найденную копию не перезаливаем."""
    monkeypatch.setattr(md, "get_reuse_library_media", lambda: True)
    monkeypatch.setattr(md, "_try_reuse_library_media", lambda page, name: md.REUSE_ATTACHED)
    assert md.reuse_before_upload(object(), "x.mp4") is True


def test_a_missing_copy_hands_over_to_the_upload(monkeypatch):
    """`None` — не отказ и не успех, а «грузи как раньше»."""
    monkeypatch.setattr(md, "get_reuse_library_media", lambda: True)
    monkeypatch.setattr(md, "_try_reuse_library_media", lambda page, name: md.REUSE_ABSENT)
    assert md.reuse_before_upload(object(), "x.mp4") is None


def test_the_switch_off_skips_the_library_entirely(monkeypatch):
    """Выключатель должен именно выключать, а не «пробовать тихо»."""
    called: list[str] = []
    monkeypatch.setattr(md, "get_reuse_library_media", lambda: False)
    monkeypatch.setattr(md, "_try_reuse_library_media",
                        lambda page, name: called.append(name) or md.REUSE_ATTACHED)
    assert md.reuse_before_upload(object(), "x.mp4") is None
    assert called == []


def test_the_three_outcomes_stay_distinct():
    """Свести их обратно к булеву — вернуть поломку 24.08."""
    assert len({md.REUSE_ATTACHED, md.REUSE_ABSENT, md.REUSE_DIRTY}) == 3


def test_reuse_is_on_by_default(monkeypatch):
    from src.fb_ads_rpa import config as rpa_config

    monkeypatch.delenv("FB_RPA_REUSE_MEDIA", raising=False)
    assert rpa_config.get_reuse_library_media() is True
    monkeypatch.setenv("FB_RPA_REUSE_MEDIA", "0")
    assert rpa_config.get_reuse_library_media() is False
