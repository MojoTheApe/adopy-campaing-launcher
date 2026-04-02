---
name: openstation.list.backlog
description: List backlog tasks. Shortcut for /openstation.list status:backlog. Use when user asks "what's in the backlog", "pending tasks", or "untriaged tasks".
---

# List Backlog Tasks

Display only `backlog` tasks from the Open Station vault.

This is a convenience shortcut — equivalent to
`/openstation.list status:backlog`.

## Procedure

Run this exact Bash command:

```bash
openstation list --status backlog
```

**IMPORTANT: Run the command exactly as shown above. Do not modify
the command in any way. Do not add `2>&1`, `2>/dev/null`,
`|| echo`, or any other shell operators.**

Display the CLI output directly — it produces an aligned table
sorted by ID.

After the table, show summary counts (backlog only):
```
Backlog: N tasks
```
