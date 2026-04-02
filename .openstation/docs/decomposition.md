---
kind: guide
name: decomposition-guidelines
---

# Decomposition Guidelines

When a task is too large for a single agent session, decompose it
into smaller tasks. These guidelines help you decide when to split,
how to split, and what structure to use.

For task format and field schema see `docs/task.spec.md`. For
sub-task lifecycle rules see `docs/lifecycle.md` § Sub-Tasks.

## Sizing Heuristics

A well-sized task is **one agent, one session, one deliverable**.
Use these concrete thresholds:

| Signal | Right-sized | Too big — split |
|--------|-------------|-----------------|
| Requirements | 1–5 concrete items | 6+ independent items |
| Files touched | 1–3 files created or modified | 4+ files across different concerns |
| Agent roles | Single assignee can do all work | Needs 2+ agent specialties (e.g., research + implementation) |
| Domains | One coherent area | 2+ unrelated domains (e.g., CLI + skills + lifecycle) |
| Verification | Reviewer can check in one pass | Reviewer needs to context-switch between unrelated areas |
| Session time | Completable in a focused session | Agent would need to juggle unrelated subtopics |

A task is **too small** if it has no independent deliverable — it's
just a step inside another task. Example: "Update one frontmatter
field" is a step, not a task. "Add decomposition guidelines to the
task spec" is a task.

## When to Split vs Keep Together

**Keep together** when:
- Same assignee, same domain, same deliverable
- Requirements are sequential steps toward one outcome
- Splitting would create tasks that can't be verified independently

**Split** when:
- Different agent specialties are needed (research vs implementation
  vs documentation)
- Requirements address independent domains that don't share context
- Work can proceed in parallel without coordination
- A single failure shouldn't block unrelated work

**Anti-patterns to avoid:**
- **Splitting by file type** — don't create separate "docs task",
  "code task", "config task" when they're all part of one coherent
  change. A feature that requires a doc update and a config change
  is one task, not three.
- **Splitting by step** — don't create a task for "write the draft"
  and another for "finalize the draft". Sequential steps within one
  deliverable belong in one task.
- **Over-decomposition** — three tasks with one requirement each is
  worse than one task with three requirements. Decomposition has
  overhead (IDs, files, verification cycles). Only split when the
  benefit exceeds the overhead.

## Subtasks vs Independent Tasks

Use **subtasks** (`--parent`) when:
- The parent cannot be reviewed until all children complete
  (the blocking rule applies — see `docs/lifecycle.md` § Sub-Tasks)
- Children share a common goal and the parent aggregates their
  outcomes
- You need status inheritance and auto-promotion behavior

Use **independent tasks** when:
- Work is related but not blocking — either can complete in any
  order
- There is no shared review gate
- The relationship is informational, not structural

Use a **dependency note** (prose in the Requirements or Context
section) when:
- One task should ideally run after another, but the ordering is
  a preference, not a hard constraint
- The dependency is on an external event, not another task

**Decision tree:**

```
Does task B block task A's review?
├── Yes → B is a subtask of A (use --parent)
└── No
    ├── Is the relationship worth tracking? → Add a dependency note
    └── No relationship → Independent tasks
```

## Parent Task Patterns

### Container Parent

A parent with **no own work** — it exists only to coordinate
subtasks. The parent's Requirements section describes the overall
goal; all execution happens in the children.

- **When to use**: Grouping 2–5 related subtasks under a shared
  review gate
- **Assignee**: Usually empty (no agent executes the parent
  directly) or set to the coordinating agent
- **Verification**: Parent verification items confirm that all
  children delivered their parts and the combined result is
  coherent

### Milestone Parent

A parent representing **phased delivery** — subtasks are created
per milestone, and later milestones are created only after earlier
ones land.

- **When to use**: Large efforts where scope for later phases
  depends on early results
- **Structure**: Parent defines all milestones in its body; only
  the current milestone has subtask files. Future milestones are
  described in prose until they're ready to execute.
- **Verification**: Per-milestone verification items in the parent

See the "Milestone-based parent" example in `docs/task.spec.md`
for the full pattern.

### When to Nest vs Stay Flat

Use nesting (parent/child) when:
- There is a real blocking relationship or shared review gate
- The parent adds organizational value (coordinates, aggregates)

Stay flat when:
- Tasks are related by topic but not by dependency
- A parent would be empty overhead with no verification role
- You'd be creating a parent just to "group" — use tags or
  naming conventions instead

**Maximum nesting depth**: 2 levels (parent → child). If you
need deeper nesting, the parent is too broadly scoped — re-scope
it or split into multiple independent parent/child groups.
