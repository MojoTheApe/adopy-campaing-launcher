"""One execution slot and explicit Todo batches, using GitHub SHA compare-and-swap."""
from __future__ import annotations
import base64
import json
import uuid
from urllib.parse import quote
from .github import APIError
from .model import names, tasks_from_issues
from .sync import now

BRANCH = "tracker-state"
FILE = ".tracker-state.json"


def empty():
    return {"schema_version": 1, "active": None, "run": None, "todo_order": [], "todo_added": {}}


class StateStore:
    def __init__(self, api, base_branch):
        self.api, self.base = api, base_branch
        self.path = api.root + "/contents/" + FILE

    def read(self, optional=False):
        row = self.api.request(self.path + "?ref=" + BRANCH, optional=optional)
        if row is None:
            return empty(), None
        value = json.loads(base64.b64decode(row["content"]))
        if value.get("schema_version") != 1 or not isinstance(value.get("todo_order"), list):
            raise ValueError("Invalid shared tracker state; inspect it before continuing")
        return value, row["sha"]

    def write(self, state, sha):
        if sha is None:
            ref = self.api.request(self.api.root + "/git/ref/heads/" + BRANCH, optional=True)
            if ref is None:
                base = self.api.request(self.api.root + "/git/ref/heads/" + quote(self.base, safe=""))
                try:
                    self.api.request(self.api.root + "/git/refs", "POST",
                                     {"ref": "refs/heads/" + BRANCH, "sha": base["object"]["sha"]})
                except APIError as error:
                    if error.status != 422:
                        raise
        request = {"message": "Update tracker execution state [skip ci]", "branch": BRANCH,
                   "content": base64.b64encode(json.dumps(state, indent=2).encode()).decode()}
        if sha:
            request["sha"] = sha
        try:
            self.api.request(self.path, "PUT", request)
        except APIError as error:
            if error.status in (409, 422):
                raise ValueError("Tracker state changed in another session; reload before retrying") from None
            # A timeout may have committed. Keep the slot reserved; explicit resume recovers it.
            raise


def public_state(state):
    active = state.get("active")
    return {"active": {k: active[k] for k in ("issue", "started_at")} if active else None,
            "run": state.get("run"), "todo_order": state.get("todo_order", []), "todo_added": state.get("todo_added", {})}


def dependency_problems(task, tasks):
    lookup = {key: t for t in tasks for key in (t["id"].lower(), f"#{t['number']}")}
    return [dep for dep in task["dependencies"] if dep.lower() not in lookup
            or lookup[dep.lower()]["status"] != "merged" or lookup[dep.lower()]["archive"]]


def ordered_todo(tasks, manual=()):
    positions = {number: index for index, number in enumerate(manual)}
    priority = {"high": 0, "medium": 1, "low": 2, "": 1}
    return sorted((t for t in tasks if t["status"] == "todo" and not t["archive"] and not t["initiative"]),
                  key=lambda t: (positions.get(t["number"], len(positions)), priority[t["priority"]],
                                 t.get("todo_at") or t["created_at"], t["number"]))


def begin(store, tasks):
    state, sha = store.read(optional=True)
    if state.get("active") or (state.get("run") and state["run"]["state"] == "running"):
        raise ValueError("An execution or batch already exists; use queue next/resume")
    for task in tasks:
        task["todo_at"] = state.get("todo_added", {}).get(str(task["number"]), "")
    ids = [t["number"] for t in ordered_todo(tasks, state["todo_order"])]
    if not ids:
        raise ValueError("No Todo tasks to snapshot")
    state["run"] = {"id": uuid.uuid4().hex, "state": "running", "issues": ids, "started_at": now()}
    store.write(state, sha)
    return state["run"]


def start(api, config, store, number, resume=False):
    state, sha = store.read(optional=True)
    active = state.get("active")
    if active and (active["issue"] != number or not resume):
        raise ValueError(f"#{active['issue']} owns the execution slot. Resume it explicitly or release it after review.")
    if resume and not active:
        raise ValueError("No execution to resume; start the task normally")
    tasks = tasks_from_issues(api.issues(), config)
    task = next((t for t in tasks if t["number"] == number), None)
    if not task or task["archive"] or task["status"] == "merged" or task["initiative"]:
        raise ValueError("Choose an open, unfinished leaf task")
    if task["blocked"] or dependency_problems(task, tasks):
        raise ValueError("Task is blocked or has unfinished dependencies")
    if task["warnings"]:
        raise ValueError("Resolve task metadata warnings before starting")
    if not active:
        state["active"] = {"issue": number, "started_at": now()}
        store.write(state, sha)
    # Reserve first. On an uncertain API result, retain the reservation for explicit resume.
    issue = api.issue(number)
    latest = tasks_from_issues([issue], config)[0]
    if latest["archive"] or latest["status"] == "merged" or latest["blocked"] or latest["warnings"] or dependency_problems(latest, tasks):
        raise ValueError("Task changed before the status write; inspect it and resume explicitly")
    api.labels(number, {"status:in_progress"}, {n for n in names(issue) if n.startswith("status:")})
    return {"issue": number, "status": "in_progress", "instruction": "Status verified. Implementation may begin."}


def release(store, number):
    state, sha = store.read(optional=True)
    if state.get("active") and state["active"]["issue"] != number:
        raise ValueError("Another task owns the execution slot")
    if state.get("active"):
        state["active"] = None
        store.write(state, sha)


def next_task(api, config, store):
    state, sha = store.read(optional=True)
    if state.get("active"):
        raise ValueError(f"Finish or explicitly resume #{state['active']['issue']} first")
    run = state.get("run")
    if not run or run["state"] != "running":
        raise ValueError("Start an explicit batch with queue begin")
    tasks = tasks_from_issues(api.issues(), config)
    lookup = {t["number"]: t for t in tasks}
    waiting = []
    for number in run["issues"]:
        task = lookup.get(number)
        if task and (task["archive"] or task["status"] == "merged"):
            continue
        if not task or task["status"] != "todo" or task["blocked"] or task["warnings"] or dependency_problems(task, tasks):
            waiting.append(number)
            continue
        return start(api, config, store, number)
    if waiting:
        return {"state": "waiting", "issues": waiting, "instruction": "Blocked, missing or moved tasks require review. No Backlog tasks were added."}
    run["state"] = "complete"
    run["completed_at"] = now()
    store.write(state, sha)
    return run
