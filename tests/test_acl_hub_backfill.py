"""Перенос истории из локальной SQLite владельца в хаб (scripts/acl_hub_backfill.py)."""
import gzip
import json

from scripts import acl_hub_backfill as bf
from src.acl_event_log.store import ingest_segment, open_db


def _ev(seq, type="job.started", payload=None):
    return {"event_id": f"s1:{seq}", "stream_id": "s1", "seq": seq, "type": type,
            "ts": f"2026-11-16T10:00:0{seq}.000+00:00", "machine_id": "m1", "machine": "M", "manager": "A",
            "flow_version": "v1", "channel": "prod", "job_id": "j1", "account": "acc", "campaign": None,
            "payload": payload or {"n": seq}}


def _db(tmp_path, events):
    path = tmp_path / "e.sqlite"
    conn = open_db(path)
    body = gzip.compress("\n".join(json.dumps(e) for e in events).encode() + b"\n")
    ingest_segment(conn, "k1", body)
    conn.close()
    return path


def test_iter_batches_round_trips(tmp_path):
    path = _db(tmp_path, [_ev(1), _ev(2), _ev(3, payload={"nested": {"a": [1, 2]}})])
    batches = list(bf.iter_batches(path))
    assert len(batches) == 1
    batch_id, body = batches[0]
    assert batch_id.startswith("backfill-")
    lines = [json.loads(x) for x in gzip.decompress(body).decode().splitlines()]
    assert [x["event_id"] for x in lines] == ["s1:1", "s1:2", "s1:3"]
    assert lines[2]["payload"] == {"nested": {"a": [1, 2]}}
    assert lines[0]["machine"] == "M" and lines[0]["job_id"] == "j1"


def test_iter_batches_splits_by_size_and_bytes(tmp_path):
    path = _db(tmp_path, [_ev(i, payload={"blob": "x" * 1000}) for i in range(1, 6)])
    assert len(list(bf.iter_batches(path, size=2))) == 3
    assert len(list(bf.iter_batches(path, max_bytes=2500))) >= 3


def test_main_posts_all_batches_and_reports(tmp_path, monkeypatch, capsys):
    path = _db(tmp_path, [_ev(1), _ev(2), _ev(3)])
    monkeypatch.setenv("ACL_HUB_URL", "https://hub.example/")
    monkeypatch.setenv("ACL_HUB_OWNER_KEY", "k" * 30)
    sent = []

    def post(url, body, headers):
        sent.append((url, headers))
        n = len(gzip.decompress(body).splitlines())
        return 200, {"ok": True, "accepted": n, "duplicates": 0, "rejected": 0}

    assert bf.main(["--db", str(path), "--size", "2"], post=post) == 0
    assert len(sent) == 2
    assert sent[0][0] == "https://hub.example/v1/backfill"
    assert sent[0][1]["Authorization"] == "Bearer " + "k" * 30
    assert sent[0][1]["X-ACL-Batch"].startswith("backfill-")
    out = capsys.readouterr().out
    assert "accepted=3" in out
    assert "k" * 30 not in out


def test_main_without_key_exits_1(tmp_path, monkeypatch):
    monkeypatch.setenv("ACL_HUB_URL", "https://hub.example")
    monkeypatch.delenv("ACL_HUB_OWNER_KEY", raising=False)
    assert bf.main(["--db", str(_db(tmp_path, [_ev(1)]))], post=lambda *a: (200, {})) == 1


def test_main_stops_on_http_error(tmp_path, monkeypatch):
    path = _db(tmp_path, [_ev(1), _ev(2)])
    monkeypatch.setenv("ACL_HUB_URL", "https://hub.example")
    monkeypatch.setenv("ACL_HUB_OWNER_KEY", "k" * 30)
    calls = []

    def post(url, body, headers):
        calls.append(1)
        return 503, {"error": "storage unavailable"}

    assert bf.main(["--db", str(path), "--size", "1"], post=post) == 2
    assert len(calls) == 1
