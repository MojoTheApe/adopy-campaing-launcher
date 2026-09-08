# Targeting integration — 2026-09-08

Base: production `d36478ceb64c3386f8ad4602b69ca0687e16a94c`.
Included: `9f80d350129527af17bf77ce1561ea28a4a99018` and
`9a7d4f469b806bcc60deeb2573381a13f48d7b77` from
`feature/account-country-links`. Codex reviewed the incoming changes and
implemented the integration fixes; those fixes received self-review, not a
separate independent review by Claude.

## Fixed during integration

- Builder options and RPA use the same local library, even without Upload FB.xlsx
  or access to Sheets. The narrowing column becomes an AND group in the draft.
- Sheet refresh imports new profiles into populated registries, keeps historical
  aliases, and updates sheet-owned records. Local edits and removal tombstones
  survive refresh. Exact, case-insensitive lookup avoids ambiguous substring hits.
- The UI explicitly labels local edits/removals. Sharing is append-only; it never
  overwrites another shared row. A persisted failure can be retried from the UI,
  including a successful append whose response was lost. Identical name/content
  retries succeed; a different audience under the same shared name is refused.
- Read fallback tries employee credentials after an actual service-account request
  fails. Unused credentials are not initialized. Read failures are distinct from
  an empty sheet and prohibit blind append. Reads include rows beyond row 500.
- Registry read/modify/write uses process and thread locks plus unique atomic
  replacement files. Invalid/corrupt stores are preserved and reported, never
  silently replaced. Fingerprints follow the same OR/AND syntax as RPA.
- Targeting endpoints move blocking I/O to workers and reject malformed field
  types. UI edit/cancel handles unusual names as data and ignores duplicate clicks
  while a request is pending.

## Verification

Synthetic tests cover concurrent processes/writers, corrupt data, aliases and
removals, offline builder and enriched drafts, Google request fallback, idempotent
retry, HTTP validation, and executable JavaScript edit/cancel/retry behavior.
Run `python scripts/acl_scan_secrets.py`, `python scripts/acl_smoke.py`, and
`python -m pytest tests/ -q`; Windows CI runs Python 3.11 and 3.13.
Actual test counts and CI links are recorded in the integration PR.

No live Google writes, campaigns, browser profiles or employee credentials are
used by these tests. The owner explicitly deferred manual Windows/Multilogin
verification until after merge. Confirm narrowing and selection on a real draft
in that pilot. Code merge does not promote an `acl-release-*` release.

The local lock cannot coordinate separate machines appending directly to Google
Sheets. Sheets has no uniqueness constraint; two simultaneous first-time writers
can still create duplicate rows. Refresh retains the first row for a name, and
sharing refuses an observed conflicting name. Shared edits/deletions remain a
manual sheet operation; the dashboard's corresponding actions are local only.
