# -*- coding: utf-8 -*-
"""Отказ шага уезжает в общий приёмник — и не может уронить прогон (ACL-354).

Сегодня о поломке на машине байера узнают из сообщения в переписке и
фотографии экрана: пересказанных, с опозданием и без фактов самой машины.
Отправка прицеплена к одному месту — записи телеметрии, — потому что через
него проходят все сорок три места вызова.

Проверки здесь **выполняют** отправку с подменённым приёмником: молчаливый
`except` вокруг неё легко превращается в «ничего не уезжает», и заметить это
можно только так.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import acl_sentry  # noqa: E402
from src.fb_ads_rpa import run_telemetry as rt  # noqa: E402


@pytest.fixture
def sent(monkeypatch, tmp_path):
    """Приёмник-двойник: записываем, что бы уехало."""
    out: list[dict] = []

    def _report(step, why="", **kw):
        out.append({"step": step, "why": why, **kw})
        return "event-id"

    monkeypatch.setattr(acl_sentry, "report_step_failure", _report)
    monkeypatch.setattr(rt, "TELEMETRY_PATH", tmp_path / "telemetry.jsonl")
    return out


def test_a_failed_row_alone_is_not_reported(sent):
    """ACL-1318: строка с `ok=False` сама по себе в Sentry не едет — только
    остановка попытки и блокирующие отказы итога (`sentry_signal`). Строка
    запоминается: её причина и снимок станут контекстом такого события."""
    rt.record("placements", False, run_id="r1", campaign="GT_x",
              diag={"why": "таблица campaign: table-loading"})
    assert sent == []
    seen = rt.last_failure("placements", "r1")
    assert seen["why"] == "таблица campaign: table-loading"
    assert seen["diag"] == {"why": "таблица campaign: table-loading"}


@pytest.mark.parametrize("ok", [True, None])
def test_success_and_skips_are_not_reported(sent, ok):
    """Пропуск — не отказ, и удача тем более. Иначе приёмник зарастёт."""
    rt.record("placements", ok)
    assert sent == []


@pytest.mark.parametrize("payload, expected", [
    ({"note": "не нашёл итоговую строку"}, "не нашёл итоговую строку"),
    ({"diag": {"why": "видно 0 категорий из 5"}}, "видно 0 категорий из 5"),
    ({"diag": {"error": "could not set the report period"}}, "could not set the report period"),
    ({"diag": {"how": "refused: report not loaded"}}, "refused: report not loaded"),
    ({}, ""),
])
def test_the_reason_is_found_wherever_the_step_put_it(sent, payload, expected):
    """Шаги кладут причину в три разных поля — так сложилось за полтора года."""
    rt.record("ad_copy", False, run_id=f"r-why-{expected}", **payload)
    assert rt.last_failure("ad_copy", f"r-why-{expected}")["why"] == expected


def test_a_broken_collector_does_not_break_the_run(monkeypatch, tmp_path):
    """Отправка не смеет уронить прогон — то же правило, что у телеметрии."""
    monkeypatch.setattr(rt, "TELEMETRY_PATH", tmp_path / "telemetry.jsonl")

    def _boom(*_a, **_k):
        raise RuntimeError("приёмник недоступен")

    monkeypatch.setattr(acl_sentry, "report_step_failure", _boom)
    rt.record("placements", False, diag={"why": "что угодно"})  # не бросает
    from src.fb_ads_rpa import sentry_signal

    assert sentry_signal.report_campaign_stop("campaign_name") == ""  # не бросает
    assert sentry_signal.report_blocking_failures(["placements"]) == [""]
    assert (tmp_path / "telemetry.jsonl").exists(), "журнал всё равно записан"


def test_the_journal_is_written_even_when_reporting_is_off(monkeypatch, tmp_path):
    """Приёмник выключен — журнал ведётся как прежде."""
    monkeypatch.setattr(rt, "TELEMETRY_PATH", tmp_path / "telemetry.jsonl")
    monkeypatch.setenv("ACL_SENTRY_DSN", "")
    rt.record("placements", False, diag={"why": "x"})
    assert "placements" in (tmp_path / "telemetry.jsonl").read_text(encoding="utf-8")


# ── кнопка «Report a problem» ──────────────────────────────────────────────
#
# Через `TestClient` не ходим: в этой сборке `httpx` с ним несовместим, и
# соседние проверки его тоже не используют. Зовём обработчик напрямую.

import asyncio  # noqa: E402
import json as _json  # noqa: E402

from src.web import app as web  # noqa: E402


class _Form(dict):
    """Форма запроса: обработчику нужен только `.get`."""


class _Request:
    def __init__(self, form):
        self._form = form

    async def form(self, **_limits):  # read_form передаёт пределы разбора
        return self._form


def _post(text, where="#home"):
    return asyncio.run(web.report_problem(_Request(_Form(text=text, where=where))))


def test_an_empty_report_is_refused_before_anything_is_sent(monkeypatch):
    calls = []
    monkeypatch.setattr(acl_sentry, "report_problem", lambda *a, **k: calls.append(a) or "id")
    response = _post("   ")
    assert response.status_code == 400
    assert calls == [], "пустое обращение ушло в приёмник"


def test_the_machine_attaches_its_own_facts(monkeypatch):
    """Человек пишет, что видел; версию и машину прикладывает ACL."""
    seen = {}

    def _report(text, *, context=None):
        seen["text"], seen["context"] = text, context or {}
        return "abc123"

    monkeypatch.setattr(acl_sentry, "report_problem", _report)
    response = _post("зависло на креативе")
    assert response.status_code == 202
    assert _json.loads(response.body)["event_id"] == "abc123"
    assert seen["text"] == "зависло на креативе"
    assert "version" in seen["context"] and "machine" in seen["context"]
    assert seen["context"]["where"] == "#home"


def test_a_failure_to_send_is_not_reported_as_success(monkeypatch):
    """Требование задачи дословно: «not falsely reported after a delivery failure»."""
    monkeypatch.setattr(acl_sentry, "report_problem", lambda *a, **k: "")
    response = _post("что-то сломалось")
    assert response.status_code == 503
    body = _json.loads(response.body)
    assert body["ok"] is False and "nothing was sent" in body["error"]


def test_a_raising_collector_is_reported_as_a_failure(monkeypatch):
    def _boom(*_a, **_k):
        raise RuntimeError("сеть недоступна")

    monkeypatch.setattr(acl_sentry, "report_problem", _boom)
    response = _post("что-то сломалось")
    assert response.status_code == 502
    assert _json.loads(response.body)["ok"] is False


def test_the_suite_itself_reports_nothing_outward():
    """Прогон тестов не должен доходить до настоящего приёмника.

    Отправка висит на `run_telemetry.record()`, а его зовут десятки тестов, и
    многие — именно с отказом. Первый полный прогон после подключения так и
    сделал: «Sentry is attempting to send 1 pending events». В CI это
    повторялось бы на каждом коммите и топило бы настоящие отказы с машин
    байеров в выдуманных.

    Выключено в `tests/conftest.py`; проверка здесь, потому что сломать это
    можно откуда угодно, а заметить — только по счёту чужих событий.
    """
    assert acl_sentry.dsn() == ""
    assert acl_sentry.init() is False
