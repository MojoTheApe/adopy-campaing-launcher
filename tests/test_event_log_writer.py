import json
import threading

import pytest

from src.acl_event_log import emit, writer
from src.acl_event_log.writer import take_dropped, use_outbox_for_tests
from src.fb_ads_rpa import run_telemetry


@pytest.fixture
def log(tmp_path):
    use_outbox_for_tests(tmp_path)
    yield tmp_path
    use_outbox_for_tests(None)
    run_telemetry.clear_current_run()


def _lines(p):
    return [json.loads(x) for f in sorted(p.glob("*.ndjson")) for x in f.read_text("utf-8").splitlines()]


def test_envelope_and_seq(log):
    emit("job.created", {"a": 1}, job_id="j1", account="acc")
    emit("job.started")
    a, b = _lines(log)
    assert a["event_id"] == f'{a["stream_id"]}:1' and b["seq"] == 2
    assert a["schema_version"] == 1 and a["type"] == "job.created" and a["job_id"] == "j1"
    for k in ("ts", "tz_machine", "machine_id", "machine", "manager", "flow_version", "channel"):
        assert k in a
    assert a["channel"] == "prod"


def test_job_id_from_thread_context(log):
    run_telemetry.set_current_job("j9")
    emit("x")
    assert _lines(log)[0]["job_id"] == "j9"


def test_payload_is_cleaned(log):
    emit("x", {"proxy_pass": "s", "campaign_id": "120212345678901234"})
    assert _lines(log)[0]["payload"] == {"proxy_pass": "<hidden>", "campaign_id": "120212345678901234"}


def test_disabled_by_env(log, monkeypatch):
    monkeypatch.setenv("ACL_EVENT_LOG", "0")
    emit("x")
    assert _lines(log) == []


def test_silent_in_pytest_without_fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(writer, "outbox_dir", lambda: tmp_path)
    emit("x")
    assert list(tmp_path.iterdir()) == []


def test_never_raises_when_outbox_is_a_file(tmp_path):
    bad = tmp_path / "file"
    bad.write_text("x")
    use_outbox_for_tests(bad)
    try:
        emit("x")  # не бросает
    finally:
        use_outbox_for_tests(None)


def test_threads_do_not_lose_or_repeat_seq(log):
    def work():
        for _ in range(100):
            emit("t")

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(x["seq"] for x in _lines(log)) == list(range(1, 801))


def test_soft_cap_drops_raw_only(log, monkeypatch):
    monkeypatch.setattr(writer, "SOFT_CAP_BYTES", 0)
    emit("raw.fb_api", {})
    emit("job.started")
    assert [x["type"] for x in _lines(log)] == ["job.started"]
    assert take_dropped() == 1


def test_hard_cap_drops_all(log, monkeypatch):
    monkeypatch.setattr(writer, "SOFT_CAP_BYTES", 0)
    monkeypatch.setattr(writer, "HARD_CAP_BYTES", 0)
    emit("job.started")
    assert _lines(log) == [] and take_dropped() == 1
