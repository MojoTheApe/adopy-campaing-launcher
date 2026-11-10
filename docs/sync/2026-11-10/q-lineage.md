# TEST9095q lineage vs main - what is not in main (2026-11-10)

Snapshot: `snapshot/2026-11-10-test9095q` (d1941c1d9), worktree `C:\acl-test-9095q`. Main: `work/main` c0cb1a619. Merge-base bd0f67419. 121 commits in the snapshot but not in main, 226 files differ from the merge-base.

## Method (and its limits)

1. For every one of the 226 files, the lines the snapshot added versus the merge-base were checked for presence in main's version of the file (exact-line set test). That gave the "net residue" per file; large residue was then read and attributed to a task.
2. Distinctive symbols were grepped in main (`config_proof`, `amapOpenProfiles`, `amap-meta`, `view-rules`, `hc-after`, `estimate_create_calls`, ...).
3. Task state: PR state via `gh pr view`; tracker status via Issue number = ACL id - 1000 (ACL-1865 = Issue #865).
4. `git merge-tree work/main HEAD` shows about 60 conflicting files. The conflicts are mostly add/add and come from main re-implementing the same tasks on a rebuilt base. They are not evidence of missing content.

Line-set comparison is a heuristic. A file shown as "partly missing" can be a reworded or refactored version of what main has. Where that mattered, main's behaviour was checked directly.

## Summary

| Category | Count | Tasks |
|---|---|---|
| (a) Fully in main (content equal or newer in main) | 25 | 141, 1138, 1184, 1309, 1356, 1377, 1529, 1642, 1739, 1800/1812/1826 (older merge-base chain), 1816, 1824, 1827, 1836, 1838, 1839, 1840 (superseded, see below), 1853, 1855, 1865, 1880, 1886, 1891, 1896, 1899, 1902, 1903, 1907, 1920, plus commit 14f5fc6e9 |
| (b) Partly in main | 0 | - |
| (c) Absent from main | 8 items | 1151, 1379, 1822, 1844, 1872, 1888 (reverted by decision), 1931, 1933, plus the d1941c1d9 report.html change |
| Needs owner decision | 3 | 1931 (PR 932), 1822 (PR 823), lazy video slots (d1941c1d9) |
| Dead (owner closed as not planned) | 6 | 1151, 1379, 1844, 1872, 1888, 1933 |

Result: only three pieces of residue are worth saving. These are ACL-1822 (via open PR 823), ACL-1931 (via open PR 932) and the lazy video slots change. Everything else is either already in main in a different form or was deliberately dropped by the owner.

No secrets and no machine-specific files were found in the residue. Details are in the last section.

## Per-task table

Tracker label = `status:*` label on the Issue (Issue #N = ACL id - 1000).

| Task | Title | PR (state) | Tracker | In main? | Complete? | Dependencies | Recommendation |
|---|---|---|---|---|---|---|---|
| ACL-1822 | Rules T4: Rules page (decision flow editor), Rules cell, Apply via human commands | #823 OPEN (head 49f2185f, updated 23.10, newer than the snapshot's copy; mergeable unknown) | #822 OPEN `status:backlog` | (c) Absent: no `_rules_page.html`, `rules_ui.js`, `view-rules`, rules cell (`hc-rulecell`), sidebar badge; main only has the engine (1816) and `rules_api.py`/`rules_runtime.py` in a different form | Looks complete and reviewed; PR 823 adds mandatory Undo and fixes a portable-JSON detector failure, so it is newer than the snapshot copy (601e5ef0c, 12.10) | Needs 1816 (merged, #820) and 1800 commands (merged). Snapshot hunks sit in `report.html`, `_campaign_table.html`, `app.py` that main has since reworked heavily, so a carry from the snapshot would conflict | Carry via its existing PR 823 (rebase on main). Do not cherry-pick from the snapshot. Needs owner decision whether Rules UI is still wanted (Issue still backlog) |
| ACL-1931 | Dashboard identity: salted, proven per request, checked on reload (follow-up of 1309) | #932 OPEN (head 437d1707 is an ancestor of the snapshot, so the content is fully preserved on GitHub) | no real Issue; #931 is a PR, "1931" is a provisional label (tracker was on hold) | (c) Absent: main has no `config_proof`, `_identity_key`, `_restrict_to_owner`; PR 929 (1309) merged in unsalted form | Complete, review follow-up applied (2 commits) | Stacked on 1309 (#929, now merged). Touches `config/paths.py` (shared file, commit separately), `packaging/windows/local_dashboard.py`, `app.py /health` | Needs owner decision. If wanted: file the real Issue, rebase PR 932 onto main (conflicts in `paths.py`, `app.py`, `local_dashboard.py`). Otherwise leave. |
| ACL-1872 | Isolated Meta app setup: protected per-account credentials, resumable Connect | #873 CLOSED (not merged; head 3033cd66 ancestor of snapshot) | #872 CLOSED not planned, `flag:archived` (30.10 "official Meta route cancelled") | (c) Absent: `src/web/meta_connections.py`, `profile_connection.py`, `config/protected_secrets.py`, `amap-meta` UI, tests, docs | Source review was APPROVED, but the whole route is cancelled | The `amap-meta` block in `report.html`, `_account_map_add.html` and `app.py` are mixed with 1151 | Keep only in snapshot (dead). PR 873 head stays on GitHub if ever revived. |
| ACL-1844 | Official Meta access: read-only one-account HTTP probe | #849 CLOSED (head efebcc75 ancestor of snapshot) | #844 CLOSED not planned, archived | (c) Absent: `official_access_probe.py`, `scripts/acl_meta_access_probe.py`, docs `META_OFFICIAL_ACCESS_PILOT.md`, tests | Complete but obsolete (official Meta route cancelled) | none | Keep only in snapshot (dead). |
| ACL-1379 | Turn a campaign off by exact name on id-less tables | #943 CLOSED (head b2411485, not an ancestor, so the snapshot copy was rebased) | #379 CLOSED not planned, owner decision 25.10: replaced by ACL-1945 (turn-off by id, #1022 merged) | (c) Absent: `_turn_off_by_name`, `refuse_turn_off`, `amap-tree-said`, `match=name`; about 270 lines in `account_spend.py`, 6 snapshot commits | Complete, but review findings were open on the PR | Spread over `account_spend.py`, `app.py`, `report.html`, 4 test files | Keep only in snapshot (dead). Do not carry. |
| ACL-1151 | Account map connects by picking a profile (open profiles listed first, no Reload) | #950 CLOSED (head 7f2bd635) | #151 CLOSED not planned 01.11 ("current behaviour of the map is enough") | (c) Absent: no `amapOpenProfiles`, `/mlx-open-profiles`, `profile_connection.derive`. A related change (ACL-1202, #976 accounts overview finds a profile by name) is merged | Complete | Overlaps the `amap-meta` UI (1872) in `_account_map_add.html` | Keep only in snapshot (dead). |
| ACL-1888 | Automatic never guesses a Facebook Page among several (follow-up of 1880) | #889 CLOSED | #888 CLOSED not planned; owner 14.10: dropped, "needs to be reverted or relaxed" | Absent, by decision. The snapshot's 9e4d6f1c was already reverted inside the lineage (1ae49a0f0) and main has no such rule. 1880 itself is in main (#1064) | n/a | none | Nothing to do. |
| ACL-1933 | Check plan: split "A or B"; " or " / " and " work like a comma | #934 draft, CONFLICTING (base is the 1920 branch) | #933 CLOSED not planned 01.11 ("not worth a separate task") | (c) Absent in `creator._interest_groups`. 1920 was reimplemented in main (`OPTIONAL_LOOKUPS`, `is_permission_refusal`) | Draft, conflicting | Stacked on the old 1920 branch, which main replaced | Keep only in snapshot (unfinished and dropped by the owner). |
| ACL-1865 | ACM loading screen with real stages | #867 MERGED | #865 `status:merged` | (a) | Complete | - | Already in main, nothing to do |
| ACL-1886 | Identify failed Graph reads without request data | #887 MERGED | #886 `status:merged` | (a) | Complete | - | Already in main |
| ACL-1899 | No time limit on file uploads, upload progress | #900 MERGED | #899 `status:merged` | (a) (test 2/450 lines differ, comments only) | Complete | - | Already in main |
| ACL-1184 | Hand-opened profile: one running check | #937 MERGED | #184 `status:merged` | (a) | Complete | - | Already in main |
| ACL-1377 | Account map matches workbook aliases without underscores | #935 CLOSED, but the Issue content merged via #1064 | #377 `status:merged` | (a) | Complete | - | Already in main |
| ACL-1642 | Queue: already-open profile needs no new memory | #936 MERGED | #642 `status:merged` | (a) main's test is longer (722 vs 446 lines), `find_open_profiles` present | Complete | - | Already in main |
| ACL-1739 | Busy tab not counted as frozen episode | #740 MERGED | #739 `status:merged` | (a) | Complete | - | Already in main |
| ACL-1836 | Recover detached browser sessions | #837 MERGED | #836 `status:merged` | (a) | Complete | - | Already in main |
| ACL-1839 | Connection panels within their host | #843 MERGED | #839 `status:merged` | (a) | Complete | - | Already in main |
| ACL-1853 | Share physical profile ownership across ACM processes | #863 MERGED (follow-up #1503 OPEN) | #853 `status:merged` | (a) | Complete | - | Already in main |
| ACL-1920 | Check plan: skip optional lookups Meta refuses | #921 MERGED | #920 `status:merged` | (a) in main as `test_acl1920_optional_refusals.py`; the snapshot's `test_acl1920_search_refused.py` was deleted in main | Complete | - | Already in main |
| ACL-1902 | Auto-suffix campaign name (_2, _3) | #904 MERGED (later #1407 copy counter) | #902 `status:merged` | (a) NAME_2 logic is in main; snapshot test is the older variant | Complete | - | Already in main |
| ACL-1907 | Language groups equal Meta expansion | #908 OPEN, conflicting (superseded by #1143 "rebuilt on main", MERGED) | #907 `status:merged` | (a) via #1143 | Complete | - | Already in main. PR 908 can be closed. |
| ACL-1891 | Videos over 200 MB in chunks | #909 MERGED | #891 `status:merged` | (a) main is newer (ACL-2370 chunk budget, ACL-2000) | Complete | - | Already in main |
| ACL-1896 | Instagram identity check | #949 MERGED | #896 `status:merged` | (a) | Complete | - | Already in main |
| ACL-1880 / 1903 / 1855 / 1816 / 1309 / 141 / 1356 / 1138 / 1529 / 1827 / 1838 | see PR titles | all MERGED (#1064, #906, #927, #820, #929, #939, #942, #947, #530, #829, #842) | all `status:merged` | (a) | Complete | - | Already in main |
| ACL-1840 | Align country campaign footer and journal | #846 MERGED | #840 `status:merged` | (a)/superseded: main removed the footer CSS and `test_country_campaign_footer_layout.py` on 04.11 (cb3a8362e, "remove change-log footnote below the accounts table") | Obsolete | - | Do not carry. Snapshot copy is stale. |
| (no task) | `fix(билдер): страны из account_links доходят до билдера` (14f5fc6e9) | - | - | (a) cherry-picked as f4e19635d (09.11) | Complete | - | Already in main |
| (no task) | `d1941c1d9` report.html: lazy video slots for hidden builder previews (`_cbpHiddenSlots`, `cbpFlushHidden`, `preload="none"`) | none | no Issue found | (c) Absent. Main still has `preload="metadata"` and no deferral in `cbRenderPreviews` (main `report.html`, line ~17108). The change targets the old `cb-slot` previews: CM Comparison took ~24 s to expand because dozens of `<video>` loaded at page open | 20 lines; an uncommitted local experiment, no test | Hunk applies to `cbRenderPreviews`, which still exists in main (moved from ~13527 to ~17076), so it should port with a small rebase. Check on main first that the symptom still exists, because the Manage/Home pages changed a lot | Needs owner decision. If the slowness still occurs: file an Issue, then port as a small PR. |

## Residue not tied to a single task (superseded merge artefacts)

These are snapshot-only lines in files that main has since rewritten. They are not worth carrying.

- `report.html` ad picker (`cbd-ed-ad`, `cbd-ad-off`, `adNames`, `cbd-ed-adlist`). Main uses the ACL-1769 "Ad tab as a stack of cards" editor (commits 038d9e45e, 3bf6a52ee). The snapshot's c47fa5866 "restore adNames" only fixed a merge artefact.
- `creator.py`, `targeting_resolver.py`, `write_policy.py`, `preflight_rules.*`, `form_templates.py`, `token_capture.py`, `instagram.py`, `profile_slots.py` and the matching tests (1774, 1776, 1891, 1896, 1902, 1907, 1642). The snapshot has an older version of the same tasks. Main is newer: `estimate_create_calls` without `size_of`, `OPTIONAL_LOOKUPS`, a longer 1642 test, and so on.
- `docs/system/CHANGELOG.md` and `PROJECT.md` (+683 lines): entries for the dead tasks and for tasks main documents through `docs/system/changelog.d/`.
- `requirements*.txt`, `packaging/windows/{launcher,updater,machine_config,diagnostics}.py` and most tests show 0 to 6 missing lines (comments or wording).

## Secrets and machine-specific files

- No `.env`, token, credential or CSV machine files, and no key-shaped strings, in the added lines.
- `config/protected_secrets.py` is code (protected storage helper for ACL-1872), not a secret.
- The fixture `tests/fixtures/acl1896_doc120_publish_result.json` holds real Doc120 test-object ids (campaign 1202523..., ad set 1202529...). It is identical in main, so nothing new. Synthetic ids like `act_1234567890` are fine.
- `config/paths.py` is a shared file (CLAUDE.md: commit it alone). The ACL-1931 change writes a `.identity_secret` file at runtime in the config dir. The file is not in git, but make sure it is never added.
- `tests/test_meta_connections_windows.py` has a `C:\Users\Example User\...` string. It is synthetic.

## Confidence

- High that the 25 "merged" tasks have no unique content worth saving. Each has a merged PR, a `status:merged` label and symbol-level confirmation in main, or a later main rewrite.
- High that 1151, 1379, 1844, 1872, 1888 and 1933 are absent from main and were closed by the owner.
- Medium for the exact set of snapshot lines that main lacks inside rewritten files such as `report.html`, `app.py` and `creator.py`. Only the symbols listed above were checked.
