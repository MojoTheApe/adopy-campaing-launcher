"""Выключение из карты аккаунтов: журнал, сохранение в снимке и защита сервера.

Бывший `test_the_account_tree_on_screen.py`. Экранная половина (старый список «in the
account» с кнопкой Turn off, раскрытием кампаний и журналом) убрана в ACL-1800: под
строкой аккаунта теперь командная таблица Home в области одного аккаунта, её проверяет
`test_acl1800_account_scoped_table.py`. Здесь остались серверные гарантии, которым
экран не нужен: каждое выключение оставляет след, подтверждённый исход правит
сохранённое чтение, обработчик не бежит в цикле веб-сервера.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TPL = ROOT / "src" / "web" / "templates"


def test_turning_off_never_runs_in_the_web_loop():
    """Playwright здесь синхронный, и вызов его из работающего цикла asyncio
    кончается `RuntimeError: Cannot enter into task …` — выключение падает, ни
    разу не тронув страницу. Так и вышло на первом живом нажатии 12.09.

    Чтение по той же причине живёт в своём потоке (`start_read`).
    """
    src = (ROOT / "src" / "web" / "app.py").read_text(encoding="utf-8")
    start = src.index("async def account_spend_turn_off(")
    body = src[start: src.index("\n@app.", start)]
    assert "run_in_threadpool" in body, "выключение снова вызывается в цикле веб-сервера"
    assert "await run_in_threadpool(\n            account_spend.turn_off," in body


# ══ след выключения ═══════════════════════════════════════════════════════
#
# Первое живое нажатие 12.09 вернуло отказ, и прочитать его причину было
# негде: результат уходил в ответ HTTP и исчезал. Действие, меняющее чужой
# аккаунт, обязано оставлять след — «что-то изменилось вчера» не ответ.

def test_every_turn_off_leaves_a_record(monkeypatch, tmp_path):
    from src.analysis_agent import account_spend as sp

    monkeypatch.setattr(sp, "_store_path", lambda: tmp_path / "readings.json")
    seen = []
    monkeypatch.setattr(sp, "_turn_off", lambda *a, **k: sp.TurnOffResult(
        account="acc", level="campaign", object_id="1", state="failed",
        error="на экране другой период — ничего не трогаем"))
    monkeypatch.setattr(sp, "_record_turn_off", lambda out: seen.append(out))
    sp.turn_off("acc", "campaign", "1")
    assert len(seen) == 1, "у действия нет следа"

def test_the_record_survives_a_refusal(monkeypatch, tmp_path):
    """Отказ нужнее удачи: именно его причину приходится искать."""
    from src.analysis_agent import account_spend as sp

    monkeypatch.setattr(sp, "_store_path", lambda: tmp_path / "readings.json")
    sp._record_turn_off(sp.TurnOffResult(
        account="KLD_BFL_X84", level="ad", object_id="777", name="ad one",
        state="failed", error="под этим id на странице «чужое имя»"))
    rows = sp.turn_off_history()
    assert len(rows) == 1
    assert rows[0]["state"] == "failed"
    assert "чужое имя" in rows[0]["error"], "причина не записана"

def test_the_record_never_breaks_the_answer(monkeypatch, tmp_path):
    """Потерять журнал плохо, потерять ответ — хуже."""
    from src.analysis_agent import account_spend as sp

    monkeypatch.setattr(sp, "_turn_off_log", lambda: tmp_path / "no" / "such" / "dir" / "x.jsonl")
    monkeypatch.setattr(Path, "mkdir", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    sp._record_turn_off(sp.TurnOffResult(account="a", level="ad", object_id="1"))

def test_the_newest_record_comes_first(monkeypatch, tmp_path):
    from src.analysis_agent import account_spend as sp

    monkeypatch.setattr(sp, "_store_path", lambda: tmp_path / "readings.json")
    for i in range(3):
        sp._record_turn_off(sp.TurnOffResult(account="a", level="ad", object_id=str(i)))
    assert [r["object_id"] for r in sp.turn_off_history()] == ["2", "1", "0"]


# Persistent confirmation is exercised by test_turn_off_safety_browser.py.


@pytest.mark.parametrize("status,payload", [
    (500, {"ok": True, "entries": []}),
    (200, {"ok": False, "error": "The record could not be read."}),
    (200, {"ok": True}),
    (200, {"ok": True, "entries": {}}),
])

def _store(monkeypatch, tmp_path, objects):
    from src.analysis_agent import account_spend as sp
    import json

    path = tmp_path / "readings.json"
    monkeypatch.setattr(sp, "_store_path", lambda: path)
    path.write_text(json.dumps({
        "acc|2026-09-08|2026-09-11": {"account": "acc", "date_from": "2026-09-08",
                                      "date_to": "2026-09-11", "objects": objects},
        "acc|2026-09-11|2026-09-11": {"account": "acc", "date_from": "2026-09-11",
                                      "date_to": "2026-09-11", "objects": objects},
        "other|2026-09-11|2026-09-11": {"account": "other", "objects": objects},
    }), encoding="utf-8")
    return sp, path

def test_a_confirmed_turn_off_updates_the_saved_tree(monkeypatch, tmp_path):
    sp, path = _store(monkeypatch, tmp_path, [
        {"id": "777", "level": "ad", "switch": "on", "delivery": "Active"},
        {"id": "888", "level": "ad", "switch": "on", "delivery": "Active"},
    ])
    touched = sp._mark_object_off("acc", "777", "Processing")
    assert touched == 2, "поправлено не во всех чтениях аккаунта"
    import json
    data = json.loads(path.read_text(encoding="utf-8"))
    for key in ("acc|2026-09-08|2026-09-11", "acc|2026-09-11|2026-09-11"):
        rows = {o["id"]: o for o in data[key]["objects"]}
        assert rows["777"]["switch"] == "off"
        assert rows["777"]["delivery"] == "Processing", "доставка не та, что прочитали"
        assert rows["888"]["switch"] == "on", "тронули соседа"

def test_another_account_is_left_alone(monkeypatch, tmp_path):
    sp, path = _store(monkeypatch, tmp_path, [
        {"id": "777", "level": "ad", "switch": "on", "delivery": "Active"}])
    sp._mark_object_off("acc", "777", "Off")
    import json
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["other|2026-09-11|2026-09-11"]["objects"][0]["switch"] == "on"

def test_an_unread_delivery_is_not_invented(monkeypatch, tmp_path):
    """Не прочитали — пусть остаётся прежняя, чем ложная."""
    sp, path = _store(monkeypatch, tmp_path, [
        {"id": "777", "level": "ad", "switch": "on", "delivery": "Active"}])
    sp._mark_object_off("acc", "777", "")
    import json
    data = json.loads(path.read_text(encoding="utf-8"))
    row = data["acc|2026-09-11|2026-09-11"]["objects"][0]
    assert row["switch"] == "off" and row["delivery"] == "Active"

def test_only_a_confirmed_outcome_touches_the_tree():
    """Отказ ничего не подтвердил, и дерево трогать не за что."""
    src = (ROOT / "src" / "analysis_agent" / "account_spend.py").read_text(encoding="utf-8")
    start = src.index("def turn_off(account: str")
    body = src[start: src.index("def _turn_off(account: str", start)]
    assert 'if out.state in ("off", "already_off"):' in body
    assert "_mark_object_off" in body


# ── таблица без id (Doc19, ACL-1255) ──────────────────────────────────────

def test_the_remove_control_keeps_its_action_and_its_name():
    """ACL-1277: «Remove» стал значком «×». Действие и подпись для экранного
    диктора остаются: кнопка убирает аккаунт из страны, а не удаляет его."""
    src = (TPL / "_account_map_country.html").read_text(encoding="utf-8")
    start = src.index('class="amap-x"')
    cell = src[start: src.index("</td>", start)]
    assert ">×</button>" in cell
    assert "amapUnlink(" in cell and "'country'" in cell
    assert "aria-label=" in cell and "out of {{ mc.country }}" in cell
    assert "keeps its Multilogin profile" in cell
