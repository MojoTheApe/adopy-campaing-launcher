"""Agent-facing task operations. GitHub labels remain the source of truth."""
import json
import re
from pathlib import Path
from .model import names, STATUSES, PRIORITIES, TYPES
from .queue import release
from .sync import now


def change(api, config, store, number, *, status=None, module=None, priority=None, kind=None,
           design=None, blocked=None, reason=None, milestone=None, required=None):
    issue = api.issue(number)
    current = names(issue)
    if status in ("in_progress", "merged"):
        raise ValueError("Use start or merged to verify this transition")
    if status and (issue.get("state") == "closed" or "status:merged" in current):
        raise ValueError("Create a follow-up or explicitly reopen the task with its new scope first")
    if status == "review" and not ({"status:in_progress", "status:review"} & current):
        raise ValueError("Start the task before moving it to Review")
    if blocked is True and not (reason and reason.strip()):
        raise ValueError("Blocking requires a reason or explicit needs: dependency")
    if required is True and not (milestone or issue.get("milestone")):
        raise ValueError("Assign a release milestone before marking a task Required")
    if milestone == 0 and "release:required" in current and required is not False:
        raise ValueError("Remove Required when clearing a task's release")
    add, remove = set(), set()
    fields = (("status:", status, STATUSES), ("module:", module, [m["id"] for m in config["modules"]]),
              ("priority:", priority, PRIORITIES), ("type:", kind, TYPES))
    for prefix, value, allowed in fields:
        if value is not None:
            if value not in allowed:
                raise ValueError("Invalid " + prefix + value)
            remove.update(n for n in current if n.startswith(prefix))
            add.add(prefix + value)
    if status in ("backlog", "todo") and priority is None and not any(n.startswith("priority:") for n in current):
        add.add("priority:medium")
    for label, value in (("facet:design", design), ("flag:blocked", blocked), ("release:required", required)):
        if value is True: add.add(label)
        if value is False: remove.add(label)
    if module: remove.add("flag:needs_classification")
    # Record queue entry time before exposing Todo, preserving order across chats.
    if status == "todo" and "status:todo" not in current:
        state, sha = store.read(optional=True)
        state.setdefault("todo_added", {})[str(number)] = now()
        store.write(state, sha)
    patch = {}
    if milestone is not None: patch["milestone"] = milestone or None
    if blocked is True:
        patch["body"] = (issue.get("body") or "") + "\n\nBlocked: " + reason.strip()
    if patch: api.request(api.root + f"/issues/{number}", "PATCH", patch)
    result = api.labels(number, add, remove)
    if blocked is True:
        state, _ = store.read(optional=True)
        if state.get("active") and state["active"]["issue"] == number: release(store, number)
    return result


def create(api, config, args):
    title = args.title.strip()
    if not title: raise ValueError("A title is required")
    issues = api.issues()
    normalized = lambda text: re.sub(r"\s+", " ", text).strip().casefold()
    duplicate = next((i for i in issues if normalized(i["title"]) == normalized(title)), None)
    if duplicate:
        raise ValueError(f"An issue with this title exists: #{duplicate['number']}; inspect it before creating another")
    labels = ["status:" + args.status]
    if args.module: labels.append("module:" + args.module)
    else: labels.append("flag:needs_classification")
    if args.kind: labels.append("type:" + args.kind)
    if args.design: labels.append("facet:design")
    priority = args.priority or ("medium" if args.status != "idea" else None)
    if priority: labels.append("priority:" + priority)
    body = Path(args.body_file).read_text(encoding="utf-8") if args.body_file else ""
    result = api.request(api.root + "/issues", "POST", {"title": title, "body": body, "labels": labels})
    # No automatic POST retry: after a timeout, search the exact title before retrying.
    verified = api.issue(result["number"])
    if not set(labels) <= names(verified): raise ValueError("Created issue labels did not verify")
    return verified


def migration_plan(api, config, mapping):
    issues = api.issues()
    if not issues: raise ValueError("Refusing to plan a migration from an empty response")
    rows = []
    for issue in issues:
        if "tracker:control" in names(issue): continue
        old = names(issue)
        target = set(old)
        statuses = {n for n in old if n.startswith("status:")}
        if len(statuses) != 1: raise ValueError(f"#{issue['number']}: resolve status conflict first")
        status = next(iter(statuses))[7:]
        migrated = {"next":"backlog", "later":"backlog", "blocked":"backlog", "shipped":"merged"}.get(status,status)
        if migrated not in STATUSES: raise ValueError("Unknown status: " + status)
        target -= statuses
        target.add("status:" + migrated)
        if status == "blocked": target.add("flag:blocked")
        alias = re.match(r"^(acl-\d+[a-z]?):", issue["title"], re.I)
        entry = mapping.get(alias[1].lower() if alias else str(issue["number"]))
        if not entry: raise ValueError(f"#{issue['number']}: add an explicit module mapping before migrating")
        if entry["module"] not in [m["id"] for m in config["modules"]]: raise ValueError("Unknown module")
        target = {n for n in target if not n.startswith(("area:", "module:"))}
        target.add("module:" + entry["module"])
        if not any(n.startswith("priority:") for n in target) and migrated != "idea": target.add("priority:medium")
        if "priority:normal" in target: target.remove("priority:normal"); target.add("priority:medium")
        if entry.get("type") and entry["type"] not in TYPES: raise ValueError("Unknown type")
        if entry.get("type") and not any(n.startswith("type:") for n in target): target.add("type:" + entry["type"])
        if entry.get("design"): target.add("facet:design")
        target.discard("flag:needs_classification")
        rows.append({"number":issue["number"],"title":issue["title"],"before":sorted(old),"after":sorted(target),"updated_at":issue.get("updated_at")})
    return {"repository":api.repository,"schema_version":2,"created_at":now(),"issues":rows}


def migrate(api, plan, rollback=False):
    if plan["repository"] != api.repository or plan.get("schema_version") != 2:
        raise ValueError("Migration plan belongs to a different repository or schema")
    source, dest = ("after", "before") if rollback else ("before", "after")
    # Check the whole batch first; partial retries only accept the planned label subsets.
    for row in plan["issues"]:
        current = names(api.issue(row["number"]))
        before, after = set(row[source]), set(row[dest])
        if not before & after <= current or not current <= before | after:
            raise ValueError(f"#{row['number']}: labels changed outside the plan; regenerate or review the plan")
    for row in plan["issues"]:
        before, after = set(row[source]), set(row[dest])
        current = names(api.issue(row["number"]))
        if not before & after <= current or not current <= before | after:
            raise ValueError(f"#{row['number']}: concurrent label edit; stop migration")
        api.labels(row["number"], after - before, before - after)
    return {"migrated":len(plan["issues"]),"rollback":rollback}
