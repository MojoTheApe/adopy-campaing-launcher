---
name: openstation.progress
description: Append a progress entry to a task. $ARGUMENTS = <task-name> <message>. Use when user says "log progress", "record progress", or wants to add a timestamped work entry.
aliases: []
id: openstation.progress
tags: []
---

# Append Progress Entry

Add a timestamped `## Progress` entry to a task file. Entries
are append-only — previous entries are never modified or removed.

## Input

`$ARGUMENTS` — the task name followed by the progress message.

Example: `0042-add-login-page Implemented auth flow and added tests`

The task name can be either the full ID-prefixed name (e.g.,
`0042-add-login-page`) or just a numeric prefix (e.g., `0042`).

## Procedure

1. Parse the task name (first argument) and message (remaining
   arguments) from `$ARGUMENTS`.
2. If no message is provided, ask the user for a progress summary.
3. Resolve the task file per `docs/task.spec.md` § Task Resolution.
4. Determine the author name:
   - If running as an agent, use the agent's `name` field from its
     frontmatter.
   - Otherwise, default to `user`.
5. Build the progress entry:

   Format with metadata blockquote:

   ```markdown
   ### YYYY-MM-DD — <author>
   > time: HH:MM–HH:MM
   > log: [[artifacts/logs/<task-name>]]
   > worktree: <worktree-name>

   <message>
   ```

   Use today's date in ISO 8601 format. Metadata line rules:
   - `time:` — use `HH:MM–HH:MM` range when both start and end
     are known; use single `HH:MM` (current time) otherwise
   - `log:` — Obsidian wikilink to the session log in
     `artifacts/logs/`
   - `worktree:` — the worktree directory name if work was done
     in a git worktree (detect via `git worktree list` or check
     if the current directory differs from the main working tree)
   - Omit any metadata line when its value is not available.
     Omit the blockquote entirely when no metadata is available.
6. Locate the `## Progress` section in the task file:
   - If it exists, append the new entry after the last existing
     entry (preserve a blank line between entries).
   - If it does not exist, create a `## Progress` section and
     place it before `## Findings`. If `## Findings` does not
     exist either, place it before `## Verification`.
7. Never modify or remove existing progress entries.
8. Confirm what was appended: show the task name, date, author,
   and message summary.
