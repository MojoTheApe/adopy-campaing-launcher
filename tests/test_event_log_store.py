import gzip
import json

import pytest

from src.acl_event_log.store import (
    ingest_segment, job_history, machines, open_db, pull_new, runs,
)


def seg(*events):
    return gzip.compress("\n".join(json.dumps(e) for e in events).encode() + b"\n")


def ev(seq, type="job.started", stream="s1", job="j1", ts="2026-10-30T10:00:00.000+00:00", **kw):
    return {"event_id": f"{stream}:{seq}", "stream_id": stream, "seq": seq, "type": type, "ts": ts,
            "machine_id": "m1", "machine": "M", "manager": "A", "flow_version": "v1", "channel": "prod",
            "job_id": job, "account": "acc", "campaign": kw.pop("campaign", "c"), "payload": kw}


@pytest.fixture
def db(tmp_path):
    conn = open_db(tmp_path / "e.sqlite")
    yield conn
    conn.close()


def test_duplicates_ignored(db):
    assert ingest_segment(db, "k1", seg(ev(1), ev(2)))["new"] == 2
    r = ingest_segment(db, "k2", seg(ev(2), ev(3)))
    assert r["new"] == 1 and r["duplicates"] == 1 and r["conflicts"] == 0


def test_same_key_twice_is_noop(db):
    ingest_segment(db, "k1", seg(ev(1)))
    assert ingest_segment(db, "k1", seg(ev(1)))["new"] == 0


def test_conflict_counted_first_kept(db):
    ingest_segment(db, "k1", seg(ev(1, a=1)))
    assert ingest_segment(db, "k2", seg(ev(1, a=2)))["conflicts"] == 1
    assert json.loads(db.execute("SELECT payload FROM events").fetchone()[0]) == {"a": 1}


def test_token_audit_reports_id_not_value(db):
    r = ingest_segment(db, "k1", seg(ev(1, m="EAAB" + "a" * 30)))
    assert r["secrets"] == ["s1:1"]
    assert "EAAB" not in json.dumps(r)


def test_access_token_param_is_flagged(db):
    assert ingest_segment(db, "k1", seg(ev(1, url="https://g/x?access_token=zzz")))["secrets"] == ["s1:1"]


def test_gaps_visible(db):
    ingest_segment(db, "k1", seg(ev(1), ev(2), ev(5)))
    assert machines(db)[0]["gaps"] == 2


def test_no_gaps_when_complete(db):
    ingest_segment(db, "k1", seg(ev(1), ev(2), ev(3, "heartbeat", outbox_bytes=10, dropped=0)))
    m = machines(db)[0]
    assert m["gaps"] == 0 and m["outbox_bytes"] == 10 and m["flow_version"] == "v1"


def test_runs_joins_result_to_job(db):
    ingest_segment(db, "k1", seg(
        ev(1, "job.finished", status="done", duration_s=12.5),
        ev(2, "publish.result", campaign_name="c", publishable=True, error="")))
    row = runs(db)[0]
    assert row["campaign"] == "c" and row["publishable"] is True and row["duration_s"] == 12.5


def test_runs_prefers_campaign_duration_and_stop_reason(db):
    ingest_segment(db, "k1", seg(
        ev(1, "job.finished", status="done", duration_s=30.0),
        ev(2, "publish.stats", duration_s=9.5, stop_kind="rate_limit"),
        ev(3, "publish.result", campaign_name="c", publishable=False, error="stopped")))
    row = runs(db)[0]
    assert row["duration_s"] == 9.5 and row["stop_kind"] == "rate_limit" and row["error"] == "stopped"


def test_runs_filters_and_old_events_dropped(db):
    ingest_segment(db, "k1", seg(
        ev(1, "publish.result", campaign_name="c"),
        ev(2, "publish.result", job="j2", ts="2020-01-01T00:00:00.000+00:00", campaign_name="old")))
    assert sorted(r["job_id"] for r in runs(db, days=100000)) == ["j1", "j2"]
    assert [r["job_id"] for r in runs(db, days=7)] == ["j1"]
    assert runs(db, account="other") == [] and runs(db, manager="other") == []


def test_job_history_is_ordered(db):
    ingest_segment(db, "k1", seg(ev(2, "job.finished"), ev(1, "job.started")))
    assert [e["type"] for e in job_history(db, "j1")] == ["job.started", "job.finished"]


def test_damaged_line_skipped_not_fatal(db):
    body = gzip.compress(b'{"bad\n' + json.dumps(ev(1)).encode() + b"\n")
    assert ingest_segment(db, "k1", body)["new"] == 1


def test_unreadable_segment_is_not_recorded_so_it_is_retried(db):
    r = ingest_segment(db, "k1", b"not gzip at all")
    assert r.get("error") and db.execute("SELECT COUNT(*) FROM segments").fetchone()[0] == 0


def test_pull_resumes_after_a_network_error(db):
    bodies = {"k1": seg(ev(1)), "k2": seg(ev(2)), "k3": seg(ev(3))}

    def list_page(cursor):
        return (["k1", "k2"], "next") if cursor is None else (["k3"], None)

    state = {"fail": True}

    def fetch(key):
        if key == "k2" and state["fail"]:
            raise OSError("network down")
        return bodies[key]

    with pytest.raises(OSError):
        pull_new(db, list_page, fetch)
    assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1   # k1 сохранён, база цела
    state["fail"] = False
    total = pull_new(db, list_page, fetch)
    assert total["segments"] == 2 and total["new"] == 2
    assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 3
