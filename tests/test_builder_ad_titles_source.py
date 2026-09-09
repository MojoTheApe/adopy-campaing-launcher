"""Билдер берёт тексты оттуда же, где их правит человек.

9 сентября под полем Ad Title в билдере стояло «No ad titles found», хотя в
разделе Ad titles лежало 316 текстов. `/api/ad-copy` ходил в таблицу напрямую,
мимо реестра, и без `GSHEET_TARGETS_ID` в окружении загрузчик возвращал пустой
словарь — молча, без ошибки и без строки в логе. Пустой список и «источник
недоступен» выглядели одинаково.

Проверяется здесь одно: **что именно билдер считает источником**. Не то, как
устроена таблица, и не разметка формы.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.analysis_agent import ad_text_registry as ar  # noqa: E402
from src.web import app as web  # noqa: E402


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(ar.paths, "get_local_config_dir", lambda: str(tmp_path))
    return tmp_path


@pytest.fixture
def sheet(monkeypatch):
    """Таблица, о которой мы знаем, звали её или нет."""
    calls = {"n": 0, "rows": {}}

    def load_ad_copy():
        calls["n"] += 1
        return calls["rows"]

    import src.web.ad_copy_gsheet as gs

    monkeypatch.setattr(gs, "load_ad_copy", load_ad_copy)
    return calls


def test_the_library_is_what_the_builder_offers(sheet):
    ar.add("ES how much", "¿Cómo funciona?", "Cuánto dinero tienes hoy", "★★★★", "", "es")
    rows = web._ad_copy_rows()
    assert [r["ad_title"] for r in rows] == ["ES how much"]
    assert rows[0]["headline"] == "¿Cómo funciona?"
    assert rows[0]["language"] == "ES"


def test_the_sheet_is_not_touched_when_the_library_has_texts(sheet):
    """Билдер открывают десятки раз за день — ходить в чужую таблицу незачем.

    А главное: таблица однажды уже ответила `403`, и тогда поле опустело бы,
    хотя тексты на машине есть.
    """
    ar.add("Текст", "Заголовок", "Тело")
    web._ad_copy_rows()
    assert sheet["n"] == 0, "сходили в таблицу при полной библиотеке"


def test_a_fresh_machine_still_gets_something_from_the_sheet(sheet):
    """Реестр пуст только до первого открытия раздела Ad titles."""
    sheet["rows"] = {"Из таблицы": {"headline": "H", "body": "B", "description": "",
                                    "asset": "", "language": "AR"}}
    rows = web._ad_copy_rows()
    assert [r["ad_title"] for r in rows] == ["Из таблицы"]
    assert sheet["n"] == 1


def test_an_unreadable_registry_does_not_take_the_builder_down_with_it(sheet, store):
    """Испорченный файл — повод показать то, что есть, а не уронить форму."""
    store.joinpath("ad_text_registry.json").write_text("{ не json", encoding="utf-8")
    sheet["rows"] = {"Запасной": {"headline": "H", "body": "B", "description": "",
                                  "asset": "", "language": ""}}
    assert [r["ad_title"] for r in web._ad_copy_rows()] == ["Запасной"]


def test_every_field_the_builder_reads_is_present(sheet):
    """Поля названы так, как их читает разметка билдера.

    Разойдясь в одном имени, поле молча окажется пустым в превью объявления —
    и это тот случай, который на экране выглядит просто «текста нет».
    """
    ar.add("Текст", "Заголовок", "Тело", "Описание", "Aramco Form", "ar")
    row = web._ad_copy_rows()[0]
    assert set(row) == {"ad_title", "headline", "body", "description", "asset", "language"}


def test_the_route_itself_answers_with_the_library(sheet):
    """Маршрут, а не только сборщик строк: между ними легко разойтись.

    Через `TestClient` не идём: в этой сборке `httpx` с ним не совместим
    (`Client.__init__() got an unexpected keyword argument 'app'`), и соседние
    проверки его тоже не используют. Зовём обработчик напрямую.
    """
    import asyncio
    import json

    ar.add("ES how much", "¿Cómo funciona?", "Cuánto dinero tienes hoy")
    response = asyncio.run(web.api_ad_copy())
    data = json.loads(response.body)
    assert [r["ad_title"] for r in data["rows"]] == ["ES how much"]
