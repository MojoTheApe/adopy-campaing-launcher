---
kind: doc
name: worktrees
---

# Worktrees

Open Station supports running agents in git worktrees. The
`.openstation/` directory is symlinked into each worktree
automatically by Claude CLI, so all worktrees share a single
vault with the main repo.

## How It Works

Claude CLI's `worktree.symlinkDirectories` setting handles
vault sharing. During `openstation init`, the setting is added
to `.claude/settings.json`:

```json
{
  "worktree": {
    "symlinkDirectories": [".openstation"]
  }
}
```

When Claude CLI creates a worktree, it automatically symlinks
`.openstation/` from the main repo into the worktree. This
means:

- The vault (tasks, agents, docs, skills, commands) is shared
  across all worktrees
- Agents work in the worktree CWD (for code changes) while
  task operations go through the shared vault
- No manual setup is needed after creating a worktree

## How `find_root()` Resolves

Single-step resolution with no directory walk-up. Returns
`Path | None`.

```
git rev-parse --show-toplevel
  → _check_dir(toplevel)
  → If .openstation/ exists: return toplevel
  → Otherwise: return None          ← Not an OS project
```

Because `.openstation/` is symlinked into every worktree,
`find_root()` always resolves to the worktree root. Non-git
directories are not supported and return `None`.

## Architecture

### Module Layout

| File | Role |
|------|------|
| `src/openstation/core.py` | `find_root()`, `_check_dir()`, `_git_toplevel()` |
| `src/openstation/init.py` | `_ensure_symlink_directories()` — adds `.openstation` to `worktree.symlinkDirectories` during init |
| `src/openstation/run.py` | `cmd_run()` — captures `exec_cwd` at entry, threads it to all execution paths |

### Key Abstractions

- **`_check_dir(d)`** — checks whether `d / ".openstation"` exists,
  returns `bool`
- **`_git_toplevel(start)`** — runs `git rev-parse --show-toplevel`,
  returns the repo or worktree root
- **`_ensure_symlink_directories()`** — ensures
  `.claude/settings.json` contains `worktree.symlinkDirectories`
  with `.openstation`

### Data Flow

```
Agent invoked in worktree CWD
  → cmd_run() captures exec_cwd = Path.cwd()
  → find_root() resolves vault root (worktree root, with .openstation/ symlinked)
  → Claude session starts with CWD = exec_cwd (worktree)
  → CLI commands resolve vault via find_root() internally
```

## CLI Behavior

All CLI commands work identically in the main repo and in
worktrees — there is no mode distinction.

| Command | Behavior |
|---------|----------|
| `openstation list` | Reads tasks from the shared vault |
| `openstation show` | Resolves against the shared tasks dir |
| `openstation create` | Creates task in the shared vault |
| `openstation status` | Updates task in the shared vault |
| `openstation run` | CWD = worktree, vault = shared `.openstation/` |

The `info:` line from `create` and `status` shows the absolute
path of the modified file, confirming which vault was used.

## Agent Guidelines

- **Always use CLI commands** for task operations — they resolve
  the correct vault automatically
- **Use `$OPENSTATION_HOME`** for direct filesystem access to
  vault artifacts — it resolves correctly in all environments
- **Do not use filesystem checks** (`ls`, `find`, `git status`)
  to verify task operations — use `openstation show <task>`
