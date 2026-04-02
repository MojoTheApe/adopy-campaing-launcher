---
kind: workflow
name: task-lifecycle
---

# Task Lifecycle

Authoritative reference for how tasks move through the system.
Skills and agents reference this file — it is the single
source of truth for lifecycle rules.

For task format, field schema, and naming conventions see
`docs/task.spec.md`.

## Status Transitions

```
backlog → ready          (use /openstation.ready)
backlog → rejected       (descoped before starting — use /openstation.reject)
ready → in-progress      (assigned agent picks up the task)
ready → backlog          (deprioritize — requirements changed, blocked, etc.)
ready → rejected         (descoped or superseded — use /openstation.reject)
in-progress → review     (agent finishes work)
in-progress → ready      (suspend — /openstation.suspend)
in-progress → backlog    (suspend — /openstation.suspend backlog)
in-progress → failed     (agent cannot complete — use /openstation.fail)
in-progress → rejected   (abandoned mid-effort — use /openstation.reject)
failed → ready           (supervisor fixes issue, retries)
review → in-progress     (verification failed — rework needed)
review → verified        (/openstation.verify — all checks pass)
review → rejected        (owner rejects — use /openstation.reject)
verified → done          (/openstation.done — owner accepts)
```

### Status Descriptions

| Status | Meaning |
|--------|---------|
| `backlog` | Created, not ready for execution |
| `ready` | Requirements defined, agent assigned |
| `in-progress` | Agent is actively working |
| `review` | Work complete, awaiting verification |
| `verified` | All verification items passed, awaiting owner sign-off |
| `done` | Owner accepted — verification passed, task complete |
| `failed` | Agent attempted but could not complete the task |
| `rejected` | Task won't be completed — descoped, superseded, or abandoned |

### Pre-Review Checklist

Before transitioning to `review`, the assignee must ensure:

1. All requirements in the task spec are addressed
2. `## Findings` section summarizes the work produced
3. `## Downstream` section lists follow-up work (if any were
   identified — omit the section if none)
4. `## Progress` entry appended for this session
5. Artifacts are stored in `artifacts/<category>/` and recorded
   in the task's `artifacts` frontmatter list

### Guardrails

- Each user-driven transition has a dedicated command.
  `/openstation.update` does not change status — it only edits
  metadata fields (assignee, owner, parent, etc.).
- `backlog → ready` is only allowed via `/openstation.ready`,
  which validates requirements and updates the status.
- `review → verified` is only allowed via `/openstation.verify`,
  which checks each verification item against the implementation.
  When all items pass, the task transitions to `verified`.
- `verified → done` is only allowed via `/openstation.done`, which
  accepts the task and completes it. Tasks must be in `verified`
  status — `/openstation.done` rejects tasks still in `review`.
- `in-progress → ready/backlog` is only allowed via
  `/openstation.suspend`, which transitions the task and records
  the suspension context (reason, branch) in `## Suspended`.
- `backlog/ready/in-progress/review → rejected` is only allowed
  via `/openstation.reject`, which records the rejection reason
  and marks the task rejected.
- `in-progress → failed` is only allowed via `/openstation.fail`.
  Transitioning to `failed` requires a `## Progress` entry
  explaining the failure. The entry must include: failure reason,
  work completed so far, and suggested fix. Only the assignee
  (executing agent) may transition to `failed`.
- `failed → ready` is used by the supervisor to retry a failed
  task after fixing the underlying issue.
- Agents must NOT self-verify their own work. After completing
  requirements, set `status: review` and stop. Only the
  designated `owner` may transition to `done` or `rejected`.

## Ownership

The `owner` field names who is responsible for verification.

- Value is an agent name or `user` (default).
- Only the designated owner may transition a task from `review` →
  `verified`, `verified` → `done`, or to `rejected`.
- When `owner: user`, a human operator verifies.

## Sub-Tasks

A task may be decomposed into sub-tasks. Sub-tasks are full
tasks with their own canonical file in `artifacts/tasks/`,
linked to their parent via frontmatter wikilinks
(`parent: "[[parent-name]]"` on the child,
`subtasks: - "[[child-name]]"` on the parent). See
`docs/storage-query-layer.md` § 5 for the full sub-task
storage model.

### Blocking Rule

All sub-tasks must reach `done` before the parent can proceed
to `review`.

### Status Inheritance

When a sub-task is created with a parent and no explicit status,
it inherits the parent's status (if `backlog` or `ready`). If
the parent is in a later state (`in-progress`, `review`, `done`),
the sub-task defaults to `backlog`. An explicit `--status` on
create always overrides inheritance.

### Parent Auto-Promotion

When a sub-task transitions to a status that outranks its parent,
the parent is automatically promoted through valid transitions
to match the minimum required level:

| Sub-task reaches              | Parent must be at least |
|-------------------------------|------------------------|
| `ready`                       | `ready`                |
| `in-progress`                 | `in-progress`          |
| `review` / `verified` / `done` | `in-progress`          |

This is enforced by the CLI on both `openstation status` and
`openstation create --parent`.

### Lifecycle

Sub-tasks follow the same status transitions as any other task.
Their status is tracked in their own frontmatter.

## Artifact Storage

Artifacts are outputs produced during task execution. They are
stored permanently in `artifacts/<category>/` and never move.
See `docs/storage-query-layer.md` §§ 1, 3 for the
canonical storage model, category directories, and routing table.

## Artifact Promotion

When a task passes verification, `/openstation.done` sets
`status: done` in frontmatter. Artifacts are already in
`artifacts/` and do not need to be moved.

For agent specs, `/openstation.done` also creates a discovery
entry in `agents/` after verification — never during task
execution. See `docs/storage-query-layer.md` § 2a.
