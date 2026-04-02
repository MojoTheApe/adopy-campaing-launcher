---
kind: spec
name: task-spec
---

# Task Specification

Defines the format for tasks in Open Station. A task is a
unit of work — a single markdown file with YAML frontmatter
and a markdown body.

## File Location

Every task file lives permanently in `artifacts/tasks/`:

```
artifacts/tasks/NNNN-kebab-slug.md
```

Task files are created here once and never move.

## Naming

### Task ID

- 4-digit zero-padded auto-incrementing integer (`0001`, `0042`)
- Unique across all tasks — assigned atomically by the `state.db`
  counter via `openstation create`

### Slug

- Kebab-case, max 5 words
- Descriptive of the task's goal
- Combined with ID: `NNNN-kebab-slug`

### Filename and `name` field

The filename (without `.md`) and the `name` frontmatter field
must match exactly: `NNNN-kebab-slug`.

### Task Resolution

When resolving a task reference (from CLI arguments, slash
commands, or frontmatter links), use the following strategy
in order (first match wins):

1. **Exact match** — `artifacts/tasks/<input>.md`
2. **Glob fallback** — `artifacts/tasks/*-<input>.md`
3. **Numeric prefix** — if input is digits only (e.g., `42`),
   zero-pad and match: `artifacts/tasks/0042-*.md`

If no match is found, report an error and suggest
`openstation list` to find the correct name. If multiple
files match, report ambiguity and list candidates.

The `openstation show` and `openstation status` CLI commands
implement this resolution natively. Slash commands should
prefer `openstation show <task>` over manual resolution.

### Sub-tasks

Sub-tasks are full tasks with their own ID and canonical file
in `artifacts/tasks/`, linked to a parent task via frontmatter
fields. See `docs/storage-query-layer.md` § 5 for the
full sub-task storage model.

#### Naming

Sub-task filename: `NNNN-kebab-slug.md` (same as any task).
The slug should be descriptive of the sub-task's goal — it
does not need to repeat the parent slug.

#### `parent:` field

Every sub-task sets `parent: "[[<parent-task-name>]]"` in its
frontmatter using an Obsidian wikilink. This is the only
required link back to the parent.

#### Parent body section

The parent task file should include a `## Subtasks` section
listing the sub-tasks with priority groups. See the
"Task with sub-tasks" example below.

#### Scope authority

The child task's body is the authoritative scope — not the
parent's description of it. Parent subtask descriptions are
brief pointers; they must not define or expand the child's
scope. If a child narrows scope from the parent (e.g., covers
only one milestone of a multi-phase epic), the child's explicit
boundaries take precedence.

When a child task excludes something from the parent's scope,
state the exclusion explicitly in the child's body — don't rely
on omission.

## Frontmatter Schema

```yaml
---
kind: task              # Required. Always "task".
name: NNNN-kebab-slug   # Required. Matches filename (without .md).
type: feature           # Optional. Task type. Default: "feature".
status: backlog         # Required. See Status Values below.
assignee:               # Optional. Agent name assigned to execute.
owner: user             # Required. Who verifies. Default: "user".
parent:                 # Optional. "[[parent-task-name]]" (wikilink).
subtasks:               # Optional. List of "[[sub-task-name]]" (wikilinks).
artifacts:              # Optional. List of "[[artifact-path]]" (wikilinks).
allowed-tools:          # Optional. Extra tool patterns for this task.
created: YYYY-MM-DD     # Required. Date the task was created.
---
```

### Editing Guardrails

- Edit only the specific field being changed.
- Preserve all other fields unchanged.
- Always update frontmatter directly — never add a body
  comment as a substitute for a field update.

### Field Reference

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `kind` | string | yes | — | Always `task` |
| `name` | string | yes | — | `NNNN-kebab-slug`, matches filename (without `.md`) |
| `type` | enum | no | `feature` | Nature of the work. See Type Values below. |
| `status` | enum | yes | `backlog` | Current lifecycle stage |
| `assignee` | string | no | empty | Agent assigned to execute the task |
| `owner` | string | yes | `user` | Who verifies: agent name or `user` |
| `parent` | string | no | empty | `"[[parent-task-name]]"` wikilink (for sub-tasks) |
| `subtasks` | list | no | empty | `"[[sub-task-name]]"` wikilinks (for parent tasks) |
| `artifacts` | list | no | empty | `"[[artifact-path]]"` wikilinks to produced artifacts |
| `allowed-tools` | list | no | empty | Extra tool patterns merged with the agent's tools at launch |
| `created` | date | yes | — | ISO 8601 date (`YYYY-MM-DD`) |

### Status Values

| Value | Meaning |
|-------|---------|
| `backlog` | Created, not ready for execution |
| `ready` | Requirements defined, agent assigned |
| `in-progress` | Agent is actively working |
| `review` | Work complete, awaiting verification |
| `verified` | All verification checks passed, awaiting owner acceptance |
| `done` | Verification passed |
| `failed` | Agent attempted but could not complete the task |
| `rejected` | Task won't be completed — descoped, superseded, or abandoned |

See `docs/lifecycle.md` for valid transitions, ownership rules,
sub-task lifecycle, and artifact routing.

### Type Values

| Value | Meaning |
|-------|---------|
| `feature` | End-to-end feature work (default when omitted) |
| `research` | Investigation, analysis, or information gathering |
| `spec` | Architecture design, technical specification, or RFC |
| `implementation` | Code implementation from an existing spec |
| `documentation` | Writing or updating docs, skills, or commands |

Tasks without a `type` field are treated as `feature` for
backward compatibility.

### Type-Specific Documentation Standards

Documentation artifacts (files in `docs/`, skills, or commands)
produced by `documentation` and `feature` tasks must meet
content standards based on their subject matter.

#### Architecture Section

Docs that describe a feature, system, or component **with code**
must include an `## Architecture` section covering:

- **Module layout** — files and directories that implement the
  feature, with a brief role for each
- **Integration points** — how the feature connects to other
  components (hooks, events, CLI entry points, file conventions)
- **Data flow** — how data moves through the system for the
  feature's key operations
- **Key abstractions** — important types, interfaces, patterns,
  or conventions the implementation relies on

The section should be concrete enough that a developer
unfamiliar with the codebase can understand *how the feature is
built*, not just how to use it.

#### Applicability

| Doc subject | Architecture section | Examples |
|---|---|---|
| Feature with code | **Required** | `docs/hooks.md`, `docs/cli.md` |
| Convention / process | Not required | `docs/lifecycle.md`, `docs/task.spec.md` |
| Pure schema / format | Not required | Field references, YAML schemas |
| Skill (operational) | Required if wrapping code | Skills that drive CLI tools or code |

When in doubt, include the section — it is easier to trim than
to retrofit.

### Artifacts Field

The `artifacts` list uses **Obsidian wikilinks** pointing to the
canonical location in `artifacts/`. Examples:

```yaml
artifacts:
  - "[[artifacts/agents/project-manager]]"
  - "[[artifacts/research/obsidian-plugin-api]]"
```

See `docs/storage-query-layer.md` §§ 3c, 4 for artifact
associations and routing.

### Allowed Tools Field

The `allowed-tools` list specifies additional tool patterns that
the agent needs for this specific task. These are merged with
the agent's own `allowed-tools` at launch time (agent tools
first, then task tools, deduplicated). The format is the same
as the agent spec's `allowed-tools` field.

```yaml
allowed-tools:
  - "Bash(docker *)"
  - "Bash(npm *)"
  - WebFetch
```

Use this when a task requires tools beyond the agent's default
set. For one-off additions, the `--tools` CLI flag on
`openstation run` can also append tools at launch time (highest
priority — after agent + task tools).

### Artifact Provenance Fields

Artifacts (agent specs, research outputs, etc.) should declare
provenance in their own frontmatter:

```yaml
agent: researcher                                # Which agent created this
task: "[[0044-storage-layer-replacement]]"       # Which task (wikilink)
```

Use `agent: manual` and omit `task` for manually created
artifacts. See `docs/storage-query-layer.md` §§ 3d, 3e.

## Body Structure

The markdown body follows the frontmatter. It starts with an
H1 title and contains required and optional sections.

### Required Sections

#### `# Title`

Human-readable task title as an H1 heading. Should clearly
describe the goal.

#### `## Requirements`

What needs to be done. Can contain:
- Prose descriptions
- Numbered sub-steps
- Sub-headings (H3) for grouping related requirements
- Tables, code blocks, and links as needed

Requirements should be concrete and testable.

#### `## Findings`

Summary of what the task produced or discovered. Written by the
assignee during execution, not by the task author upfront.

Every completed task must have a Findings section. Content
varies by task type:

| Task type        | What to include                                    |
|------------------|----------------------------------------------------|
| `research`       | Key results, sources consulted, confidence levels   |
| `spec`           | Summary of design, key trade-offs made              |
| `implementation` | What was built/changed, design decisions, gotchas   |
| `documentation`  | What was written/updated, scope of changes. Note: if the doc covers a feature with code, confirm the artifact includes an Architecture section per § Type-Specific Documentation Standards. |
| `feature`        | What was built, how it works, notable decisions. Note: if docs were produced, confirm they include an Architecture section per § Type-Specific Documentation Standards. |

Link to artifacts where relevant. Lead with conclusions, not
process narrative.

#### `## Verification`

Checklist of items that must be true when the task is complete.
Each item is a GitHub-flavored markdown checkbox:

```markdown
## Verification

- [ ] First verification criterion
- [ ] Second verification criterion
```

Items are checked (`[x]`) as they are verified.

### Optional Sections

| Section              | Purpose                                                                                                |
| -------------------- | ------------------------------------------------------------------------------------------------------ |
| `## Context`         | Background information, links to related tasks or research                                             |
| `## Subtasks`        | Decomposition into sub-tasks (H3 per group with numbered items)                                        |
| `## Progress`        | Timestamped log of work done across agent sessions. Append-only.                                       |
| `## Suspended`       | Suspension record written by `/openstation.suspend`. Contains date, target status, reason, and branch.  |
| `## Downstream`      | Follow-up work identified during execution (docs needed, behavior changes, refactoring opportunities)  |
| `## Recommendations` | Actionable suggestions based on findings                                                               |
| `## Verification Report` | Machine-written by `/openstation.verify`. Contains date, pass/fail table, summary, and fix suggestions. Not authored manually. |

Optional sections appear between Requirements and Verification
when present. `## Verification Report` appears after
`## Verification` (not between Requirements and Verification).

## Progressive Disclosure

Tasks start minimal and gain detail as they mature. Only add
fields and sections when they become relevant — never front-load
structure that isn't needed yet.

### Stages

| Stage          | Frontmatter                                  | Body                                                                  |
| -------------- | -------------------------------------------- | --------------------------------------------------------------------- |
| **Idea**       | `kind`, `name`, `status: backlog`, `created`, optionally `type` | `# Title`, `## Requirements` (rough), `## Verification` (placeholder) |
| **Scoped**     | + `assignee`, `owner`                        | Requirements become concrete and testable                             |
| **Decomposed** | + `parent` wikilink (on sub-tasks)            | + `## Subtasks` with priority groups                                  |
| **In-flight**  | `status: in-progress`                        | + `## Context` if background is needed                                |
| **Completed**  | `status: done`                               | + `## Findings` (all types), `## Recommendations` (if applicable), `## Downstream` (if applicable) |

### Rules

1. **Start with the minimum** — `kind`, `name`, `status`,
   `created`, a rough Requirements section, and placeholder
   Verification items. This is enough for backlog.
2. **Add assignment when ready** — set `assignee` and `owner`
   when the task moves to `ready`. Leave them empty until then.
3. **Decompose only when needed** — add `## Subtasks` and
   `parent` fields only if the task is too large for a single
   agent pass.
4. **Add output sections at the end** — `## Progress`,
   `## Findings`, `## Downstream`, and `## Recommendations`
   are written by agents during execution, not by the task
   author upfront.

### Canonical Section Order

1. `# Title` (required)
2. `## Context` (optional)
3. `## Requirements` (required)
4. `## Subtasks` (optional)
5. `## Progress` (optional — append-only, written during execution)
6. `## Suspended` (optional — written by `/openstation.suspend`)
7. `## Findings` (required — written during execution)
8. `## Downstream` (optional — written during execution)
9. `## Recommendations` (optional — written during execution)
10. `## Verification` (required)
11. `## Verification Report` (optional — machine-written by `/openstation.verify`)

## Decomposition

For sizing heuristics, split-vs-keep criteria, subtask vs
independent task guidance, and parent task patterns, see
`docs/decomposition.md`.

## Examples

### Minimal backlog task

```markdown
---
kind: task
name: 0010-add-login-page
type: implementation
status: backlog
assignee:
owner: user
created: 2026-02-24
---

# Add Login Page

## Requirements

Create a login page with email and password fields. On submit,
call the `/auth/login` endpoint and redirect to the dashboard
on success.

## Verification

- [ ] Login page renders at `/login`
- [ ] Successful login redirects to `/dashboard`
- [ ] Invalid credentials show an error message
```

### Full investigation task

```markdown
---
kind: task
name: 0003-research-obsidian-plugin-api
type: research
status: done
assignee: researcher
owner: user
created: 2026-02-21
---

# Research Obsidian Plugin API

## Requirements

Investigate the Obsidian plugin API to understand how Open Station
could integrate with Obsidian as a plugin in the future. Focus on:

- How plugins read and write vault files
- How plugins can add custom views (e.g., a task board)
- How plugins hook into file change events

## Verification

- [x] Research notes cover all three focus areas
- [x] Notes include links to relevant Obsidian documentation
- [x] Findings include a recommendation on feasibility
```

### Task with sub-tasks

#### File structure

```
artifacts/tasks/
├── 0006-adopt-spec-kit-patterns.md   # subtasks: ["[[0007-...]]", "[[0008-...]]"]
├── 0007-add-constitution.md          # parent: "[[0006-adopt-spec-kit-patterns]]"
└── 0008-add-templates.md             # parent: "[[0006-adopt-spec-kit-patterns]]"
```

Sub-tasks are linked via frontmatter wikilinks: `subtasks` on
the parent, `parent` on each child.

#### Parent task (`0006-adopt-spec-kit-patterns.md`)

```markdown
---
kind: task
name: 0006-adopt-spec-kit-patterns
type: feature
status: backlog
assignee: author
owner: user
subtasks:
  - "[[0007-add-constitution]]"
  - "[[0008-add-templates]]"
created: 2026-02-21
---

# Adopt Spec-Kit Patterns

Implement patterns learned from Spec-Kit research.

## Requirements

Adopt conventions that fit Open Station's zero-dependency
philosophy.

## Subtasks

### HIGH Priority

1. **Add constitution file** — Create `constitution.md` at vault
   root with versioned project principles.

2. **Add templates** — Create `templates/` with structured
   templates for tasks, agents, and research artifacts.

## Verification

- [ ] Constitution file exists and is referenced in manual
- [ ] Templates directory exists with task, agent, and research templates
```

#### Sub-task (`0007-add-constitution.md`)

```markdown
---
kind: task
name: 0007-add-constitution
type: documentation
status: backlog
assignee: author
owner: user
parent: "[[0006-adopt-spec-kit-patterns]]"
created: 2026-02-21
---

# Add Constitution File

## Requirements

Create `constitution.md` at vault root with versioned project
principles.

## Verification

- [ ] Constitution file exists and is referenced in manual
```

### Milestone-based parent

When a parent task is delivered in phases, group scope and
subtasks by milestone. Each child task covers one milestone
and explicitly excludes the rest.

#### Parent (`0050-worktree-integration.md`)

```markdown
---
kind: task
name: 0050-worktree-integration
type: feature
status: in-progress
owner: user
subtasks:
  - "[[0051-research-worktree]]"
  - "[[0052-spec-worktree-passthrough]]"
created: 2026-03-01
---

# Worktree Integration

Enable agents to work in git worktrees with isolated branches.

## Scope

Milestone-based. Each milestone is usability-tested before
the next begins. Implementation subtasks are created
per-milestone after the previous one lands.

### M1 — Pass-through (current)
- Vault resolution from linked worktrees
- `--worktree` flag on `openstation run`

### M2 — Branch scoping (future)
- `branch` frontmatter field
- Branch-scoped task filtering

### M3 — Agent awareness (future)
- Agent skills document worktree workflows
- Parallel agent execution on separate branches

## Subtasks

### M1
1. **0051 — Research** — Consolidate worktree research.
2. **0052 — Spec** — Pass-through design and vault resolution.
   M1 scope only.

### Future
*(Created when the previous milestone lands)*

## Verification

- [ ] M1: Agents dispatch in worktrees and find the shared vault
- [ ] M2: Branch-scoped tasks filter correctly
- [ ] M3: Agent skills document worktree workflows
```

#### Child (`0052-spec-worktree-passthrough.md`)

```markdown
---
kind: task
name: 0052-spec-worktree-passthrough
type: spec
status: ready
assignee: architect
owner: user
parent: "[[0050-worktree-integration]]"
created: 2026-03-01
---

# Spec: Worktree Pass-Through

Scoped to M1 (pass-through) only. No branch field or
branch-scoped task filtering.

## Requirements

1. Define `find_root()` worktree resolution design
2. Define `--worktree` pass-through on `openstation run`
3. Define how agents discover the shared vault from a worktree

## Verification

- [ ] Spec covers vault resolution
- [ ] Spec covers `--worktree` pass-through
- [ ] No branch-scoping content included
```

The child states its boundary and its exclusion. An agent
reading only the child knows exactly what is in and out of
scope.
