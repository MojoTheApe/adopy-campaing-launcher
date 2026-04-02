---
name: openstation.done
description: Mark a task as done. $ARGUMENTS = task name. Use when user says "done task", "complete task", "mark done", or wants to finish a task.
---

# Done Task

Mark a task as done.

## Input

`$ARGUMENTS` — the task name (ID-prefixed or slug).

Example: `0003-research-obsidian-plugin-api` or
`research-obsidian-plugin-api`

## Procedure

0. Read `docs/lifecycle.md` § "Artifact Promotion" for the
   canonical routing table.

1. Parse the task name from `$ARGUMENTS`.
2. Resolve the task file per `docs/task.spec.md` § Task Resolution.
3. Read the task frontmatter. Verify `status: verified`
   — only `verified` → `done` is a valid transition.
   - If `status: review`, refuse with an error:
     "Task is in `review` — run `/openstation.verify <task-name>`
     first to verify all checklist items before completing."
   - If any other status, refuse with an error:
     "Task must be in `verified` status to complete."
4. Set the status using the CLI:

   ```bash
   openstation status <task-name> done
   ```

   **Manual fallback** — if the CLI is unavailable, edit
   `status: verified` → `status: done` directly in the task
   frontmatter.

5. Artifacts are already in `artifacts/` — they do not need to
   be moved.
6. Check the task's `artifacts` frontmatter field for paths
   matching `artifacts/agents/*.md`. For each, create a discovery
   symlink: `agents/<name>.md → ../artifacts/agents/<name>.md`
   (skip if already exists).
7. Confirm the task was completed and show the file path.
