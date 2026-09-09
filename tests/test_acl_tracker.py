import json
import subprocess
from types import SimpleNamespace

import pytest

from scripts import acl_tracker as tracker


SNAPSHOT = """## 1 · Accounts
> Account control
### Metrics
- [x] `acl-101` OLD TITLE #shipped
"""


def issue(number=1, **overrides):
    return dict(number=number, title="acl-101: Current title", state="open",
                labels=[{"name": "status:next"}, {"name": "area:1"}],
                body="Details\n### Subtasks\n- [x] `acl-101a` Done part\n- [ ] Remaining",
                **overrides)


def test_github_overrides_snapshot_and_preserves_layout_groups():
    board = tracker.build_board([issue()], SNAPSHOT, tracker.REPOSITORY, "today")
    task = board.tasks[0]
    assert task.title == "Current title" and task.shown_status == "next"
    assert task.sub_progress == (1, 2)
    assert board.areas[0].title == "Accounts" and board.areas[0].caps[0].title == "Metrics"
    assert "OLD TITLE" not in tracker.wall.render(board)


def test_closed_is_not_shipped_or_merged_without_evidence():
    item = issue()
    item.update(state="closed", labels=[])
    task = tracker.build_board([item], SNAPSHOT, tracker.REPOSITORY, "today").tasks[0]
    assert task.shown_status == "closed"


def test_all_new_issues_and_duplicate_board_ids_are_visible():
    items = [issue(1), issue(2), issue(3)]
    items[2]["title"] = "New unrelated issue"
    items[2]["labels"] = []
    board = tracker.build_board(items, SNAPSHOT, tracker.REPOSITORY, "today")
    assert len(board.tasks) == 3
    assert len({t.id for t in board.tasks}) == 3
    assert board.areas[-1].title == "Other tasks"


def test_issue_content_cannot_inject_html():
    item = issue()
    item["title"] = '<script>alert(1)</script>'
    item["body"] = '<img src=x onerror="alert(1)">'
    item["html_url"] = "javascript:alert(1)"
    page, _ = tracker.render_issues([item], SNAPSHOT, tracker.REPOSITORY, "today")
    assert '<script>alert(1)</script>' not in page and '<img src=x' not in page
    assert 'javascript:alert' not in page
    assert "GitHub #1" in page and "Source of truth:" in page and "GitHub Issues" in page
    assert "in Obsidian" not in page


def test_fetches_all_pages_and_excludes_pull_requests(monkeypatch):
    pr = issue(2)
    pr["pull_request"] = {}
    def run(args, **kwargs):
        assert "--paginate" in args and "--slurp" in args
        return SimpleNamespace(stdout=json.dumps([[issue(), pr], [issue(3)]]))
    monkeypatch.setattr(tracker.subprocess, "run", run)
    assert [i["number"] for i in tracker.fetch_issues(["gh"], tracker.REPOSITORY)] == [1, 3]


def test_partial_failure_keeps_previous_board_and_last_sync_time(tmp_path, monkeypatch):
    output = tmp_path / "public"
    output.mkdir()
    (output / "index.html").write_text("previous successful board")
    (output / "status.json").write_text(json.dumps({"ok": True, "synced_at": "yesterday", "digest": "old"}))
    def fail(*args):
        raise subprocess.CalledProcessError(1, "gh", stderr="SENSITIVE")
    monkeypatch.setattr(tracker, "fetch_issues", fail)
    with pytest.raises(RuntimeError, match="previous board preserved"):
        tracker.sync(["gh"], tracker.REPOSITORY, tmp_path / "snapshot", output)
    assert (output / "index.html").read_text() == "previous successful board"
    status = json.loads((output / "status.json").read_text())
    assert status["ok"] is False and status["synced_at"] == "yesterday"
    assert "SENSITIVE" not in (output / "status.json").read_text()


def test_successful_sync_updates_html_and_status_together(tmp_path, monkeypatch):
    snapshot = tmp_path / "snapshot.md"
    snapshot.write_text(SNAPSHOT)
    monkeypatch.setattr(tracker, "fetch_issues", lambda *args: [issue()])
    tracker.sync(["gh"], tracker.REPOSITORY, snapshot, tmp_path / "public")
    status = json.loads((tmp_path / "public/status.json").read_text())
    html = (tmp_path / "public/index.html").read_text()
    assert status["ok"] and status["issues"] == 1 and status["digest"] in html
    assert "Current title" in html


def test_empty_response_cannot_replace_existing_board_and_next_sync_recovers(tmp_path, monkeypatch):
    snapshot = tmp_path / "snapshot.md"
    snapshot.write_text(SNAPSHOT)
    output = tmp_path / "public"
    monkeypatch.setattr(tracker, "fetch_issues", lambda *args: [issue()])
    tracker.sync(["gh"], tracker.REPOSITORY, snapshot, output)
    html = (output / "index.html").read_text()
    old = json.loads((output / "status.json").read_text())
    monkeypatch.setattr(tracker, "fetch_issues", lambda *args: [])
    for _ in range(2):
        with pytest.raises(RuntimeError, match="previous board preserved"):
            tracker.sync(["gh"], tracker.REPOSITORY, snapshot, output)
        assert (output / "index.html").read_text() == html
        status = json.loads((output / "status.json").read_text())
        assert not status["ok"] and status["error"] == "empty_response"
        assert status["issues"] == 1 and status["digest"] == old["digest"]
        assert status["synced_at"] == old["synced_at"]
    updated = issue()
    updated["title"] = "Recovered task"
    monkeypatch.setattr(tracker, "fetch_issues", lambda *args: [updated])
    tracker.sync(["gh"], tracker.REPOSITORY, snapshot, output)
    assert "Recovered task" in (output / "index.html").read_text()
    assert json.loads((output / "status.json").read_text())["ok"]


def test_new_empty_project_and_explicit_empty_reset_are_supported(tmp_path, monkeypatch):
    snapshot = tmp_path / "snapshot.md"
    snapshot.write_text(SNAPSHOT)
    output = tmp_path / "public"
    monkeypatch.setattr(tracker, "fetch_issues", lambda *args: [])
    tracker.sync(["gh"], tracker.REPOSITORY, snapshot, output)
    assert "No tasks yet" in (output / "index.html").read_text()
    monkeypatch.setattr(tracker, "fetch_issues", lambda *args: [issue()])
    tracker.sync(["gh"], tracker.REPOSITORY, snapshot, output)
    monkeypatch.setattr(tracker, "fetch_issues", lambda *args: [])
    tracker.sync(["gh"], tracker.REPOSITORY, snapshot, output, allow_empty=True)
    assert json.loads((output / "status.json").read_text())["issues"] == 0


def test_response_with_no_pages_is_not_a_valid_empty_repository(monkeypatch):
    monkeypatch.setattr(tracker.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="[]"))
    with pytest.raises(ValueError, match="complete GitHub issue pages"):
        tracker.fetch_issues(["gh"], tracker.REPOSITORY)


def test_deployment_changes_trigger_refresh_but_timestamps_do_not(monkeypatch):
    _, original = tracker.render_issues([issue()], SNAPSHOT, tracker.REPOSITORY, "earlier")
    _, later = tracker.render_issues([issue()], SNAPSHOT, tracker.REPOSITORY, "later")
    assert later == original
    monkeypatch.setattr(tracker.wall, "_PAGE", tracker.wall._PAGE.replace("#dfe9dd", "#d0e5d0"))
    _, changed = tracker.render_issues([issue()], SNAPSHOT, tracker.REPOSITORY, "later")
    assert changed != original


def test_all_button_includes_every_status_in_its_count():
    a, b = issue(1), issue(2)
    b["labels"] = [{"name": "status:merged"}]
    page, _ = tracker.render_issues([a, b], SNAPSHOT, tracker.REPOSITORY, "today")
    assert 'data-filter="all" aria-pressed="true">All<b>2</b>' in page
