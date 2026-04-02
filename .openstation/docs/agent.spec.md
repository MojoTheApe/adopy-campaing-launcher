---
kind: spec
name: agent-spec
---

# Agent Specification

Defines the format for agent specs in Open Station. An agent is
a role definition — a single markdown file with YAML frontmatter
and a markdown body that tells Claude Code who the agent is and
what it can do.

Project-agnostic agent templates live in `templates/agents/` and
are used by `openstation init` to create project agents. See
`artifacts/specs/general-agent-templates.md` for template
authoring guidelines.

## File Location

Agent specs live permanently in `artifacts/agents/`:

```
artifacts/agents/<agent-name>.md
```

Discovery symlinks in `agents/` point to the canonical location:

```
agents/<agent-name>.md  -->  ../artifacts/agents/<agent-name>.md
```

Claude Code resolves `--agent <name>` via the `agents/` directory.

## Naming

- Kebab-case, no numeric prefix (unlike tasks)
- Descriptive of the agent's role: `researcher`, `developer`,
  `project-manager`
- The filename (without `.md`) and the `name` frontmatter field
  must match exactly

## Frontmatter Schema

```yaml
---
kind: agent                 # Required. Always "agent".
name: <agent-name>          # Required. Matches filename (without .md).
description: >-             # Required. 1-line role description.
  <What this agent does.>
model: <model-name>         # Required. Claude model to use.
skills:                     # Optional. Skills loaded at runtime.
  - openstation-execute
tools: <comma-sep list>     # Optional. Human-readable tool summary.
allowed-tools:              # Optional. Tools usable without confirmation.
  - Read
  - Glob
  - Grep
  # ... agent-specific tools ...
  - "Bash(openstation *)"
  - "Bash(ls *)"
  - "Bash(readlink *)"
---
```

### Field Reference

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `kind` | string | yes | -- | Always `agent` |
| `name` | string | yes | -- | Kebab-case, matches filename (without `.md`) |
| `description` | string | yes | -- | 1-line role description |
| `model` | string | yes | -- | Claude model identifier (e.g., `claude-sonnet-4-6`) |
| `skills` | list | no | empty | Skills loaded at agent startup |
| `tools` | string | no | empty | Comma-separated human-readable tool list (informational) |
| `allowed-tools` | list | no | empty | Tools the agent can use without human confirmation |

### Description

- Job title + brief scope in one line
- Example: `"Technical architect for Open Station — designs systems and writes specs"`

### skills

Lists skills loaded at agent startup. All agents using the Open
Station task system must include `openstation-execute`:

```yaml
skills:
  - openstation-execute
```

### allowed-tools

Lists tools the agent can use without human confirmation.
Includes both role-specific tools (e.g., `Bash(pytest *)` for
a developer) and task-system tools (e.g., `Bash(openstation *)`).

Common task-system tools (required for all agents):

```yaml
- "Bash(openstation *)"    # CLI access for task operations
- "Bash(ls *)"             # Directory inspection
- "Bash(readlink *)"       # Symlink resolution
```

## Body Structure

The markdown body follows the frontmatter.

### Required Sections

#### `# <Agent Name>`

Title-case agent name as an H1 heading.

#### Role Statement

1-2 sentences immediately after the H1. Describes WHO the agent
is and WHAT its job is.

#### `## Capabilities`

Bulleted list of what the agent CAN do. Each item describes a
skill expressed in domain terms.

#### `## Constraints`

Bulleted list of behavioral boundaries. Each item describes what
the agent MUST NOT do or conditions it must follow.

### Optional Sections

| Section | Purpose |
|---------|---------|
| `## Technical Expertise` | Domain-specific knowledge areas (e.g., languages, frameworks) |

Optional sections appear between Capabilities and Constraints.

## Progressive Disclosure

Agent specs start minimal and gain detail as the role solidifies.
Only add sections when they earn their token cost.

### Stages

| Stage | Frontmatter | Body |
|-------|-------------|------|
| **Minimal** | `kind`, `name`, `description`, `model` | Role statement, 2-3 capabilities, 2-3 constraints |
| **Full** | + `skills`, `tools`, `allowed-tools` | + Technical Expertise (if needed), refined constraints with concrete paths and delegation targets |

### Rules

1. **Start with identity** — role statement, a few capabilities,
   and the key constraints that define where the role stops.
2. **Add tooling when the agent runs** — `skills`, `allowed-tools`,
   and `tools` are added when the agent is ready to execute.
3. **Refine through use** — observe failures, add the minimum
   instruction to fix them, cut anything the agent handles without
   prompting.

---

## Example

```markdown
---
kind: agent
name: researcher
description: Research agent for gathering, analyzing, and synthesizing information
model: claude-sonnet-4-6
skills:
  - openstation-execute
tools: Read, Glob, Grep, Write, Edit, Bash, WebSearch, WebFetch
allowed-tools:
  - Read
  - Glob
  - Grep
  - Write
  - Edit
  - WebSearch
  - WebFetch
  - "Bash(openstation *)"
  - "Bash(ls *)"
  - "Bash(readlink *)"
---

# Researcher

You are a research agent. Your job is to gather, analyze, and
synthesize information to support decision-making.

## Capabilities

- Search codebases, documentation, and the web for relevant
  information
- Analyze and compare technical approaches
- Produce structured research summaries with clear recommendations

## Constraints

- Always call `openstation` directly — never `python3 bin/openstation`
- Present findings with evidence, not opinion
- Flag uncertainty explicitly — distinguish "confirmed" from
  "likely" from "unknown"
- Keep summaries concise — lead with conclusions, support with
  details
```
