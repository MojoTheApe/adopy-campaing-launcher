# Targeting review integration — September 8, 2026

Sources: `3c30690891eda9580daa3f1fd2002ffab972a8dc` and
`96bc4aa0a578098216916bc883aa9b59fc2becf7`, based on `9a7d4f4`.
Integration started from production `c2eb0f1` and preserves its registry format,
cross-process locking, corruption protection, local overrides, removal
tombstones, exact interest resolution, narrowing and worker-thread HTTP I/O.
Later commits about the dashboard header and theme are outside this change.

## Behavior

The existing shared-library model is retained: new profiles are saved locally
and appended to Sheets; cached profiles work offline. Local edits/removals
remain local unless the operator explicitly exports the saved library.
The automatic-sharing removal in `96bc4aa` is not adopted in this integration.
An unanswered storage-model preference is not recorded as user approval of a
switch to local-only operation.

Existing profile names are preserved. Spaces are not replaced by the underscore
that campaign parsers use as their separator. No live profile or sheet is
renamed or migrated by this change.

The Targeting screen reports duplicate normalized names and duplicate audience
sets, including equivalent AND expressions. It does not delete historical aliases.
The renderer belongs inside the existing script; no text precedes the HTML doctype.

An explicit export replaces A:C below the header with the saved local library.
The interface explains the replacement and requires confirmation. It is never
triggered by opening the page, adding or editing a profile. A successful full
read is saved to a unique, durable local JSON backup before any sheet mutation.
Read/disk failures abort the export. Backups retain values/formulas from A:C,
including headers and rows beyond 500; formatting and other columns are not backed up.

Expansion, replacement and clearing of the unused tail are submitted in one
`spreadsheets.batchUpdate`. Google documents [atomic application of the batch](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets/batchUpdate)
and [clearing uncovered cells in an UpdateCells range](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets/request#UpdateCellsRequest).
Only `userEnteredValue` in A:C below the header is changed. A lost response is
reported as unconfirmed, with the backup path; the server might have committed
the whole replacement, so the operator must inspect before retrying.

This is not a distributed lock: concurrent editors can still overwrite one
another's changes. The interface asks other editors to pause for a full export.
Existing local copies on other machines are not deleted by replacing the sheet.
Sharing individual new profiles retains the safer append-only behavior.

## Regression coverage

- R1: actual Targeting click/fetch/render behavior in Node, escaped duplicate
  names/sets, doctype placement, export confirmation/cancellation and pending guard.
- R2: offline HTTP creation followed by builder options and enriched RPA draft,
  with the same interest set and narrowing; existing local edit/removal tests retained.
- R3: registry → campaign-name builder → analytics round trips for multiword names.
- R4: batch failure before application and lost response after application;
  neither leaves a half-cleared library, and both retain the backup.
- R5: failed read, failed fsync, absent credentials, genuine empty sheet,
  unique backups in the same second, corrupt registry and attempted backup opt-out.
- R6: empty/one/501-profile exports, full backup, retained header and other columns,
  and grid expansion in the same request.
- Local changes made during export are preserved when updating sharing status.
- Export HTTP I/O executes in a worker thread and requires explicit confirmation.

All fixtures are synthetic and use a separate config directory. No live Sheets,
Meta or Multilogin writes are acceptance tests. Full pytest, smoke, secret scan
and Windows 3.11/3.13 CI are required on the final candidate; exact outcomes and
run URLs are recorded in its PR. The owner's deferred live Windows/Multilogin
pilot remains outstanding. Merge does not promote an `acl-release-*` tag.

Claude authored the source features. Codex reviewed, fixed and self-checked this
integration under the owner's chosen workflow; no independent review of Codex's
fixes is claimed.
