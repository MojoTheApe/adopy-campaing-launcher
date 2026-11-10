# ACL local version → main sync, 2026-11-10

Status: **sync with main is NOT complete.** The prepared candidate is reviewed and green, but it cannot enter `main` through the allowed route while integration batch `d13e429edaf84d19b2d16755417b8518` (PR #1501) is held.

## 1. Snapshots (GitHub, MojoTheApe/adopy-campaing-launcher)

| Branch | Commit | What it is |
|---|---|---|
| `snapshot/2026-11-10-test9095zzzc` | `19e999e50` | Current local version (served on TEST9095 since 12:50 local): main `c0cb1a619` + PR 1507 (ACL-2506) + PR 1505 (ACL-2504) + PR 1503 (ACL-2502) + PR 1490 (ACL-2369). This is also the sync candidate. |
| `snapshot/2026-11-10-test9095q` | `d1941c1d9` | Previous test-build lineage (14f5fc6e, base 2026-10-16) + one previously uncommitted `report.html` change, committed so it is not lost. |

Other local copies checked:
- Port 9090 runs the installed release `2026-10-27.1240` (source `bff41b374`, an ancestor of main). Nothing to carry.
- TEST9095zzzb (`444f3a607`, served until today): ancestor of main, 0 commits outside main.
- `C:\fb_agents` checkout: one commit outside any remote (`14f1b5593`, Remove hides the account); its content is in main as ACL-2373 (`7dfd50d23`, same test file). Untracked files there are coordination notes/specs/probe outputs, not product code; kept locally, not pushed (some name live accounts).
- No secrets or machine files in the pushed residue (account ids present are synthetic `act_1234…`).

## 2. Comparison

Candidate vs main: 4 source PRs, all complete, no conflicts. Main changes missing locally: none (candidate is built on current main).

Old lineage (`9095q`) vs main, by content (details: `q-lineage.md`):
- ~25 tasks fully in main (1184, 1377, 1642, 1739, 1836, 1839, 1853, 1865, 1886, 1899, 1920, …).
- Closed "not planned" by the owner, kept in snapshot only: ACL-1151, 1379 (→ACL-1945), 1844, 1872, 1888, 1933.
- Stale: ACL-1840 footer (removed from main on purpose 2026-11-04).
- Absent from main and still alive: ACL-1822 (Rules page, open PR #823, Issue backlog), ACL-1931 (dashboard identity, open PR #932, no Issue filed), `d1941c1d9` (lazy video slots, no Issue, untested).
- PR #908 (ACL-1907) is superseded by #1143 and can be closed.

Dependencies: none between the four candidate PRs and the old-lineage residue; the residue is not needed by the candidate.

## 3. Checks of the candidate (`19e999e50`)

- Full CI, run 42811782657 (workflow_dispatch on the snapshot branch): success — all Linux shards (3.11/3.13), Windows tests, tooling, process instructions, ACL verification. macOS smoke skipped (advisory).
- Affected test files locally: 294 passed; reviewer's wider affected set: 460 passed (Windows).
- Independent review (separate reviewer, not the implementers): APPROVE for the combination and each PR (`candidate-review.md`). Non-blocking findings: F1 (owner decision on ACL-2369 scope), F2–F5 (low).
- Note: this review is not recorded in the tracker. Tracker `record-review` needs green CI on each source PR, and #1490/#1503/#1505 are red only because `tests/test_acl1448_israel_time.py::test_dates_are_a_period_first_day_from_midnight_last_day_inclusive` fails on main today (verified locally on `c0cb1a619`); its fix is ACL-2506, which can only reach main in a batch.

## 4. Blocker (exact)

Integration batch `d13e429e…` (PR #1501, merged into main `c0cb1a619` 2026-11-09 19:38 UTC; members Issues 1495, 1491, 862, 1419) and its intake `3baed735…` are held. Issue 862 (ACL-1862 part 1) has six unchecked criteria that belong to the next PR (ACL-2502), so batch acceptance cannot be recorded truthfully. Refusals (previews, nothing applied):

| Operation | Refusal |
|---|---|
| `batch intake --issues 1506 --actor Coordinator-M1031A --session coord_m1031a_20261031` | `Stale or foreign claim; inspect execution show and resume the owning run` (earlier the same day: `Finish the active integration batch first`) |
| `merged ACL-2495 ACL-2491 ACL-1862 ACL-2419 --pr 1501` | `Acceptance evidence is missing or stale for the exact batch/owner` |
| `batch close-sources --batch-id d13e429e…` | `Only a completed admitted batch can close superseded source PRs` |

Required resolution: engine recovery for a merged batch with a declared partial member — MojoTheApe/task-tracker#270 (open, unassigned). Not done: no tracker state rewrite, no checkbox edits, no bypass.

Order once unblocked: close d13e429e → release → batch with ACL-2506 → merge main into #1490/#1503/#1505 → CI → tracker reviews → owner `batch approve` → next batch → release.

## 5. Backlog revision (decisions for the owner; nothing was closed or moved)

| Task | Done | Evidence | Remaining | Recommendation |
|---|---|---|---|---|
| ACL-2495 (Issue 1495) | Removed accounts no longer reach the Builder | In main via #1501; batch review | Batch acceptance + release | **Done candidate** after release |
| ACL-2419 (Issue 1419) | Home status from Graph API `account_status` | In main via #1501; batch review | Batch acceptance + release | **Done candidate** after release |
| ACL-2506 (Issue 1506) | Date-independent Lifetime/Israel test | Approved; candidate CI green; review APPROVE | Batch → main → release | **Done candidate** after merge |
| ACL-2504 (Issue 1504) | Builder target substitution resets hidden code/hint | Candidate CI green; review APPROVE | Tracker review + approve (needs PR CI green after ACL-2506 in main), batch | **Done candidate** after route |
| ACL-2491 (Issue 1491) | Interest lookup via ad-account search | In main via #1501; live check on Doc283 done | Live criterion recordable only in batch acceptance; release | **Done candidate** after release + record |
| ACL-1862 (Issue 862) | Part 1: lease for reads/writes/warm cleanup | In main via #1501 | Six criteria carried in ACL-2502 | **Partial** — finish through ACL-2502; parent stays open |
| ACL-2502 (Issue 1502) | G1 checkout fails closed; G4 leak watch on read/write/publish | Candidate CI green; review APPROVE | Subprocess tests through real callers and mixed-path pairs only partly covered (F2/F3) | **Partial** — add the missing tests now, or split them into a follow-up |
| ACL-2369 (Issue 1369) | Memory gate for warm profiles | Candidate CI green; review APPROVE | F1: Check plan on a warm profile is still memory-gated when another action runs; the Issue says open-profile actions are not gated | **Needs clarification** (owner reading of scope) |
| ACL-1822 (Issue 822) | Rules page in PR #823 (open) | Not in main | Whole task (backlog) | **Future development** |
| ACL-1931 (no Issue) | Dashboard identity in PR #932 (open) | Not in main | Issue never filed; touches shared `config/paths.py` | **Needs clarification** — file a task or drop |
| `d1941c1d9` (no Issue) | Lazy video slots in hidden builder previews | Snapshot only, untested | Confirm slowness on main; task, tests | **Needs clarification** |
| ACL-1907 PR #908 | Superseded by #1143 (in main) | q-lineage analysis | Close the stale PR | Housekeeping |
| ACL-1151, 1379, 1844, 1872, 1888, 1933 | — | Issues closed "not planned" | — | Nothing; kept in snapshot only |

Needs a release or a practical check: every row above that reaches main needs the next release. ACL-2491 already has its live check; ACL-2502 and ACL-2369 change profile lease and memory gating and should get one owner run on an allowed test account after release.
