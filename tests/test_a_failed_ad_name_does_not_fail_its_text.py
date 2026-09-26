# -*- coding: utf-8 -*-
"""Отказ шага имени не валит текст того же объявления (финальное ревью PR #314 (c)).

Шаг имени отказал — в поле осталось имя Meta («New Leads Ad») или пусто, а
текст сверялся с НАШИМ именем и падал ещё и с `wrong_ad_open`: один отказ
имени стоил объявлению и текста. Теперь текст сверяется с тем, что в поле
(`_name_in_the_field`), а не прочитать — с именем свежей заготовки Meta.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import meta_dom as md  # noqa: E402
from tests.test_additional_ad_without_creative_has_no_text import flow  # noqa: E402,F401


def test_a_failed_name_step_checks_the_text_against_the_name_in_the_field(flow, monkeypatch):
    """(c): имя «Example Ad» не вписалось, в поле — «New Leads Ad». Текст
    сверяется с «New Leads Ad», а отказ остаётся одним — `ad_name`."""
    loc = flow["page"].get_by_placeholder.return_value

    def fill(value, timeout=None):
        if value in ("Example Ad", "Ad 2"):
            raise TimeoutError("the name field did not take the text")

    loc.fill.side_effect = fill
    monkeypatch.setattr(md, "_open_ad_name", lambda p: "New Leads Ad")
    flow["fail_upload"] = set()
    _result, soft = flow["run"]()
    assert "ad_name" in soft and "ad2_name" in soft
    names = [c[0] for c in flow["copies"]]
    assert names == ["New Leads Ad", "New Leads Ad"], names


def test_an_unreadable_field_after_a_failed_name_falls_back_to_metas_default(flow, monkeypatch):
    loc = flow["page"].get_by_placeholder.return_value

    def fill(value, timeout=None):
        if value == "Example Ad":
            raise TimeoutError("the name field did not take the text")

    loc.fill.side_effect = fill
    monkeypatch.setattr(md, "_open_ad_name", lambda p: None)
    flow["fail_upload"] = set()
    flow["run"]()
    assert flow["copies"] and flow["copies"][0][0] == "New Leads Ad"


def test_another_ads_name_in_the_field_keeps_the_check_on_our_name(flow, monkeypatch):
    """Ревью PR #323 (F5): шаг имени второго объявления отказал после
    перезагрузки, открывшей первое, — в поле «Example Ad». Сверка остаётся с
    НАШИМ именем («Ad 2»), и текст второго не ляжет в первое."""
    loc = flow["page"].get_by_placeholder.return_value

    def fill(value, timeout=None):
        if value == "Ad 2":
            raise TimeoutError("the name field did not take the text")

    loc.fill.side_effect = fill
    monkeypatch.setattr(md, "_open_ad_name", lambda p: "Example Ad")
    flow["fail_upload"] = set()
    flow["run"]()
    names = [c[0] for c in flow["copies"]]
    assert names[-1] == "Ad 2", names


def test_the_fresh_shell_name_is_accepted_in_any_case(monkeypatch):
    for got, want in (("new leads ad", "new leads ad"), ("Example Ad 1", "Ad 2"), ("AD 2", "AD 2")):
        monkeypatch.setattr(md, "_open_ad_name", lambda p, g=got: g)
        assert md._name_in_the_field(object(), "Ad 2") == want
