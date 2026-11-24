"""Локальный архив владельца читает события из хаба (/api/events) постранично."""
import json

from src.acl_event_log.store import open_db, pull_hub


def _ev(seq):
    return {"hub_seq": seq, "event_id": f"s:{seq}", "stream_id": "s", "seq": seq, "type": "job.started",
            "ts": f"2026-11-16T10:00:0{seq}.000+00:00", "machine_id": "m", "machine": "M", "manager": "A",
            "flow_version": "v", "channel": "prod", "job_id": "j", "account": "a", "campaign": None,
            "payload": {"n": seq}}


class FakeHub:
    def __init__(self, events, page=2):
        self.events, self.page, self.calls = events, page, []

    def __call__(self, after):
        self.calls.append(after)
        chunk = [e for e in self.events if e["hub_seq"] > after][: self.page]
        body = "".join(json.dumps(e) + "\n" for e in chunk).encode()
        return body, (chunk[-1]["hub_seq"] if chunk else after)


def test_pull_from_hub_pages_until_empty(tmp_path):
    conn = open_db(tmp_path / "e.sqlite")
    hub = FakeHub([_ev(1), _ev(2), _ev(3)])
    total = pull_hub(conn, hub)
    assert total["new"] == 3
    assert hub.calls == [0, 2, 3]
    assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 3
    row = conn.execute("SELECT payload FROM events WHERE event_id='s:3'").fetchone()
    assert json.loads(row[0]) == {"n": 3}
    again = pull_hub(conn, hub)
    assert again["new"] == 0
    assert hub.calls[-1] == 3          # продолжает с запомненного курсора, а не с нуля
    conn.close()
