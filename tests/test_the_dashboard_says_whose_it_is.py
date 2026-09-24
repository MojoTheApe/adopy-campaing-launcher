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


def _health() -> httpx.Response:
    """Настоящий маршрут через ASGI — как соседние тесты маршрутов дешборда."""
    async def run():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            return await client.get("/health")

    return asyncio.run(run())


def test_the_dashboard_reports_whose_configuration_it_serves(monkeypatch, tmp_path):
    import local_dashboard

    anna = tmp_path / "anna" / "ACL"
    dan = tmp_path / "dan" / "ACL"
    monkeypatch.setenv("ACL_CONFIG_DIR", str(anna))
    answer = _health().json()

    assert answer["app"] == "ACL" and answer["ok"] is True
    assert local_dashboard.is_mine(answer, local_dashboard.identity(anna)), (
        "лаунчер Анны не узнаёт её собственный дешборд — каждый запуск поднимал бы новый"
    )
    assert not local_dashboard.is_mine(answer, local_dashboard.identity(dan)), (
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
