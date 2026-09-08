# Windows installer integration — 2026-09-08

Includes all eight commits through `7e223bbdebb2d1e0a19bbd1b461cf75b7f471666`
from `codex/acl-windows-installer-v1`, on the production runtime with the targeting
integration (`6c21adfb68e2a8527dbbe2b53bc085d348064836`, PR #5).
Codex reviewed the incoming changes and self-reviewed the integration fixes;
there was no separate independent review of those fixes.

## Corrections

1. The obsolete `efa636b` runtime freeze prevented integration with master.
   Integrated mode now builds the actual clean reviewed commit; metadata names
   that source, and ancestry still traces to the approved integration baseline.
   Frozen mode remains available and its guard now detects deletions, staged and
   unstaged edits, and moves into the packaging allowlist. Build refuses dirty
   inputs or inconsistent bundle/current/version metadata.
2. Repeat setup preserves existing account/profile/instance/page/proxy mappings,
   extra CSV columns, other accounts and unrelated environment settings. Conflicting
   account identities are refused before any write. Duplicate/spaced/exported
   environment entries cannot override the enforced safety settings. Replacement
   failure rolls back committed files.
3. The wizard retains asynchronous error messages, disables navigation while
   checking, reads Tk fields on the UI thread, and invalidates successful checks
   when inputs change. Finish rechecks the verified inputs. Google authorization
   is staged and committed with the configuration only on Finish; cancellation or
   denial preserves old tokens. A different OAuth client cannot silently replace
   a client with existing shared-write authorization.
4. The launcher honors ACL_CONFIG_DIR, uses pointer-safe Windows mutex handles,
   prevents simultaneous setup/server operation, waits for concurrent startup,
   and binds the exact advertised port. Stop validates executable, creation time
   and server arguments, preventing termination through stale PID reuse. A Start
   Menu stop action allows controlled repair/setup with the backend closed.
5. Packaged dashboard and telemetry use embedded source identity and never offer
   the Git updater, even with inherited ACL_SUPERVISED. Operational telemetry,
   strategy ranks and unknown-target logs use the durable user workspace.
6. Diagnostic exports redact quoted credentials and modern UUIDs, use anonymous
   archive filenames and preserve valid JSON summaries. Files and export size are
   bounded; credentials/configuration files are not included.

## Acceptance and remaining checks

Local checks: `python -m unittest discover -s packaging/windows/tests -p "test_*.py" -v`,
`python scripts/acl_scan_secrets.py`, `python scripts/acl_smoke.py`,
`python -m pytest tests/ -q`. CI checks the branch and current PR integration;
Windows runtime tests run on Python 3.11 and 3.13. Installer CI builds a PyInstaller
onedir bundle and an Inno Setup per-user executable, tests imports, HTTP health,
embedded version and local targeting, then install/launch/repair/uninstall and
preservation of existing configuration files. Counts and run links are in the PR.

All automated fixtures are synthetic. No live Google writes, Multilogin profile
operations or campaign actions were performed. The owner explicitly deferred the
manual Windows/Multilogin pilot until after merge: exercise the full wizard with
company access, connect a profile, read account data, and create/inspect a draft
including narrowing without destroying an existing draft. Test on a real user
machine without Python/Git as well as an existing installation.

This is an unsigned Windows preview, not a promoted fleet release. Automatic
updates are disabled and no `acl-release-*` tag is created by merging it. Signing,
SmartScreen behavior and real Windows pilot acceptance remain release tasks.
