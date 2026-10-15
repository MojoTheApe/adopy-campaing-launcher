# -*- coding: utf-8 -*-
"""`/health` говорит не только «я ACL», но и чей он (ACL-1309).

По этому ответу лаунчер решает, открыть ли дешборд или встать рядом на другом
порту. Без отпечатка он видел «какой-то ACL» и открывал его как свой — так
24.09 второй человек на машине оказался в дешборде владельца.

Проверяем настоящий маршрут, а не функцию отпечатка: сломай маршрут — и
лаунчер снова перестанет отличать свой дешборд от чужого.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "packaging" / "windows"))

from src.web.app import app  # noqa: E402


def _health(nonce: str | None = None) -> httpx.Response:
    """Настоящий маршрут через ASGI — как соседние тесты маршрутов дешборда."""
    async def run():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            return await client.get("/health", params={"nonce": nonce} if nonce else None)

    return asyncio.run(run())


def test_the_dashboard_reports_whose_configuration_it_serves(monkeypatch, tmp_path):
    import local_dashboard

    anna = tmp_path / "anna" / "ACL"
    dan = tmp_path / "dan" / "ACL"
    monkeypatch.setenv("ACL_CONFIG_DIR", str(anna))
    answer = _health("challenge-1").json()

    assert answer["app"] == "ACL" and answer["ok"] is True
    assert local_dashboard.is_mine(answer, anna, "challenge-1"), (
        "лаунчер Анны не узнаёт её собственный дешборд — каждый запуск поднимал бы новый"
    )
    assert not local_dashboard.is_mine(answer, anna, "challenge-2"), (
        "ответ на прошлый вызов принят за свежий — его можно было бы повторить"
    )
    assert not local_dashboard.is_mine(answer, dan, "challenge-1"), (
        "лаунчер Дана принял бы дешборд Анны за свой — ровно то, что случилось 24.09"
    )


def test_the_answer_does_not_give_away_the_configuration_path(monkeypatch, tmp_path):
    """/health слышит любой процесс на машине. Путь к настройкам выдал бы имя
    чужой учётной записи Windows; для сравнения хватает отпечатка."""
    folder = tmp_path / "SomeoneElse" / "ACL"
    monkeypatch.setenv("ACL_CONFIG_DIR", str(folder))
    raw = _health().text

    assert "SomeoneElse" not in raw
    assert str(tmp_path) not in raw.replace("\\\\", "\\")


def test_the_page_reloads_only_onto_its_own_dashboard(monkeypatch, tmp_path):
    """ACL-1931: страница после обновления ждёт /health и перезагружается.
    Она несёт вызов и ответ своего дешборда; /health того же человека — даже
    новым процессом — отвечает так же, соседский — нет."""
    from src.web import app as web

    anna = tmp_path / "anna" / "ACL"
    monkeypatch.setenv("ACL_CONFIG_DIR", str(anna))
    own = web._page_identity()
    assert _health(own["nonce"]).json()["proof"] == own["proof"]

    monkeypatch.setattr(web, "_PAGE_NONCE", [])  # перезапуск после обновления
    assert _health(own["nonce"]).json()["proof"] == own["proof"]

    monkeypatch.setenv("ACL_CONFIG_DIR", str(tmp_path / "dan" / "ACL"))
    assert _health(own["nonce"]).json()["proof"] != own["proof"]


def test_the_page_carries_its_own_challenge():
    """Шаблон отдаёт вызов и ответ странице: без них проверка в report.html
    сравнивала бы с пустотой."""
    source = (ROOT / "src" / "web" / "templates" / "report.html").read_text(encoding="utf-8")
    assert 'data-own-nonce="{{ own.nonce }}"' in source
    assert 'data-own-proof="{{ own.proof }}"' in source
    assert "{%- set own = page_identity() %}" in source
