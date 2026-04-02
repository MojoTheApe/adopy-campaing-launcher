---
name: openstation.list
description: List all tasks with status, agent, and dates. Supports filters via $ARGUMENTS (e.g., status:ready, agent:researcher). Use when user asks "what tasks exist", "show tasks", "task status", or wants a status overview.
---

# List Tasks

Display tasks from the Open Station vault as a readable table.

## Input

`$ARGUMENTS` — optional space-separated filters in `key:value` format.

Supported filters:
- `status:<value>` — filter by status (backlog, ready, in-progress, review, done, rejected, all)
- `assignee:<value>` — filter by assigned agent

If no arguments provided, show **active** tasks (ready +
in-progress + review). To see backlog tasks, pass
`status:backlog`. To see everything, pass `status:all`.

## Procedure

Run this exact Bash command (translate filters from `$ARGUMENTS`):

```bash
# No filters:
openstation list

# With status filter:
openstation list --status ready

# With assignee filter:
openstation list --assignee researcher

# Combined:
openstation list --status ready --assignee researcher
```

**IMPORTANT: Run the command exactly as shown above. Do not modify
the command in any way. Do not add `2>&1`, `2>/dev/null`,
`|| echo`, or any other shell operators.**

| Filter | CLI flag |
|--------|----------|
| `status:<value>` | `--status <value>` |
| `assignee:<value>` | `--assignee <value>` |
| _(no status filter)_ | _(default: active = ready + in-progress + review)_ |

Display the CLI output directly — it produces an aligned table
sorted by ID.

After the table, show summary counts:
```
Total: N | backlog: N | ready: N | in-progress: N | review: N | done: N | rejected: N
```
Only include statuses that have at least one task.

### Fallback: Manual Scan

Only if `openstation` is not installed:

1. Scan `artifacts/tasks/*.md` for files with `kind: task`
   frontmatter.
2. Parse YAML frontmatter from each file.
3. Apply any filters from `$ARGUMENTS`. By default, show only
   active tasks (`status: ready`, `in-progress`, or `review`)
   unless a `status:` filter is explicitly provided (use
   `status:all` to show everything).
4. Display a markdown table with columns:
   | ID | Task | Status | Assignee | Owner | Created |
   The ID column shows the 4-digit numeric prefix extracted from the
   filename (e.g., `0003`). The Owner column shows the `owner`
   field value (default `manual` if absent).
5. Sort by ID (ascending) as primary sort.
6. Below the table, show summary counts.
