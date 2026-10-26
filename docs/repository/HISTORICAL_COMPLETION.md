# ACL-1248: audited historical completion

Current cutover verification preserved this exact 74-task manifest in the published
board; see [ADOPTION.md](ADOPTION.md). The audit below is a retained historical
snapshot, not a current unpublished-state notice. No task list or accepted-scope
hash was changed at publication.

## Historical preparation snapshot

Read-only preview captured on 2026-09-21. No live Issue labels, completion records or board configuration have been changed.

Source: current ACL Issues, original tracker/config.json from master 6e871edd425afe1c74b955d7ba7b9793e06a7022, prepared config from PR #249, and compatible Tracker ea969826398a808925bbd907ca8719445177ab59. The current execution-state snapshot contains no delivery_tasks evidence. The same compatible engine renders both configurations, isolating the effect of the policy change.

## Observed impact

- 157 tasks are visible in the source inventory, including archived items.
- Original rules count 74 tasks as completed; the prepared package policy without migrated evidence counts 0.
- Public IDs, statuses, modules and archived flags remain identical for all 157 tasks.
- Current release scope: 10 tasks, 7 completed under old rules; without evidence migration it becomes 0 of 10 completed.
- Existing dependency #175 -> #170 remains unresolved; #23 -> #124 would additionally become unresolved.
- Neither configuration reports the release ready.

## Owner decision and verified candidate

On 2026-09-21 the owner explicitly selected: preserve the 74 audited historical completions; apply new requirements to new work. No old review, stage, release or installation receipts are asserted.

The reviewed Task Tracker extension is TT-61 / PR #62, commit 5f211d3095a392a16c2c01a65eb02ff8719a7243. Master Repo MR-9 pins that compatible implementation. The ACL candidate stores an explicit `historical_completion` allowlist in `tracker/config.json`, bound to the original config, original source revision, target policy and each Issue's accepted scope/state. The list exactly matches the original 74 records below; it was not enlarged from a later audit.

A fresh read of all 157 tasks verifies: 74 complete before and after adoption; current release 7 of 10 complete; the same unresolved dependency #175 -> #170; release remains not ready. IDs, statuses and classifications remain preserved. A historical task is labelled Completed before adoption, never Delivery verified. New/incomplete tasks stay subject to review, staging and stable-package requirements. Changed scope or restarted work cannot borrow historical acceptance; release membership changes do not erase it.

At the preparation checkpoint this was reviewed candidate configuration only. The
recorded plan was: it becomes active after the compatible Tracker is published and ACL's coordinated cutover prerequisites are fulfilled. Before cutover, compare fresh Issues with this frozen list and report differences; do not regenerate it automatically or force old counts green.

## Later work remains outside the frozen list

After old-PR reconciliation the inventory contains 160 tasks. ACL-1250 is the new
source-integration follow-up merged through #247; ACL-1251 and ACL-1252 preserve
separate unfinished work. None was added to this manifest. The original 74 accepted
records remain fixed. Under legacy rules ACL-1250 counts as source-complete; under
the adopted package policy its Merged label alone is delivery-pending. This is an
expected new-task distinction, not lost historical completion. Release membership
is unchanged. Fresh comparison must report any other drift before cutover.

## Tasks whose completed count changes

| Task | Accepted status | Included in current release | Title |
| --- | --- | --- | --- |
| [ACL-102](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/12) | merged | No | Account map: spend, leads, CPL, CPA, CR by country |
| [ACL-105](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/14) | merged | No | Timeframe that survives real use |
| [ACL-108](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/16) | merged | No | Read cost through the RPA |
| [ACL-113](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/23) | merged | Yes | Open and close through the RPA |
| [ACL-117](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/25) | merged | Yes | Every open and close leaves a record |
| [ACL-348](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/40) | merged | No | The builder belongs to an account, not to a recommendation |
| [ACL-347](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/41) | merged | No | A missing tracking link warns before upload, it does not forbid |
| [ACL-342](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/42) | merged | No | Publishing no longer needs `Upload FB.xlsx` |
| [ACL-343](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/43) | merged | No | An empty tracking link must warn, not block |
| [ACL-344](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/44) | merged | No | Cover the decisions a run is lost on |
| [ACL-334](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/50) | merged | No | Ad titles are a section, not a tab in someone's spreadsheet |
| [ACL-336](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/52) | merged | No | The builder takes its ad titles from the library |
| [ACL-304](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/61) | merged | No | Install in one file |
| [ACL-312](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/63) | merged | No | Telemetry on by default |
| [ACL-650](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/65) | merged | No | One store, two views: Obsidian and a wall |
| [ACL-653](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/66) | merged | No | Next to "In review" — the branch and the commit |
| [ACL-654](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/67) | merged | No | "In master" is a state of its own |
| [ACL-655](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/68) | merged | No | The plan itself has a history |
| [ACL-145](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/74) | merged | No | The map joins the workbook by name, and the names are drifting |
| [ACL-703](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/76) | merged | No | Eleven doors into one country, and four lead to the same room |
| [ACL-704](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/86) | merged | Yes | The builder's Diagnostic preview — compact, collapsible, beside the ad cards |
| [ACL-353](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/93) | merged | Yes | "Read all" on the account map |
| [ACL-354](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/94) | merged | Yes | Report a problem from the launcher — a CM's ticket lands with us |
| [ACL-1095](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/95) | merged | No | Connect ACL to the shared task tracker |
| [ACL-705](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/98) | merged | Yes | Duplicate ad belongs on the ad card, not under the list |
| [ACL-1099](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/99) | merged | No | Adopt tracker v3 IDs, Release and mandatory task intake |
| [ACL-1102](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/102) | merged | No | Reduce duplicate CI and scope ACL Windows checks to relevant changes |
| [ACL-1104](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/104) | merged | No | Duplicate campaign belongs on the campaign card — and carries the language |
| [ACL-1108](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/108) | merged | No | Grant evgenis-ctrl read access to MojoTheApe/task-tracker |
| [ACL-1110](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/110) | merged | No | A collapsed country row carries its numbers — account-read spend and BI totals |
| [ACL-1113](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/113) | merged | No | The RPA read splits an account's cost by country — from the account itself, not the source |
| [ACL-1115](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/115) | merged | No | Adopt readable task descriptions in the project workflow |
| [ACL-1117](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/117) | merged | No | Adopt Description and Specifications for project tasks |
| [ACL-1119](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/119) | merged | No | Reject unsafe or incomplete country spend readings |
| [ACL-1124](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/124) | merged | Yes | Read an account's ad sets and ads with their ids and on/off state |
| [ACL-1126](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/126) | merged | No | Two tests blink in a full run |
| [ACL-1128](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/128) | merged | No | An empty period says whose emptiness it is, and the builder test stops reading the machine |
| [ACL-1130](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/130) | merged | No | Connecting an account is one job, not two panels |
| [ACL-1134](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/134) | merged | No | Four things the builder let a person do wrong |
| [ACL-1135](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/135) | merged | No | Assemble campaigns from a drawer open on every tab |
| [ACL-1140](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/140) | merged | No | Keep the screen awake while a publish runs |
| [ACL-1141](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/141) | merged | No | Say that the screen was locked instead of a blank failure |
| [ACL-1146](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/146) | merged | No | A failure should leave its text behind |
| [ACL-1150](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/150) | merged | No | Hide accounts this machine has nothing to do with |
| [ACL-1155](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/155) | merged | No | A country with no accounts has nothing to connect one with |
| [ACL-1156](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/156) | merged | No | The installer reopens itself until the windows pile up |
| [ACL-1158](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/158) | merged | No | The setup block eats the drawer when the account is not configured |
| [ACL-1160](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/160) | merged | No | Accounts tab connects an account the way the country section does |
| [ACL-1161](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/161) | merged | No | One hiccup in eighteen requests loses the whole profile list |
| [ACL-1162](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/162) | merged | No | Manual placements not applied on accounts served the newer section markup |
| [ACL-1164](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/164) | merged | No | Detailed targeting never reaches the campaign: the form sends interests where a code is expected |
| [ACL-1166](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/166) | merged | No | Heavy creative: no file input exists in the page tree — the media picker is out-of-process |
| [ACL-1171](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/171) | merged | No | Do not upload the same creative twice - take the copy already in the library |
| [ACL-1173](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/173) | merged | No | Журналы теряют записи при одновременной работе (Windows append не атомарен) |
| [ACL-1189](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/189) | merged | No | Compress a heavy creative before upload instead of the special path |
| [ACL-1192](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/192) | merged | No | Library pick never succeeds: the success test assumes an empty grid |
| [ACL-1196](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/196) | merged | No | An account linked to a country on the dashboard still shows no country |
| [ACL-1198](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/198) | merged | No | Profile names and ids come from Multilogin itself, not from window titles |
| [ACL-1200](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/200) | merged | No | An open profile is never handed to the MLX automation API |
| [ACL-1203](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/203) | merged | No | Stopping an open profile through the MLX API answers 404; the kill fallback carries the run |
| [ACL-1205](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/205) | merged | No | Setup wizard test file lets pytest collect test_multilogin_access as a test |
| [ACL-1207](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/207) | merged | No | A fresh upload that opens the creative wizard is reported as failed, and the open wizard breaks every step after it |
| [ACL-1208](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/208) | merged | No | Age is left at 18-65+ when Advantage+ audience is still on at the age step |
| [ACL-1210](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/210) | merged | No | Publish several accounts at once instead of one after another |
| [ACL-1214](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/214) | merged | No | The RPA sets and checks the driven window size instead of inheriting whatever the operator tiled |
| [ACL-1216](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/216) | merged | No | Writing account links fails with WinError 5 when Windows still holds the file |
| [ACL-1219](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/219) | merged | No | Mix targets use Define further for AND groups, and the default country is really removed |
| [ACL-1220](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/220) | merged | No | Placements are set manually under Show more settings on the new ad set card |
| [ACL-1221](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/221) | merged | No | Ad-level steps report what the screen shows: ad copy, extra empty ad, destination sweep, budget strategy |
| [ACL-1223](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/223) | merged | No | Spend read refuses day-first dates like 16 Sep 2026, and its errors are not in English |
| [ACL-1225](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/225) | merged | No | Age step clicks the Start time hour 18 instead of the minimum age |
| [ACL-1226](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/226) | merged | No | Country is not added when location suggestions do not open |
| [ACL-1234](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/234) | merged | No | Read spend restarts a manually opened profile the way Publish does |
| [ACL-1240](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/240) | merged | No | Deliver updates as GitHub Release archives with an in-dashboard Update button |

## ACL-1250 amendment (2026-10-26)

Tracker task: ACL-2200 (Issue #1200). The owner approved recording ACL-1250 (Issue #250,
"Verify and finish collapsed audience fields and age readback") as historically completed,
shown as **Done · Accepted before adoption**: [Issue 250 comment 6400546760](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/250#issuecomment-6400546760)
(2026-10-25), repeated on 2026-10-26. This is not a claim of package delivery under the
current policy. PR #247 merged on 2026-09-21 as 79caea74d4f88c559d79caf6fc86ef9b7b43f9e8 and its code first shipped in
`acl-release-2026-09-23`; Issue #250 has no tracker execution record.

The engine's `audit-history` refuses to extend an existing manifest, so this is a reviewed,
hand-merged amendment of `historical_completion` in `tracker/config.json`:

- Only Issue #250 is added; the manifest now lists 75 tasks. The 74 original rows are
  byte-identical (their canonical-JSON SHA-256 is pinned in
  `tests/test_historical_completion_manifest.py`). A fresh read-only `audit-history` run
  against current Issues reproduces every one of them.
- The manifest records one top-level provenance block, so it now describes the latest
  audit. The original provenance of the 74 rows is preserved here: audited
  2026-09-21T10:55:31.841147+00:00; owner approval of 2026-09-21 recorded in the ACL-1248
  adoption PR; original pre-policy configuration from master
  `6e871edd425afe1c74b955d7ba7b9793e06a7022`, config SHA-256
  `0c19cf33cae0555059eb6af9ab2d0a344a65b4916572de934c9b685404b61d18`; report = the sections above.
- The pre-policy configuration is also reachable at 79caea74d4f88c559d79caf6fc86ef9b7b43f9e8
  (the #247 merge): `tracker/config.json` there is the identical blob (`70c9f43c`) and has
  the same SHA-256, so `source_revision` now names 79caea74 and `source_config_sha256` is
  unchanged. The target policy fingerprint (`1bd91869...`) is unchanged.
- The Issue #250 `issue_sha256` is the accepted-scope digest of its final state:
  open, labels `status:merged`, `module:campaigns`, `type:bug` (priority and release labels
  are excluded from the digest) and **without** `flag:blocked`. While the Issue still carries
  `flag:blocked`, the engine does not accept it as historical. The blocker is removed by the
  authorized tracker actor with `python -m tracker set ACL-1250 --no-blocked`, not by this PR.
  Any later edit of the Issue's title, body, state or functional/workflow labels invalidates the
  entry and requires a new explicit decision.

Fresh in-memory engine check (Tracker 771d55f3): manifest validates with 75 tasks, the original 74
unchanged, #250 historically accepted once unblocked and not accepted while blocked.

