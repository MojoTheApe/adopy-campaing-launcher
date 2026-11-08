# Optional lossless state archive

This is an operator tool for a project's large private Tracker state. New projects
keep their existing state format and need no archive configuration. Archiving
changes storage only: it does not finish tasks, free occupied places, release
owners, clear blockers or supply review, acceptance or product delivery.

## Compatibility and activation

Publish and install a compatible central reader first. Upgrade every actual
project writer, including the author client and CI receipt helpers, before live
activation. The exported contract advertises `state_archive:[1]`; outer format9
preserves the incumbent state format and its existing guards. Old writers reject
format9. Publishing helpers alone does not activate an archive.

Use the existing Maintainer-owned idle maintenance window. With the same actual
owner, session and claim, preview `maintenance archive` and inspect its byte counts
and exact logical-state preservation. Apply with `--expected-archive` set to that
preview's binding and `--apply`. A changed state or main requires a fresh preview.
Resume maintenance with the same owner even if activation fails. Never clear
another owner's operation to obtain an idle window. The command's existing CLI
help supplies the selected repository and maintenance identity arguments.

## What remains available

Closed records and old history prefixes move to an immutable
`.tracker-archive/<sha256>.json` blob on `tracker-state`. The live state retains
current work, ownership and guards, a verified archive pointer and recent events.
Every semantic reader restores the complete original logical state before making
decisions. Closed and archived tasks retain their actual status; neither becomes
Done through physical archiving. Original history and previous state blobs remain
reachable through Git history.

Native commands, exported PR/receipt readers, CI gates and central projection use
the same decoder. CI reads the archive selected by its immutable state commit,
even after later archive rotation. Modified historical records have explicit
whole-record overrides; deletions have explicit tombstones. Retained history is
append-only. Conflicting overlays, history rewrites, missing or corrupt archives
fail rather than returning partial or empty state.

## Size, cache and writes

The encoder normally rotates above750KiB and requires a live representation at
most1MiB. Active data that cannot fit causes a measured refusal without discarding
records. Each immutable archive is bounded at48MiB. A verified private cache at
`$XDG_CACHE_HOME/task-tracker/state-archive` (or the user's `.cache` directory)
avoids repeated archive downloads. Cache absence or corruption cannot establish
authority; cold reads still fetch and restore the full history.

Ordinary writes retain the existing file-SHA compare-and-swap check. Rotation
commits archive and live state together against a fresh branch tree, then advances
the branch without force. A neighboring activity-file update is preserved. A real
state change aborts; a lost write response is reconciled by supported readback,
never a blind replay. Each native store retains codec context for the actual
repository and state blob rather than a shared mutable latest-snapshot pointer.

No application package release or repeated package acceptance is inherent in
archive activation. Existing product publication obligations remain unchanged.
