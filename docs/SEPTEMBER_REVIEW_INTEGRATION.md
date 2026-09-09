# Integration review — 9 September 2026

The candidate combines the ad-text library, country account table, spend reader,
task board and RPA test changes. Codex reviewed the incoming code and implemented
the corrections below under the owner's instruction to review, fix and merge.
Corrections received self-review and behavioral tests; no independent review of
Codex's corrections is claimed.

Production base: `80136098ac86c8a9e0b3668ba9cd8a67773c2d68` (`master`).
Integration branch: `codex/september-review-fixes`.
Core corrections: `6a1b10e`. The final head and CI results are recorded in the PR.
This document records the review, not a claim of deployment or successful Windows CI.

## Included source commits

All 17 incoming commits are preserved as ancestors, including overlapping refs.
The final account Setup change arrived during review and was inspected separately.

| Source branch | Commits outside the production base |
| --- | --- |
| `feature/ad-text-library`, `feature/builder-ad-texts` | `abd054e`, `3aa45ef` |
| `feature/board-branch-link` | `87a5ba7` |
| `feature/task-board` | `3bd6ec5` |
| `feature/board-merged-state` | `113689b` |
| `feature/board-snapshot` | `b38c26d` |
| `feature/board-full-tests` | `aacb684`, `61fb88c`, `839ce4b`, `08752a9`, `5364787`, `c61e88b` |
| `fix/spend-totals-scroll`, `feature/manage-todo-column` | `292c78a`, `6e61025`, `4ce9c91`, `11cd654` |
| `fix/attach-short-path` | `8129c88` |

Also included: previously prepared collaboration guide
`c39df9d45bd56c398a7705205acf63e48e1330ec`, with shared rules, project workflow,
Claude prompt and a template for future projects.

## Corrections made during review

| Finding and consequence | Correction and evidence |
| --- | --- |
| P1: Builder offered locally edited text, while RPA enrichment read the older Google Sheet value. Deleted or unknown names could silently select another text by substring. | Both consumers use the local registry. Exact normalized lookup rejects missing/ambiguous names before opening a browser. Tests compare actual enriched draft headline, body and description with the edited library. |
| P1: Removing the last text could restore sheet content; a corrupt registry could silently show old copy. | Bootstrap only an uninitialized store. Preserve intentional emptiness and corrupt bytes; API and picker expose errors. Deletion remains effective after navigation and reload. |
| P1: Browser picker and previews retained stale records after editing/deleting. Direct `#ad-texts` navigation opened Home. | Refresh through the library-change event, discard late startup responses, and accept global view hashes. Real dashboard handlers and isolated Chrome scenarios verify edits, selection, preview, deletion, errors and navigation. |
| P2: Sheet import rejected a legitimate row after another row released its old content; invalid names could enter the store. | Validate imported records and update the duplicate index when sheet records change. Keep local edits and tombstones. Display rejected-row reports even when no records were added. |
| P2: Migrating the old copy reader could discard configured sheet/tab overrides. | Preserve legacy environment aliases, key the bootstrap cache by sheet/tab, and invalidate cached success after a failed refresh. Tests exercise configured sources and service-account-to-OAuth fallback without contacting Google. |
| P1: UI allowed building without a tracking link but backend enrichment still rejected a DRAFT. Saving a link also left the UI's saved-value cache stale. | Missing links warn for DRAFT and still reject ACTIVE. Save updates the captured account's preflight value; confirmation describes all warning cases and escapes URLs once. Tests run the real save/confirmation handlers. |
| P1: Board exposed an In master state, but commit handling still called a master merge Shipped. | Master commits mark merged. Parent state follows children and completed work cannot be downgraded by a feature follow-up. Actual rollout remains separate. |
| P2: Board branch attribution could infer one child's branch for unknown siblings; links were not URL-escaped; snapshot writes could replace a valid backup partially. | Require agreement across every child, escape URLs, retain SHA-only commit links, and replace snapshots atomically. A failed replace preserves the previous backup. Sync now tells the owner to stage the snapshot explicitly. |
| P1: Windows short-path attachment checked both spellings, but the manual-process guard checked only the long spelling and could start a second profile. | Check both spellings before startup and refuse startup when process inspection fails. Mocked boundary tests assert that no provider open occurs for an existing manual profile. |
| P2: Added lock tests used a reentrant lock from the owning thread, so missing/leaked locks could pass. | Probe from another thread. Mutation checks confirm that removing lock acquisition or leaking the lock is detected. New path fixtures use synthetic names. |
| P2: Account Setup/readiness lookup could miss an account whose workbook name differed in case. | Normalize lookup and aliases consistently. Tests cover case differences and rendered ready/reason/unknown states, escaping, actions and the new 11-column drill width. |

The incoming spend-scroll change was checked against actual browser layout with
synthetic nested scroll containers. The incoming pacing takeover correction was
retained and covered by the expanded RPA suite.

## Merge conflicts

The old `feature/task-board` commit duplicates branch-link work on an older board
base. Its merge retains current production duplicate-ID guards and status logic;
it does not restore the older implementation. The merged-state and snapshot
conflicts retain both applicable test groups, branch links and snapshot behavior.
The final suite exercises these resolutions together.

## Validation

Environment: macOS, Python 3.12, Node available, isolated `ACL_CONFIG_DIR`,
synthetic records. No live Meta, Google or Multilogin account was modified.

- Integrated incoming baseline before corrections: **945 passed, 1 skipped**.
- The initial 12 regression scenarios failed before corrections and passed after.
- Final suite including `11cd654` and rendered Setup tests:
  `python -m pytest tests/ -q -rs --tb=short` — **971 passed, 1 skipped**.
  The skip is `test_windows_launcher_contract.py`: native Windows PowerShell.
- `python scripts/acl_scan_secrets.py` — passed on explicitly staged files.
- `python scripts/acl_smoke.py` — dashboard and RPA imports passed.
- `git diff --check` — passed.
- Isolated headless Chrome: library hash navigation, edit/save, builder selection
  and preview, tracking save, final-entry deletion and reload, corrupt-file error,
  and spend scroller against real layout all passed; no JavaScript errors.
  Network responses were intercepted and synthetic. Library endpoints used real
  local registry handlers; external integrations were not exercised.
- Source commit ancestry and the production base were checked explicitly.

## Remaining platform checks and release state

GitHub Windows CI must be assessed on the final PR candidate. The latest source
run `34320356731` did not start its Windows jobs because of the repository owner's
Billing/payment or spending-limit restriction. This is not a passed check or a
test failure. The PR records its own result and any explicit owner exception.

The live Windows/Multilogin pilot remains deferred under the owner's earlier
instruction. A Windows tester still needs to verify long/8.3 path attachment,
reading an already-open account without losing its draft, and building a draft
with locally edited copy and a missing tracking link. No final Meta publication
is required for that pilot. Native Windows PowerShell/bootstrap remains untested
in this macOS session.

No release tag, release promotion, installer distribution or rollout is performed
by this integration. Merge status and Windows evidence must not be described as
proof that this version has been installed and tested on users' machines.
