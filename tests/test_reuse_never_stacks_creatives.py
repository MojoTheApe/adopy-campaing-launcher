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
    monkeypatch.setattr(md, "_library_any_tile_marked", lambda page: bool(g.selected))
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


def test_a_wrong_or_broken_file_on_the_ad_refuses_the_step_without_upload(grid, monkeypatch):
    """Прикрепилось не то (или файл не обработался) — это уже в объявлении.

    Ревью 18.09 (ACL-1192): сетки медиатеки на экране в этот момент нет,
    «откатывать» нечего, и загрузка поверх дала бы объявление с чужим или
    битым креативом. Поэтому отказ шага без загрузки — REUSE_DIRTY.
    """
    class _Dialog:
        def is_visible(self, timeout: int = 0) -> bool:
            return True

        def get_by_role(self, role, name=""):
            return _Btn()

    class _Btn:
        first = None

        def __init__(self):
            self.first = self

        def count(self) -> int:
            return 1

        def is_disabled(self) -> bool:
            return False

        def click(self, timeout: int = 0) -> None:
            pass

    class _Root:
        def get_by_role(self, role, name=""):
            d = _Dialog()
            d.first = d
            return d

    said = []
    monkeypatch.setattr(md, "_roots", lambda page: [_Root()])
    monkeypatch.setattr(md, "_verify_ad_media_present", lambda page: True)
    monkeypatch.setattr(md, "_verify_ad_media_is_the_file",
                        lambda page, filename, picked=None: (False, {"why": "other_file_attached"}))
    monkeypatch.setattr(md._telemetry, "record",
                        lambda step, ok, **kw: said.append({"step": step, "ok": ok, **kw}))

    class _P:
        url = "https://adsmanager.facebook.com/adsmanager/manage/ads/edit/standalone"

    out = md._try_reuse_library_media(_P(), "2012.mp4")

    assert out == md.REUSE_DIRTY, "чужой файл уже в объявлении — загружать поверх нельзя"
    assert [s["note"] for s in said if s["step"] == "ad_creative_reuse"] == ["other_file_attached"]


def test_a_zero_counter_with_a_tile_still_marked_is_not_a_clean_grid(grid, monkeypatch):
    """Счётчик «0 selected» отстал от сетки — загрузку поверх это не разрешает."""
    monkeypatch.setattr(md, "_library_any_tile_marked", lambda page: True)
    out = md._try_reuse_library_media(object(), "x.mp4")
    assert out == md.REUSE_DIRTY


def test_an_unreadable_grid_is_not_a_clean_grid(grid, monkeypatch):
    monkeypatch.setattr(md, "_library_any_tile_marked", lambda page: None)
    assert md._try_reuse_library_media(object(), "x.mp4") == md.REUSE_DIRTY


def _advancing_dialog(monkeypatch):
    class _Btn:
        def __init__(self):
            self.first = self

        def count(self):
            return 1

        def is_disabled(self):
            return False

        def click(self, timeout=0):
            pass

    class _Dialog:
        def __init__(self):
            self.first = self

        def is_visible(self, timeout=0):
            return True

        def get_by_role(self, role, name=""):
            return _Btn()

    class _Root:
        def get_by_role(self, role, name=""):
            return _Dialog()

    monkeypatch.setattr(md, "_roots", lambda page: [_Root()])
    monkeypatch.setattr(md, "_verify_ad_media_present", lambda page: True)
    monkeypatch.setattr(md, "_ad_media_state", lambda page, filename: {
        "named": [], "others": [], "processing": False, "failed": False,
        "ready": True, "thumbs": md._ad_media_thumb_ids(page),
    })
    monkeypatch.setattr(md._telemetry, "record", lambda *a, **k: None)

    class _P:
        url = "https://adsmanager.facebook.com/adsmanager/manage/ads/edit/standalone"

    return _P()


def test_a_different_thumbnail_on_the_ad_is_a_wrong_file(grid, monkeypatch):
    """ACL-1171: имя не показано, но миниатюра объявления — не та, что выбрали."""
    p = _advancing_dialog(monkeypatch)
    monkeypatch.setattr(md, "_tile_thumb_ids", lambda tile: ["111_2222222222_3333333333_n"])
    monkeypatch.setattr(md, "_ad_media_thumb_ids", lambda page: ["999_8888888888_7777777777_n"])
    assert md._try_reuse_library_media(p, "2012.mp4") == md.REUSE_DIRTY


def test_the_same_thumbnail_on_the_ad_confirms_the_pick(grid, monkeypatch):
    p = _advancing_dialog(monkeypatch)
    monkeypatch.setattr(md, "_tile_thumb_ids", lambda tile: ["111_2222222222_3333333333_n"])
    monkeypatch.setattr(md, "_ad_media_thumb_ids", lambda page: ["111_2222222222_3333333333_n"])
    assert md._try_reuse_library_media(p, "2012.mp4") == md.REUSE_ATTACHED
