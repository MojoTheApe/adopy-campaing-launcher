# -*- coding: utf-8 -*-
"""Отказ `adset_action_menu_not_shown` не превращается в «созданное» объявление (ACL-1691).

После честного отказа (ничего не нажато) вызывающий код раньше брал «New Leads Ad»
из дерева и считал объявление созданным — на деле это чужое объявление. Теперь
при этом отказе ни усыновления, ни повторного создания; причина остаётся.
"""
from __future__ import annotations

from src.fb_ads_rpa import meta_dom as dom

from tests.test_additional_ad_without_creative_has_no_text import flow  # noqa: F401


def test_a_blocked_create_is_not_adopted_and_keeps_its_reason(flow, monkeypatch):
    calls = {"create": 0, "adopt": 0}

    def create(p, name):
        calls["create"] += 1
        dom._LAST_CREATE_BLOCK = "adset_action_menu_not_shown"
        return False

    def adopt(p, name):
        calls["adopt"] += 1
        return True

    monkeypatch.setattr(dom, "create_additional_ad", create)
    monkeypatch.setattr(dom, "_open_tree_ad", adopt)
    _result, soft = flow["run"]()
    assert calls["adopt"] == 0, "an existing ad was adopted after the block"
    assert calls["create"] == 1, "the blocked create was retried"
    assert "ad2_create:adset_action_menu_not_shown" in soft, soft
    assert flow["uploads"] == ["a.png"], "ad 2 was filled although it was not created"


def test_an_ordinary_failed_create_still_adopts_the_shell(flow, monkeypatch):
    """Остальные отказы — прежнее поведение (усыновление строки)."""
    adopted = []

    def create(p, name):
        dom._LAST_CREATE_BLOCK = ""
        return False

    monkeypatch.setattr(dom, "create_additional_ad", create)
    monkeypatch.setattr(dom, "_open_tree_ad", lambda p, name: adopted.append(name) or True)
    monkeypatch.setattr(dom, "_close_creative_setup_modal", lambda p: None)
    flow["run"]()
    assert adopted == ["New Leads Ad"]
