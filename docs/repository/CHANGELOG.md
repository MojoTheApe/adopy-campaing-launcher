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
