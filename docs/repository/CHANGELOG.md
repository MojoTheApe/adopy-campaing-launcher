# Repository process history

## Prepared adoption — 2026-09-21 / ACL-1248

Adopt Master Repo's separated repository/system/runtime documents and pinned shared
Tracker helpers. Preserve ACL IDs/modules and the legacy entry points. Prepare package
completion, independent review, stage/main routing and a guarded master-channel cutover.
Keep original instructions in history and the pristine template baseline for future
three-way upgrades. No live branch, board, employee install or release changes yet.

Why: the owner wants one shared engineering process while retaining the Windows
installer and automatic updates. Existing open PRs, unavailable hosted checks and
unverified package/stage operation are explicit blockers to activation, not discarded
requirements. See ADOPTION.md for current evidence and the registry companion.

## Hosted verification resumed — 2026-09-21 / ACL-1248

Hosted Actions now executes jobs. Its first real Windows run exposed an old
origin/master reference in the isolated bootstrap test repository. Align that
fixture with the candidate's main release channel so Windows exercises the actual
annotated-tag and ancestry gates. Keep the production compatibility bridge and
remaining activation criteria unchanged; local macOS skips did not prove this test.

## Preserve accepted history and verify current packaging — 2026-09-21 / ACL-1248

The owner chose to retain 74 completed tasks during adoption. Pin reviewed TT-61
and Master MR-9, record that exact audited scope, and prepare the matching registry
configuration. Fresh projection preserves completion/dependency/release counts while
marking history separately from verified delivery. The pristine template baseline
is re-rendered from the new pin without inserting ACL's local history exemption.

Hosted installer checks found two older test mismatches: direct-file imports lacked
the repository root, and the bundle smoke test expected the updater contract from
before ACL-1240. Correct those checks and retain source/version assertions with an
offline process. No product updater, employee installation or release is changed.

## Actual package update/recovery acceptance — 2026-09-21 / ACL-1248

Extend Windows CI with two real test installers built from the candidate source.
The installed frozen helper performs an update, restart and controlled recovery;
the harness checks actual version health, current pointer, every application
shortcut and unchanged synthetic machine/workspace/creative files. This closes
the gap between unit fixtures/install-repair checks and an actual updater cycle
on an isolated runner. Reports identify source/tree and installer hashes. They do
not claim a published release/download channel or the deferred live account pilot.

## Coordinated workflow activation — 2026-09-21 / ACL-1248

Integrate the reviewed migration, create main/stage and retain the old master
compatibility ref. Publish the matching Tracker registry through its guarded
installer; verify six snapshots, 160 ACL identities, seven columns and all 74
historical completions with existing authentication intact. Record these actual
facts in the descriptor and adoption record, then activate through normal independent
review, stage package acceptance and unchanged-content main promotion. Preserve the
unpublished standard's draft status and separate stable-package delivery requirement.

Guard regressions now use an explicit prepared fixture, so activating the real
repository cannot accidentally turn a test into a live Tracker command. The read-only
adoption audit follows current main after activation, retaining original master as
provenance rather than testing its frozen compatibility channel as current source.
