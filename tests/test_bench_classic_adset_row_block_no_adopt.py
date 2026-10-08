# -*- coding: utf-8 -*-
"""Отказ `adset_action_menu_not_shown` не превращается в «созданное» объявление (ACL-1691).

После честного отказа (ничего не нажато) вызывающий код раньше брал «New Leads Ad»
из дерева и считал объявление созданным — на деле это чужое объявление. Теперь
при этом отказе нет усыновления, возможен один чистый повтор создания, причина
остаётся в отказе шага. Причина хранится по потокам: два аккаунта публикуют
одновременно в потоках одного процесса.
"""
from __future__ import annotations

import threading

from src.fb_ads_rpa import meta_dom as dom

from tests.test_additional_ad_without_creative_has_no_text import flow  # noqa: F401

BLOCK = "adset_action_menu_not_shown"
# ACL-1675: заготовки опознаются по id строки дерева. OTHER — «New Leads Ad» чужого
# (упавшего раньше) объявления: она есть в снимке до создания и усыновляться не должна.
OTHER = dom._SHELL_ROW_PREFIX + "100000000000007"
NEW = dom._SHELL_ROW_PREFIX + "100000000000009"


def _tree(monkeypatch, *, other: bool, new_after_create=None):
    """Стаб дерева: `other` — чужая заготовка видна всегда; `new_after_create()` — True,
    когда после упавшего создания в дереве появилась своя (новая) заготовка."""
    opened: list[str] = []

    def ids(p):
        rows = {OTHER} if other else set()
        if new_after_create is not None and new_after_create():
            rows.add(NEW)
        return frozenset(rows)

    monkeypatch.setattr(dom, "_tree_shell_ids", ids)
    monkeypatch.setattr(dom, "_open_shell_by_id", lambda p, rid: opened.append(rid) or True)
    monkeypatch.setattr(dom, "_close_creative_setup_modal", lambda p: None)
    return opened


def test_a_block_twice_is_not_adopted_and_fails_with_its_reason(flow, monkeypatch):
    calls = {"create": 0}
    opened = _tree(monkeypatch, other=True)

    def create(p, name):
        calls["create"] += 1
        dom._set_create_block(BLOCK)
        return False

    monkeypatch.setattr(dom, "create_additional_ad", create)
    _result, soft = flow["run"]()
    assert opened == [], "an existing ad was adopted after the block"
    assert calls["create"] == 2, "exactly one clean retry after the block"
    assert f"ad2_create:{BLOCK}" in soft, soft
    assert flow["uploads"] == ["a.png"], "ad 2 was filled although it was not created"


def test_a_block_then_a_successful_retry_creates_the_ad(flow, monkeypatch):
    calls = {"create": 0}
    opened = _tree(monkeypatch, other=True)

    def create(p, name):
        calls["create"] += 1
        if calls["create"] == 1:
            dom._set_create_block(BLOCK)
            return False
        dom._set_create_block("")
        return True

    monkeypatch.setattr(dom, "create_additional_ad", create)
    _result, soft = flow["run"]()
    assert calls == {"create": 2} and opened == []
    assert not any(s.startswith("ad2_create") for s in soft), soft
    assert "b.png" in flow["uploads"]


def test_an_ordinary_failed_create_still_adopts_the_shell(flow, monkeypatch):
    """Остальные отказы — прежнее поведение (усыновление строки)."""
    made = {"n": 0}
    adopted = _tree(monkeypatch, other=True, new_after_create=lambda: made["n"] > 0)

    def create(p, name):
        made["n"] += 1          # create нажал кнопку и оставил свою заготовку, но сообщил отказ
        dom._set_create_block("")
        return False

    monkeypatch.setattr(dom, "create_additional_ad", create)
    flow["run"]()
    assert adopted == [NEW], "ровно своя новая заготовка, не чужая OTHER"
    assert made["n"] == 1


def test_an_unreadable_tree_after_a_block_keeps_the_block_reason(flow, monkeypatch):
    """N1: дерево не прочиталось после честного отказа — в отказе шага остаётся причина."""
    reads = {"n": 0}

    def ids(p):
        reads["n"] += 1
        return frozenset() if reads["n"] == 1 else None      # снимок до create читается, дальше нет

    def create(p, name):
        dom._set_create_block(BLOCK)
        return False

    monkeypatch.setattr(dom, "_tree_shell_ids", ids)
    monkeypatch.setattr(dom, "create_additional_ad", create)
    _result, soft = flow["run"]()
    assert f"ad2_create:{BLOCK}" in soft, soft


def test_a_block_of_one_account_is_not_wiped_by_another_thread(flow, monkeypatch):
    """Два аккаунта в потоках: блок A не стирается вызовом B, ошибка B не метится блоком A."""
    a_blocked, b_called = threading.Event(), threading.Event()
    adopts: dict[str, int] = {"A": 0, "B": 0}
    creates: dict[str, int] = {"A": 0, "B": 0}
    softs: dict[str, list] = {}

    def create(p, name):
        who = threading.current_thread().name
        creates[who] += 1
        if who == "A":
            dom._set_create_block(BLOCK)
            if creates["A"] == 1:
                a_blocked.set()
                assert b_called.wait(10)
            return False
        dom._set_create_block("")  # B's call resets ITS OWN state
        b_called.set()
        return False

    def ids(p):
        who = threading.current_thread().name
        if who == "A":
            return frozenset({OTHER})                 # чужая заготовка всегда в дереве
        return frozenset({NEW}) if creates["B"] > 0 else frozenset()   # у B своя появилась после create

    def open_by_id(p, rid):
        adopts[threading.current_thread().name] += 1
        return True

    monkeypatch.setattr(dom, "create_additional_ad", create)
    monkeypatch.setattr(dom, "_tree_shell_ids", ids)
    monkeypatch.setattr(dom, "_open_shell_by_id", open_by_id)
    monkeypatch.setattr(dom, "_close_creative_setup_modal", lambda p: None)

    def work(who):
        soft: list[str] = []
        dom.run_recorded_campaign_flow(
            flow["page"], campaign_name="Example", budget="10", adset_name="Set",
            creative_path="a.png", ad_title="t1", ad_body="b1",
            destination_url="https://example.com",
            additional_ads=[{"ad_name": "Ad 2", "creative_type": "Image", "creative_path": "a.png",
                             "ad_title": "t2", "ad_body": "b2"}],
            soft_fails_out=soft)
        softs[who] = soft

    ta = threading.Thread(target=work, args=("A",), name="A")
    tb = threading.Thread(target=work, args=("B",), name="B")
    ta.start()
    assert a_blocked.wait(10)
    tb.start()
    ta.join(60)
    tb.join(60)
    assert adopts["A"] == 0, "A adopted an existing ad after its own block"
    assert adopts["B"] == 1, "B's ordinary failure lost its adopt"
    assert f"ad2_create:{BLOCK}" in softs["A"], softs["A"]
    assert not any(BLOCK in s for s in softs["B"]), softs["B"]
