# -*- coding: utf-8 -*-
"""Итоги кампаний всех машин читаются из Sentry (ACL-1447, `--from-sentry`).

Сеть подменена: `fetch` отдаёт ответ Discover API, как его отдаёт Sentry.
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts import run_summaries  # noqa: E402

ROWS = [
    {"timestamp": "2026-10-01T10:00:00+00:00", "machine": "Dan", "machine_id": "a1b2c3d4e5f6", "manager": "Dan", "result": "clean",
     "release": "v99+1@aaa", "account": "FacebookDoc19", "campaign": "GT_a", "editor": "classic"},
    {"timestamp": "2026-10-01T11:00:00+00:00", "machine": "Dan", "tags[machine_id]": "a1b2c3d4e5f6",
     "manager": "Dan",
     "result": "do-not-publish", "release": "v99+2@bbb", "account": "FacebookDoc19",
     "campaign": "GT_b", "editor": "single-screen"},
    {"timestamp": "2026-10-01T09:00:00+00:00", "tags[machine]": "LAPTOP-UA006", "manager": "Yotam",
     "result": "stopped", "release": "v99+1@aaa", "account": "Facebook_9943", "campaign": "CL_c",
     "editor": "unknown"},
]


def _fetch(seen):
    def fetch(url, headers):
        seen.append((url, headers))
        return 200, json.dumps({"data": ROWS}).encode(), {}
    return fetch


def test_machines_are_listed_with_their_label_and_results(monkeypatch):
    monkeypatch.setenv("SENTRY_AUTH_TOKEN", "test-token")
    seen: list = []
    digest = run_summaries.machines_from_sentry(period="24h", fetch=_fetch(seen))
    assert digest["events"] == 3
    dan, other = digest["machines"]
    assert dan["machine"] == "Dan" and dan["manager"] == "Dan" and dan["runs"] == 2
    assert dan["machine_id"] == "a1b2c3d4e5f6"
    assert dan["clean"] == 1 and dan["do-not-publish"] == 1
    assert dan["release"] == "v99+2@bbb" and dan["last_campaign"] == "GT_b"
    assert other["machine"] == "LAPTOP-UA006" and other["manager"] == "Yotam" and other["stopped"] == 1
    url, headers = seen[0]
    query = parse_qs(urlsplit(url).query)
    assert query["query"] == ["kind:run_summary"] and query["statsPeriod"] == ["24h"]
    assert "manager" in query["field"] and "machine" in query["field"]
    assert "machine_id" in query["field"]
    assert headers["Authorization"] == "Bearer test-token"


def test_the_script_prints_both_machines(monkeypatch):
    monkeypatch.setenv("SENTRY_AUTH_TOKEN", "test-token")
    out = io.StringIO()
    assert run_summaries.main(["--from-sentry"], fetch=_fetch([]), out=out) == 0
    text = out.getvalue()
    assert "Dan" in text and "LAPTOP-UA006" in text and "Yotam" in text


def test_no_token_is_a_skip_not_a_failure(monkeypatch):
    monkeypatch.delenv("SENTRY_AUTH_TOKEN", raising=False)
    out = io.StringIO()
    assert run_summaries.main(["--from-sentry", "--format", "json"], fetch=_fetch([]), out=out) == 0
    assert "SENTRY_AUTH_TOKEN" in json.loads(out.getvalue())["skipped"]


def test_two_computers_of_one_manager_are_two_rows():
    """Ревью PR #450, MEDIUM: ярлык `machine` = имя менеджера; ключ — `machine_id`."""
    events = [
        {"timestamp": "2026-10-01T10:00:00+00:00", "machine": "Dan", "machine_id": "aaaaaa111111",
         "manager": "Dan", "result": "clean"},
        {"timestamp": "2026-10-01T11:00:00+00:00", "machine": "Dan", "machine_id": "bbbbbb222222",
         "manager": "Dan", "result": "stopped"},
        # Ярлык переименовали — та же машина, та же строка, показан новый ярлык.
        {"timestamp": "2026-10-01T12:00:00+00:00", "machine": "Dan (office)", "machine_id": "aaaaaa111111",
         "manager": "Dan", "result": "warnings"},
    ]
    rows = run_summaries.machines(events)
    assert [(r["machine_id"], r["machine"], r["runs"]) for r in rows] == [
        ("aaaaaa111111", "Dan (office)", 2), ("bbbbbb222222", "Dan", 1)]
    text = run_summaries.to_text({"period": "7d", "events": 3, "machines": rows})
    assert "aaaaaa111111" in text and "bbbbbb222222" in text
