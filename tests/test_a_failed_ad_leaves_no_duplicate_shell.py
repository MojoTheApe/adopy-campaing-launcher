# -*- coding: utf-8 -*-
"""ACL-1675: повтор создания объявления после перезагрузки не плодит пустые.

Первое `create_additional_ad` могло упасть, уже оставив в дереве заготовку
«New Leads Ad»; `recover_stuck_page` перезагружал редактор, и второй create
добавлял ещё одну — пустое объявление-дубликат. Теперь после перезагрузки
сначала читается дерево: новая заготовка есть — её усыновляют, дерево не
читается — create не вызывается. Заготовку упавшего объявления N следующее
объявление не берёт: ищет только свежую, а хвост отмечает `leftover_shell`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import meta_dom as md  # noqa: E402
from tests.test_additional_ad_without_creative_has_no_text import flow  # noqa: E402,F401


def _wire(monkeypatch, shells, create_results):
    """shells — очередь ответов `_tree_new_ad_shells` (последний повторяется)."""
    seen = {"create": 0, "open": []}
    queue = list(shells)

    def tree(p):
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def create(p, name):
        seen["create"] += 1
        return create_results[min(seen["create"], len(create_results)) - 1]

    def open_ad(p, name, newest=False):
        seen["open"].append(newest)
        return True

    monkeypatch.setattr(md, "_tree_new_ad_shells", tree)
    monkeypatch.setattr(md, "create_additional_ad", create)
    monkeypatch.setattr(md, "_open_tree_ad", open_ad)
    monkeypatch.setattr(md, "recover_stuck_page", lambda *a, **k: True)
    monkeypatch.setattr(md, "_close_creative_setup_modal", lambda p: None)
    return seen


def test_first_create_leaves_a_shell_then_reload_gives_exactly_one_ad(flow, monkeypatch):
    # до create — 0 заготовок; первый create упал и оставил одну; после перезагрузки — 1
    seen = _wire(monkeypatch, [0, 1, 1], [False])
    flow["fail_upload"] = set()
    _result, soft = flow["run"]()
    assert seen["create"] == 1, "второй create добавил бы пустое объявление"
    assert seen["open"] and "ad2_create" not in soft


def test_an_unreadable_tree_after_the_reload_does_not_create(flow, monkeypatch):
    seen = _wire(monkeypatch, [0, -1], [False])
    flow["fail_upload"] = set()
    _result, soft = flow["run"]()
    assert seen["create"] == 1
    assert any(s.startswith("ad2_create") for s in soft)


def test_a_clean_tree_after_the_reload_still_retries_the_create(flow, monkeypatch):
    seen = _wire(monkeypatch, [0, 0], [False, True])
    flow["fail_upload"] = set()
    flow["run"]()
    assert seen["create"] == 2


def test_the_next_ad_does_not_take_the_leftover_shell(monkeypatch):
    seen = _wire(monkeypatch, [1], [True])
    # заготовка одна и она старая (shells_before=1): новой нет — создаём, не открываем
    assert md._adopt_shell_or_recreate(object(), "set", shells_before=1) is True
    assert seen["open"] == [] and seen["create"] == 1


def test_with_a_leftover_the_fresh_shell_is_the_bottom_one(monkeypatch):
    seen = _wire(monkeypatch, [2], [True])
    assert md._adopt_shell_or_recreate(object(), "set", shells_before=1) is True
    assert seen["open"] == [True] and seen["create"] == 0


def test_the_leftover_is_marked_for_the_audit_and_not_deleted(flow, monkeypatch):
    _wire(monkeypatch, [1], [True])
    records = []
    monkeypatch.setattr(md._telemetry, "record",
                        lambda step, ok, **kw: records.append((step, ok, kw.get("note"))))
    flow["fail_upload"] = set()
    flow["run"]()
    assert ("ad2_create", False, "leftover_shell") in records
