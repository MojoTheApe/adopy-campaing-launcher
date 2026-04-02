---
name: openstation.ready
description: Promote a task from backlog to ready. $ARGUMENTS = task-name [agent:<name>]. Use when user says "ready task", "promote task", "make ready", or wants to activate a backlog task.
---

# Promote Task to Ready

Promote a task from `backlog` to `ready`.

## Input

`$ARGUMENTS` — the task name followed by an optional `agent:<name>`.

Examples:
- `0010-refactor-commands-lifecycle`
- `0010-refactor-commands-lifecycle agent:author`
- `refactor-commands-lifecycle agent:author`

## Procedure

1. Parse the task name (first argument) and optional `agent:<name>`
   from `$ARGUMENTS`.
2. Resolve the task file per `docs/task.spec.md` § Task Resolution.
3. Read the task frontmatter. Verify `status: backlog` — refuse
   with an error if the task is not in backlog. Only
   `backlog` → `ready` is a valid transition for this command.
4. Validate that the `## Requirements` section exists and is
   non-empty. Warn (but allow) if requirements look sparse.
5. If `agent:<name>` was provided:
   - Check that `agents/<name>.md` exists. Warn if not found,
     but allow the assignment.
   - Set `assignee: <name>` in frontmatter.
6. If no agent was provided and `agent` field is empty, ask
   the user which agent to assign. List available agents from
   `agents/`.
7. Set the status using the CLI:

   ```bash
   openstation status <task-name> ready
   ```

   **Manual fallback** — if the CLI is unavailable, edit
   `status: backlog` → `status: ready` directly in the task
   frontmatter.

8. Confirm with: task name, assigned agent, and file path.
