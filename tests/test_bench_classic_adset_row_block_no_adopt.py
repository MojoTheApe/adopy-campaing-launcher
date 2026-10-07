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


def test_a_block_twice_is_not_adopted_and_fails_with_its_reason(flow, monkeypatch):
    calls = {"create": 0, "adopt": 0}

    def create(p, name):
        calls["create"] += 1
        dom._set_create_block(BLOCK)
        return False

    def adopt(p, name):
        calls["adopt"] += 1
        return True

    monkeypatch.setattr(dom, "create_additional_ad", create)
    monkeypatch.setattr(dom, "_open_tree_ad", adopt)
    _result, soft = flow["run"]()
    assert calls["adopt"] == 0, "an existing ad was adopted after the block"
    assert calls["create"] == 2, "exactly one clean retry after the block"
    assert f"ad2_create:{BLOCK}" in soft, soft
    assert flow["uploads"] == ["a.png"], "ad 2 was filled although it was not created"


def test_a_block_then_a_successful_retry_creates_the_ad(flow, monkeypatch):
    calls = {"create": 0, "adopt": 0}

    def create(p, name):
        calls["create"] += 1
        if calls["create"] == 1:
            dom._set_create_block(BLOCK)
            return False
        dom._set_create_block("")
        return True

    monkeypatch.setattr(dom, "create_additional_ad", create)
    monkeypatch.setattr(dom, "_open_tree_ad", lambda p, name: calls.__setitem__("adopt", calls["adopt"] + 1) or True)
    _result, soft = flow["run"]()
    assert calls == {"create": 2, "adopt": 0}
    assert not any(s.startswith("ad2_create") for s in soft), soft
    assert "b.png" in flow["uploads"]


def test_an_ordinary_failed_create_still_adopts_the_shell(flow, monkeypatch):
    """Остальные отказы — прежнее поведение (усыновление строки)."""
    adopted = []

    def create(p, name):
        dom._set_create_block("")
        return False

    monkeypatch.setattr(dom, "create_additional_ad", create)
    monkeypatch.setattr(dom, "_open_tree_ad", lambda p, name: adopted.append(name) or True)
    monkeypatch.setattr(dom, "_close_creative_setup_modal", lambda p: None)
    flow["run"]()
    assert adopted == ["New Leads Ad"]


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

    def adopt(p, name):
        adopts[threading.current_thread().name] += 1
        return True

    monkeypatch.setattr(dom, "create_additional_ad", create)
    monkeypatch.setattr(dom, "_open_tree_ad", adopt)
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
