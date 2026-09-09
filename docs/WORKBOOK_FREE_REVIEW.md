# Workbook-free builder integration review — 9 September 2026

The candidate integrates nine incoming commits with production
`e94639d591f5196027820966bab545944d98f1a5` (`master`). Codex reviewed the incoming
changes, corrected integration defects and self-reviewed the corrections under
the owner's instruction to review, fix and merge. No separate independent review
of those corrections is claimed.

Branch: `codex/workbook-free-review`. The final head, tree, CI evidence and merge
result are recorded in the PR. The source branches were unchanged at the final
pre-push check; all nine source commits are preserved as ancestors.

## Included commits

| Branch | Commits |
| --- | --- |
| `feature/upload-without-workbook` | `9cf6a41`, `9ba628b`, `ced49f1`, `3c1c4b8` |
| `feature/builder-from-links` | `c4bd7db` |
| `feature/connect-without-act` | `3559fb6` |
| `feature/manage-todo-column` | `ac640bf` |
| `fix/builder-per-account` | `55c6761` |
| `fix/calendar-days-not-disabled` | `f3123eb` |

## Review findings and corrections

- **P1 — opening a new profile still failed at the real browser provider.**
  The new wrapper accepted an absent local folder, but its provider rejected it.
  Added explicit support for first startup through Multilogin with a folder ID.
  The dashboard path forbids restarting an existing profile, including API lock
  conflicts after process inspection; tests verify no stop, kill or second start.
  Existing strict attachment, short-path checks and serialized browser access
  are retained. The default behavior of other provider callers is preserved.
- **P1 — a connection without an ad-account number disappeared on reload.**
  The CSV writer accepted it but the reader discarded that row. Named incomplete
  entries now survive, keeping their profile/folder/instance. Preflight reports
  the missing account number; campaign validation still rejects missing required
  IDs. Ambiguous partial account names do not choose an arbitrary profile.
- **P1 — link reads and saves still depended on Upload FB.xlsx.** Preflight and
  RPA now read the local map even without a workbook. Save writes that same map
  first, using the shared atomic CSV writer. Exact names take priority and
  ambiguous matches are refused, so saving Demo cannot update Demo longer.
- **P1 — clearing a local link restored an old value.** A local
  `tracking_link_set` marker records an explicit clear, including maps using the
  legacy `link` column. Unconfigured fields can still inherit legacy workbook
  values; configuring only a pixel does not discard a legacy tracking link or
  domain. Other existing map columns are preserved.
- **P1 — explicit empty language/link fields were replaced by defaults.** The
  real form-to-slot-to-draft pipeline preserves whether a field was supplied.
  Clearing Arabic for Saudi Arabia now skips Language as the UI promises.
  Omitting a field in an older caller still applies its default. The preview
  displays the selected language, and tracking warnings name the value used by
  the current form instead of incorrectly promising the previous saved value.
- **P1 — linked accounts could remain absent or become stale in the builder.**
  A country with no statistics now gets a renderable empty section and its linked
  accounts. Initial map seeding precedes builder collection. Each render copies
  the cached report before adding local links, so unlinking an account is visible
  on the next request and does not mutate cached statistics.
- **P2 — workbook membership was confused with selected-period availability.**
  Membership recognizes case-insensitive account identity and checks the whole
  workbook when the current report lacks a row. A known account without current
  statistics is not mislabeled as absent from the workbook.
- **P2 — tests overstated coverage or expected superseded behavior.** New tests
  execute the real provider with a fake launcher, persist/reload actual temporary
  CSV files, render the real report handler and carry actual form fields into RPA.
  Existing fixtures now identify known workbook rows explicitly, allow newly
  linked countries and patch the function actually consumed by enrichment.

The enrichment merge conflict keeps production's exact local ad-text lookup and
missing/deleted/corrupt-library errors, plus its distinction between a missing
link on DRAFT and ACTIVE. The profile conflict keeps production's strict
short-path/process guards. The duplicate ad-copy fallback introduced by the
incoming branch was removed. The selected targeting library remains authoritative;
the incoming missing-target warning behavior is retained without an Excel
interests fallback. Calendar presentation and both server/manual campaign card
builders were reviewed together with the existing dashboard handlers.

## Validation

Environment: macOS, Python 3.12, Node available, isolated configuration and
synthetic data. No live Meta, Google or Multilogin account was modified.

- Integrated baseline: **1029 passed, 4 failed, 1 skipped**. The four failures
  were the relocated lock decorator, a changed Setup fixture, warning wording
  and an ad-copy mock attached to the wrong import.
- Ten initial regression scenarios reproduced the defects before corrections;
  all ten passed after. Additional coverage checks explicit clear vs omission,
  legacy columns/fields, ambiguous identities and workbook membership.
- Final `python -m pytest tests/ -q -rs --tb=short`: **1049 passed, 1 skipped**.
  The skip requires native Windows PowerShell.
- `python scripts/acl_smoke.py`: dashboard and RPA import successfully.
- `git diff --check`: passed.
- Isolated headless Chrome: a country with no statistics receives its builder;
  a manually added campaign receives the country language default; clearing it
  reaches real RPA preview unchanged; local ad copy and the typed tracking URL
  reach preview; saving without Excel survives reload; Open then Connect with an
  empty account number preserves the selected profile/instance and shows only
  the missing account ID. No JavaScript errors. External responses, including
  profile startup/probing, were synthetic; local map/link/preview handlers were
  real. The profile startup boundary was separately tested with the real provider
  and a fake Multilogin launcher, not a live browser profile.

The PR records the tracked-file secret scan and exact candidate checks. GitHub
Windows CI must be assessed separately; a Billing-blocked job is not a passed
test. The live Windows/Multilogin pilot remains deferred under the owner's earlier
instruction. No release tag, installer distribution or rollout is included.
