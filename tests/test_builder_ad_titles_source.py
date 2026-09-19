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


def test_the_library_is_what_the_builder_offers():
    ar.add("ES how much", "¿Cómo funciona?", "Cuánto dinero tienes hoy", "★★★★", "", "es")
    rows = web._ad_copy_rows()
    assert [r["ad_title"] for r in rows] == ["ES how much"]
    assert rows[0]["headline"] == "¿Cómo funciona?"
    assert rows[0]["language"] == "ES"


def test_an_unreadable_registry_reports_an_error_and_preserves_the_file(store):
    """Повреждение не должно молча заменять локальную правку старым текстом."""
    store.joinpath("ad_text_registry.json").write_text("{ не json", encoding="utf-8")
    with pytest.raises(ar.AdTextError):
        web._ad_copy_rows()
    assert store.joinpath("ad_text_registry.json").read_text(encoding="utf-8") == "{ не json"


def test_every_field_the_builder_reads_is_present():
    """Поля названы так, как их читает разметка билдера.

    Разойдясь в одном имени, поле молча окажется пустым в превью объявления —
    и это тот случай, который на экране выглядит просто «текста нет».
    """
    ar.add("Текст", "Заголовок", "Тело", "Описание", "Aramco Form", "ar")
    row = web._ad_copy_rows()[0]
    assert set(row) == {"ad_title", "headline", "body", "description", "asset", "language"}


def test_the_route_itself_answers_with_the_library():
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
