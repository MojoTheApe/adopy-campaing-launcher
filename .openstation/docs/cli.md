---
kind: spec
name: cli-reference
---

# CLI Reference

## Quick Reference

| Command   | Description                                  |
|-----------|----------------------------------------------|
| `list`    | List tasks (default: active tasks)           |
| `show`    | Display a single task spec                   |
| `create`  | Create a new task                            |
| `status`  | Change a task's lifecycle status             |
| `run`     | Launch an agent on tasks                     |
| `artifacts` | Browse non-task artifacts (research, specs, agents) |
| `agents`  | Manage and inspect agent specs               |
| `hooks`   | Inspect and trigger lifecycle hooks           |
| `init`    | Initialize Open Station in current directory |
| `self-update` | Update Open Station to latest version    |

## Global Flags

| Flag        | Description              |
|-------------|--------------------------|
| `--version` | Print version and exit   |
| `--help`    | Show help for any command |

---

## `list`

List tasks from the vault.

### Synopsis

```
openstation list [FILTER] [--status STATUS] [--assignee NAME]
                 [--type TYPE] [-q | -j | -e]
```

### Arguments

| Argument | Description |
|----------|-------------|
| `FILTER` | Optional. Task ID/slug or assignee name (auto-detected). Numeric values resolve as task IDs and show the subtask tree; non-numeric values filter by assignee. |

### Flags

| Flag               | Default  | Description |
|--------------------|----------|-------------|
| `--status STATUS`  | `active` | Filter by status: `backlog`, `ready`, `in-progress`, `review`, `done`, `failed`, `active`, or `all`. `active` = ready + in-progress + review. When a task ID is given as FILTER, defaults to `all`. |
| `--assignee NAME`  | —        | Filter by assignee (exact match) |
| `--type TYPE`      | —        | Filter by type: `feature`, `research`, `spec`, `implementation`, `documentation` |
| `-q`, `--quiet`    | —        | One task name per line, no header (pipe-friendly) |
| `-j`, `--json`     | —        | JSON array of task objects |
| `-e`, `--editor`      | —        | Open matching task files in `$EDITOR` (default: vim) |

Output flags (`-q`, `-j`, `-e`) are mutually exclusive.

### Examples

```bash
openstation list                          # active tasks (ready + in-progress + review)
openstation list --status all             # all tasks regardless of status
openstation list --status ready --assignee researcher
openstation list -q --status ready        # one task name per line
openstation list --json                   # JSON array of task objects
openstation list --editor                    # open active tasks in editor
openstation list 0042                     # show task 0042 and its subtask tree
```

---

## `show`

Display a single task spec.

### Synopsis

```
openstation show TASK [-j | -e]
```

### Arguments

| Argument | Description |
|----------|-------------|
| `TASK`   | Required. Task ID, slug, or full name. Short numeric IDs are zero-padded automatically (e.g. `42` → `0042`). |

### Flags

| Flag            | Description |
|-----------------|-------------|
| `-j`, `--json`  | Emit parsed frontmatter and body as a JSON object |
| `-e`, `--editor`   | Open the task file in `$EDITOR` (default: vim) |

### Task Resolution

Resolution tries these strategies in order (first match wins):

1. **Exact match** — full task name (e.g. `0042-cli-improvements`)
2. **ID prefix** — zero-padded numeric prefix (e.g. `0042` or `42`)
3. **Slug match** — slug portion after the ID (e.g. `cli-improvements`)

Ambiguous matches produce an error listing all candidates.

### Examples

```bash
openstation show 0042                     # show by numeric ID
openstation show 42                       # short ID (auto-padded)
openstation show 0042-cli-improvements    # show by full name
openstation show cli-improvements         # show by slug
openstation show 0042 --json              # frontmatter + body as JSON
openstation show 0042 --editor               # open in editor
```

---

## `create`

Create a new task file in `artifacts/tasks/`.

### Synopsis

```
openstation create DESCRIPTION [--assignee NAME] [--owner NAME]
                   [--status STATUS] [--type TYPE] [--parent TASK]
                   [--body BODY | --body-file PATH]
```

### Arguments

| Argument      | Description |
|---------------|-------------|
| `DESCRIPTION` | Required. Free-text task description. Used to generate the slug (kebab-case, max 5 words) and H1 title. |

### Flags

| Flag              | Default   | Description |
|-------------------|-----------|-------------|
| `--assignee NAME` | —         | Agent name to assign |
| `--owner NAME`    | `user`    | Who verifies: agent name or `user` |
| `--status STATUS` | see below | Initial status: `backlog` or `ready` only |
| `--type TYPE`     | `feature` | Task type: `feature`, `research`, `spec`, `implementation`, `documentation` |
| `--parent TASK`   | —         | Parent task ID/slug. Wikilink added to child's `parent` field and parent's `subtasks` list automatically. |
| `--body BODY`     | —         | Markdown body content (replaces skeleton). Use `--body -` to read from stdin. |
| `--body-file PATH`| —         | Read markdown body from a file. Mutually exclusive with `--body`. |

**Status default logic:** If `--parent` is set and no `--status` given, inherits the parent's status when the parent is `backlog` or `ready`; otherwise defaults to `backlog`. Without `--parent`, defaults to `backlog`.

**Parent auto-promotion:** When creating a sub-task, the parent is auto-promoted through valid transitions if the child's status outranks it.

### Examples

```bash
openstation create "add login page"
openstation create "fix auth bug" --assignee developer --status ready
openstation create "child task" --parent 0042
openstation create "desc" --body "## Requirements\n\nDo X.\n\n## Verification\n\n- [ ] X done"
openstation create "desc" --body-file spec-body.md
echo "## Requirements..." | openstation create "desc" --body -
```

Prints the created task name (e.g. `0113-add-login-page`) to stdout.

---

## `status`

Change a task's lifecycle status.

### Synopsis

```
openstation status TASK [NEW_STATUS]
```

### Arguments

| Argument     | Description |
|--------------|-------------|
| `TASK`       | Required. Task ID, slug, or full name. |
| `NEW_STATUS` | Optional. Target status: `backlog`, `ready`, `in-progress`, `review`, `done`, `failed`. When omitted, shows an interactive picker of valid transitions. |

### Flags

| Flag | Description |
|------|-------------|
| `-f`, `--force` | Bypass transition validation, allowing any status → any status. Prints a warning for invalid transitions. Hooks and parent auto-promotion still run. When combined with the interactive picker (no `NEW_STATUS`), shows all statuses. |

### Valid Transitions

```
backlog → ready → in-progress → review → done
                   ready → backlog
                   in-progress → ready      (suspend)
                   in-progress → backlog    (suspend)
                                  review → in-progress (rework)
                                  review → failed → in-progress
```

Invalid transitions produce an error showing allowed targets from the current status.

If the task has a parent, auto-promotion is applied after a successful transition.

### Hooks

If lifecycle hooks are configured in `openstation.yaml`, matching
hooks run before the status is written. A failed hook aborts the
transition (exit code 10). See `docs/hooks.md` for configuration.

### Examples

```bash
openstation status 0042                   # interactive picker
openstation status 0042 ready             # backlog → ready
openstation status 42 in-progress         # short ID works
openstation status cli-improvements review
openstation status 0042 backlog --force   # bypass validation
openstation status 0042 --force           # picker shows all statuses
```

---

## `run`

Launch an agent to execute tasks.

### Synopsis

```
openstation run AGENT [flags]             # by-agent mode
openstation run --task TASK [flags]       # by-task mode
openstation run --task TASK --verify [flags]  # verify mode
```

A positional argument starting with a digit is treated as a task ID (equivalent to `--task`).

### Modes

**By-agent (always interactive):** Launches the named agent in an interactive Claude session. Uses `os.execvp` to replace the process. The `-i` flag is accepted but redundant. Detached-only flags (`--budget`, `--turns`, `--max-tasks`, `--quiet`, `--json`) are rejected or warned.

**By-task:** Resolves the task, finds ready subtasks (if any), and executes them sequentially with `stream-json` output. If no subtasks exist, executes the task directly. The agent is read from the task's `assignee` field. Supports both detached (default) and interactive (`-i`) modes.

**Interactive (`-i` / `--interactive`):** Launches an interactive Claude session with task context pre-loaded. Uses `os.execvp` to replace the process. No log capture — Claude's built-in session persistence (`--resume`) provides replay. In by-task mode, this must be explicitly requested. In by-agent mode, this is always the behavior.

**Detached (`-d` / `--detached`, by-task only):** The agent runs in the background via the configured detached backend (default: tmux). Supports budget, turns, and log capture. Only available in by-task mode. An optional session name can be passed: `-d mysession`.

**Verify (`--verify`):** Launches task verification. Requires `--task` and task must be in `review` status. Pre-loads `/openstation.verify <task-id>` as the prompt. Works with `-i` and `--worktree`.

Agent resolution order (highest to lowest priority):

1. `--agent` CLI argument
2. Task `owner` field (skipped if `user` or empty)
3. `settings.verify.agent` project-level default (see `docs/settings.md`)
4. Hardcoded fallback: `project-manager`

### Flags

| Flag                | Default | Description |
|---------------------|---------|-------------|
| `--task TASK`       | —       | Task ID or slug (explicit by-task mode) |
| `-i`, `--interactive` | —     | Interactive mode (replace process, no log capture) |
| `-d`, `--detached [SESSION]` | — | Detached background execution via configured backend (optional session name) |
| `--tmux-mode MODE`  | —       | Override tmux mode for this run: `session`, `window`, or `pane` |
| `--budget USD`      | `5`     | Max USD per invocation (detached only) |
| `--turns N`         | `50`    | Max turns per invocation (detached only) |
| `--max-tasks N`     | `1`     | Max subtasks to execute (detached only) |
| `-w`, `--worktree [NAME]` | — | Run in a Claude worktree (optional name, default: auto-derived from task or agent) |
| `--force`           | —       | Skip task status checks (allow non-ready tasks) |
| `--dry-run`         | —       | Print the command without executing |
| `-q`, `--quiet`     | —       | Suppress progress output (detached only) |
| `-j`, `--json`      | —       | Structured JSON dry-run output (detached only) |
| `--context-only`    | —       | Load task context without injecting "Execute task" prompt (implies `-i`) |
| `--verify`          | —       | Launch verification (agent from task `owner`, requires `--task` in `review`) |

### Incompatibilities

| Combination | Behavior |
|-------------|----------|
| by-agent + `--json` | Error: "JSON output not supported in interactive mode" |
| by-agent + `--quiet` | Error: "Quiet mode not supported in interactive mode" |
| by-agent + `--budget` | Warning to stderr, flag ignored |
| by-agent + `--turns` | Warning to stderr, flag ignored |
| by-agent + `--max-tasks` | Warning to stderr, flag ignored |
| `-i` + `-d` | Error: "cannot combine --interactive and --detached" |
| `-i` + `--json` | Error: "JSON output not supported in interactive mode" |
| `-i` + `--quiet` | Error: "Quiet mode not supported in interactive mode" |
| `-i` + `--budget` | Warning to stderr, flag ignored |
| `-i` + `--turns` | Warning to stderr, flag ignored |
| `-i` + `--max-tasks` | Warning to stderr, flag ignored |
| `-i` + `--dry-run` | Allowed — prints the command that would be `execvp`'d |
| `-i` + task with subtasks | Error with hint to target a specific subtask |
| `-d` + `--json` | Error (unless `--dry-run`) |
| `-d` + by-agent mode | Error: "detached requires --task" |
| `--tmux-mode` + non-tmux backend | Warning to stderr, flag ignored |
| `--context-only` without `--task` | Error: "--context-only requires --task" |
| `--context-only` + `-d` | Error: "--context-only is incompatible with --detached" |
| `--verify` without `--task` | Error: "--verify requires --task" |
| `--verify` + task not in `review` | Error with current status |

### Deprecated Flags

The following flags are deprecated and will be removed in a future
release. They still work but emit a deprecation warning to stderr.

| Deprecated | Replacement | Migration |
|------------|-------------|-----------|
| `-a`, `--attached` | `-i`, `--interactive` | `openstation run agent -a` → `openstation run agent -i` |
| `-t`, `--tmux [SESSION]` | `-d`, `--detached [SESSION]` | `openstation run --task 42 -t` → `openstation run --task 42 -d` |

Migration examples:

```bash
# Old                                     → New
openstation run agent -a                   → openstation run agent -i
openstation run --task 42 -a               → openstation run --task 42 -i
openstation run --task 42 --tmux           → openstation run --task 42 -d
openstation run --task 42 -t myname        → openstation run --task 42 -d myname
```

### Examples

```bash
openstation run researcher                  # interactive agent session (always interactive)
openstation run researcher -i               # same (-i is redundant for by-agent)
openstation run --task 0042 -i              # interactive task session
openstation run --task 0042                 # autonomous (foreground)
openstation run --task 0042 -d              # detached (background)
openstation run --task 0042 -d mysession    # detached with custom session name
openstation run --task 0042 --worktree -i   # in a worktree (auto-named)
openstation run --task 0042 --worktree my-feature -i  # explicit worktree name
openstation run --task 0042 -i --dry-run    # preview interactive command
openstation run researcher --dry-run        # show command without executing
openstation run --task 42 --dry-run --json  # structured JSON dry-run output
openstation run --task 42 --verify -i       # interactive verification
openstation run --task 42 --verify          # autonomous verification
openstation run --task 0042 -d --tmux-mode window  # detached in tmux window mode
```

### Logs

By-task detached execution writes stream-json output to `artifacts/logs/<task-name>.jsonl`. Session IDs are extracted and displayed for resumption via `claude --resume <session-id>`. Interactive mode does not capture logs.

---

## `artifacts`

Browse non-task artifacts from `artifacts/` subdirectories.

### Synopsis

```
openstation artifacts [list] [--kind KIND] [-q | -j]
openstation artifacts show <name> [-j | -e]
```

Bare `openstation artifacts` (no sub-action) defaults to `list`.

Alias: `art` (e.g. `openstation art list`).

### Sub-Actions

#### `artifacts list` (default)

List artifacts with name, kind, and one-line summary. Without `--kind`, lists all non-task artifacts (agents, research, specs).

| Flag | Short | Description |
|------|-------|-------------|
| `--kind KIND` | — | Filter by subdirectory: `agents`, `research`, `specs` |
| `--json` | `-j` | JSON array of artifact objects |
| `--quiet` | `-q` | One artifact name per line (pipe-friendly) |

`--json` and `--quiet` are mutually exclusive. Using `--kind tasks` is rejected with a hint to use `openstation list`.

#### `artifacts show <name>`

Display a single artifact by name, resolved across `artifacts/research/`, `artifacts/specs/`, and `artifacts/agents/`. Resolution matches filename stems. Ambiguous matches produce an error listing candidates.

| Flag | Description |
|------|-------------|
| `--json` | Frontmatter fields + `body` key as JSON object |
| `--editor` | Open artifact file in `$EDITOR` (default: vim) |

Exit code 3 if artifact not found. Exit code 4 if ambiguous.

### Examples

```bash
openstation artifacts                         # list all non-task artifacts
openstation artifacts list                    # same as bare 'artifacts'
openstation artifacts list --kind research    # only research artifacts
openstation artifacts list --json             # JSON array
openstation artifacts list -q                 # one name per line
openstation artifacts show cli-feature-spec   # print full artifact
openstation artifacts show cli-feature-spec --json  # as JSON
openstation artifacts show cli-feature-spec --editor   # open in editor
openstation art list -q                       # alias works too
```

---

## `agents`

Manage and inspect agent specs from `artifacts/agents/`.

### Synopsis

```
openstation agents [list] [--json | --quiet]
openstation agents show <name> [--json | --editor]
```

Bare `openstation agents` (no sub-action) defaults to `list`.

### Sub-Actions

#### `agents list` (default)

List all agents with name and description.

| Flag | Short | Description |
|------|-------|-------------|
| `--json` | `-j` | JSON array of agent objects |
| `--quiet` | `-q` | One agent name per line (pipe-friendly) |

`--json` and `--quiet` are mutually exclusive.

#### `agents show <name>`

Display the full agent spec (frontmatter + body).

| Flag | Description |
|------|-------------|
| `--json` | Frontmatter fields + `body` key as JSON object |
| `--editor` | Open spec file in `$EDITOR` (default: vim) |

Exit code 3 if agent not found (hints available agents).

### Examples

```bash
openstation agents                          # list all agents (default)
openstation agents list                     # same as bare 'agents'
openstation agents list --json              # JSON array of agent objects
openstation agents list --quiet             # one name per line
openstation agents show researcher          # print full agent spec
openstation agents show researcher --json   # frontmatter + body as JSON
openstation agents show researcher --editor    # open in editor
```

---

## `hooks`

Inspect and trigger lifecycle hooks configured in `openstation.yaml`.

### Synopsis

```
openstation hooks [list]
openstation hooks show <index|matcher>
openstation hooks run <task> <old-status> <new-status> [--phase PHASE] [--dry-run]
```

Bare `openstation hooks` (no sub-action) defaults to `list`.

### Sub-Actions

#### `hooks list` (default)

Display all configured `StatusTransition` hooks in a table showing
index, matcher, phase, timeout, and command.

If no hooks are configured, prints an informational message.

#### `hooks show <index|matcher>`

Display a single hook entry with full details (index, matcher,
command, phase, timeout).

The query can be a 0-based numeric index or a matcher pattern
(e.g. `*→done` or `*->done`). If the matcher matches multiple
entries, an ambiguity error is reported.

Exit code 3 if hook not found. Exit code 4 if ambiguous.

#### `hooks run <task> <old-status> <new-status>`

Manually trigger matching hooks for a simulated transition against
a real task. Sets `OS_*` environment variables as documented in
`docs/hooks.md`.

| Flag | Default | Description |
|------|---------|-------------|
| `--phase PHASE` | `all` | Which phase hooks to fire: `pre`, `post`, or `all` |
| `--dry-run` | — | Show matched hooks without executing them |

Pre-hook failures return exit code 10 (`EXIT_HOOK_FAILED`).
Post-hook failures are reported but return exit code 0.
Invalid status values return exit code 1.

### Examples

```bash
openstation hooks                                          # list all hooks
openstation hooks list                                     # same as bare 'hooks'
openstation hooks show 0                                   # show hook at index 0
openstation hooks show "*→done"                            # show hook by matcher
openstation hooks run 0042 in-progress review              # trigger matching hooks
openstation hooks run 0042 in-progress review --dry-run    # preview without executing
openstation hooks run 0042 ready in-progress --phase pre   # only pre-hooks
```

---

## `init`

Initialize Open Station in the current directory.

### Synopsis

```
openstation init [--agents NAMES | --no-agents] [--dry-run] [--force]
```

### What It Does

1. Creates the `.openstation/` directory structure and `.claude/`
2. Copies commands, skills, and docs from the install cache (`$OPENSTATION_DIR` or `~/.local/share/openstation`)
3. Installs agent templates (adapted for the project name)
4. Creates `.claude/` symlinks → `.openstation/` for agent, command, and skill discovery

### Flags

| Flag              | Description |
|-------------------|-------------|
| `--agents NAMES`  | Comma-separated agent names to install (default: all available templates) |
| `--no-agents`     | Skip installing agent specs |
| `--dry-run`       | Show what would be done without writing |
| `--force`         | Overwrite existing user-owned files |

`--agents` and `--no-agents` are mutually exclusive.

### Default Agents

When no filter is specified: `architect`, `author`, `developer`, `project-manager`, `researcher`.

### Examples

```bash
openstation init                          # full init with all agents
openstation init --agents researcher,author
openstation init --no-agents
openstation init --dry-run                # preview without writing
```

### Guards

- Refuses to run inside the Open Station source repo itself.
- Requires the install cache to exist (prompts to run the installer if missing).

---

## `self-update`

Update the Open Station install cache and re-link the CLI binary.

### Synopsis

```
openstation self-update [--version TAG]
```

### Flags

| Flag              | Default  | Description |
|-------------------|----------|-------------|
| `--version TAG`   | latest   | Target version tag (e.g. `v0.10.0`). When omitted, updates to the latest tag from the remote. Bare version numbers are auto-prefixed with `v`. |

### What It Does

1. Fetches tags from the remote in the install cache (`~/.local/share/openstation/` or `$OPENSTATION_DIR`)
2. Checks out the target version (latest tag or `--version`)
3. Force-checkouts to handle dirty install caches (the cache is not user-editable)
4. Re-creates the CLI binary symlink (`~/.local/bin/openstation` → `dist/openstation`)
5. Prints the old and new version
6. If run inside a project with `.openstation/`, suggests running `openstation init` to update the project

### Prerequisites

- The install cache must exist (run the installer first)
- The install cache must be a git clone (curl-only installs are not supported)
- `git` must be available on `$PATH`

### Examples

```bash
openstation self-update                    # update to latest tag
openstation self-update --version v0.10.0  # pin to a specific version
openstation self-update --version 0.10.0   # bare version (auto-prefixed with v)
```

---

## Exit Codes

| Code | Constant              | Meaning |
|------|-----------------------|---------|
| 0    | `EXIT_OK`             | Success |
| 1    | `EXIT_USAGE`          | Invalid arguments or usage error |
| 2    | `EXIT_NO_PROJECT`     | Not in an Open Station project (no `.openstation/` found) |
| 3    | `EXIT_NOT_FOUND`      | Task or agent not found |
| 4    | `EXIT_AMBIGUOUS`      | Ambiguous task query (multiple matches) |
| 5    | `EXIT_TASK_NOT_READY` | Task status is not `ready` (use `--force` to override) |
| 6    | `EXIT_INVALID_TRANSITION` | Invalid lifecycle status transition |
| 7    | `EXIT_NO_CLAUDE`      | `claude` CLI not found on `$PATH` |
| 8    | `EXIT_AGENT_ERROR`    | Agent execution failed (non-zero exit from claude) |
| 9    | `EXIT_SOURCE_GUARD`   | Refused to init inside the source repo |
| 10   | `EXIT_HOOK_FAILED`    | A lifecycle hook failed or timed out (see `docs/hooks.md`) |

## Project Discovery

All commands except `init` require an Open Station project root. The CLI walks up from `$CWD` looking for:

1. A directory containing both `agents/` and `install.sh` (source repo — prefix: `""`)
2. A `.openstation/` subdirectory (installed project — prefix: `".openstation"`)

If the walk-up finds nothing and `$CWD` is inside a git worktree, the main worktree root is checked as a fallback.
