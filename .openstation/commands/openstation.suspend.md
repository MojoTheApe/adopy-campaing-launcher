---
name: openstation.suspend
description: Suspend an in-progress task back to ready or backlog. $ARGUMENTS = <task> [ready|backlog] ["reason"]. Use when user says "suspend task", "pause task", "park task", or wants to move an in-progress task backward.
---

# Suspend Task

Suspend an `in-progress` task back to `ready` (default) or
`backlog`. The git workflow (branch creation, auto-commit) is
handled by lifecycle hooks — this command is a thin wrapper
around `openstation status`.

## Input

`$ARGUMENTS` — task name, optional target status, optional reason.

```
<task-name> [ready|backlog] ["reason text"]
```

- **task-name** — required. Resolved per `docs/task.spec.md`
  § Task Resolution.
- **target status** — optional. `ready` (default) or `backlog`.
  Any other value is an error.
- **reason** — optional. Free text after the target status. If
  the second argument is neither `ready` nor `backlog`, treat
  the entire remainder as the reason (target defaults to `ready`).

Examples:
- `0042` — suspend to ready, no reason
- `0042 backlog` — suspend to backlog, no reason
- `0042 ready "blocked on API"` — suspend to ready, with reason
- `0042 backlog need more research` — suspend to backlog, with reason
- `0042 blocked on API` — suspend to ready (default), reason = "blocked on API"

## Procedure

1. **Parse arguments** — extract task name, target status
   (default: `ready`), and optional reason from `$ARGUMENTS`.
   If no arguments, print usage and stop:
   ```
   usage: /openstation.suspend <task> [ready|backlog] [reason]
   ```

2. **Resolve and read task** — use `openstation show <task>` to
   resolve per task resolution rules. Verify `status: in-progress`.
   If the task is in any other status, print error and stop:
   ```
   error: task <name> is '<status>', can only suspend from in-progress
   ```

3. **Validate target** — if a target was provided and is not
   `ready` or `backlog`, print error and stop:
   ```
   error: target must be 'ready' or 'backlog', got '<value>'
   ```

4. **Transition status** — call the CLI:
   ```bash
   openstation status <task-name> <target-status>
   ```
   **Manual fallback** — if the CLI is unavailable, edit
   `status: in-progress` → `status: <target>` directly in the
   task frontmatter.

5. **Confirm** — print: task name, target status, and reason
   (if provided).
