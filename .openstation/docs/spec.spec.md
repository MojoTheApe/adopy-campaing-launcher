---
kind: spec
name: feature-spec
---

# Feature Spec Format

Defines the format for feature specs in Open Station. A feature
spec is a markdown document that captures how an implemented
feature works — architecture, components, data flow, and design
decisions.

For task format see `docs/task.spec.md`.

## Purpose

Feature specs serve two roles:

1. **Implementation guide** — concrete enough for agents to build
   from without ambiguity.
2. **Living documentation** — permanent reference for how a feature
   works after the implementing task is complete.

Unlike task specs (which describe *what to do*), feature specs
describe *how something works*.

## File Location

Feature specs live in `artifacts/specs/`:

```
artifacts/specs/<kebab-name>.md
```

### Naming Convention

- Kebab-case, descriptive of the feature
- No numeric prefix (specs are not sequenced like tasks)
- Examples: `autonomous-execution.md`, `cli-architecture.md`,
  `hook-system.md`

## Frontmatter Schema

```yaml
---
kind: spec              # Required. Always "spec".
name: kebab-name        # Required. Matches filename (without .md).
task: NNNN-task-slug    # Required. Producing task name.
created: YYYY-MM-DD     # Required. Date the spec was created.
status: draft           # Optional. Default: "draft". See § Status.
version: 1              # Optional. Increment on major revisions.
tags:                   # Optional. List of topic tags.
  - architecture
  - hooks
---
```

### Field Reference

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `kind` | string | yes | — | Always `spec` |
| `name` | string | yes | — | Kebab-case, matches filename |
| `task` | string | yes | — | Name of the task that produced this spec |
| `created` | date | yes | — | ISO 8601 date (`YYYY-MM-DD`) |
| `status` | enum | no | `draft` | Current maturity: `draft` or `final` |
| `version` | integer | no | `1` | Incremented on major revisions |
| `tags` | list | no | empty | Topic tags for discovery |

### Status Values

| Value | Meaning |
|-------|---------|
| `draft` | Spec is in progress or awaiting implementation |
| `final` | Implementation complete, spec reflects actual state |

## Body Structure

The markdown body follows the frontmatter. It starts with an H1
title and contains required and optional sections in the order
shown below.

### Required Sections

#### `# Title`

Human-readable feature name as an H1 heading, followed by a 1-2
sentence overview of what the feature does.

#### `## Problem`

What problem this feature solves. Why it exists. Keep it concise
— 1-3 paragraphs.

#### `## Architecture`

How the feature is structured. Include any combination of:

- ASCII diagrams (runtime flow, data flow, layer diagrams)
- Tables (layer separation, property comparisons)
- Prose explaining key design choices

Use H3 sub-sections to organize (e.g., `### Runtime Flow`,
`### Data Flow`, `### Invariants`).

#### `## Components`

Enumerate the feature's building blocks. Start with a summary
table:

```markdown
| # | Component | Location | Purpose |
|---|-----------|----------|---------|
| C1 | Name | `path/to/file` | One-line purpose |
```

Then provide a detailed section (H2 or H3) for each component
covering:

- **File location** and install path (if applicable)
- **Contract** — inputs, outputs, behavior rules
- **Constraints** — dependencies, performance, compatibility

#### `## Verification`

Criteria that confirm the feature works as designed. Use a table
or checklist:

```markdown
| Component | Criterion |
|-----------|-----------|
| C1 | Description of what must be true |
```

### Optional Sections

Include these between the required sections when relevant.
Place them in the order listed.

| Section | After | Purpose |
|---------|-------|---------|
| `## Build Sequence` | Components | Dependency order for implementation |
| `## Design Decisions` | Verification | Trade-off analysis (see § format below) |
| `## Dependencies` | Scope | External prerequisites |
| `## Configuration` | Components | Settings, environment variables |
| `## Migration` | Design Decisions | Upgrade path from prior state |

### Design Decision Format

Use a numbered `DD-N` prefix for each decision:

```markdown
## Design Decisions

### DD-1: Short title

Description of the choice and why.
**Trade-off:** What you give up.

### DD-2: Another decision

...
```

## Progressive Disclosure

Specs grow from minimal drafts to complete documentation as
implementation proceeds. Only add sections when they become
relevant.

### Stages

| Stage | Frontmatter | Body |
|-------|-------------|------|
| **Draft** | `kind`, `name`, `task`, `created`, `status: draft` | `# Title`, `## Problem`, `## Architecture` (sketch), `## Components` (table only) |
| **Detailed** | + `version`, `tags` | + Component detail sections, `## Verification`, `## Build Sequence` |
| **Final** | `status: final` | + `## Design Decisions`, `## Configuration`, any remaining optional sections |

### Rules

1. **Start with the skeleton** — title, problem statement, rough
   architecture diagram, and component summary table. This is
   enough for a draft.
2. **Add detail during implementation** — flesh out component
   contracts and verification criteria as the design solidifies.
3. **Finalize after verification** — add design decisions, mark
   `status: final`, and increment `version` if revising an
   existing spec.
4. **Keep it current** — when implementation changes, update the
   spec. A stale spec is worse than no spec.

## Relationship to Tasks

### Producing Task

The task that creates the spec is recorded in the `task`
frontmatter field. The producing task also lists the spec in
its `artifacts` field:

```yaml
# In the task's index.md
artifacts:
  - artifacts/specs/feature-name.md
```

### Consuming Tasks

Implementation tasks reference the spec in their Requirements
section:

```markdown
## Requirements

Implement the feature described in
`artifacts/specs/feature-name.md`.
```

There is no formal backlink from the spec to consuming tasks —
the task's own spec carries that reference.

## Example

### Minimal draft

```markdown
---
kind: spec
name: hook-system
task: 0030-design-hook-system
created: 2026-03-15
---

# Hook System

Pre/post tool-use hooks for constraining agent behavior in
autonomous execution.

## Problem

Agents running autonomously can invoke destructive operations.
We need a hook mechanism to intercept and gate tool calls.

## Architecture

(Sketch of hook dispatch flow)

## Components

| # | Component | Location | Purpose |
|---|-----------|----------|---------|
| C1 | Write-path hook | `hooks/validate-write-path.sh` | Blocks writes outside vault |
| C2 | Git safety hook | `hooks/block-destructive-git.sh` | Blocks destructive git ops |

## Verification

| Component | Criterion |
|-----------|-----------|
| C1 | Writes inside vault → allow |
| C1 | Writes outside vault → deny |
| C2 | `git push --force` → deny |
| C2 | `git status` → allow |
```

### Complete spec

See `artifacts/specs/autonomous-execution.md` for a full example
with all sections populated.
