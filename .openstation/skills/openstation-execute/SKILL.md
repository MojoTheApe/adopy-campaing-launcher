---
name: openstation-execute
description: Teaches agents how to operate within Open Station — find tasks, execute work, update state, and store artifacts.
user-invocable: false
---

# Open Station Executor

You are operating within **Open Station**, a task management system
where all state is stored as markdown files with YAML frontmatter.

## Vault Structure

```
docs/              — Project documentation (lifecycle, task spec, README)
artifacts/         — Canonical artifact storage (source of truth)
  tasks/           —   Task files (NNNN-slug.md, single files, never moved)
  agents/          —   Agent specs (canonical location)
  research/        —   Research outputs
  specs/           —   Specifications & designs
agents/            — Agent discovery (symlinks → artifacts/agents/)
skills/            — Skills (including this one)
commands/          — User-invocable slash commands
```

All tasks are single files in `artifacts/tasks/` — e.g.,
`artifacts/tasks/0010-add-login-page.md`. There are no task
folders or bucket directories.

## CLI Tool

The `openstation` CLI provides scriptable access to the task vault.
Prefer it for discovery, inspection, and status transitions.
See `docs/cli.md` for the full reference (all flags, exit codes,
resolution rules).

**Always call `openstation` directly** — never `python3 bin/openstation`
or any other indirect path. The binary is on `$PATH` during agent
sessions. Fall back to direct file reads/edits only if the command
is not found.

Key commands:

| Command | Purpose |
|---------|---------|
| `openstation list --status ready,in-progress --assignee <you>` | Find your tasks |
| `openstation show <task>` | Read a task spec |
| `openstation status <task> <new-status>` | Transition lifecycle state |
| `openstation create "<desc>" [--parent <p>]` | Create a task or sub-task |

## Available Slash Commands

These are user-invocable commands you may also use during
execution when appropriate:

| Command | Purpose |
|---------|---------|
| `/openstation.create` | Create a new task (with decomposition support) |
| `/openstation.list` | List tasks with filters |
| `/openstation.show` | Show full task details |
| `/openstation.ready` | Promote backlog → ready |
| `/openstation.done` | Mark verified → done (with artifact promotion) |
| `/openstation.suspend` | Suspend in-progress → ready/backlog |
| `/openstation.reject` | Mark task → rejected (with reason) |
| `/openstation.verify` | Verify a task's checklist against evidence |
| `/openstation.update` | Edit task metadata (not status) |
| `/openstation.progress` | Append a timestamped progress entry to a task |

## Worktree Awareness

Use `$OPENSTATION_HOME` to build absolute paths for any filesystem
access to vault artifacts. This works in all modes (linked worktree,
independent worktree, and non-worktree).

- **Prefer CLI commands** (`openstation show/list/create/status`)
  for discovery and status transitions — they resolve the correct
  root automatically
- For direct file reads, use `$OPENSTATION_HOME/artifacts/tasks/`,
  `$OPENSTATION_HOME/docs/`, etc. — never relative paths
- The `info:` line from `create`/`status` shows the absolute path
  of the modified file, confirming which vault was used

See `docs/worktrees.md` for full details on primary vs linked
modes and how `find_root()` resolves the vault.

## Environment

`OPENSTATION_HOME` — absolute path to the `.openstation/` vault
directory. Set automatically by `openstation run` before launching
the agent session. Use it to build absolute paths to vault
artifacts:

- `$OPENSTATION_HOME/artifacts/tasks/` — task files
- `$OPENSTATION_HOME/artifacts/agents/` — agent specs
- `$OPENSTATION_HOME/docs/` — project documentation
- `$OPENSTATION_HOME/commands/` — slash commands

This variable works in all modes (linked worktree, independent
worktree, and non-worktree). Prefer CLI commands for discovery
and status transitions; use `$OPENSTATION_HOME` paths when you
need direct filesystem access.

## On Startup

1. Determine your agent name from your agent spec (`name` field in
   your frontmatter).
2. Read `docs/lifecycle.md` for lifecycle rules (statuses,
   transitions, ownership, artifact routing, guardrails).
3. Read `docs/task.spec.md` for task format (fields, naming,
   body structure, editing guardrails).
4. Run `openstation list --status ready,in-progress --assignee <your-name>`
   to find assigned tasks. If the CLI is unavailable, fall back to
   scanning `$OPENSTATION_HOME/artifacts/tasks/*.md` for files where
   `assignee` matches your name AND `status` is `ready` or `in-progress`.
5. If multiple tasks exist, pick the one with the earliest
   `created` date (prefer `in-progress` over `ready` to resume work).
6. If no tasks exist, report: "No ready or in-progress tasks assigned
   to agent [name]." and stop.

## Executing a Task

### 1. Load Context

- Run `openstation show <task-name>` to load the full task spec
  (or read `artifacts/tasks/<task-name>.md` directly). Note
  requirements and verification checklist.
- If the task has a `## Suspended` section, it was previously
  in-progress and paused. Read the branch reference and suspension
  reason. Use `git diff main...suspend/<task-name>` to understand
  what was done before. The `## Progress` section from the prior
  session provides additional context.
- If the task has a `parent` field, read the parent task for
  background context.
- Transition the task to in-progress:

  ```bash
  openstation status <task-name> in-progress
  ```

  **Fallback** — if the CLI is unavailable, edit `status: ready`
  → `status: in-progress` directly in the task frontmatter.

### 2. Evaluate Complexity (optional)

If the task is too large for a single pass, decompose it into
sub-tasks before starting work. Decompose when **any** of these
are true:

- Requirements list **6+ independent items** (count them)
- Work spans **2+ agent roles** (e.g., code + docs, spec + research)
- Task requires creating or modifying **4+ files**
- Requirements reference **2+ unrelated domains** (e.g., CLI + skills + lifecycle)

See `docs/decomposition.md` for full sizing heuristics,
split-vs-keep criteria, and parent task patterns.

If decomposition is needed, skip to step 8 (Create Sub-Tasks),
set `status: review`, and stop. The owner will promote the
sub-tasks for execution.

### 3. Work Through Requirements

- Follow the requirements section of the task spec.
- Apply your agent capabilities and constraints as defined in your
  agent spec.
- Break complex work into logical steps.

### 4. Store Artifacts

- Store artifacts in `artifacts/<category>/` (the canonical
  location) and record them in the task's `artifacts` frontmatter
  list using Obsidian wikilinks: `"[[artifacts/research/name]]"`.
  **Every produced artifact must appear in the `artifacts` list** —
  this is how verification and promotion find them.
- Set provenance fields (`agent`, `task`) on each artifact.
  Use wikilinks for the `task` field: `task: "[[0047-name]]"`.
- **Do NOT create discovery or promotion symlinks** (e.g.
  `agents/<name>.md`). `/openstation.done` handles promotion
  after verification.
- See `docs/storage-query-layer.md` §§ 3c–3d, 4 for
  routing and provenance conventions.

### 5. Record Findings

After completing the work, add a `## Findings` section to the
task file summarizing what you produced. This is required for
all task types.

Content varies by type:
- **research**: Key results, sources, confidence levels
- **spec**: Design summary, trade-offs, key decisions
- **implementation**: What was built/changed, design decisions,
  gotchas
- **documentation**: What was written/updated, scope
- **feature**: What was built, how it works, notable decisions

Lead with conclusions. Link to artifacts. Keep it concise —
a reviewer should understand the work without reading every
file you touched.

### 6. Flag Downstream Work (if applicable)

If your work introduced any of the following, add a
`## Downstream` section to the task file:

- Behavior changes that need documentation updates
- New conventions or patterns others should know about
- Gaps you noticed but didn't address (out of scope)
- Follow-up tasks that would improve the result

Each item is a bullet describing what needs to happen and why.
The reviewer will decide whether to create tasks for them.

Omit this section if there is no downstream work to flag.

### 7. Record Progress

Use `/openstation.progress <task-name> <message>` to append a
timestamped progress entry. The command handles format, placement,
and append-only rules. See the command for full details.

- Include the log path (`artifacts/logs/<task-name>.jsonl`) if
  your session is being logged
- Add your entry before transitioning to `review` or `rejected`

### 8. Create Sub-Tasks (if needed)

If a task requires decomposition:

1. Use `/openstation.create` or `openstation create` to create each
   sub-task. **Never create task files manually** — the CLI assigns
   unique IDs atomically via `state.db`. Scanning `artifacts/tasks/`
   to pick the next ID causes collisions when agents run concurrently.
2. Set `parent: "[[<current-task-name>]]"` in each sub-task's
   frontmatter (Obsidian wikilink format).
3. Add `"[[<sub-task-name>]]"` to the parent's `subtasks`
   frontmatter list and add an entry to the parent's
   `## Subtasks` body section.

See `docs/storage-query-layer.md` § 5 for the full
sub-task storage model and `docs/lifecycle.md` § "Sub-Tasks"
for blocking rules.

### 9. Update Documentation

If your changes affect behavior, conventions, or structures
documented in `docs/` or `CLAUDE.md`, update those files to
stay in sync.

When updating an artifact, check its `task` frontmatter field.
If the linked task has a `## Findings` section that conflicts
with your changes, update the Findings to match.

Skip this step if no documentation or findings are affected.

## Fail Fast

If you cannot complete a task due to a hard blocker, fail
immediately — do not retry endlessly or work around the problem.

### Hard Blockers — Fail Immediately

Stop and use `/openstation.fail` as soon as you discover any of:

- **Missing tools** — required CLI tools (`gh`, `git`, `npm`,
  `docker`, etc.) are not available or not in the allowlist
- **Permission errors** — tool calls return "not allowed",
  access denied, or authentication failures
- **External services unavailable** — APIs returning errors,
  timeouts, or unreachable endpoints that the task depends on
- **Missing prerequisites** — a dependency task is not done, or
  required data/files do not exist and cannot be created

Do not retry, do not improvise a workaround. The failure report
gives the supervisor the information needed to fix the setup
and retry.

### Soft Blockers — Fail After 5 Stuck Turns

If you have been stuck for **5+ turns** without meaningful
progress, stop and fail. Signals:

- Same operation failing repeatedly with the same error
- Looping on a fix that does not converge
- Generating output that does not advance any requirement

Five turns of no progress means the problem is structural, not
a matter of persistence.

### Writing a Good Failure Report

The reason passed to `/openstation.fail` must include three
parts: what blocked you, what you completed, and what to fix.

**Example — missing tools:**

```
/openstation.fail 0230 "Missing gh and git in allowed tools.
Task requires PR review (gh pr view, gh pr diff) and merge
(gh pr merge) but neither Bash(gh *) nor Bash(git *) are in
the allowlist. Completed: read the task spec and identified
required operations. Fix: add Bash(gh *) and Bash(git *) to
the agent's allowed-tools or the task's allowed-tools field."
```

**Example — repeated failure:**

```
/openstation.fail 0115 "pytest segfaults on import of
native extension (5 attempts, same core dump each time).
Completed: implemented the handler and wrote tests.
Fix: check native dependency versions in the CI image or
run tests in a clean virtualenv."
```

## Completing a Task

After working through all requirements:

1. Self-check: review the `## Verification` items and confirm
   your work addresses each one. Do **not** check the boxes
   yourself — that is the owner's job via `/openstation.verify`.
2. Transition the task to review:

   ```bash
   openstation status <task-name> review
   ```

   **Fallback** — if the CLI is unavailable, edit
   `status: in-progress` → `status: review` directly in the
   task frontmatter.

3. Stop. The designated owner handles verification from here.

See `docs/lifecycle.md` § "Status Transitions" for guardrails.

## Verifying a Task (when you are the owner)

Only the designated owner may approve or reject a task. The
`owner` field in task frontmatter names the owner — either
an agent name or `user` (meaning a human verifies).

### Agent Owner

If `owner` is your agent name:

1. Read the task spec and its **Verification** section.
2. Check each verification item against the artifacts and changes
   produced.
3. If ALL items pass: run `/openstation.verify <task-name>` to
   transition to `verified`, then `/openstation.done <task-name>`
   to complete the task.
4. If ANY item fails: set `status: rejected` and document which
   items failed and why (add a note in the task body or as an
   artifact).

### User Owner

If `owner` is `user`, a human operator handles verification.
Do not verify on their behalf.
