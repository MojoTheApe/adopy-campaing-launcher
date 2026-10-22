# Adopy Campaign Launcher — current system

ACL is a Windows desktop-installed application with a local web dashboard for
campaign managers. It reads performance data, prepares campaigns and drives
Multilogin's browser through controlled automation. It is not the shared Task
Tracker server, even though a legacy Tracker implementation remains in this repo.

## Components and current behavior

- `scripts/acl_ci_minutes.py`: on-demand native Actions execution accounting with
  private GET snapshot replay, using one exact released standalone canonical
  accounting artifact with ACL defaults/privacy and no workflow-engine adoption.
  It distinguishes carried-forward rows from actual
  observed intervals and reports query/period/identity gaps, estimates and current
  storage separately from unknown billing. See [accounting limits and commands](../repository/CI_ACCOUNTING.md).
  It changes no CI trigger, retry budget, admission gate or application runtime.

- `src/meta_campaign_agent/official_token_verifier.py` and
  `scripts/acl_meta_token_probe.py`: explicit standalone official-token application
  inspection. One fixed versioned Graph GET returns typed, sanitized evidence;
  it reads no machine configuration and makes no account/spend/publication calls.
  See [safe diagnostic use](../runtime/OFFICIAL_TOKEN_DIAGNOSTIC.md). This is a
  future connection-flow boundary, not a dashboard connection or readiness check.

- `src/web`, `serve.py`: local dashboard, account/report views, campaign builder,
  machine configuration and update endpoints.
  ACL-1651 refreshes the Builder creative list on search/window focus (ten-second
  throttle) and once per missed search streak. A miss during the two-second request
  gap waits in the background for its refresh; typing remains available and the
  visible latest query is searched again without switching Image/Video. Selected
  creatives and campaign fields remain unchanged until the user chooses a file.
  ACL-1381 keeps ACL-1769's numbered primary-text fields and separately displayed
  headline. Builder search/help and empty/error messages call the shared library
  entries ad texts; internal copy/copy2 fields and submission behavior are unchanged.
  Countries with the existing live activity state show a static 2 px blue vertical
  line at the left of their sidebar row (ACL-1838), including countries opened in
  the account map before their first spend. Inactive countries have no marker;
  the expanded-country background remains a separate selection indication.
- `src/analysis_agent`: statistics workbook reading, reports and reconciliation.
- `src/campaign_bridge`, `src/upload_agent`: campaign input/output preparation.
- `src/fb_ads_rpa`, `src/meta_campaign_agent`: Ads Manager interactions, verified
  profile identity, draft preparation, queues and browser lifecycle. ACL-1250/#247
  confirms audience age/country values within their own field and opens the correct
  collapsed Locations/Languages control, preserving adjacent fields. Later hover-only
  controls remain ACL-1251; the deferred real pilot remains ACL-1215.
- `src/fb_api`: API backend (`ACL_FB_BACKEND=api`) - Graph API calls made from inside the Multilogin
  profile's Ads Manager tab; everything is created PAUSED, never activated, never deleted. File uploads
  (`advideos`, `adimages`; ACL-1899) have no time limit: the page helper sends the file with
  `XMLHttpRequest` as a job (`startCall` / `poll` / `abort`; no upload listener, because it would trigger a CORS preflight that Graph rejects for credentialed requests), Python polls it about once per second
  (local CDP only, no Meta call) and the dashboard job panel shows `ad 1: uploading video (180 MB),
  1m 20s` (elapsed time, no byte progress; a real percentage comes with chunked upload, ACL-891). The wait ends on the answer, a network/HTTP/Meta error, a lost page, or Stop (the request is
  aborted, the result is treated as unknown and reconciled by readback; a file is never uploaded twice).
  After an unknown upload result the readback is repeated up to 6 times over about 2 minutes, counted in
  the per-campaign call budget. Writes without a file keep the short timeout. The write policy is
  unchanged and identical in Python and in the page helper.
  Campaign name (ACL-1902): before any write, in the run and in Check plan, the creator picks the first
  free name of `NAME`, `NAME_2` ... `NAME_20`. No live campaign with that name (deleted/archived do not
  count) -> use it; our own (ACL tag of that name) and identical to the builder -> reuse it (resume of a
  partial run, no duplicate); our own but different, or the same name without our tag -> next suffix. The
  suffix is always appended to the full name (`X_2` becomes `X_2_2`; the user's own numbers are not
  parsed). Ad set and ad names stay as built. The tag is computed from the chosen name, so `NAME_2` has
  its own tag and its own creatives. All 20 taken -> stop before any write (`name_taken`). The rule
  `existing_diffs` shows a WARNING with the chosen name when a suffix is used and an ERROR only when all
  20 are taken. One campaigns search on the base name serves all candidates; if it returns the maximum
  200 rows it is not trusted and each name is looked up with an exact (EQUAL) search, and if that is
  also full the run stops before any write. At most 6 of our own earlier campaigns are diffed per
  resolution (call budget); a name over 400 characters stops (conservative guard, the Meta limit is unverified). A NAME_k (k>1) of ours is
  reused only if its ads are a subset of the plan's ad names (a builder campaign literally named `X_2`
  is not taken for our suffix). Ad sets and ads under our tagged campaign are matched by name,
  so a lost child tag does not make the campaign "different" and a re-run resumes it. No extra writes, no new lock.
  Names in the app: `campaign_name` in every result, job progress and draft status stays the builder
  name (the dashboard keys its dots and verdicts by it); the chosen name is the separate field
  `published_as` (empty when no suffix), shown as "-> published as NAME_2" in the plan and job panels.
  Meta, statistics and reports show the chosen name `NAME_2`. Each NAME_k has its own tag, so creatives
  are created again and a video may be uploaded again; an older partial campaign stays PAUSED and is
  left as is.
- `src/creative_agent`, `src/knowledge_agent`: creative analysis and domain references.
- `src/analysis_agent/team_library.py` and related library modules: shared targets/ad texts in
  the separately provisioned private team library; PR #244 replaced Google Sheets
  for that library. Existing optional Google integrations are not all removed.
- `packaging/windows`: packaged ACL.exe, first-run setup, per-user installer and
  detached update helper. `src/web/release_updates.py` offers verified release updates.
- `config/paths.py`: machine-specific paths. User configuration, account maps and
  credentials belong outside Git, normally under `%LOCALAPPDATA%/ACL`.

Read [distribution boundaries](../DISTRIBUTION.md), [product/design context](../../PRODUCT.md),
[design guidance](../../DESIGN.md) and existing specs for subsystem details.
`content_machine`, `jira_agent` and legacy `fb_publish_agent` are not automatically
part of the ACL runtime; preserve the tested distribution boundary.

## Delivery state at adoption

PR #245 (installer) and #246 (updater) are merged at the recorded source baseline.
The source install path still exists alongside the packaged path. No stable GitHub
Release or employee rollout is claimed by adoption. Hosted Windows tests and real
build/install/repair/uninstall now pass, as does actual isolated package update,
restart and compatible recovery using two test versions built from the same source.
Initial stage `09eb8af` passed CI 35603056418 and installer 35603056536; its report
verifies the exact source/tree, actual installed executable, all shortcuts and
unchanged synthetic data. Changed activation content requires its own stage report.
These results do not verify the published release/download channel or the deferred
live Multilogin pilot. Company Google sign-in remains ACL-1242 and part of ACL-1239's scope;
shared-library provisioning remains ACL-1241. Do not mark either parent complete
because its code exists. Media reuse remains governed by its explicit feature flags
and outstanding acceptance, not by adoption.

The owner selected a test version on stage and stable installed releases from main.
Those branches and the matching shared Tracker registry now exist; ACL-1248
activates ordinary use through its separately reviewed stage/main promotion and
normal Tracker handoff. See the current repository adoption record. This change organizes engineering; it does not change campaign behavior,
credentials, library setup or employee installations.

Engineering delivery now has an explicitly prepared Approved/batch policy;
actual state activation is separately verified under
[ACL-1566](../repository/BATCH_INTEGRATION_ACL.md). Campaign runtime, employee
installations and stable-package publication retain their existing boundaries.

[Source child ACL-1875](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/875)
prepares five implementation slots, five independent source-review slots and
batches of at most five through explicit execution schema 3. ACL-owned CI scope
and assembly recognize both v2/v3; combined verification and a single integration
Coordinator/Reviewer remain mandatory. Actual source completion, compatible shared
service publication and idle queue-preserving migration are separate gates in
[SOURCE_REVIEWS.md](../repository/SOURCE_REVIEWS.md), retained in parent
[ACL-1874](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/874). Preparation changes no
campaign runtime or installed employee version.

## Graph read failure diagnostics

ACL-1886 adds the fixed GET method and an allowlisted operation category to
GraphClient errors. Supported search lookups also name only a fixed location,
locale or interest subtype. Unknown paths/subtypes do not echo request data;
URLs, parameters, object IDs and names are excluded from this added context.
Existing error text consumers can display the category without another request.
This identifies the failed read; it does not establish the cause of a live Meta
permission denial or change access, retry, token, pagination or stop rules.
Callers that replace the exception text with a generic message still need their
own integration; this boundary alone does not update those messages.

## CI feedback and admission

The existing `Classify and check project tooling` result supplies bounded tooling
fast feedback. The [ACL-owned fast/full plan](../repository/VERIFICATION.md#fast-feedback-and-full-verification)
records its exact commands and omitted coverage. Full applicable product and
installer admission keeps the separate required aggregates and existing native
platform/producer rules. Parsed workflow regressions preserve linkage-only PR
metadata feedback and normal verification on a changed head; no workflow, runtime,
retry policy or employee installation is changed by documenting this plan.

## Maintaining this document

Describe implemented behavior and limitations here when changing the system. Preserve
plans under original specs, label them as plans, and record what/why and ticket/PR
links in a per-task fragment `changelog.d/ACL-NNNN.md` (folded into CHANGELOG.md by
`scripts/fold_changelog.py`; see changelog.d/README.md). Process-only changes belong in ../repository/CHANGELOG.md.

Repository instructions have a separately prepared direct-main route described in
[PROCESS_INSTRUCTIONS.md](../repository/PROCESS_INSTRUCTIONS_ACL.md). Eligibility uses
normal fixed Markdown paths and complete Git proof, with independent review and
applicable instruction checks. This policy/automation source is not yet active in
main; it changes no campaign-launching behavior or employee-installed version.

## Authorized package completion

The repository provides `.github/workflows/complete-delivery.yml` to continue an
actual authorized frozen batch after admitted main integration. It reuses the
existing release publisher, verifies actual published packages on isolated Windows
and records exact task delivery only from the successful native acceptance job.
Source preparation, policy activation and actual delivery remain distinct facts;
see [the process runbook](../repository/POST_MAIN_DELIVERY.md) and parent ACL-1923.

### ACL-1880 — campaign-local Facebook Page selection (2026-10-13)

The builder reads available Page names and IDs through `/api/account-pages`, using the existing read-only profile session and profile lock. New campaign cards start with Automatic; duplicates copy other settings but reset Page selection. The first Page in the destination account's returned order is shown and resolved before creation. Explicit campaign choices never write the account map. Legacy non-builder drafts retain map fallback.

Loading failures and confirmed empty lists are distinct and support Refresh pages. A truncated discovery result is refused. API preflight still validates an explicit Page's membership, and its existing creative readback validates the resulting identity. Instant Form caches are keyed by the resolved Page ID, including in Automatic mode.

RPA resolves the selection before campaign creation and passes the ID and unique display name to the recorded flow. Every ad verifies Identity; failure is non-retryable and not publishable. The existing sanitized `tests/fixtures/ads_manager/simple_create_2026_09_27.html` supplies regression coverage for the captured collapsed Identity summary. Picker interaction has offline native/custom-control tests; a live alternate-Page picker pilot has not been performed. Unrecognized or ambiguous controls fail closed. Other RPA engines are refused for builder Page selections until they implement Identity verification.

Verification uses synthetic accounts and mocked browser/API transports only. The isolated local dashboard preview is not a production deployment or a stage/main merge.
# ACL-1880: manual completion when the automatic Page list is empty

Owner revised the acceptance on 2026-10-13: use the first available Page; if none
is available, allow preparation for manual completion. In the API website path,
a confirmed empty list and no explicit Page now produces a warning. The worker
creates only a PAUSED campaign and its PAUSED ad sets, with readback of those
objects. It does not upload media or create creatives/ads without a Page. Results
carry `manual_required` and remain non-publishable. The operator adds Page/ads in
Ads Manager or retries after Page access is restored; normal identity labels
reuse the same campaign/ad sets. The original builder retains the ad inputs.
Instant Forms and an explicitly unavailable Page still block, as do failed Page
reads. The RPA path retains its existing Identity requirements. This supersedes
the earlier requirement that every empty Page list blocks all preparation.
