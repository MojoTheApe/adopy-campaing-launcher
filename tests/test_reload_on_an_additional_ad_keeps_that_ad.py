# -*- coding: utf-8 -*-
"""Перезагрузка из-за окна ad blocker на втором объявлении не отдаёт его имя
первому (PR #314, ревью).

`_clear_blocking_overlays` на уровне ad2/ad3 перезагружал редактор простым
`_reload_editor(page)` — без адреса объявления и без проверки, что открыто
после. Meta после перезагрузки может открыть первое объявление, и следующий
шаг вписал бы имя второго в первое. Теперь после перезагрузки нужное
объявление (свежая заготовка «New Leads Ad») открывается снова по имени и
проверяется по полю имени; не вышло — объявление пропускается с отказом
шага имени, первое не трогается.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import meta_dom as md  # noqa: E402
from tests.test_additional_ad_without_creative_has_no_text import flow  # noqa: E402,F401
from tests.test_blocking_overlays_are_cleared import AD_BLOCKER, _set, browser, page  # noqa: E402,F401


def _editor(name: str) -> str:
    return (AD_BLOCKER
            + "<div><span>Example Ad 1</span><span>New Leads Ad</span></div>"
            + f"<input placeholder='Enter your ad name here...' value='{name}'>")


def _tree_opens(page, calls, lands_on: str | None):
    def open_tree_ad(p, name):
        calls.append(name)
        if lands_on is not None:
            p.fill("input[placeholder='Enter your ad name here...']", lands_on)
        return True
    return open_tree_ad


def test_after_a_reload_that_opened_the_first_ad_the_new_ad_is_reopened(page, monkeypatch):
    monkeypatch.setattr(md, "_all_edits_saved", lambda p: True)
    calls: list[str] = []
    monkeypatch.setattr(md, "_open_tree_ad", _tree_opens(page, calls, "New Leads Ad"))
    _set(page, _editor("Example Ad 1"))
    closed = md._clear_blocking_overlays(page, "ad2", ad_name="New Leads Ad")
    assert "network_trouble:reloaded" in closed
    assert calls == ["New Leads Ad"]
    assert closed[-1] == "reopened_ad:ok"


def test_a_reopen_that_does_not_land_on_the_new_ad_is_reported(page, monkeypatch):
    monkeypatch.setattr(md, "_all_edits_saved", lambda p: True)
    calls: list[str] = []
    monkeypatch.setattr(md, "_open_tree_ad", _tree_opens(page, calls, None))
    _set(page, _editor("Example Ad 1"))
    closed = md._clear_blocking_overlays(page, "ad2", ad_name="New Leads Ad")
    assert calls == ["New Leads Ad"]
    assert closed[-1] == "reopened_ad:failed"


def test_the_new_ad_still_open_after_the_reload_is_not_clicked_again(page, monkeypatch):
    monkeypatch.setattr(md, "_all_edits_saved", lambda p: True)
    calls: list[str] = []
    monkeypatch.setattr(md, "_open_tree_ad", _tree_opens(page, calls, None))
    _set(page, _editor("New Leads ad"))
    closed = md._clear_blocking_overlays(page, "ad2", ad_name="New Leads Ad")
    assert calls == []
    assert closed[-1] == "reopened_ad:ok"


def test_without_a_reload_nothing_is_reopened(page, monkeypatch):
    monkeypatch.setattr(md, "_all_edits_saved", lambda p: False)
    calls: list[str] = []
    monkeypatch.setattr(md, "_open_tree_ad", _tree_opens(page, calls, None))
    _set(page, _editor("Example Ad 1"))
    closed = md._clear_blocking_overlays(page, "ad2", ad_name="New Leads Ad")
    assert calls == [] and not any(c.startswith("reopened_ad") for c in closed)


# ── в потоке ───────────────────────────────────────────────────────────────

def test_the_second_ad_asks_for_its_own_shell_after_a_reload(flow, monkeypatch):
    seen: list[tuple[str, str]] = []

    def clear(p, where, **kw):
        seen.append((where, kw.get("ad_name", "")))
        return []

    monkeypatch.setattr(md, "_clear_blocking_overlays", clear)
    flow["fail_upload"] = set()
    flow["run"]()
    assert ("ad2", "New Leads Ad") in seen


def test_the_second_ad_is_skipped_when_it_could_not_be_reopened(flow, monkeypatch):
    def clear(p, where, **kw):
        return ["ad_blocker_modal:ok", "network_trouble:reloaded", "reopened_ad:failed"] if where == "ad2" else []

    monkeypatch.setattr(md, "_clear_blocking_overlays", clear)
    flow["fail_upload"] = set()
    _result, soft = flow["run"]()
    assert "ad2_name:not_the_new_ad_after_reload" in soft
    assert flow["uploads"] == ["a.png"], "медиа второго объявления легло в чужое объявление"
    assert all(c[0] != "Ad 2" for c in flow["copies"])
