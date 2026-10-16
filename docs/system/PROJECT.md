# Adopy Campaign Launcher — current system

ACL is a Windows desktop-installed application with a local web dashboard for
campaign managers. It reads performance data, prepares campaigns and drives
Multilogin's browser through controlled automation. It is not the shared Task
Tracker server, even though a legacy Tracker implementation remains in this repo.

## Components and current behavior

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
- `src/analysis_agent`: statistics workbook reading, reports and reconciliation.
- `src/campaign_bridge`, `src/upload_agent`: campaign input/output preparation.
- `src/fb_ads_rpa`, `src/meta_campaign_agent`: Ads Manager interactions, verified
  profile identity, draft preparation, queues and browser lifecycle. ACL-1250/#247
  confirms audience age/country values within their own field and opens the correct
  collapsed Locations/Languages control, preserving adjacent fields. Later hover-only
  controls remain ACL-1251; the deferred real pilot remains ACL-1215.
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

### ACL-1896 — Instagram placements need an Instagram account on the Page ([issue](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/896))

Live finding on Doc120 (2026-10-14): the API publish wrote the campaign, ad set and creative PAUSED, then Meta refused the ad (Graph 100, subcode 1772103, "Instagram Account Is Missing") because the Page had no Instagram account and the creative carried no Instagram identity. The creator always sends Instagram feed, Stories and Reels placements.

- Read-only discovery (`src/fb_api/instagram.py`), at most two Graph reads, cached per Page for the run: the Page's `connected_instagram_account`, `instagram_business_account` and `connected_page_backed_instagram_account` fields (one read); only when all are empty, the ad account's `instagram_accounts` edge (for the message only; an account listed there is not assumed to belong to the Page). A failed read is "unknown", never a pass. The per-campaign create budget (150 calls) and the Check plan reset per draft absorb the two reads.
- New server pre-flight rule `instagram_identity` (shared table `preflight_rules.py` and its JSON; the page draws server rows from the table, so `report.html` needs no change). Plans with Instagram placements and no confirmed identity are refused in Check plan and again before the first write: "This Facebook Page has no Instagram account, and the ad is set for Instagram placements. Connect an Instagram account to the Page, or wait for manual placements (ACL-1772) to publish Facebook-only." An unconfirmed read says Meta did not confirm one. When the Page itself is not settled the row is "unknown" (the Page row already blocks).
- With an identity the creative carries `object_story_spec.instagram_user_id`, and the readback compares it. The closed write policy (`write_policy.py` and the page helper in `token_capture.py`) gained exactly one field: `instagram_user_id`, a string of 5-25 digits, only as a direct key of a creative's `object_story_spec`. `instagram_actor_id` is refused everywhere. No Page-backed Instagram account is created; that would be a write and needs an owner decision.
- Documentation evidence (developers.facebook.com, 2026-10-14). Certain: `instagram_user_id` is a numeric string field of the creative's `object_story_spec`; the ad account `instagram_accounts` edge is read-only and lists IGUser nodes; the Page node has the three fields above. Not documented: `instagram_actor_id` (not used) and Page edges `instagram_accounts` / `page_backed_instagram_accounts` (not used).
- Needs a live check on an authorized test account: that the profile's token returns the three Page fields; that Meta accepts that id as `instagram_user_id` (including a Page-backed id); where the creative read returns it (`object_story_spec` or the creative's own `instagram_user_id`, both are compared); behaviour on a Page with an Instagram account the ad account cannot use.
- Known limit: a creative left by an earlier failed run (found by name) is reused as is; if it lacks the Instagram id, the readback reports the mismatch instead of silently passing.

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
[ACL-1566](../repository/BATCH_INTEGRATION.md). Campaign runtime, employee
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

## Maintaining this document

Describe implemented behavior and limitations here when changing the system. Preserve
plans under original specs, label them as plans, and record what/why and ticket/PR
links in CHANGELOG.md. Process-only changes belong in ../repository/CHANGELOG.md.

Repository instructions have a separately prepared direct-main route described in
[PROCESS_INSTRUCTIONS.md](../repository/PROCESS_INSTRUCTIONS.md). Eligibility uses
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
