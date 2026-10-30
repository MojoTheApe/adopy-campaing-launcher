import gzip
import json
import os
import re
import time

import pytest

from src.acl_event_log import emit, shipper
from src.acl_event_log.writer import use_outbox_for_tests


class Sink:
    """Подмена `post`: пишет вызовы, ответ задаётся классом."""

    def __init__(self):
        self.calls = []
        self.accepted = []   # тела пачек, принятых с ответом 200
        self.replies = []    # очередь кодов; пусто -> 200

    def _post(self, code):
        def post(url, body, headers):
            self.calls.append((url, body, headers))
            got = self.replies.pop(0) if self.replies else code
            if got == 200:
                self.accepted.append(body)
            return got
        return post

    @property
    def ok(self):
        return self._post(200)

    @property
    def fail_503(self):
        return self._post(503)

    @property
    def fail_401(self):
        return self._post(401)

    def all_decoded(self):
        return b"".join(gzip.decompress(b) for b in self.accepted)

    decoded = all_decoded


@pytest.fixture
def log(tmp_path, monkeypatch):
    monkeypatch.setenv("ACL_EVENT_LOG_URL", "https://log.example")
    monkeypatch.setenv("ACL_LIBRARY_TOKEN", "tok")
    use_outbox_for_tests(tmp_path)
    yield tmp_path
    use_outbox_for_tests(None)


@pytest.fixture
def sink():
    return Sink()


def test_acks_and_does_not_resend(log, sink):
    emit("a")
    emit("b")
    assert shipper.ship_once(post=sink.ok)["sent"] == 3    # два события и heartbeat
    assert shipper.ship_once(post=sink.ok)["sent"] == 1    # только новый heartbeat


def test_headers_and_gzip(log, sink):
    emit("a")
    shipper.ship_once(post=sink.ok)
    url, body, h = sink.calls[0]
    assert url.endswith("/v1/batch") and h["Authorization"] == "Bearer tok"
    assert re.fullmatch(r"[0-9a-f]{32}-\d+-\d+", h["X-ACL-Batch"])
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,64}", h["X-ACL-Machine"])
    assert h["Content-Encoding"] == "gzip" and h["X-ACL-Count"] == "2"
    assert json.loads(gzip.decompress(body).splitlines()[0])["type"] == "a"


def test_failure_keeps_events_for_next_cycle(log, sink):
    emit("a")
    assert shipper.ship_once(post=sink.fail_503)["status"] == "retry"
    assert shipper.ship_once(post=sink.ok)["sent"] >= 1 and b'"type": "a"' in sink.decoded()


def test_401_stops_cycle_and_keeps_data(log, sink):
    emit("a")
    r = shipper.ship_once(post=sink.fail_401)
    assert r["status"] == "unauthorized" and len(sink.calls) == 1
    assert shipper.ship_once(post=sink.ok)["sent"] >= 1


def test_413_halves_batch(log, sink):
    for i in range(10):
        emit("e", {"i": i})
    sink.replies = [413]
    r = shipper.ship_once(post=sink.ok)
    types = [json.loads(x)["type"] for x in sink.all_decoded().splitlines()]
    # все 10 событий и heartbeat доставлены, большая пачка не прошла
    assert r["status"] == "ok" and types.count("e") == 10 and len(sink.calls) > 2


def test_batch_size_limit(log, sink, monkeypatch):
    monkeypatch.setattr(shipper, "BATCH_MAX_BYTES", 300)
    for _ in range(20):
        emit("a", {"x": "y" * 50})
    shipper.ship_once(post=sink.ok)
    assert len(sink.calls) > 1


def _crashed_stream(log):
    """Файл упавшего процесса: одна целая строка и оборванный хвост."""
    f = log / ("c" * 32 + ".ndjson")
    whole = json.dumps({"event_id": "c:1", "seq": 1, "type": "whole"})
    f.write_text(whole + "\n" + '{"type": "broken', encoding="utf-8")
    return f


def test_partial_last_line_never_sent(log, sink):
    f = _crashed_stream(log)
    emit("a")
    shipper.ship_once(post=sink.ok)
    sent = sink.all_decoded()
    assert b"whole" in sent and b"broken" not in sent
    assert f.exists()   # свежий файл: хвост ещё может дописаться, ждём


def test_partial_tail_of_a_dead_stream_is_given_up(log, sink):
    f = _crashed_stream(log)
    long_ago = time.time() - shipper.PARTIAL_LOST_S - 60
    os.utime(f, (long_ago, long_ago))
    shipper.ship_once(post=sink.ok)
    acked = json.loads(f.with_suffix(".acked").read_text())
    assert acked["offset"] == f.stat().st_size
    assert b"broken" not in sink.all_decoded()


def test_foreign_stream_old_file_removed(log, sink):
    old = log / ("f" * 32 + ".ndjson")
    line = json.dumps({"event_id": "x:1", "seq": 1, "type": "z"}) + "\n"
    old.write_text(line, encoding="utf-8")
    old.with_suffix(".acked").write_text(json.dumps({"offset": len(line.encode()), "last_seq": 1}))
    long_ago = time.time() - shipper.FOREIGN_TTL_S - 60
    os.utime(old, (long_ago, long_ago))
    emit("a")
    shipper.ship_once(post=sink.ok)
    assert not old.exists() and not old.with_suffix(".acked").exists()


def test_foreign_stream_not_yet_sent_is_sent_and_kept(log, sink):
    other = log / ("e" * 32 + ".ndjson")
    other.write_text(json.dumps({"event_id": "e:1", "seq": 1, "type": "from_crash"}) + "\n", encoding="utf-8")
    shipper.ship_once(post=sink.ok)
    assert b"from_crash" in sink.all_decoded()
    assert other.exists()   # свежий файл удаляется не сразу, а через сутки


def test_no_url_or_token_is_silent(log, sink, monkeypatch):
    monkeypatch.delenv("ACL_LIBRARY_TOKEN")
    emit("a")
    r = shipper.ship_once(post=sink.ok)
    assert r["status"] == "not_configured" and sink.calls == []
    assert shipper.status()["configured"] is False


def test_lock_busy_skips(log, sink):
    emit("a")
    (log / ".shipper.lock").write_text("other")   # свежий замок
    assert shipper.ship_once(post=sink.ok)["status"] == "busy"


def test_stale_lock_is_taken(log, sink):
    emit("a")
    lock = log / ".shipper.lock"
    lock.write_text("dead")
    old = time.time() - shipper.LOCK_TTL_S - 60
    os.utime(lock, (old, old))
    assert shipper.ship_once(post=sink.ok)["status"] == "ok"
    assert not lock.exists()


def test_status_reports_last_ship(log, sink):
    emit("a")
    assert shipper.status()["last_ship_at"] == ""
    shipper.ship_once(post=sink.ok)
    st = shipper.status()
    assert st["configured"] is True and st["last_ship_at"]


def test_start_shipper_is_silent_under_pytest():
    use_outbox_for_tests(None)
    shipper.start_shipper()
    assert shipper._thread is None


def test_doctor_line_is_never_a_failure(log, capsys, monkeypatch):
    from scripts import acl_doctor

    monkeypatch.delenv("ACL_LIBRARY_TOKEN")
    acl_doctor._failed = 0
    acl_doctor.check_event_log()
    out = capsys.readouterr().out
    assert "Event log" in out and "not configured yet" in out and acl_doctor._failed == 0
