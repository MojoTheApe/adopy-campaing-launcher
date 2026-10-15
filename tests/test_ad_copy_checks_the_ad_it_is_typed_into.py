# -*- coding: utf-8 -*-
"""Текст пишется только в своё объявление (финальное ревью PR #314, G2 и G7).

* G2: после повторного раскрытия полей (`_reveal_ad_copy_fields` может
  перезагрузить редактор, и Meta откроет другое объявление) проверка «то ли
  объявление» делается ещё раз — вторая попытка в чужое объявление не пишет.
* G7: поле имени не прочитать — судить не по чему, текст пишется (прежнее
  поведение, закреплено тестом).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import meta_dom as md  # noqa: E402
from tests.test_ad_copy_field_redrawn_is_taken_again import _NAMED, _PAGE, browser, page  # noqa: E402,F401


def test_a_second_attempt_after_the_reveal_opened_another_ad_types_nothing(page, monkeypatch):
    """G2: заголовок не встал с первой попытки; повторное раскрытие открыло
    другое объявление, а вернуть нужное не вышло — второй попытки нет."""
    page.set_content(_NAMED % "Example Ad 2")
    page.evaluate("window.redrawLimit = 99")   # заголовок не встаёт с первой попытки
    reveals = []

    def reveal(p):
        reveals.append(1)
        if len(reveals) > 1:   # повторное раскрытие «перезагрузило» редактор
            p.fill("#adname", "Example Ad 1")
        return True

    monkeypatch.setattr(md, "_reveal_ad_copy_fields", reveal)
    monkeypatch.setattr(md, "_open_tree_ad", lambda p, name: False)
    typed = []
    real_type = md._type_human
    monkeypatch.setattr(md, "_type_human", lambda p, t: typed.append(len(reveals)) or real_type(p, t))
    report = {"ad_name": "Example Ad 2"}
    assert md.fill_ad_copy(page, "Short headline", "Body of the ad", report) is False
    assert report["why"] == "wrong_ad_open"
    assert len(reveals) == 2
    assert all(n == 1 for n in typed), "после повторного раскрытия печатали в чужое объявление"


def test_an_unreadable_name_field_does_not_stop_the_text(page):
    """G7: поля имени нет — не мешаем, текст пишется."""
    page.set_content(_PAGE)
    page.evaluate("window.redrawLimit = 0")
    report = {"ad_name": "Example Ad 2"}
    assert md._open_ad_name(page) is None
    assert md.fill_ad_copy(page, "Short headline", "Body of the ad", report) is True
    assert page.locator("#title").input_value() == "Short headline"
    assert report.get("why") != "wrong_ad_open"
