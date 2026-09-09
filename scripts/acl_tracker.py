"""Read GitHub Issues into the existing task-wall UI; never write to GitHub.

The sync job owns GitHub access. The web container receives only rendered files.
Snapshot metadata supplies area/capability names, never task contents or status.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

if __package__:
    from . import acl_board as wall
else:
    import acl_board as wall

REPOSITORY = "MojoTheApe/adopy-campaing-launcher"
TASK_ID = re.compile(r"^(acl-\d+[a-z]?)\s*:\s*", re.I)
SUBTASK = re.compile(r"^\s*-\s*\[([ xX])\]\s*(?:`(acl-\d+[a-z]?)`\s*)?(.+)$")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=".tracker-", delete=False) as handle:
        handle.write(text)
        temporary = Path(handle.name)
    temporary.chmod(0o644)
    temporary.replace(path)


def fetch_issues(command: list[str], repository: str) -> list[dict]:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Invalid repository")
    result = subprocess.run(
        command + ["api", "--paginate", "--slurp",
                   f"repos/{repository}/issues?state=all&per_page=100&sort=created&direction=asc"],
        capture_output=True, text=True, timeout=90, check=True,
    )
    pages = json.loads(result.stdout)
    if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
        raise ValueError("Expected complete GitHub issue pages")
    issues = [item for page in pages for item in page]
    if any(not isinstance(item, dict) or not isinstance(item.get("number"), int)
           or not isinstance(item.get("title"), str) or item.get("state") not in ("open", "closed")
           for item in issues):
        raise ValueError("Invalid GitHub issue response")
    return [item for item in issues if "pull_request" not in item]


def build_board(issues: list[dict], snapshot: str, repository: str, synced_at: str) -> wall.Board:
    structure = wall.parse(snapshot)
    locations = {task.id: (area.number, cap.title)
                 for area in structure.areas for cap in area.caps for task in cap.tasks}
    area_info = {area.number: area for area in structure.areas}
    groups: dict[str, dict[str, list[wall.Task]]] = {}
    ids = Counter((TASK_ID.match(i["title"]).group(1).lower()
                   if TASK_ID.match(i["title"]) else f"issue-{i['number']}") for i in issues)
    for issue in sorted(issues, key=lambda item: item["number"]):
        if "pull_request" in issue:
            continue
        match = TASK_ID.match(issue["title"])
        task_id = match.group(1).lower() if match else f"issue-{issue['number']}"
        labels = [x["name"] if isinstance(x, dict) else x for x in issue.get("labels", [])]
        statuses = [x[7:] for x in labels if x.startswith("status:") and x[7:] in wall.STATUSES]
        status = statuses[0] if len(statuses) == 1 else "idea"
        closed = issue["state"].lower() == "closed"
        if closed and status not in ("shipped", "merged"):
            status = "closed"  # Closing an issue is not evidence of a deployment.
        if not closed and status == "closed":
            status = "idea"
        area_labels = [x[5:] for x in labels if re.fullmatch(r"area:\d+", x)]
        previous_area, previous_cap = locations.get(task_id, ("0", "Tasks"))
        area = area_labels[0] if len(area_labels) == 1 else previous_area
        cap = previous_cap if area == previous_area else "Tasks"
        tracks = [x[6:] for x in labels if x.startswith("track:")]
        body = issue.get("body") or ""
        notes, subtasks = [], []
        fenced = False
        for line in body.splitlines():
            if line.strip().startswith("```"):
                fenced = not fenced
            sub = SUBTASK.match(line) if not fenced else None
            if sub:
                subtasks.append(wall.Task(id=sub.group(2) or "", title=sub.group(3),
                                          done=sub.group(1).lower() == "x"))
            elif not re.match(r"^\s*(?:### Subtasks|_Board id:)", line):
                notes.append(line)
        assignees = ", ".join(x["login"] for x in issue.get("assignees", []))
        if assignees:
            notes.append(f"Assigned to: {assignees}")
        if len(statuses) > 1:
            notes.append("Conflicting status labels: " + ", ".join(statuses))
        task = wall.Task(
            id=task_id if ids[task_id] == 1 else f"{task_id} · #{issue['number']}",
            title=issue["title"][match.end():] if match else issue["title"],
            status=status, explicit=True, track=tracks[0] if tracks else "",
            note="\n".join(notes).strip(), subs=subtasks,
            done=closed and issue.get("state_reason") != "not_planned",
            needs=wall.NEEDS_RE.findall(body),
            source_url=f"https://github.com/{repository}/issues/{issue['number']}",
            source_label=f"GitHub #{issue['number']} · {'Closed' if closed else 'Open'} →",
        )
        groups.setdefault(area, {}).setdefault(cap, []).append(task)
    board = wall.Board(
        title="ACL Task Wall", updated=synced_at,
        description="Live tasks from GitHub Issues. Open an area, filter by status, "
                    "and expand a card for details. Changes in GitHub appear automatically.",
        source_url=f"https://github.com/{repository}/issues", source_label="GitHub Issues",
    )
    for number, caps in sorted(groups.items(), key=lambda item: (item[0] == "0", int(item[0]))):
        meta = area_info.get(number)
        board.areas.append(wall.Area(
            number=number, title=meta.title if meta else ("Other tasks" if number == "0" else f"Area {number}"),
            blurb=meta.blurb if meta else "",
            caps=[wall.Capability(title=title, tasks=tasks) for title, tasks in caps.items()],
        ))
    return board


def render_issues(issues: list[dict], snapshot: str, repository: str, synced_at: str) -> tuple[str, str]:
    digest = hashlib.sha256(json.dumps(issues, sort_keys=True).encode()).hexdigest()
    page = wall.render(build_board(issues, snapshot, repository, synced_at))
    page = '<!doctype html>\n<html lang="en">\n' + page
    page = page.replace('<meta charset="utf-8">',
                        '<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">')
    page = page.replace('<span>Built by <code>scripts/acl_board.py</code></span>',
                        '<span id="sync-state" role="status">Updates every minute</span>')
    page += '\n<script>\n' + CLIENT.replace("__DIGEST__", json.dumps(digest)) + '\n</script>\n</html>\n'
    return page, digest


CLIENT = """
(() => {
  const digest = __DIGEST__;
  const message = document.getElementById('sync-state');
  const key = 'acl.tracker.filter';
  try {
    const wanted = sessionStorage.getItem(key);
    document.querySelectorAll('.chip').forEach(c => {
      if (c.dataset.filter === wanted) c.click();
      c.addEventListener('click', () => {
        try { sessionStorage.setItem(key, c.getAttribute('aria-pressed') === 'true' ? c.dataset.filter : ''); }
        catch (_) {}
      });
    });
  } catch (_) {}
  async function refresh() {
    try {
      const response = await fetch('/status.json', {cache: 'no-store'});
      if (!response.ok) throw new Error('sync unavailable');
      const state = await response.json();
      const age = Date.now() - Date.parse(state.synced_at);
      if (!state.ok || !Number.isFinite(age) || age > 180000) {
        message.textContent = 'Sync delayed · showing the last successful update';
        return;
      }
      message.textContent = 'Synced ' + new Date(state.synced_at).toLocaleTimeString() + ' · updates every minute';
      if (state.digest !== digest) location.reload();
    } catch (_) { message.textContent = 'Connection lost · showing the last successful update'; }
  }
  refresh();
  setInterval(refresh, 60000);
})();
"""


def sync(command: list[str], repository: str, snapshot: Path, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    status_path = output / "status.json"
    try:
        issues = fetch_issues(command, repository)
        timestamp = now()
        page, digest = render_issues(issues, snapshot.read_text(encoding="utf-8"), repository, timestamp)
        atomic_write(output / "index.html", page)
        atomic_write(status_path, json.dumps({"ok": True, "synced_at": timestamp,
                                              "digest": digest, "issues": len(issues)}))
        print(f"Synced {len(issues)} issues at {timestamp}")
    except Exception:
        try:
            previous = json.loads(status_path.read_text())
        except (OSError, ValueError):
            previous = {}
        previous.update(ok=False, attempted_at=now())
        atomic_write(status_path, json.dumps(previous))
        # Never log subprocess stderr: authentication errors may contain secrets.
        raise RuntimeError("GitHub sync failed; previous board preserved") from None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default=REPOSITORY)
    parser.add_argument("--gh-command-json", default='["gh"]')
    parser.add_argument("--snapshot", type=Path, default=Path(__file__).resolve().parent.parent / "docs/task-board-snapshot.md")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    command = json.loads(args.gh_command_json)
    if not isinstance(command, list) or not command or any(not isinstance(x, str) for x in command):
        parser.error("--gh-command-json must be a nonempty array of strings")
    try:
        sync(command, args.repository, args.snapshot, args.output)
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
