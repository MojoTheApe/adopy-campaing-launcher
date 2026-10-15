"""ACL-1649: scripts/acl_ci_minutes.py counts minutes like the bill does; no test touches the network."""
import json

import pytest

from scripts import acl_ci_minutes as minutes

REPO = "owner/repo"


def job(name, start, end, labels=("ubuntu-latest",), attempt=1):
    return {"name": name, "started_at": start, "completed_at": end, "labels": list(labels), "run_attempt": attempt}


def run(run_id, name, event="pull_request", branch="x", created="2026-10-05T10:00:00Z", workflow="ci.yml"):
    return {"id": run_id, "name": name, "event": event, "head_branch": branch, "created_at": created,
            "path": f".github/workflows/{workflow}"}


@pytest.mark.parametrize("start,end,expected", [
    ("2026-10-05T10:00:00Z", "2026-10-05T10:00:01Z", 1),       # rounded up per job
    ("2026-10-05T10:00:00Z", "2026-10-05T10:01:00Z", 1),
    ("2026-10-05T10:00:00Z", "2026-10-05T10:01:01Z", 2),
    ("2026-10-05T10:00:00Z", "2026-10-05T10:00:00Z", 0),
    (None, "2026-10-05T10:00:00Z", 0),                           # skipped jobs never started
    ("2026-10-05T10:00:00Z", None, 0),
])
def test_job_minutes_round_up(start, end, expected):
    assert minutes.job_minutes({"started_at": start, "completed_at": end}) == expected


def test_windows_is_recognised_by_label_or_runner():
    assert minutes.is_windows({"labels": ["windows-latest"]})  # linux-ok: name of a runner label, passed as data
    assert minutes.is_windows({"labels": [], "runner_name": "Windows Server 2022"})  # linux-ok: name of a runner label, passed as data
    assert not minutes.is_windows({"labels": ["ubuntu-latest"]})  # linux-ok: name of a runner label, passed as data


class Api:
    def __init__(self, runs, jobs):
        self.runs, self.jobs = runs, jobs

    def __call__(self, path):
        if "/jobs?" in path:
            return {"jobs": self.jobs[int(path.split("/runs/")[1].split("/")[0])]}
        return {"workflow_runs": self.runs}


def test_collect_sums_per_day_workflow_event_and_counts_retries_and_reuse():
    runs = [
        run(1, "CI", "pull_request"),
        run(2, "CI", "push", "stage", "2026-10-06T09:00:00Z"),
        run(3, "CI", "push", "stage", "2026-10-06T10:00:00Z"),
        run(4, "CI", "push", "main", "2026-10-06T11:00:00Z"),
        run(5, "Windows installer", "pull_request", created="2026-10-06T12:00:00Z", workflow="windows-installer.yml"),
    ]
    jobs = {
        1: [job("linux tests (3.13, 1)", "2026-10-05T10:00:00Z", "2026-10-05T10:10:30Z"),
            job("windows tests (3.13)", "2026-10-05T10:00:00Z", "2026-10-05T10:05:00Z", ["windows-latest"], attempt=2)],
        2: [job("linux tests (3.13, 1)", "2026-10-06T09:00:00Z", "2026-10-06T09:02:00Z"),
            job("windows tests (3.13)", "2026-10-06T09:00:00Z", "2026-10-06T09:01:00Z", ["windows-latest"])],
        3: [dict(job("linux tests (3.13, 1)", None, None), conclusion="skipped"),
            dict(job("windows tests (3.13)", None, None, ["windows-latest"]), conclusion="skipped")],
        4: [dict(job("linux tests (3.11, 1)", None, None), conclusion="skipped")],
        5: [job("Build, install, repair, and uninstall", "2026-10-06T12:00:00Z", "2026-10-06T12:10:00Z", ["windows-latest"])],
    }
    result = minutes.collect(Api(runs, jobs), REPO, "2026-10-05", "2026-10-06")
    assert minutes.totals(result) == {
        "2026-10-05": {"linux": 11, "windows": 5, "linux_equivalent": 21},
        "2026-10-06": {"linux": 2, "windows": 11, "linux_equivalent": 24},
    }
    assert result["days"]["2026-10-06"]["Windows installer"] == {"linux": 0, "windows": 10, "runs": 1}
    assert result["events"]["push"] == {"linux": 2, "windows": 1}
    assert result["retried_attempt_minutes"] == {"linux": 0, "windows": 5}
    assert result["reuse"] == {"stage": {"runs": 2, "reused": 1}, "main": {"runs": 1, "reused": 1}}
    assert next(iter(result["by_job"])) == "Windows installer: Build, install, repair, and uninstall"


def test_until_drops_later_runs_and_render_is_readable():
    runs = [run(1, "CI", created="2026-10-06T10:00:00Z"), run(2, "CI", created="2026-10-06T17:00:00Z")]
    jobs = {i: [job("linux tests (3.13, 1)", "2026-10-06T10:00:00Z", "2026-10-06T10:02:00Z")] for i in (1, 2)}
    result = minutes.collect(Api(runs, jobs), REPO, "2026-10-06", "2026-10-06", until="2026-10-06T16:06:00Z")
    assert minutes.totals(result)["2026-10-06"]["linux"] == 2
    text = minutes.render(result)
    assert "2026-10-06: Linux 2  Windows 0  Linux-equivalent 2" in text and "Heaviest jobs" in text
    json.dumps(result)                                              # --json output is serialisable


def test_pagination_reads_every_page():
    pages = []

    def api(path):
        pages.append(path)
        return {"workflow_runs": [{"id": n} for n in range(100)]} if path.endswith("&page=1") else {"workflow_runs": [{"id": 1000}]}
    assert len(minutes.list_runs(api, REPO, "2026-10-05", "2026-10-05")) == 101 and len(pages) == 2


def test_gh_api_retries_a_network_failure(monkeypatch):
    import subprocess
    calls = []

    def fake(cmd, **kwargs):
        calls.append(cmd)
        if len(calls) < 3:
            raise subprocess.CalledProcessError(1, cmd)
        return '{"ok": true}'
    monkeypatch.setattr(minutes.subprocess, "check_output", fake)
    monkeypatch.setattr(minutes.time, "sleep", lambda s: None)
    assert minutes.gh_api("repos/x/y") == {"ok": True} and len(calls) == 3
