---
name: openstation.check
description: Check on a running or completed agent session. $ARGUMENTS = task ID or run ID. Analyzes logs for health, progress, issues, and blockers.
---

# Check Agent Session

Analyze an agent's execution logs and provide actionable insights
about health, progress, issues, and blockers.

## Input

`$ARGUMENTS` — a task ID (e.g., `0253`) or run ID (e.g., `r0056`).

## Constraints

- **Diagnose only, never execute.** This command reads logs and
  reports findings. Do not run tests, make edits, fix code, update
  task status, or perform any agent work.
- **Read-only tools only.** Only use `openstation sessions`,
  `openstation show`, `tail`, and the Read tool.
- **No session data = stop.** If `openstation sessions` returns
  no runs, report "No session data available" and stop. Do not
  attempt to compensate by running the task's work yourself.

## Procedure

### 1. Gather context

Run these commands to collect session and task info:

```bash
openstation sessions <ref>
openstation show <task>
```

If `$ARGUMENTS` is a run ID, extract the task from the sessions
output. If it's a task ID, use it directly.

### 2. Read the log

Read the log file from the sessions output. Use the JSONL path
directly:

```bash
# Read last 200 lines of the raw log
tail -200 <log_path>
```

Parse the stream-json lines. Focus on:
- `type: assistant` — agent messages and tool calls
- `type: user` — tool results (especially errors)
- `type: result` — final outcome
- `type: rate_limit_event` — rate limiting

### 3. Analyze and report

Present a structured diagnosis:

**Status**: running | complete | failed

**Progress**: What has the agent accomplished so far?
List the key actions taken (files read, edits made, tests run).

**Current activity** (if running): What is the agent doing
right now? What was the last tool call?

**Health**:
- Turn count vs limit (e.g., 15/50 — healthy, 48/50 — near limit)
- Cost so far
- Rate limiting events (if any)
- Tool error rate (how many tool calls failed vs succeeded)

**Issues**:
- Tool errors or permission denials
- Repeated failed attempts (agent stuck in a loop)
- Tests failing
- Files not found or wrong paths

**Blockers**:
- Is the agent blocked on a permission prompt?
- Is it waiting for user input (`AskUserQuestion`)?
- Is it stuck retrying the same action?
- Has it hit the turn or budget limit?

**Recommendation**: One of:
- "Healthy — on track to complete"
- "Needs attention — [specific issue]"
- "Stuck — [what to do: kill, re-run with different params, fix X first]"
- "Complete — [success/failure summary]"
