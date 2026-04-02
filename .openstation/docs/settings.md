---
kind: spec
name: settings
---

# Settings

Project-level configuration for Open Station. Settings control
runtime behavior such as lifecycle hooks.

## File Location

| Context | Path |
|---------|------|
| Installed project | `.openstation/openstation.yaml` |
| Source repo | `openstation.yaml` (vault root) |

If the file is missing, Open Station runs with defaults (no
hooks, no overrides).

## Format

YAML document with top-level keys:

```yaml
hooks: ...
defaults: ...
```

## Keys

| Key | Type | Description | Details |
|-----|------|-------------|---------|
| `hooks` | object | Lifecycle hooks that run on status transitions | [hooks.md](hooks.md) |
| `defaults` | object | Default flag values for CLI commands | See below |
| `run` | object | Settings for `openstation run` (detached backend, tmux modes) | See below |
| `verify` | object | Verification settings | See below |

Keys not listed above are ignored.

## `defaults`

Maps command names to default flag values. When a flag is not
explicitly passed on the command line, the corresponding default
is applied.

### Schema

```yaml
defaults:
  <command>:
    <flag>: <value>
```

**Command keys** use the subcommand name directly for top-level
commands (`show`, `list`, `status`, `run`, `create`) and dot
notation for nested commands (`agents.show`, `agents.list`,
`artifacts.show`, `artifacts.list`).

**Flag names** match the argparse attribute (long form, dashes
replaced with underscores): `editor`, `json`, `quiet`, `status`,
`assignee`, `dry_run`, etc.

**Values** are the default to apply — `true`/`false` for boolean
flags, strings for value flags.

### Scoping: Human-Only

Defaults apply **only in human CLI context**. When the
`CLAUDECODE` environment variable is set (indicating an agent
session), all defaults are skipped. This prevents defaults like
`editor: true` from interfering with agent automation.

### Override Precedence

Explicit CLI flags always override defaults:

```
explicit CLI flag  >  settings default  >  argparse default
```

If a user passes `--json` and the default says `editor: true`,
`--json` wins — the default for `editor` is not applied. This
also respects mutually exclusive flag groups.

### Examples

Open tasks in the editor by default:

```yaml
defaults:
  show:
    editor: true
```

Default list to ready tasks only:

```yaml
defaults:
  list:
    status: ready
```

Multiple commands:

```yaml
defaults:
  show:
    editor: true
  agents.show:
    editor: true
  list:
    status: ready
```

## `run`

Settings for `openstation run`.

### Schema

```yaml
run:
  detached_backend: tmux
  tmux:
    mode: session
    target_session: os
```

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `detached_backend` | string | `"tmux"` | Backend for `-d`/`--detached`. Validated against registered backends at dispatch time. |
| `tmux.mode` | enum | `"session"` | Tmux launch mode: `session` (new detached session per run), `window` (new window in target session), or `pane` (split pane in target session) |
| `tmux.target_session` | string | `"os"` | Target tmux session name for `window` and `pane` modes. Ignored in `session` mode. |

The `--tmux-mode` CLI flag overrides `tmux.mode` for a single
invocation without changing `openstation.yaml`.

## `verify`

Settings for `openstation run --verify`.

### Schema

```yaml
verify:
  agent: <agent-name>
```

| Key | Type | Description |
|-----|------|-------------|
| `agent` | string | Default agent for `--verify` mode. Used when the task `owner` is `user` or empty and no `--agent` flag is given. |

### Agent Resolution Order

When `--verify` resolves the verification agent, it uses this
precedence (highest to lowest):

1. `--agent` CLI argument
2. Task `owner` field (skipped if `user` or empty)
3. `settings.verify.agent` (project-level default)
4. Hardcoded fallback: `project-manager`

### Example

```yaml
verify:
  agent: reviewer
```

With this setting, `openstation run --task 42 --verify` uses
the `reviewer` agent when the task's `owner` is `user`.

## Example

Settings file with a hook, defaults, run config, and verify agent:

```yaml
hooks:
  StatusTransition:
    - matcher: "*→done"
      command: bin/hooks/auto-commit
      phase: post
      timeout: 120

defaults:
  show:
    editor: true

run:
  detached_backend: tmux
  tmux:
    mode: window
    target_session: os

verify:
  agent: project-manager
```

See [hooks.md](hooks.md) for the full hook schema, matchers,
environment variables, and execution model.

## Architecture

### Module layout

| File | Role |
|------|------|
| `src/openstation/hooks.py` | Settings loading (`load_settings`), hook loading and execution |
| `src/openstation/cli.py` | Default application (`_apply_cli_defaults`), argv scanning (`_explicit_flags`) |

### Integration points

- `hooks.load_settings(root)` reads and parses
  `openstation.yaml` from the vault root, returning the full dict.
  Used by both the hook system and the defaults system.
- `cli._apply_cli_defaults(args, settings)` is called in
  `main()` after argparse and `find_root()`, before command
  routing. Guarded by `CLAUDECODE` env var check.
- `cli._explicit_flags()` scans `sys.argv` to determine which
  flags were explicitly passed, preventing defaults from
  overriding user intent in mutually exclusive groups.

### Data flow

1. `main()` calls `parser.parse_args()` → `args` namespace
2. `find_root()` locates the vault → `root`
3. If `CLAUDECODE` is not set:
   a. `load_settings(root)` reads `openstation.yaml`
   b. `_command_key(args)` derives the lookup key (e.g. `"show"`)
   c. `_explicit_flags()` scans `sys.argv` for user-provided flags
   d. `_apply_cli_defaults()` merges unset flags from settings
4. Command handler runs with merged args
