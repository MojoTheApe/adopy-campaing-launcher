---
kind: spec
name: hooks
---

# Hooks

Hooks run shell commands when a task's status changes. Use them
to enforce checks, send notifications, or automate workflows
around status transitions.

Hooks run in one of two **phases**:

- **Pre-transition** (default) — runs after validation but
  before the status is written to disk. If a pre-hook fails,
  the transition aborts and the task file is unchanged.
- **Post-transition** — runs after the status is written and
  parent auto-promotion succeeds. Post-hook failure does **not**
  roll back the transition — it is reported as a warning.

## Configuration

### Settings File

Hooks are defined in the project's settings file:

| Context | Path |
|---------|------|
| Installed project | `.openstation/openstation.yaml` |
| Source repo | `openstation.yaml` (vault root) |

If the file is missing or has no `hooks` key, no hooks fire.

### Schema

```json
{
  "hooks": {
    "StatusTransition": [
      {
        "matcher": "<pattern>",
        "command": "<shell command>",
        "timeout": 30
      }
    ]
  }
}
```

`StatusTransition` is an array. Each entry has:

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `matcher` | string | yes | — | Transition pattern (see Matchers below) |
| `command` | string | yes | — | Shell command to run (`/bin/sh -c`) |
| `timeout` | integer | no | `30` | Max seconds before the hook is killed |
| `phase` | string | no | `"pre"` | When to run: `"pre"` (before write) or `"post"` (after write) |

### Matchers

A matcher has the form `<old-status>→<new-status>`. Each side
is a literal status name or `*` (wildcard). The ASCII `->` is
also accepted.

| Pattern | Matches |
|---------|---------|
| `in-progress→review` | Exactly that transition |
| `*→done` | Any status → done |
| `review→*` | review → done or review → failed |
| `*→*` | Every transition (catch-all) |
| `*->done` | Same as `*→done` (ASCII alias) |

## Environment Variables

Hook commands receive task context via `OS_`-prefixed
environment variables:

| Variable | Description | Example |
|----------|-------------|---------|
| `OS_TASK_NAME` | Task name (no `.md`) | `0042-cli-improvements` |
| `OS_OLD_STATUS` | Status before transition | `in-progress` |
| `OS_NEW_STATUS` | Target status | `review` |
| `OS_TASK_FILE` | Absolute path to task file | `/home/user/project/.openstation/artifacts/tasks/0042-cli-improvements.md` |
| `OS_VAULT_ROOT` | Absolute path to vault root | `/home/user/project` |

All five variables are always set.

## Execution

### Ordering

Hooks fire in **declaration order** (array index in
`StatusTransition`). All matching hooks run unless one fails.

### Timing

```
validate status value
validate transition legality
─── pre-hooks fire here ───       ← failure aborts transition
update_frontmatter()              ← only if all pre-hooks pass
auto_promote_parent()
print confirmation
─── post-hooks fire here ───      ← failure is a warning only
```

### Pre-Hook Failure

If a pre-hook exits non-zero:

1. Remaining pre-hooks are **skipped**
2. The status transition is **aborted** (task file unchanged)
3. An error is printed: `hook failed: <command> (exit <code>)`
4. `openstation status` exits with code 10 (`EXIT_HOOK_FAILED`)
5. Post-hooks do **not** run

### Post-Hook Failure

If a post-hook exits non-zero:

1. Remaining post-hooks still run
2. The transition is **not** rolled back (status already written)
3. A warning is printed: `warning: hook failed: <command> (exit <code>)`
4. `openstation status` exits with code 0 (success)

### Timeout

If a hook exceeds its timeout:

1. `SIGTERM` is sent
2. After 5 seconds, `SIGKILL` is sent if still running
3. Treated as a failure (transition aborted)
4. Error: `hook timed out after <N>s: <command>`

### Output

Hook stdout and stderr are inherited — output appears inline
in the terminal.

## Examples

### Lint before review

```json
{
  "hooks": {
    "StatusTransition": [
      {
        "matcher": "in-progress→review",
        "command": "bin/lint-task $OS_TASK_FILE"
      }
    ]
  }
}
```

### Notify on completion (post-hook)

```json
{
  "hooks": {
    "StatusTransition": [
      {
        "matcher": "*→done",
        "command": "notify-send 'Task $OS_TASK_NAME completed'",
        "phase": "post"
      }
    ]
  }
}
```

Notifications should run **after** the status is written so the
task file reflects the new status. Use `"phase": "post"` — a
failure won't block the transition.

### Log all transitions

```json
{
  "hooks": {
    "StatusTransition": [
      {
        "matcher": "*→*",
        "command": "echo \"$OS_TASK_NAME: $OS_OLD_STATUS → $OS_NEW_STATUS\" >> .openstation/hook.log"
      }
    ]
  }
}
```

### Auto-commit on task completion

Use Claude Code (`claude -p`) as a post-hook to identify
task-related files and create a commit. The agent reads the task
file for context, reviews the git diff, and judges which changes
belong to the task — no brittle file-matching heuristics needed.

```json
{
  "hooks": {
    "StatusTransition": [
      {
        "matcher": "*→done",
        "command": "bin/hooks/auto-commit",
        "phase": "post",
        "timeout": 120
      }
    ]
  }
}
```

The `bin/hooks/auto-commit` script:

1. **Guards** — exits 0 (no-op) if there are no uncommitted
   changes or if `claude` is not on `$PATH`
2. **Invokes** `claude -p` with a prompt that instructs the
   agent to:
   - Read `$OS_TASK_FILE` for task context (title, requirements,
     findings)
   - Run `git status` / `git diff` to find related changes
   - Stage only task-related files (explicit paths, never
     `git add .`)
   - Create a conventional commit:
     `chore(<task-id>): complete <task-name>`
3. **Scopes tools** to the minimum needed: `Bash(git:*)`,
   `Read`, `Glob`, `Grep` — only git commands, no file writes,
   no network access

The timeout is set to 120 seconds to allow the agent time to
read the task file, review diffs, and create the commit.

**Edge cases:**

| Scenario | Behavior |
|----------|----------|
| No uncommitted changes | Script exits 0 before invoking claude |
| `claude` not on PATH | Script exits 0 with a warning |
| No task-related changes in diff | Agent prints a message and exits without committing |
| Large diffs (>500 lines) | Agent uses `git diff --stat` for overview first |
| Worktree | Works normally — `$OS_VAULT_ROOT` resolves to the correct directory |

**Works in both contexts:**

| Context | Settings path | Script path |
|---------|---------------|-------------|
| Source repo | `openstation.yaml` | `bin/hooks/auto-commit` |
| Installed project | `.openstation/openstation.yaml` | `.openstation/bin/hooks/auto-commit` |

### Multiple hooks with phases

```json
{
  "hooks": {
    "StatusTransition": [
      {
        "matcher": "in-progress→review",
        "command": "bin/lint-task $OS_TASK_FILE"
      },
      {
        "matcher": "*→done",
        "command": "bin/archive-task $OS_TASK_NAME",
        "timeout": 60,
        "phase": "post"
      },
      {
        "matcher": "*→*",
        "command": "echo \"$OS_TASK_NAME: $OS_OLD_STATUS → $OS_NEW_STATUS\" >> .openstation/hook.log",
        "phase": "post"
      }
    ]
  }
}
```

The lint hook (pre, default) gates the transition. The archive
and log hooks run after the status is persisted.

## Scope

Hooks fire only on `openstation status` transitions. They do
not fire on task creation, manual file edits, or slash commands
that edit frontmatter directly.

Features deferred for future work: create-time hooks, dry-run
mode, conditional hooks (filtering by task fields), and
structured output capture.

## Architecture

Internal design reference. For the full design spec see
`artifacts/specs/task-lifecycle-hooks.md`.

### Module Layout

All hook logic lives in `src/openstation/hooks.py`. Three
public functions form the API:

| Function | Purpose |
|----------|---------|
| `load_hooks(root)` | Read `StatusTransition` entries from `openstation.yaml` |
| `match_hooks(hooks, old, new)` | Filter entries whose matcher matches the transition |
| `run_matched(root, prefix, task_name, old, new, task_file, *, phase)` | Orchestrate: load → match → execute for the given phase. Returns `None` on success (or always for post-hooks), `EXIT_HOOK_FAILED` on pre-hook failure. |

Private helpers: `_settings_path` resolves the settings file,
`_normalize_matcher` converts ASCII `->` to `→`,
`_build_hook_env` constructs the `OS_` environment dict,
`_run_hook` executes a single command via `subprocess.Popen`.

### Integration Point

Hooks are invoked from two call sites in `cmd_status()`
(`src/openstation/tasks.py`):

```
validate status value
validate transition legality
─── hooks.run_matched(phase="pre") ───    ← aborts on failure
update_frontmatter()
auto_promote_parent()
print confirmation
─── hooks.run_matched(phase="post") ───   ← warnings only
```

Pre-hooks run after validation but before the task file changes.
Post-hooks run after the status is persisted and parent
auto-promotion completes.

### Data Flow

```
openstation.yaml
  │
  ▼
load_hooks()        ← parse YAML, extract StatusTransition array
  │
  ▼
match_hooks(phase)  ← filter by old→new pattern + phase ("pre"/"post")
  │
  ▼
run_matched()       ← build OS_ env vars, run each hook via subprocess.Popen
  │                    pre: stop on first failure
  │                    post: warn and continue
  ▼
pre:  None | EXIT_HOOK_FAILED (10)
post: always None
```

### Exit Code

`EXIT_HOOK_FAILED = 10` is defined in `src/openstation/core.py`.
When `run_matched` returns this code, `cmd_status()` returns it
directly — the transition is aborted and the task file is unchanged.
