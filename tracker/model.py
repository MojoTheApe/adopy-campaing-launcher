"""Normalize GitHub data without inferring task completion from checkboxes."""
from __future__ import annotations

import json
import re
from pathlib import Path

STATUSES = ("idea", "backlog", "todo", "in_progress", "review", "merged")
PRIORITIES = ("high", "medium", "low")
TYPES = ("bug", "feature", "improvement", "research", "operations")
LABELS = {"idea": "Idea", "backlog": "Backlog", "todo": "Todo", "in_progress": "In progress", "review": "Review", "merged": "In master"}
DEFAULT_CONFIG = Path(__file__).with_name("config.json")
# Retained historical implementation: its six-stage model must never consume the
# new live delivery policy. New operations use scripts/task_tracker.py instead.
_LEGACY_CONFIG = Path(__file__).resolve().parents[1] / '.workflow/legacy/tracker-config.json'
if _LEGACY_CONFIG.is_file():
    DEFAULT_CONFIG = _LEGACY_CONFIG


def load_config(path=DEFAULT_CONFIG):
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    if config.get("schema_version") != 2 or tuple(config.get("workflow", [])) != STATUSES:
        raise ValueError("Expected tracker schema 2 and the six supported workflow stages")
    project = config["project"]
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", project["repository"]):
        raise ValueError("Invalid repository")
    if not project.get("base_branch") or not project.get("name"):
        raise ValueError("Project name and base branch are required")
    ids = [m["id"] for m in config["modules"]]
    if len(ids) != len(set(ids)) or any(not re.fullmatch(r"[a-z][a-z0-9_-]*", x) for x in ids):
        raise ValueError("Module IDs must be unique stable names")
    if tuple(config.get("priorities", [])) != PRIORITIES:
        raise ValueError("Priorities must be high, medium, low")
    if not 15 <= config.get("refresh_seconds", 60) <= 3600:
        raise ValueError("Refresh interval must be between 15 and 3600 seconds")
    return config


def names(issue):
    return {x["name"] if isinstance(x, dict) else x for x in issue.get("labels", [])}


def one(labels, prefix, allowed, warnings):
    found = sorted(x[len(prefix):] for x in labels if x.startswith(prefix))
    if len(found) == 1 and found[0] in allowed:
        return found[0]
    if found:
        warnings.append("Check " + prefix.rstrip(":") + " labels")
    return ""


def dependencies(body, repo):
    # Explicit needs: links only; a casual issue mention is not a dependency.
    pattern = r"\bneeds:\s*(?:#(\d+)|([a-z][a-z0-9]*-\d+[a-z]?)|https://github\.com/" + re.escape(repo) + r"/issues/(\d+))"
    return list(dict.fromkeys("#" + (a or c) if (a or c) else b.lower()
                              for a, b, c in re.findall(pattern, body, re.I)))


def tasks_from_issues(issues, config):
    modules = {m["id"] for m in config["modules"]}
    repo = config["project"]["repository"]
    tasks = []
    for issue in sorted(issues, key=lambda x: x["number"]):
        if "pull_request" in issue or "tracker:control" in names(issue):
            continue
        labels, warnings = names(issue), []
        status = one(labels, "status:", STATUSES, warnings)
        # Transitional read compatibility never hides an unmigrated task.
        if not status:
            old = {x for x in labels if x.startswith("status:")}
            if old in ({"status:next"}, {"status:later"}, {"status:blocked"}):
                status = "backlog"
            elif old == {"status:shipped"}:
                status = "merged"
            else:
                status = "idea"
                if not old:
                    warnings.append("Needs a status")
        module = one(labels, "module:", modules, warnings)
        if not module:
            warnings.append("Needs a module")
        priority = one(labels, "priority:", PRIORITIES, warnings)
        kind = one(labels, "type:", TYPES, warnings)
        body = issue.get("body") or ""
        archive = issue.get("state") == "closed" and status != "merged"
        if issue.get("state_reason") == "not_planned":
            archive = True
        alias = re.match(r"^([a-z][a-z0-9]*-\d+[a-z]?)\s*:\s*", issue["title"], re.I)
        checks = []
        fence = None
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith(("```", "~~~")):
                marker = stripped[:3]
                fence = None if fence == marker else marker if fence is None else fence
            match = re.match(r"^\s*[-*+]\s+\[([ xX])\]\s+(.+)", line)
            if match and not fence:
                checks.append({"done": match[1].lower() == "x", "title": match[2]})
        milestone = issue.get("milestone")
        if "release:required" in labels and not milestone:
            warnings.append("Required task needs a release")
        if status == "merged" and any(not c["done"] for c in checks):
            warnings.append("Completed task has unfinished checklist items")
        tasks.append({
            "number": issue["number"], "id": alias[1].lower() if alias else f"#{issue['number']}",
            "title": issue["title"][alias.end():] if alias else issue["title"],
            "body": re.sub(r"(?m)^_Board id:.*$", "", body).strip(),
            "url": f"https://github.com/{repo}/issues/{issue['number']}",
            "status": status, "module": module, "priority": priority, "type": kind,
            "design": "facet:design" in labels, "non_code": "resolution:non_code" in labels,
            "blocked": bool({"flag:blocked", "status:blocked"} & labels),
            "required": "release:required" in labels, "initiative": "kind:initiative" in labels,
            "archive": archive, "warnings": warnings, "checks": checks,
            "dependencies": dependencies(body, repo),
            "milestone": milestone["number"] if milestone else None,
            "milestone_title": milestone["title"] if milestone else "",
            "created_at": issue.get("created_at", ""), "updated_at": issue.get("updated_at", ""),
        })
    return tasks


def release_summary(tasks, milestone):
    required = [t for t in tasks if t["milestone"] == milestone and t["required"] and not t["initiative"]]
    lookup = {key: t for t in tasks for key in (t["id"].lower(), f"#{t['number']}")}
    unresolved = []
    for task in required:
        for dep in task["dependencies"]:
            item = lookup.get(dep.lower())
            if not item or item["archive"] or item["status"] != "merged":
                unresolved.append({"task": task["number"], "dependency": dep})
    complete = sum(t["status"] == "merged" and not t["archive"] for t in required)
    blocked = sum(t["blocked"] for t in required)
    return {"total": len(required), "complete": complete, "remaining": len(required) - complete,
            "blocked": blocked, "unresolved": unresolved,
            "ready": bool(required) and complete == len(required) and not blocked and not unresolved
                     and not any(t["warnings"] for t in required)}


def payload(issues, config, milestones=(), queue=None):
    tasks = tasks_from_issues(issues, config)
    for task in tasks:
        task["todo_at"] = (queue or {}).get("todo_added", {}).get(str(task["number"]), "")
    releases = {m["number"]: {"number": m["number"], "title": m["title"]} for m in milestones}
    releases.update({t["milestone"]: {"number": t["milestone"], "title": t["milestone_title"]}
                     for t in tasks if t["milestone"] is not None})
    return {"schema_version": 2, "project": config["project"], "modules": config["modules"],
            "completion_label": config.get("completion_label", "Merged"),
            "refresh_seconds": config.get("refresh_seconds", 60),
            "release_view_label": config.get("release_view_label", "Releases"),
            "default_release": config.get("default_release", ""), "tasks": tasks,
            "milestones": [dict(m, summary=release_summary(tasks, m["number"])) for m in releases.values()],
            "queue": queue or {}}
