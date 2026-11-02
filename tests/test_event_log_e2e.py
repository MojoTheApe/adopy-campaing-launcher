"""Ящик -> отправка -> SQLite владельца, без внешней сети."""
import json

import pytest

from src.acl_event_log import emit, shipper
from src.acl_event_log.store import ingest_segment, machines, open_db, runs
from src.acl_event_log.writer import use_outbox_for_tests


@pytest.fixture
def outbox(tmp_path, monkeypatch):
    monkeypatch.setenv("ACL_EVENT_LOG_URL", "https://log.example")
    monkeypatch.setenv("ACL_LIBRARY_TOKEN", "tok")
    use_outbox_for_tests(tmp_path / "outbox")
    yield tmp_path / "outbox"
    use_outbox_for_tests(None)


def test_outbox_to_sqlite(outbox, tmp_path):
    emit("job.created", {"total": 1}, job_id="j1", account="acc")
    emit("job.finished", {"status": "done", "duration_s": 3.0}, job_id="j1", account="acc")
    emit("publish.result", {"campaign_name": "c", "campaign_id": "120212345678901234", "publishable": True,
                            "success": True, "error": None}, job_id="j1", account="acc", campaign="c")

    objects = {}

    def post(url, body, headers):
        objects[f"v1/{headers['X-ACL-Batch']}-{len(objects)}"] = body   # как два разных ключа Worker
        return 200

    shipper.ship_once(post=post)
    # повторная доставка той же пачки (ответ потерялся): тот же файл ещё раз
    for body in list(objects.values()):
        objects[f"v1/again-{len(objects)}"] = body

    db = open_db(tmp_path / "e.sqlite")
    seen = [ingest_segment(db, key, body) for key, body in objects.items()]
    unique = db.execute("SELECT COUNT(DISTINCT event_id) FROM events").fetchone()[0]
    assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == unique == 4   # 3 события + heartbeat
    assert sum(r["new"] for r in seen) == 4 and sum(r["duplicates"] for r in seen) == 4
    assert machines(db)[0]["gaps"] == 0
    row = runs(db)[0]
    assert row["campaign"] == "c" and row["publishable"] is True and row["duration_s"] == 3.0
    stored = db.execute("SELECT payload FROM events WHERE type='publish.result'").fetchone()[0]
    assert json.loads(stored)["campaign_id"] == "120212345678901234"
    db.close()
