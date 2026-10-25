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
  ACL-141 uses queued, labelled page dialogs in place of browser alert/confirm/prompt;
  Escape/Cancel refuse, destructive actions initially focus Cancel, and focus returns
  to the initiating control. Recoverable control errors stay inline. Forms deletion
  also rechecks the row/list after confirmation; existing Page forms remain untouched.
  Manual profile-ID saves and automatic profile links keep refusals beside their
  visible form/list; saved/linked messages follow the real successful response.
  Missing-creative Publish refusals stay inline beside the visible form/footer
  action, preserving the named rows and refusal before submission.
  ACL-142: the account map's Connect step asks for the ad account in an in-page
  dialog instead of a browser prompt. It reads the open profile on opening and
  again on "Read from the open account"; the result (name and `act_` number) is
  shown in an editable field and, because Ads Manager opens the profile's
  default ad account, an unedited read must be ticked as the intended account
  before Connect is enabled. A typed number needs no tick; an empty one connects
  without a number as before. In a country row, `x` takes the account out of
  that country only (confirmed; it stays in ACL and moves to Available
  accounts), and "Remove from ACL" is a separate confirmed action that deletes
  this computer's account-map row and the account's country/asset placements
  (`POST /account-map-remove` with `also_unlink`). The shared sheet, history,
  Meta campaigns and the Multilogin profile are never touched.
  Full removal uses the same case-insensitive account identity as local placements;
  reconnect does not restore old country/asset links. Its confirmations use the
  shared page dialogs and recoverable errors stay beside the originating action.
  ACL-1651 refreshes the Builder creative list on search/window focus (ten-second
  throttle) and once per missed search streak. A miss during the two-second request
  gap waits in the background for its refresh; typing remains available and the
  visible latest query is searched again without switching Image/Video. Selected
  creatives and campaign fields remain unchanged until the user chooses a file.
  ACL-1668 marks selected files missing from a refreshed catalog while retaining
  their names. Publish and Publish all name the affected rows and refuse them;
  publish-time enrichment also checks the file again and reports deletion/rename.
  ACL-1381 keeps ACL-1769's numbered primary-text fields and separately displayed
  headline. Builder search/help and empty/error messages call the shared library
  entries ad texts; internal copy/copy2 fields and submission behavior are unchanged.
  Countries with the existing live activity state show a static 2 px blue vertical
  line at the left of their sidebar row (ACL-1838), including countries opened in
  the account map before their first spend. Inactive countries have no marker;
  the expanded-country background remains a separate selection indication.
- `src/web/builder_drafts.py` and `/api/builder-state`: debounced machine-local
  copies of Builder campaigns, drawer open/wide state and publish-job memory.
  localStorage remains a fast cache; initial reconciliation restores the newer
  copy and backs up displaced data. Atomic writes use the shared ACL config path
  and machine lock, preserving corrupt files before replacement. Each document
  serializes normal saves; delayed acknowledgements/refusals cannot replace a newer
  local edit or rewind its timestamp. The latest queued snapshot uses the last
  acknowledged server base. Browser-only fallback remains available if the server
  is unreachable. A closing-page flush sends the latest keepalive copy immediately,
  without relying on an earlier response callback; superseded replies are ignored.
- `src/web/draft_status.py`: current build verdicts in machine-local JSON. Accepted
  jobs invalidate earlier readiness before starting, retain the previous success
  as history and persist their job/plan identity, partial results and stopped or
  failed outcome. Reloads mark unfinished jobs absent from this process interrupted;
  missing/unreadable status is explicitly unverified. Thread and process locks cover
  the complete read/modify/write transaction, with job/revision fencing for late writers.
- `src/analysis_agent`: statistics workbook reading, reports and reconciliation.
  The dashboard tells an empty chosen period apart by owner (ACL-106): no workbook rows for
  anyone (the file may need updating) versus rows for other managers but none for the
  selected manager (nothing to review); the account map stays available in both.
- `src/web/period_compare.py`: the dashboard can compare the selected workbook
  period with the previous range of equal length or a custom range (ACL-110).
  Both use the existing report and metric formulas. Earlier zero is retained;
  zero or missing denominators give a dash for the change. Country/account/manager,
  recommendation and breakdown rows present in only one report are marked new/gone.
  Historical-only evidence is read only and does not restore account assignments
  or become a Builder/Workbench action. Current Workbench row indices and cached
  reports are preserved. Home and Workbench metrics retain their existing cohort
  and weighting semantics; a map-summary refresh keeps the same comparison dates.
  Measured spend and FTD summaries also compare in volume cards, row You tags,
  asset chips and sidebar counters; structural counts, goals and draft inputs keep
  their existing meaning. Historical-only exact versions and CM Net/ROI remain visible.
- `src/campaign_bridge`, `src/upload_agent`: campaign input/output preparation.
- `src/fb_ads_rpa/pacing.py` and job progress:
  ACL-1142 keeps Website teach waits and Continue answers per publish job. The
  banner names its account, and status/progress both expose that job's wait for
  the orange attention dot. Cancellation, timeout or an exception clears only
  that job's wait; the manual Pause button, profile cap and leases remain shared
  safety controls. Ordinary builds still pulse grey, finished failures stay
  orange, clean results green, and reduced motion disables only the animation.
- `src/web/profile_slots.py` (ACL-1642): a publish job whose Multilogin profile is already
  open does not reserve memory for a new profile. The configured concurrency ceiling,
  per-profile ownership and kept-window rules still apply. A read-only background probe
  uses the same long/short-folder and profile-ID process check as the robot; only a fresh
  answer (five seconds) bypasses memory checks. Missing, failed or stale answers retain
  the normal memory guard. A hung probe's late reply cannot replace a newer result.
  Before closing an owned idle window for memory alone, the queue gives a pending
  background check up to five seconds to answer. An open answer avoids that close;
  closed/failed answers and an expired wait retain the existing conservative policy.
  Capacity/ownership checks still apply immediately, and the wait holds no queue lock.
  The first eligible open-profile job may pass a head waiting for memory; the dashboard
  explains this and shows the real free/needed memory with a close-window hint.
- `src/fb_ads_rpa`, `src/meta_campaign_agent`: Ads Manager interactions, verified
  profile identity, draft preparation, queues and browser lifecycle. See
  [same-user process ownership and its boundaries](PROFILE_OWNERSHIP.md). ACL-1250/#247
  confirms audience age/country values within their own field and opens the correct
  collapsed Locations/Languages control, preserving adjacent fields. Later hover-only
  controls remain ACL-1251; the deferred real pilot remains ACL-1215.
  ACL-1138: before attaching to or starting a Multilogin profile, every Multilogin
  provider, `attach_existing_only` and `fb_api.profile.open_profile` read the profile's proxy from the MLX API (`/profile/search` by id) and
  stop the account with `profile_no_proxy` when none is configured. A failed check
  does not block the run; `FB_RPA_PROXY_GATE=0` is the machine switch. The
  browser's `--proxy-server` flag is only logged as evidence.
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
- `scripts/meta_docs_watch.py`: documentation digests use the shared Obsidian
  folder resolver (`ACL_OBSIDIAN_DIR`, then real/legacy locations). Missing
  default or requested vaults warn on stderr; `--vault` overrides the folder,
  `--no-obsidian` disables note output, and dry-run writes nothing (ACL-2101).

Read [distribution boundaries](../DISTRIBUTION.md), [product/design context](../../PRODUCT.md),
[design guidance](../../DESIGN.md) and existing specs for subsystem details.
`content_machine`, `jira_agent` and legacy `fb_publish_agent` are not automatically
part of the ACL runtime; preserve the tested distribution boundary.

### ACL-1896 — Instagram placements need an Instagram account on the Page ([issue](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/896))

Live finding on Doc120 (2026-10-14): the API publish wrote the campaign, ad set and creative PAUSED, then Meta refused the ad (Graph 100, subcode 1772103, "Instagram Account Is Missing") because the Page had no Instagram account and the creative carried no Instagram identity. The creator always sends Instagram feed, Stories and Reels placements.

- Read-only discovery (`src/fb_api/instagram.py`), at most two Graph reads, cached per Page for the run: the Page's `connected_instagram_account`, `instagram_business_account` and `connected_page_backed_instagram_account` fields (one read); only when all are empty, the ad account's `instagram_accounts` edge (for the message only; an account listed there is not assumed to belong to the Page). A failed read is "unknown", never a pass. The per-campaign create budget (150 calls) and the Check plan reset per draft absorb the two reads.
- New server pre-flight rule `instagram_identity` (shared table `preflight_rules.py` and its JSON; the page draws server rows from the table, so `report.html` needs no change). Plans with Instagram placements and no confirmed identity are refused in Check plan and again before the first write: "This Facebook Page has no Instagram account, and the ad is set for Instagram placements. Connect an Instagram account to the Page, or wait for manual placements (ACL-1772) to publish Facebook-only." An unconfirmed read says Meta did not confirm one. When the Page itself is not settled the row is "unknown" (the Page row already blocks).
- With an identity the creative carries `object_story_spec.instagram_user_id`, and the readback compares it. The closed write policy (`write_policy.py` and the page helper in `token_capture.py`) gained exactly one field: `instagram_user_id`, a string of 5-25 digits, only as a direct key of a creative's `object_story_spec`. `instagram_actor_id` is refused everywhere. No Page-backed Instagram account is created; that would be a write and needs an owner decision.
- Documentation evidence (developers.facebook.com, 2026-10-14). Certain: `instagram_user_id` is a numeric string field of the creative's `object_story_spec`; the ad account `instagram_accounts` edge is read-only and lists IGUser nodes; the Page node has the three fields above. Not documented: `instagram_actor_id` (not used) and Page edges `instagram_accounts` / `page_backed_instagram_accounts` (not used).
- Needs a live check on an authorized test account: that the profile's token returns the three Page fields; that Meta accepts that id as `instagram_user_id` (including a Page-backed id); where the creative read returns it (`object_story_spec` or the creative's own `instagram_user_id`, both are compared); behaviour on a Page with an Instagram account the ad account cannot use.
- Known limit: a creative left by an earlier failed run (found by name) is reused as is; if it lacks the Instagram id, the readback reports the mismatch instead of silently passing.

## In-profile Graph session recovery

- `src/fb_api`: in-profile Graph transport keeps tokens inside an isolated page
  world. ACL-1836 permits one recovery of a detached CDP session on the same owned
  target and existing browser connection. Target and main-frame location must
  match the navigated Ads Manager account; subsequent reads recheck that location.
  The owned tab retains the successful explicit navigation intent and recovery
  guard across replacement helpers, including warm read/write mode changes.
  A helper never learns its expected account from the current page URL.
  Local helper setup can retry, but a sent Graph request is never replayed.
  Missing/changed targets, repeat detachment and unavailable transport stop with
  `GraphStop`, so creator reconciliation can retain already-created object details.
  Recovery does not open a replacement tab, restart the profile or reload for a
  missing token. Pending creator source integration and live delivery remain separate.

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

## Profile Page fallback and no-creation proof

ACL-2105 merges ad-account Pages with the profile's own ADVERTISE/MANAGE Pages
when the account list is empty or omits an explicit choice. Account Pages retain
priority; picker, plan/result and Instant Forms retain each Page's origin.

Before any objects, uploads or forms are created with a profile Page, both Check
plan and the real run require one `validate_only` creative for the selected account,
Page and link. The isolated helper binds its account to navigation (or the verified
creation account), constructs the fixed parameters itself, and leaves ordinary
write permissions closed in read sessions. Explicit successful Meta validation
without an error/object ID is required. Unknown, unsupported and refused responses
stop with the Page and reason. Proof is reused only within the current operation;
the actual run checks again after an earlier plan check. Existing call budgets,
usage/stop guards and unknown-write fences still apply.

The official Meta SDK at `efd8423a2e595ea8d4c04eb824ce113f2f1d68cd` supports
[creative execution options](https://github.com/facebook/facebook-python-business-sdk/blob/efd8423a2e595ea8d4c04eb824ce113f2f1d68cd/facebook_business/adobjects/adaccount.py),
[`validate_only`](https://github.com/facebook/facebook-python-business-sdk/blob/efd8423a2e595ea8d4c04eb824ce113f2f1d68cd/facebook_business/adobjects/adcreative.py),
[Page/link story fields](https://github.com/facebook/facebook-python-business-sdk/blob/efd8423a2e595ea8d4c04eb824ce113f2f1d68cd/facebook_business/adobjects/adcreativeobjectstoryspec.py)
and [link data](https://github.com/facebook/facebook-python-business-sdk/blob/efd8423a2e595ea8d4c04eb824ce113f2f1d68cd/facebook_business/adobjects/adcreativelinkdata.py).
These fields do not guarantee the minimal link creative is accepted for every
Page/plan without uploaded media. Such a refusal blocks creation; there are no
extra asset reads or fallback writes. This is Page/account usability evidence,
not a full media/content/ad-review guarantee. Tests use synthetic transports and
the actual helper offline; no live Meta or profile pilot has been performed.

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

## Several primary texts and Stories / Reels media (ACL-1774)

API backend only. One text and one file keep the old `object_story_spec`. Findings from the
Meta capability spike (Doc120, 2026-10-14): `asset_feed_spec` with 3 bodies plus two placement
customization rules was refused (error 100, subcode 1885878); a rule applies one asset per type.
Owner decision 2026-10-20 (the recorded PAUSED capability spike accepted Dynamic Creative;
the visual Ads Manager presentation check remains pending):

- 2-5 texts (or several headlines/descriptions) go as Dynamic Creative: `is_dynamic_creative: true`
  on the ad set (the only new ad-set key in the write allowlist, `true` only), one file, no rules,
  text optimizations stay OFF. The ad must be alone in its ad set; Instant Form refuses it.
- A Stories / Reels 9:16 file (two labelled files, two placement rules) works with one text per kind.
- Both write-policy layers (`write_policy.py`, the in-page mirror in `token_capture.py`) and the
  pre-flight rules (Python table + `cbPreflight` in `report.html`) carry the same limits.
- Library entries with the same body retain their distinct headlines/descriptions. Empty text
  slots preserve their field numbers. A partially created feed creative is reused only for the
  same payload; a retry with changed texts uses a new creative without uploading the media twice.
- Readback checks media labels and IDs even when ads are reused. Feed creatives retain the current
  Instagram identity; vertical videos use the same bounded/chunked loader as the main video.
  Website media/CTA controls stay outside the hidden Instant Form section, and campaign copies
  preserve their ad counts. Choosing a square `..._post` file that has a `..._story` partner in
  the same folder fills the empty Stories / Reels field from `creative_pairing` (the person can
  change or clear it; an ad with several texts is not prefilled).
- Over-length texts (1024 body, 255 headline/description) are refused before any write with a message
  naming the length. The vertical file upload is labelled "Stories / Reels file image/video" in progress.
- Current ACL-2114 safety still governs creation: the campaign stays PAUSED and child objects use
  the existing confirmed-parent guard. This work adds no live activation. ACL-1767 is merged.
  The RPA backend is out of scope (owner decision 2026-10-25). Open conditions: the live checks on
  TEST9095 (2-5 texts as Dynamic Creative; one text with a square + 9:16 pair and placement rules)
  and Instant Form asset feeds, which stay refused.


## ACL-1891 — Videos over 200 MB upload in chunks ([issue](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/891), 2026-10-14)

Videos up to 200 MB still go up in one request. A video from 200 MB up to 1536 MB (`VIDEO_MAX_BYTES` = `write_policy.UPLOAD_FILE_MAX`) goes through Meta's resumable upload on `/act_X/advideos`: `upload_phase=start` (`file_size`), `transfer` (`upload_session_id`, `start_offset`, multipart `video_file_chunk`), `finish` (`upload_session_id`, `title`, `name`). Everything stays PAUSED; nothing is deleted or activated.

- Memory: the server never reads the whole file. `creator.LargeFile` reads slices from disk (a plain slice larger than one chunk is refused); the hash for the idempotency tag (`acl-<tag>-<md5[:12]>`) is computed in 1 MB blocks. The page helper holds one chunk (at most 8 MB, `UPLOAD_CHUNK_MAX`) at a time; the existing staging bridge (base64 pieces over CDP, SHA-256 and size check before sending) is reused per chunk.
- Resume: Meta's reported `start_offset`/`end_offset` drive the loop. The next chunk is `min(Meta's range, 8 MB, rest of file)`. A refusal that names the offset (`error_data`) resumes from there, not from zero. An unknown result on a chunk re-sends the same chunk at the same offset once or twice (the protocol's own offset query); a stuck chunk stops with `unknown`. Offsets that are not digits, go backwards or lie outside the file stop the upload before `finish`.
- Reconcile: an unknown `finish` is reconciled by reading the library back by the title tag; a found video is reused, never uploaded twice. A re-run finds a finished video by the same tag. An abandoned unfinished session is not visible in the library; the re-run starts a new one.
- Processing wait is unchanged (`VIDEO_MAX_POLLS`). A large video costs `ceil(size / 8 MB) + 3` Graph calls (`creator.upload_calls`); `_prepare` refuses a plan up front ("split the campaign") when those calls plus a reserve of 40 (`CALLS_RESERVE_OTHER`) exceed the run's budget (`ACL_FB_API_CREATE_MAX_CALLS`, default 150, ceiling 250). With the default a video up to about 850 MB fits; a 1.5 GB video needs the budget raised to 250 and goes alone.
- Write policy, both layers (`write_policy._check_upload`, the page helper `uploadOk`): exactly three phases, a closed key set per phase, numeric `file_size` within the limit, numeric session id, numeric `start_offset` inside the limit, a file only on `transfer` (field `video_file_chunk`, 1..8 MB; the helper checks the staged blob). `cancel`, `end_offset`, other keys, other edges: refused. Python and Node parity is tested case by case.
- Pre-flight `media_limits`: video limit 1536 MB, message "file is larger than 1536 MB".
- Documentation evidence (developers.facebook.com, 2026-10-14). Certain (Ad Account `advideos` reference): chunked upload parameters `upload_phase` (start, transfer, finish, cancel), `file_size`, `upload_session_id`, `start_offset`, `end_offset`, `video_file_chunk`; return fields `upload_session_id`, `start_offset`, `end_offset`, `video_id`, `success`. Not stated in the pages we could fetch: chunk size rules, file size limit, error shape for a wrong offset.
- Needs a live check on an authorized test account (Doc120, PAUSED only): that `start` returns `video_id` and the first range; the range size Meta returns (if it is smaller than 8 MB, more calls than estimated are needed); that a chunk with the profile's token and multipart works in one page request; that a wrong offset is refused with the expected offset in `error_data` (otherwise resume falls back to stopping); that `finish` accepts `title`/`name` and the title appears in the library; that a 250 MB and a 1 GB file process and pass the readback; the profile's memory during the 1 GB upload; Meta's real maximum size for this edge (we assume the 4 GB the task states and cap at 1536 MB).

### ACL-1880 — campaign-local Facebook Page selection (2026-10-13)

The builder reads available Page names and IDs through `/api/account-pages`, using the existing read-only profile session and profile lock. New campaign cards start with Automatic; duplicates copy other settings but reset Page selection. The first Page in the destination account's returned order is shown and resolved before creation. Explicit campaign choices never write the account map. Legacy non-builder drafts retain map fallback.

Loading failures and confirmed empty lists are distinct and support Refresh pages. A truncated discovery result is refused. API preflight still validates an explicit Page's membership, and its existing creative readback validates the resulting identity. Instant Form caches are keyed by the resolved Page ID, including in Automatic mode.

RPA resolves the selection before campaign creation and passes the ID and unique display name to the recorded flow. Every ad verifies Identity; failure is non-retryable and not publishable. The existing sanitized `tests/fixtures/ads_manager/simple_create_2026_09_27.html` supplies regression coverage for the captured collapsed Identity summary. Picker interaction has offline native/custom-control tests; a live alternate-Page picker pilot has not been performed. Unrecognized or ambiguous controls fail closed. Other RPA engines are refused for builder Page selections until they implement Identity verification.

ACL-1897 applies the later-created ACL-2105 ordering to campaign-local Automatic:
the first merged usable Page wins, ad-account Pages before profile Pages, without
an inherited map overriding it. The option names the resolved Page. RPA carries
that Page through the existing recorded Identity check, and its result names the
Page and origin. A profile Page first needs the same constrained account-and-Page
validate-only proof used by the API; refusal or unknown proof stops before the
RPA creation flow. Proof is scoped to the current build, never the picker cache.
A confirmed-empty Automatic Website RPA list keeps its existing Identity behavior;
API Website shells, Forms refusals and legacy non-builder map fallback remain.

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
