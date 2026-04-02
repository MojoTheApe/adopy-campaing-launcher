---
name: openstation.fail
description: Mark a task as failed with a mandatory reason. $ARGUMENTS = task-name reason... Use when an agent cannot complete a task (max turns exhausted, blocked, missing permissions).
---

# Fail Task

Mark a task as `failed` (attempted but could not complete).

## Input

`$ARGUMENTS` — the task name followed by a failure reason.

The reason is **mandatory** and must include:
- **Failure reason** — concrete, actionable description
- **Work completed so far** — what was accomplished before failure
- **Suggested fix** — what the supervisor should do to retry

Examples:
- `0042-implement-auth "Max turns exhausted debugging flaky test_hooks.py. Completed auth middleware and route guards. Suggested fix: increase turn limit or split into smaller subtasks."`
- `0010 "Missing write permission to /etc/systemd. Completed config generation. Suggested fix: grant root access or use systemd --user."`

## Procedure

1. Parse the task name (first token) and reason (remaining text)
   from `$ARGUMENTS`. If no reason is provided, refuse with an
   error — a reason is always required.

2. Resolve the task file per `docs/task.spec.md` § Task Resolution.

3. Read the task frontmatter. Verify the task is `in-progress`.
   Refuse with an error if the task is in another status.

4. Set the status using the CLI with `--reason`:

   ```bash
   openstation status <task-name> failed --reason "<reason>"
   ```

   This will:
   - Append a `## Progress` entry with the failure reason
   - Transition the status to `failed`

   **Manual fallback** — if the CLI is unavailable:
   a. Append to the task body:

      ```markdown
      ## Progress

      ### YYYY-MM-DD — Failed

      **Reason:** <reason text>
      ```

   b. Edit `status: in-progress` to `status: failed` in frontmatter.

5. Confirm with: task name, failure reason summary, and file path.

## Recovery

After the supervisor fixes the blocking issue, transition the
task back to `ready`:

```bash
openstation status <task-name> ready
```
