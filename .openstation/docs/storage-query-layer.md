---
kind: spec
name: storage-query-layer
---

# Storage & Query Layer

Authoritative reference for how tasks, artifacts, and agents are
stored on the filesystem and how they are discovered.

For lifecycle rules (status transitions, ownership, verification)
see `docs/lifecycle.md`. For task field schema and naming see
`docs/task.spec.md`.

---

## Part I — Storage Layer

### 1. Canonical Storage Model

All persistent data lives under `artifacts/`, which is the single
source of truth. Each artifact type has a dedicated subdirectory:

```
artifacts/
├── tasks/       — Task files (one per task, never moved)
├── agents/      — Agent spec files (canonical location)
├── research/    — Research outputs (from researcher agent)
└── specs/       — Specifications and designs
```

**Immutability rule:** Once a file is created in `artifacts/`, it
stays at that path permanently. Nothing in `artifacts/` is ever
moved or renamed — only its contents are updated in place.

All artifact types are single markdown files with YAML
frontmatter, stored directly in their category directory:

```
artifacts/tasks/0010-add-login-page.md
artifacts/agents/researcher.md
artifacts/research/obsidian-plugin-api.md
artifacts/specs/storage-query-layer.md
```

### 2. Discovery Symlinks

Open Station uses a single kind of symlink: **discovery
symlinks** for agent resolution. No other symlinks exist in the
system.

#### 2a. Agent Discovery Symlinks

Symlinks in `agents/` that point to canonical specs in
`artifacts/agents/`. They make agents available via
`claude --agent <name>`:

```
agents/researcher.md → ../artifacts/agents/researcher.md
```

Discovery symlinks are created by:

- `openstation init` — for bundled example agents at install time.
- `/openstation.done` — for agents produced by tasks, after
  verification passes (agent promotion).

Agents must NOT create discovery symlinks during task execution.

### 3. Frontmatter Associations

All relationships between tasks, sub-tasks, and artifacts are
encoded in YAML frontmatter fields using **Obsidian wikilinks**
(`[[name]]`). Wikilinks make references clickable in Obsidian's
properties panel and graph view. Quote them for valid YAML.

There are no symlinks for these associations.

#### 3a. Parent → Children (subtasks)

A parent task lists its sub-tasks in a `subtasks` frontmatter
field:

```yaml
subtasks:
  - "[[0046-spec-storage-query-layer]]"
  - "[[0047-implement-storage-replacement]]"
```

#### 3b. Child → Parent

Each sub-task declares its parent via the `parent` field:

```yaml
parent: "[[0045-replace-storage-obsidian-cli]]"
```

#### 3c. Task → Artifacts

Tasks list the artifacts they produced in the `artifacts`
frontmatter field:

```yaml
artifacts:
  - "[[artifacts/agents/project-manager]]"
  - "[[artifacts/research/obsidian-plugin-api]]"
```

#### 3d. Artifact Provenance

Artifacts declare which agent generated them and which task they
belong to:

```yaml
agent: researcher
task: "[[0044-storage-layer-replacement]]"
```

Use `agent: manual` and omit `task` for manually created
artifacts. These fields are optional — not all artifacts track
provenance (e.g., task files do not set these on themselves).

#### 3e. Wikilink Convention

All frontmatter fields that reference other files use Obsidian
wikilinks: `"[[name]]"`. This includes `subtasks`, `parent`,
`task`, and `artifacts`. The CLI strips `[[...]]` when resolving
names, so both `"[[0047-implement-storage-replacement]]"` and
`0047-implement-storage-replacement` are accepted.

### 4. Artifact Routing

During task execution, agents store artifacts in the appropriate
`artifacts/<category>/` directory. The routing table:

| Artifact Type        | Destination              |
|----------------------|--------------------------|
| Task creation        | `artifacts/tasks/`       |
| Researcher output    | `artifacts/research/`    |
| Agent spec           | `artifacts/agents/`      |
| Other agent output   | `artifacts/specs/`       |

Agents also record produced artifacts in the task's frontmatter
`artifacts` list using canonical paths (§ 3c) and should set
provenance fields on the artifact itself (§ 3d).

### 5. Sub-task Storage

Sub-tasks are full tasks with their own canonical file in
`artifacts/tasks/`, linked to a parent through frontmatter.
They differ from top-level tasks in two ways:

1. **Parent field.** The sub-task's frontmatter sets
   `parent: "[[<parent-task-name>]]"` (§ 3b).
2. **Subtasks field.** The parent's frontmatter lists the
   sub-task in its `subtasks` field as a wikilink (§ 3a).

#### Creating a sub-task

1. Create canonical file: `artifacts/tasks/MMMM-sub-slug.md`.
2. Set `parent: "[[<parent-task-name>]]"` in sub-task frontmatter.
3. Add `"[[MMMM-sub-slug]]"` to the parent's `subtasks`
   frontmatter field.
4. Add an entry to the parent's `## Subtasks` body section.

#### Blocking rule

All sub-tasks must reach `done` before the parent can proceed
to `review`.

#### Status tracking

Each sub-task tracks its own status in its frontmatter,
following the same transitions as any task.

#### Discovery

Sub-tasks are discovered through the parent's `subtasks`
frontmatter field, or by querying all tasks with a given
`parent` value.

### 6. Install-time Layout

When installed into a target project via `openstation init`, the entire
vault is placed under `.openstation/`:

```
target-project/
├── .openstation/
│   ├── docs/                    — lifecycle.md, task.spec.md
│   ├── artifacts/
│   │   ├── tasks/
│   │   ├── agents/
│   │   ├── research/
│   │   └── specs/
│   ├── agents/                  — Discovery symlinks → artifacts/agents/
│   ├── skills/                  — Agent skills (openstation-execute)
│   ├── commands/                — Slash commands
│   └── hooks/                   — Pre-tool-use hooks
├── .claude/
│   ├── commands → ../.openstation/commands
│   ├── agents  → ../.openstation/agents
│   ├── skills  → ../.openstation/skills
│   └── settings.json            — Claude Code settings
└── CLAUDE.md                    — Contains managed openstation section
```

**Claude Code integration** is achieved through three directory
symlinks in `.claude/` that point into `.openstation/`:

| `.claude/` entry  | Target                          | Purpose                  |
|-------------------|---------------------------------|--------------------------|
| `commands/`       | `../.openstation/commands`      | Slash command discovery   |
| `agents/`         | `../.openstation/agents`        | `--agent` resolution     |
| `skills/`         | `../.openstation/skills`        | Skill loading            |

The installer also:

- Places `.gitkeep` files in empty content directories.
- Deploys pre-tool-use hooks (`validate-write-path.sh`,
  `block-destructive-git.sh`) and registers them in
  `.claude/settings.json` (Claude Code settings, not Open Station settings).
- Injects a managed `<!-- openstation:start -->` …
  `<!-- openstation:end -->` section into `CLAUDE.md`.
- Copies example agent specs to `artifacts/agents/` (skippable
  with `--no-agents`) and creates their discovery symlinks.

### 7. Design Rationale

**Canonical paths are stable.** Task files in `artifacts/tasks/`
never move or rename. Any reference to
`artifacts/tasks/NNNN-slug.md` remains valid across all lifecycle
stages.

**Flat `artifacts/tasks/` over nested.** All task files are
siblings under `artifacts/tasks/` rather than nested by status
or parent. This keeps:

- Task IDs globally unique and easily scannable (`ls artifacts/tasks/`).
- No deep nesting that obscures the task list.

**Symlink elimination.** Only discovery symlinks remain (for
Claude Code `--agent` resolution). All other relationships —
parent/child, task/artifact provenance — are encoded in YAML
frontmatter. This eliminates:

- State-split bugs (symlink says one thing, frontmatter another).
- Obsidian incompatibility (intra-vault symlinks violate
  Obsidian's disjoint-folder rule).
- Extra git diffs from symlink create/delete on transitions.

**Frontmatter as single source of truth.** Task lifecycle state,
relationships, and artifact provenance live exclusively in YAML
frontmatter fields. There is no secondary representation to keep
in sync.

**Convention over database.** The filesystem *is* the database.
Markdown files with YAML frontmatter are both human-readable and
machine-parseable. This means:

- Zero runtime dependencies — no database server, no ORM, no
  migrations.
- Git-native — all state is versioned, diffable, and mergeable.
- Agent-friendly — LLM agents can read and write tasks with
  standard file tools (Read, Write, Edit, Glob, Grep).
- Portable — `openstation init` bootstraps the full system from
  a single command.

**Dual-path query model.** The Obsidian CLI
(`/Applications/Obsidian.app/Contents/MacOS/obsidian`) provides
fast structured property queries when Obsidian is running. The
filesystem + `grep` fallback covers headless and CI environments.
This layered approach offers:

- Fast interactive queries via `obsidian search` with
  `[property: value]` syntax and JSON output.
- Reliable fallback when Obsidian is not running — `grep` across
  `artifacts/tasks/*.md` achieves the same results.
- No hard dependency on Obsidian — the system is fully functional
  with filesystem queries alone.

---

## Part II — Query Layer

The **`openstation` CLI** is the primary query interface. It
handles resolution, filtering, and output formatting. Under the
hood it uses filesystem queries (`grep` on frontmatter). The
**Obsidian CLI** is an optional supplement for users who have
Obsidian running — it provides fast property queries with
structured output but is never required.

### 8. Find Tasks by Status

**CLI:**

```bash
openstation list --status ready
```

**Filesystem (how it works internally):**

```bash
grep -rl 'status: ready' artifacts/tasks/*.md
```

**Obsidian CLI (optional):**

```bash
obsidian search vault="open-station" query='[kind: task] [status: ready]' format=json
```

### 9. Get Artifacts for a Task

Read the task's frontmatter `artifacts` field:

```yaml
artifacts:
  - "[[artifacts/agents/project-manager]]"
  - "[[artifacts/research/obsidian-plugin-api]]"
```

### 10. Get Sub-tasks of a Parent

**CLI:**

```bash
openstation list 0045           # shows task and its subtask tree
```

**Filesystem:**

```bash
grep -rl 'parent: 0045-replace-storage-obsidian-cli' artifacts/tasks/*.md
```

Alternatively, read the parent's `subtasks` frontmatter field
for the canonical list.

**Obsidian CLI (optional):**

```bash
obsidian search vault="open-station" query='[kind: task] [parent: 0045-replace-storage-obsidian-cli]' format=json
```

### 11. Find Tasks by Assignee

**CLI:**

```bash
openstation list --assignee researcher
openstation list --status ready --assignee researcher   # combined
```

**Filesystem:**

```bash
grep -rl 'assignee: researcher' artifacts/tasks/*.md
grep -rl 'status: ready' artifacts/tasks/*.md | xargs grep -l 'assignee: researcher'
```

**Obsidian CLI (optional):**

```bash
obsidian search vault="open-station" query='[kind: task] [status: ready] [assignee: researcher]' format=json
```

### 12. Agent Discovery

**CLI:**

```bash
openstation agents list              # all agents
openstation agents show researcher   # single agent spec
```

Agents are resolved through symlinks in the `agents/` directory:

```
claude --agent researcher
  → .claude/agents/researcher.md          (Claude Code lookup)
  → ../.openstation/agents/researcher.md  (install-time symlink)
  → ../artifacts/agents/researcher.md     (discovery symlink)
```

In the source repo (no `.openstation/` prefix):

```
claude --agent researcher
  → .claude/agents/researcher.md
  → agents/researcher.md                  (discovery symlink)
  → ../artifacts/agents/researcher.md     (canonical file)
```

Discovery symlinks are created by:

- `openstation init` — for bundled example agents at install time.
- `/openstation.done` — for agents produced by tasks, after
  verification passes (agent promotion).

### 13. Query Patterns Summary

Quick-reference table mapping common queries to CLI commands,
with filesystem and Obsidian alternatives.

| Query                        | CLI Command                                              | Filesystem ¹                      | Obsidian ² |
|------------------------------|----------------------------------------------------------|-----------------------------------|------------|
| Tasks with status X          | `openstation list --status X`                            | `grep -rl 'status: X' artifacts/tasks/*.md` | `[status: X]` |
| Tasks assigned to agent A    | `openstation list --assignee A`                          | `grep -rl 'assignee: A' …`       | `[assignee: A]` |
| Status + assignee            | `openstation list --status X --assignee A`               | pipe grep commands                | `[status: X] [assignee: A]` |
| Sub-tasks of parent P        | `openstation list P`                                     | `grep -rl 'parent: P' …`         | `[parent: P]` |
| Single task details          | `openstation show <task>`                                | Read `artifacts/tasks/<task>.md`  | — |
| Artifacts for task T         | Read `artifacts` field in task frontmatter               | —                                 | — |
| Artifact provenance          | Read `task` and `agent` fields on the artifact           | —                                 | — |
| All known agents             | `openstation agents list`                                | `ls artifacts/agents/`            | — |
| Next available task ID       | `openstation create` (auto-assigns)                      | `ls artifacts/tasks/ \| sort \| tail -1` | — |

¹ Always available — no dependencies.
² Requires Obsidian running. Prefix queries with `obsidian search vault="<name>" query='[kind: task] …' format=json`.
