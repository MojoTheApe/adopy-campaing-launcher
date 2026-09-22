# -*- coding: utf-8 -*-
"""Заблокированный редактор текста: узнать сразу, чинить один раз (ACL-1281).

22.09 живой прогон: у четырёх объявлений на трёх аккаунтах шаг текста шёл
6–13 минут и всё равно кончался без текста. Порядок был такой: пробуем
писать → ждём поля 12 секунд → разворачиваем блок → замечаем ошибку Meta →
чиним Browser add-ons → открываем объявление заново → пробуем всё сначала.
Meta при этом подставляет «Instant form», редактор креатива не рисуется, и
полей текста на странице нет вовсе.

Теперь блокировка проверяется до первой попытки, лечение одно, а объявление,
оставшееся без текста, названо в итоге прогона.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import meta_dom as md  # noqa: E402


@pytest.fixture
def calls(monkeypatch):
    """Счётчики: сколько раз писали текст и сколько раз лечили редактор."""
    seen = {"fill": 0, "repair": 0, "blocked": True, "fill_ok": False, "records": []}

    monkeypatch.setattr(md, "_review_errors_visible", lambda page: seen["blocked"])
    monkeypatch.setattr(md, "_await_creative_settled", lambda page, **kw: True)
    monkeypatch.setattr(md._telemetry, "record",
                        lambda step, ok=None, **kw: seen["records"].append((step, ok, kw.get("note"))))

    def fill(page, title, body, report=None):
        seen["fill"] += 1
        return seen["fill_ok"]

    def repair(page, ad_name, destination_url):
        seen["repair"] += 1
        return not seen["blocked"]

    monkeypatch.setattr(md, "fill_ad_copy", fill)
    monkeypatch.setattr(md, "_repair_blocked_copy_editor", repair)
    return seen


def test_a_blocked_editor_is_known_before_the_first_try(calls):
    """Ошибка Meta на экране — лечим, а потом пишем: не наоборот."""
    md.write_ad_copy(object(), "t", "b", ad_name="Ad 1", destination_url="u")
    assert calls["repair"] == 1
    assert ("ad_copy_blocked", False, "before_first_try") in calls["records"]


def test_repair_happens_once_and_the_ad_is_reported(calls):
    """Лечение не помогло — отказываем сразу, без второго круга минут."""
    report: dict = {}
    ok = md.write_ad_copy(object(), "t", "b", ad_name="Ad 1", destination_url="u", report=report)
    assert ok is False
    assert calls["repair"] == 1, "второе лечение того же объявления"
    assert calls["fill"] == 0, "писали в заблокированный редактор"
    assert report["why"] == "editor_blocked"


def test_after_a_successful_repair_the_text_is_written(calls):
    """Починилось — пишем текст в том же заходе."""
    def repair(page, ad_name, destination_url):
        calls["repair"] += 1
        calls["blocked"] = False
        calls["fill_ok"] = True
        return True

    md._repair_blocked_copy_editor = repair  # noqa: SLF001 - подменяем в тесте
    ok = md.write_ad_copy(object(), "t", "b", ad_name="Ad 1", destination_url="u")
    assert ok is True and calls["fill"] == 1 and calls["repair"] == 1


def test_an_ordinary_miss_gets_one_honest_second_try(calls):
    """Редактор в порядке, ввод не сошёлся — вторая попытка, без лечения."""
    calls["blocked"] = False
    md.write_ad_copy(object(), "t", "b", ad_name="Ad 1", destination_url="u")
    assert calls["fill"] == 2 and calls["repair"] == 0


def test_a_block_found_only_after_the_first_try_is_repaired_once(calls):
    """Ошибка появилась по ходу: одно лечение и одна попытка после него."""
    calls["blocked"] = False
    state = {"n": 0}

    def fill(page, title, body, report=None):
        state["n"] += 1
        calls["fill"] += 1
        if state["n"] == 1:
            calls["blocked"] = True  # ошибка Meta проявилась после ввода
        return False

    md.fill_ad_copy = fill  # noqa: SLF001 - подменяем в тесте
    report: dict = {}
    ok = md.write_ad_copy(object(), "t", "b", ad_name="Ad 1", destination_url="u", report=report)
    assert ok is False and calls["repair"] == 1
    assert calls["fill"] == 1, "после неудачного лечения писать некуда"
    assert ("ad_copy_blocked", False, "review_error") in calls["records"]


def test_lead_terms_are_not_our_repair(monkeypatch):
    """Условия Lead Generation принимает человек — чинить нечего."""
    records = []
    monkeypatch.setattr(md._telemetry, "record",
                        lambda step, ok=None, **kw: records.append((step, kw.get("note"))))
    monkeypatch.setattr(md, "_lead_terms_not_accepted", lambda page: True)
    assert md._repair_blocked_copy_editor(object(), "Ad 1", "u") is False
    assert ("lead_terms_not_accepted", "page_must_accept_lead_terms") in records


def test_the_run_names_the_ad_that_has_no_text():
    """В итоге прогона объявление без текста названо, а не «ad_copy»."""
    src = (ROOT / "src/fb_ads_rpa/meta_dom.py").read_text(encoding="utf-8")
    assert 'f"ad_copy:{ad_name or \'ad 1\'} has no text"' in src
    assert 'f"{tag}_copy:{extra_name or tag} has no text"' in src


def test_waiting_for_fields_stops_when_the_editor_is_blocked():
    """Полей текста у заблокированного редактора нет — ждать их незачем."""
    src = (ROOT / "src/fb_ads_rpa/meta_dom.py").read_text(encoding="utf-8")
    block = src[src.index("def _reveal_ad_copy_fields"): src.index("def _reveal_ad_copy_fields") + 3000]
    assert "blocked_by_review_error" in block and "_review_errors_visible(page)" in block
