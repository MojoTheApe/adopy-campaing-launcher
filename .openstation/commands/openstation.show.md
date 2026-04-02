---
name: openstation.show
description: Show full details of a single task. $ARGUMENTS = task name. Use when user says "show task", "view task", "task details", or wants to inspect a specific task.
---

# Show Task

Display the full spec of a single task.

## Input

`$ARGUMENTS` — the task name (ID-prefixed or slug).

Examples:
- `0010-refactor-commands-lifecycle`
- `refactor-commands-lifecycle`
- `0010`

## Procedure

Run this exact Bash command:

```bash
openstation show <task>
```

Replace `<task>` with `$ARGUMENTS`. The CLI supports exact match
and prefix match (e.g., `0010` resolves to
`0010-refactor-commands-lifecycle`).

**IMPORTANT: Run the command exactly as shown above. Do not modify
the command in any way. Do not add `2>&1`, `2>/dev/null`,
`|| echo`, or any other shell operators.**

The CLI prints the full task content (frontmatter + body).
After displaying it, also report:
- The canonical location (e.g., `artifacts/tasks/0010-refactor-commands-lifecycle.md`)

### Fallback: Manual File Reads

Only if `openstation` is not installed:

1. Parse the task name from `$ARGUMENTS`.
2. Resolve the task file per `docs/task.spec.md` § Task Resolution.
3. Read the full `.md` file.
4. Display:
   - The frontmatter fields in a readable format
   - The full markdown body
   - The canonical location (e.g., `artifacts/tasks/0010-refactor-commands-lifecycle.md`)
