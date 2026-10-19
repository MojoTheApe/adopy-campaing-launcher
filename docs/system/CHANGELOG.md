# System history

## Instagram identity for Instagram placements - ACL-1896

Check plan now refuses a plan with Instagram placements when the chosen Facebook Page has no Instagram account (Doc120 live finding: Meta 100/1772103 after the campaign, ad set and creative were already written). New server rule `instagram_identity`; at most two read-only Graph calls per Page, cached for the run; a failed read is a refusal, not a pass. When an account exists it is sent as `object_story_spec.instagram_user_id` and read back. The write policy allows that one numeric field in both layers (Python and page helper), refuses `instagram_actor_id`, and creates no Page-backed account. Details and the live-check list: `docs/system/PROJECT.md`, ACL-1896 ([issue](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/896)).

## ACL-1381 — Consistent Builder ad-text wording — 2026-10-18

Complete the older text-label request while preserving ACL-1769's later numbered
Primary text 1/2 interface and separate Headline display. Search/help and empty/error
messages now refer to ad texts consistently. Library keys, copy/copy2 fields,
selection, draft restoration and submission behavior are unchanged.
[Task](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/381).

## ACL-1971 — Explicit tooling fast/full plan — 2026-10-17

Document the existing tooling check's exact commands, omissions and correction
workflow while retaining full applicable ACL/installer admission. Add the missing
parsed-workflow activity regression: PR metadata edits run linkage only, while a
new code head retains candidate verification. Existing aggregate negatives,
producer names, triggers, policies and runtime behavior stay intact. This records
project verification behavior; it does not establish a cost saving or delivery.
[Task](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/971).

## ACL-1924 — Published-package acceptance continuation — 2026-10-15

Add a durable authorized release workflow using the maintained installer publisher
and updater. Its isolated Windows acceptance downloads the actual signed-channel
previous/current packages, verifies the frozen release SHA/tasks, runs real
installation/update/restart/recovery and data preservation, and emits the native
receipt only on success. Existing same-candidate mechanism tests and employee
update behavior are preserved; live rollout and business pilot are not asserted.
Parent ACL-1923 retains actual activation and publication verification.

## Builder: pre-flight audit card that blocks Publish - ACL-1819

- Every check is defined once in `src/fb_api/preflight_rules.py` (id, label, severity, field, hint,
  client or server side). The same table is exported to `preflight_rules.json` and rendered into the
  page (`window.CB_PREFLIGHT_RULES`); a test fails if the file drifts from the table.
- The editor has a "Pre-flight audit" card for the selected campaign (red cross = error, orange "!" =
  warning, green tick = ok, grey "?" = not checked) with an "errors: N" badge, the same badge in the
  campaign tree, and a click on a line focuses its field.
- Client checks run instantly on edit and never touch the network: country, link (https, well formed),
  tracking parameters (warning), media chosen, primary text, headline, headline over 40 and primary text
  over 125 characters (warnings), CTA allowed for the objective, daily budget, age 18-65 with min <= max,
  unique ad names, Instant Form chosen.
- Account checks come from the read-only `/publish-plan` (the creator's own functions, no POST): account
  ACTIVE, Page on the account, pixel (website) or ACTIVE form (Instant Form), country / language /
  interests resolved with exact matches, media readable, file type and size, video length (over 60 s is a
  warning), vertical 9:16 file (warning), daily budget against Meta's minimum, existing campaign with
  other settings. They are cached per campaign draft revision and refreshed only by the "Check" button
  and right before Publish - never on a keystroke and never on a timer. After an edit they show
  "Edited since the last check".
- Minimum daily budget: Meta does not publish a per-currency table. Only USD 1.00 (impressions billing)
  is listed; any other account currency is "unknown" (it does not block). Extend `MIN_DAILY_BUDGET`
  from Ads Manager when a currency is confirmed.
- Gate: Publish and Publish all stay disabled while any selected campaign has an error, and the reason
  is shown under the button. Warnings never block and are listed in the confirm dialog. Under
  `ACL_FB_BACKEND=rpa` only client checks run (the card says "Account checks need the API backend")
  and the RPA publish itself is unchanged; the gate looks at client errors only.
- The gate fails closed: if the audit state or rule table is missing, Publish is blocked with
  "Pre-flight audit unavailable — reload the page". Retry of an unanswered submission goes through the
  same gate. Client ad names follow the server's naming (creative number from the file name plus the
  text), and the vertical-crop check honours the video's `tkhd` rotation.

## Builder: Instant Forms - use an existing form of the Page (API backend) - ACL-1775

- The ad set has a real "Conversion location": Website (the default, unchanged) or Instant Form.
  With Instant Form every ad gets a form picker filled from the Page's forms (archived, deleted and
  draft forms are hidden) and a "Refresh forms" button. The forms are read only when a person
  presses that button (one read per press, no polling). With `ACL_FB_BACKEND=rpa` the control is
  disabled with the reason "API backend only"; a stored Instant Form choice is never dropped
  silently, the server refuses it out loud. Website values stay in the draft while Instant Form is
  chosen (and the other way round), but only the chosen destination's values are sent.
- Reading: `GET /{page_id}/leadgen_forms` (id, name, status, locale, created_time), read-only,
  through the same client and session. A pure capability probe (`fb_api/forms.py`) turns the answer
  into a clear text: "This Page's forms can't be read with the current session", or "This Page has
  not accepted the Lead Ads terms" (the robot never accepts terms and never creates forms - that is
  ACL-1776). Whether the token captured in the profile can read the forms is NOT confirmed yet: it
  is checked on the 9095 test accounts.
- Creating: ad set `destination_type=ON_AD`, `optimization_goal=LEAD_GENERATION`,
  `promoted_object={page_id}` (no pixel); the creative's `call_to_action.value` carries
  `lead_gen_form_id`. Everything stays PAUSED. Before any write the form of every ad must be a
  usable form of the plan's Page; another Page's form, an archived form or unreadable forms stop
  the campaign with 0 writes.
- Write policy, narrow change in both layers (Python `write_policy` and the page helper in
  `token_capture`): `lead_gen_form_id` is allowed ONLY in `adcreatives` ->
  `object_story_spec.{link_data|video_data}.call_to_action.value`, as the only key of that value,
  a numeric id, and only when the caller says the ad set of the same plan is ON_AD (`on_ad`). It
  stays forbidden everywhere else; creating forms (`/{page}/leadgen_forms`) and terms remain
  outside the allowlist.
- Readback adds the creative's form id, "the form is listed under the Page", and for the ad set
  the destination, goal and promoted Page. "Check plan" shows the form name and id.

## Campaign commands: human-confirmed on/off, budget and bid with readback - ACL-1800

Campaigns are created published and PAUSED through the API; a person switches them on or off and
adjusts budget or bid from the dashboard. Server side and a confirm-dialog helper only; the Home
table that calls them is ACL-1798. Only with `ACL_FB_BACKEND=api`; under `rpa` every endpoint answers
a clear refusal.

- `POST /campaign-commands/preview` returns an immutable diff with a revision id: affected objects,
  before -> after, currency, skipped objects with the reason (for example "Automatic · no manual
  bid", "Budget is set on the campaign") and parent constraints ("On · Campaign paused").
- `POST /campaign-commands/execute` takes the revision id and an idempotency key, re-reads every
  object first and writes nothing if any value changed since the preview. One write per object,
  never retried, then a readback. `GET /campaign-commands/{op_id}`: Preview, Running, Verifying,
  Succeeded, Failed or Outcome unknown. A timeout is reconciled by readback only.
- Enabling a child never enables its parents. The robot adds no rules and activates nothing.
- Separate narrow write policy (`write_policy.check_object_update`) and a matching page-helper
  policy: POST to the id of an existing object, exactly one of `status` (ACTIVE or PAUSED),
  `daily_budget`, `bid_amount` (positive whole minor units). No edges, no creation, no DELETE.
- History of operations (actor label, time, object, before, requested, saved, result) is kept in
  `campaign_command_history.jsonl` in the local config dir. The page helper is `acm_commands.js`
  (`acmCommands.preview/execute/poll/confirm`).
- Home campaign table (ACL-1798) is wired to the commands: the on/off switch, budget editor (only on
  objects that own a budget), bid -/+ editor (only on bid cap and cost cap; automatic bidding shows
  "Automatic · no manual bid") and a batch bar for the selected rows (one account, capped by the
  call limit). Each goes preview, confirm dialog, execute, poll; the row changes only from the
  confirmed readback, and a failed or unknown outcome keeps the previous state. Duplicate and Copy
  stay disabled (ACL-1773). With the rpa backend or a non-loopback bind the controls stay disabled
  with the reason (`GET /campaign-commands/availability`).
- Under `ACL_FB_BACKEND=api` accounts and campaigns are one tree inside each country block. The account row of
  the country's accounts list is the parent: clicking the row (anywhere except its own buttons, inputs and the
  note), the chevron, or Enter/Space on the focused row expands that account's campaigns, then ad sets and ads,
  in place (`aria-expanded`). Expanding only shows what is saved and never reads Meta. There is no separate
  campaign table and no account bands any more: the account row carries "Read campaigns", the snapshot age, one
  status/error line and, when the warm-sessions API is present, the profile chip with "Open profile". Search,
  delivery filter, column-group jumps, Expand/Collapse all, columns and the batch bar sit in one compact toolbar
  above the accounts list; "Read campaigns in <Country>" reads the accounts one at a time. Horizontal scroll and
  the pinned name width are shared by all open accounts. Columns follow the design prototype (Delivery,
  Duplicate, Bid control, Budget, Optimization hint, Alerts, Goal CPA, Benchmark CPA, BI, Meta, Tracking); Goal
  CPA comes from My Goals and Benchmark CPA from `config/benchmarks`, hints and alerts are advisory text computed
  from the numbers on screen, and a missing input is a dash with the reason. Results of commands and read
  failures are one compact line with Details; common causes (Multilogin not running, profile opened by hand,
  closing, cooldown, logged out, checkpoint, permission, stale preview) have short friendly messages in one
  place, `acmCommands.friendly`, and a row failure with the same cause as its account shows only "Profile
  problem - see account above". `GET /campaign-commands/history?account=` backs "What was changed here".
- Backend switch: the country table and "Read campaigns in <Country>" render only with `ACL_FB_BACKEND=api`. Under `rpa` (the default) the previous per-account list from Read spend, with its Off/On chips, "Turn off?" flow, log and coverage note, is unchanged; the two are never shown for the same country.
- The first live check runs on the 9095 test dashboard with test accounts, after the owner agrees.

## Explicit official-token application inspection — ACL-1879

- A narrow verifier and standalone CLI inspect an explicitly supplied subject token
  against an expected App ID, using a separate App ID|App Secret inspector credential
  and an explicit Graph version. Inputs use hidden prompts or explicitly named
  environment sources; rejected arguments and fallback input do not expose values.
- One fixed HTTPS GET disables ambient proxies, cookies, redirects and retries.
  Bounded body reads and socket timeouts return sanitized typed outcomes, never raw
  upstream bodies, URLs, free-text errors or credentials. DNS/header timing retains
  the standard-library limitation documented in the runtime procedure.
- Validity, exact application identity, known token type, expiry and scope evidence
  remain separate from account/read/publication readiness, which is always unchecked.
  Offline tests execute the public verifier, real urllib handler chain and real CLI
  entry point with fake HTTPS; no live account or deployment is verified.

## Account-label ID suffix keeps existing alias precedence — ACL-1824

- A long numeric account-ID suffix is removed only after the existing tail lookup
  finds no candidate. Exact names and configured name rules retain their priority;
  ambiguous tail matches remain unmatched instead of selecting the suffix alias.
- Explicit account links continue to select the country before workbook resolution,
  and account rows and country totals use the same resolved alias without duplicates.
- When that country has no rows in the selected period, the elsewhere diagnostic
  also uses the already-resolved alias. Unresolved or ambiguous labels keep their
  original identity; another country cannot supply a guessed suffix match.
- Required CI exposed an inherited logging-filter leak when the added regressions
  changed shard assignment. The pilot tests now restore each handler's pre-test
  filters, so later tests see their own logging behavior. Production redaction and
  the routing-log assertions remain unchanged.

## Media wizard: Crop -> Media loop no longer ends in a false "stuck" stop - ACL-1736

Run 445fa5460ce6 (Facebook_5265, ad 2, library pick): Next on Crop led back to Media, then
Creative setup -> Media -> Creative setup -> Crop. The captures show the library pick was NOT lost
(the tile is still "selected", "1 selected" on Media), and the second Crop was a fresh arrival where
Next had not been pressed. The ACL-1551 guard ("Next on Crop was already pressed") took it for the
Crop where Next hung and closed the wizard with Close, leaving the ad without media.

- When the wizard leaves Crop and later comes back to it through another step, the Crop step is
  re-checked (Original, no expansion) and Next is pressed once more, for a single-file plan.
- A second return to Crop, a post/story pair (Replace) or a Crop where Next already hung still
  ends in the honest `crop_wizard_stuck`, with the exit reason `looped_back`.
- The wizard telemetry row gets `looped_back_to` (first earlier step the wizard went back to, e.g.
  `Media`), also inside `exit`. No new clicks: only the existing Next on a Crop that is on screen.
## Window recovery enlarges a window restored below the editor minimum — ACL-1727

- Run 4db2039f760d (Facebook_9403A): a minimised window was restored to its normal size
  (inner 1117 px, minimum 1168) and the run stopped at `window_too_small`.
- Now, when the window is un-minimised but still below the minimum, recovery enlarges it through
  CDP `Browser.setWindowBounds` and measures again; `window_too_small` is recorded only if it is
  still too small. The branch is chosen from the real window bounds: a window on screen is
  maximised in place; a window parked off-screen grows proportionally and is re-parked so its right
  edge stays at or left of 0; an already maximised or fullscreen window is not touched. A Mimic
  scale drop (dpr below 0.85 of before) is undone and not retried.
- The `window_bounds` diag records `enlarged: true` and `enlarged_to` (the new size). No new
  windows are opened and the window is never moved to another monitor.
## Campaign open: the AI-guided / Manual chooser counts as Create taken - ACL-1726

- Run 4db2039f760d (Facebook_9403A, 08.10): Create now opens Meta's "Create new campaign" chooser
  ("Meta AI-guided setup" / "Manual campaign setup"). It was not recognised, so Create was pressed
  again and the open budget ran out (`objective_leads` after 103 s, about 2 minutes lost per campaign).
- The chooser is now `create_took` with note `ai_manual_chooser` (ok), and `_wait_objective_dialog`
  returns at once while it is on screen. The existing `manual` step then picks Manual setup. The
  AI-guided option is never chosen.

## Account map: notes only per account, Enter saves — ACL-1721

- Country rows on the account map no longer show or edit a note. Stored country notes are kept in
  `account_notes.json` untouched (the backend and the `countries` map are unchanged).
- Account notes work as before, except in the editor: Enter saves and closes, Shift+Enter inserts a
  newline. Esc, outside click and Ctrl+Enter behave as before.

## Notes: a country note, and nothing on screen when a note is empty — ACL-1633

Owner review of ACL-1620 (06.10).

- Country notes: each country header row on the account map takes a note in the same style. They
  are stored in the same local `account_notes.json` under a separate `countries` map. An old file
  (account notes only) is read as it is and gains the `countries` key on the first save; nothing is
  lost and no migration step is needed.
- No noise: an empty note shows nothing. A faint pencil appears only while the row (or the note)
  is hovered or focused. A filled note shows its first line with an ellipsis; the full text is in
  the tooltip.
- Simpler editor: only the textarea, a small "Delete" and a one-word state ("Saving", "Saved",
  "Save failed"). The cursor opens at the end. It saves on outside click, Esc or Ctrl+Enter; there
  is no Save or Close button. Delete asks nothing and shows "Deleted - Undo" for 5 seconds.
- Click or Enter opens the editor, including from a country header (the country does not fold).
- Kept from ACL-1620: escaping, the strict read on save with the `.corrupt` backup, the in-flight
  guard, columns Account 28% / Note 9%. Notes are never sent anywhere: the only call is the local
  `POST /api/account-notes` ({account, text} or {country, text}).
- The note is drawn as an input box (field border, radius, light background, one line with an
  ellipsis). An empty note shows the same quiet box with no text, so it is always clear that it can
  be clicked. The country box stays compact beside the name.

## Honest dashboard loading stages and ACM identity — ACL-1865

The report-loading screen uses the existing centered ACM logo above the product
name, followed by workbook reading, statistics processing and dashboard preparation.
`/report/status` adds a `stage` and `waiting` flag. Progress comes from real pipeline
boundaries and is bound to its background job, requested period and captured manager;
another request's progress is never shown as this page's completed work. A cached
workbook skips reading/processing, and period-only loading shows preparation.
The existing ready/error polling and automatic dashboard transition remain in place,
with responsive light/dark styling, reduced motion and connection retry messages.

## One metric set in every attribution table, Totals and Campaign managers on Home — ACL-1652, ACL-1654, ACL-1656

Every attribution table now shows **Spend, Leads, FTDs, CPL, CPA, CR** in this order, with the same
names, formulas and number format. Decisions: owner, 2026-10-06 (issue #652).

- **One source.** `src/analysis_agent/metric_columns.py` holds the header lists, the three formulas and the
  number format; templates take headers and cells from `_metrics.html`; the JS twin `acl6Metrics` in
  `report.html` is pinned to the Python helper by `tests/test_metric_columns.py`.
- **Formulas.** CPL = Spend / Leads, CPA = Spend / FTDs, CR = FTDs / Leads, "—" when the divisor is 0.
  Money is whole dollars rounded up everywhere (the asset-landing drill used to round to nearest).
- **Fixed.** A zero CPA showed "$0" in Targeting, Creatives and CM Comparison; CR 0.0% showed "—" in the
  account row and tree rows (it is a real number when there are leads).
- **Tables.** Home country rows (+CPL), Home and Manage account table ("FTD" is now "FTDs", Account spend stays),
  tree rows, CM Comparison, Assets, the asset-landing drill, Landings, Trends, Combos, Targeting, Creatives,
  Optimization ad sets and the three drills. Net, ROI and Player Value stay after the six metrics.
- **Trends**: two-line cells (last days bold, whole period grey), and "Trending down" is shown.
  **Combos**: one identity cell (offer, device, age, category chip; interest and creative below).
- **Drills** scroll horizontally inside the table when the builder is open.
- **Home headers** are in normal case (they were upper case, which turned "FTDs" into "FTDS").
- **Totals row** (ACL-1654) under the country rows, before Available accounts: Spend, Leads and FTDs are summed,
  CPL/CPA/CR are computed from the sums. "Account read" is summed once per account and only when every
  read account is in one currency; otherwise "—".
- **Campaign managers** (ACL-1656) at the bottom of Home, collapsed by default (state in localStorage):
  country rows are the sum of all managers, the current manager is highlighted. Same rows as CM Comparison.
- **Ad drills**: the Date column and the dead "+ 🛒" button are gone.
- **Copy for group** (button and hint under a red account status) is hidden for now; `SHOW_COPY_FOR_GROUP = True` in `app.py` brings it back. Screenshot and status code are unchanged.
- Python edits (`morning_report.py` trend builder, `app.py`) need a `serve.py` restart.
## next_to_ad confirms the ad level and names the reason on that step — ACL-1693

Manager laptop LAPTOP-UA006, run 81c1bfbb5456 (07.10, window 1536x730 at dpr 1.25): `next_to_ad`
was recorded ok, but the editor stayed on "Ad sets - Edit" (`levels: ["ad_set"]`). Five `ad_name`
tries ended in `not_on_ad_level`, then every creative upload ended in `wizard_not_opened` - about
four minutes lost, and the failure was named on the wrong step.

- After the Next press the robot reads the editor level. Still on the ad set: it closes the
  notices that may cover Next (the "WhatsApp Status included" notice was open over the page) and
  presses Next again, up to three presses in all.
- Still on the ad set after all presses: the campaign stops at `next_to_ad_stuck_on_adset` with a page
  capture and a reason (`next_covered`, `next_outside_viewport`, `dialog_open`, `next_not_found` or
  `next_clicked_no_change`). The stop is blocking and never replayed: a rebuild would leave another
  partial draft in a live account. Plain `next_to_ad` (retryable) stays for a page that cannot be read.
- The level is re-read after every press and before failing, so a slow transition is not a failure.
  Page markers (`/editor:<level>`) decide; the wizard sidebar guess is used only without markers.
- A page whose level cannot be read is not pressed blindly (the old behaviour is kept).
- The browser zoom is not touched. Evidence is not enough to pin the cause (see the PR for the
  captures to request from the laptop).

## Classic editor: ads 2-4 are created again, the ad set row's Action menu is opened by hover — ACL-1691

On 07.10 (Facebook_5265 run f2c513f0a22d, KW_TRL on 9403A) `create_additional_ad` failed with
`create_menu_item_not_found` (`menuSurfaces: []`, click point on an ad's text) and ads 2-4 were not created.
Two causes on the captures:

- The classic tree draws the row buttons only under the cursor (as the single-screen editor did, ACL-1630):
  there is no "Action menu" in the DOM until the pointer is over the row. The step waited 30 s for it
  to appear and then fell back to a blind coordinate click.
- That fallback took the first text equal to the ad set name, sorted by height. The breadcrumb header above the
  tree carries the same name (y = 22), so the click landed on the breadcrumb's ad name, not on the row.

Fix: the tree counts as ready once the ad set row is there; the robot hovers that row (mouse move only, no
press, no focus, at most 3 tries, stopping as soon as the button appears), then the existing Action menu ->
"Create ad" path runs. If the row still shows no button the step blocks with `adset_action_menu_not_shown`
("The ad set row in the left tree shows no Action menu button, even with the cursor over it; nothing was
clicked.") instead of clicking blindly. The legacy coordinate fallback now ignores the breadcrumb.

After that block the caller does not adopt an existing "New Leads Ad" row (ACL-1641's `_adopt_shell_or_recreate` is
not called): nothing was clicked, so there is no new shell, and adopting another ad would report a false "created" and
overwrite it. It makes one clean re-create instead (only when the tree shows no new shell), and if that blocks too the
ad fails as `adN_create:adset_action_menu_not_shown`. The reason is kept per thread (`last_create_block()`), because two
accounts publish at once in threads of one process; the caller reads it once per attempt.

Where the blind coordinate fallback is still reachable: when the tree readiness check times out (30 s, no ad set
row) or when the hover finds no ad set row (`hidden`, or no point after a timeout). With the breadcrumb and
left-third filters the only thing it can click there is the ad set row itself; it cannot reach a breadcrumb or an ad row.

## Placements: a complete Stories list that flickers is still read as "not offered" — ACL-1684

On FacebookDoc120 (run 4e4969867d28) "Messenger Stories" was judged missing and a good run became a
false do-not-publish: the list was complete (end proven, nothing hidden) but never read as stable.

- Meta folded the Stories list twice while it was read; each re-opening spent the wait counted from
  the first read, so the 1.5 s quiet window never fitted. The wait is now counted from the last
  re-opening, with a hard limit of 15 s from the first read.
- The row count no longer takes part in "stable" (it flickers while Meta remounts the list; the
  end proof already covers hidden rows).
- Unchanged: an incomplete list, or an offered but hidden placement, is never "not offered".

## B97: when Meta shows no media counter the robot refuses honestly; it waits for the Crop cards — ACL-1669

Two live B97 runs of the same build (07.10) failed in the media step. In both the robot read a step body once, immediately. Only the Crop case is established as a late draw.

- Library pick (run 3edaf71b0613): the "N selected" counter was read 0.1 s after the Media step opened, found nothing and refused (`selection_state_unavailable`), so no ad got media and `url_field_missing` followed on each. The robot now waits up to 4 s for the counter. If it is still missing, the robot refuses honestly instead of guessing: it neither clicks a tile nor uploads, and the ad ends with the named blocking reason `library_selection_unreadable` (the media must be attached by hand). A safe automatic fallback needs a live capture of the missing-counter state; that is follow-up work. Two review rounds found that an upload "when no tile is marked" cannot be proven safe from the markup we have (hidden or unrendered selections), so it was removed.
- What is NOT established: that the counter was merely "late" in run 3edaf71b0613. The real 07.10 picker capture of the other run (bc4302862ee7, wizard open on Media at 0.1 s) shows the counter readable ("1 selected" / "0 selected") and the selected tile wrapped in `data-test-decoration="selected"`. Why the counter was not found in 3edaf71b0613 is unknown: no picker markup of that run exists. The 4 s wait is a bounded response, not a proven fix.
- When the counter is missing the robot does not search the library for a known copy. A reuse-only ad that reaches that point is named `library_not_searched_counter_unreadable`, not `library_has_no_copy`, and it does NOT trigger the ACL-1637 re-upload (that stays for a searched library that answered "no copy"). The `counter_missing` flag is per call, so per ad: it is reset at the start of every `upload_ad_creative`.
- Crop (run bc4302862ee7): the pick worked, but Crop was read while Meta still showed loading placeholders (no Square/Vertical cards) and ended `crop_replace_button_missing`. The step now waits up to 20 s for the cards; if they never appear it names the missing card. The 20 s figure is a cap, not a measured load time.
- `url_field_missing` is reported as `<ad>_destination:url_field_missing_no_media` (still blocking, with a sentence for the dashboard) only when the creative step failed AND the empty Media state is positively seen on the page (Meta's "Please specify the media" text, or the single-screen editor's `empty` state on a mounted block). A Media block that cannot be read or is unmounted keeps the plain `url_field_missing`.
- ACL-1637 did not cause this: the counter check dates from 17.09 and ad 1 never goes through the ACL-1637 path.
- Bench: `wizard_media_picker_selected_2026_10_07.html` is cut from the real 07.10 capture (counters emptied; expected result is a REFUSAL). `scripts/capture_to_fixture.py` now keeps `data-test-decoration`, only when its value is `selected` (the scrubber used to drop Meta's selected mark, which is why the old 04.10 picker fixture looked unmarked). The Crop-loading fixture is hand-reduced from the real captures and keeps their three `role=progressbar` placeholders.

## Conversion location: the menu is opened by click or script, and a failure names its reason — ACL-1572

`conversion_website_pick: not_selected` failed campaigns on Doc21, Elf403, KLD_BFL_B91 and
Facebook_5265 (01-06.10). Telemetry showed `menu_closed: 8, reopened: 0`: the menu never opened,
so none of the eight pick methods ever ran against an open menu.

- Opening the menu now escalates: the existing click, then a single script press; the next opener runs only when the previous one left the menu closed.
- "Menu is open" also reads the trigger's `aria-expanded`, not only the Single/Multiple headings
  (a menu without those headings was taken for closed and then clicked shut).
- Focus is released from the ad set name field before the trigger is pressed (its suggestion list
  swallowed the first press on the failing captures; no Escape, it closes the wizard).
- After three failed openings the step stops instead of spending 60-190 s on the remaining methods.
- A failed pick says why, in English, in the `conversion_website_pick` note: the menu would not
  stay open; Website is not offered in the open menu; Conversion location is not on this screen
  (the ad editor was showing, as on Facebook_5265).
- Review fixes: the script press is one click (two would close a toggle trigger). The keyboard (focus + Enter) opener was dropped entirely: Enter could accept a suggestion in the ad set name field and rename the ad set, so the only openers are the click and the script press, and the name field is blurred first (in the frame that holds it). As a single safety check, the ad set name is read once at the start of the Conversion location step and compared at its end, across all retries; a change stops the step (the recorded flow returns the blocking, never replayed `conversion_website_name_changed`) and says only that the name changed, never the names. "Not on this screen" is returned only when the field is truly absent (a page script failing in every frame is its own read-error reason); "would not stay open" only after both openers pressed and failed; the reason is added to the step's error text, not only telemetry; the step state is per thread.
## Advantage+ audience on WhatsApp campaigns: a collapsed Age row with a note is opened again — ACL-1638

The classic ad set editor failed the `advantage_plus_audience` step (`still_on`, campaign not published)
on 06.10 on FacebookDoc120 and Facebook_9403A. These were two different failures.

- Doc120 (and 4285247f763a on 05.10): on WhatsApp campaigns Meta writes the collapsed Age row as
  "35 - 65+ · Unknown age on WhatsApp: Excluded" with a "Your suggestion" label. The robot only
  recognised a summary that ended right after the bounds, so it never opened the row
  (`age_row_opened: false`, `age: []`) and never unchecked "Use as a suggestion". The summary now
  accepts a note after the bounds, so the row is opened, the suggestion is unchecked and the badge is
  re-read as before.
- 9403A (d4f9d5f016c1, 327c0b934cb3): the age was already a hard 35 - 65+ and the checkbox was off, but
  the badge read "Advantage+ on" again after reading "off" before the age step. Nothing is left for the
  robot to click, and the cause of the flip is not known from the captures, so the step still blocks the
  draft. A failure now carries `age_suggestion_checked` and `mode` in its telemetry, and a read-only
  `audience_badge_probe` row after the age, location, languages and targeting steps shows which step
  flips the badge on the next occurrence.

## AI rescue core module: ask Claude for one safe action when the robot is stuck (not wired in yet) — ACL-1613

The last rung of the fallback ladder: when a step fails on a screen the robot does not know (a
collapsed card, a stray dialog, a moved button), `src/fb_ads_rpa/ai_rescue.py` can ask Claude what
to do. This first PR is the standalone module only; nothing in the flow calls it, and
`ACL_AI_RESCUE` defaults to `off`.

- The model gets text only, never a screenshot: open dialogs, headings and up to 150 visible
  controls (12k characters at most) with role, label, state and section, each with a stable index.
- Everything sent is scrubbed first: ids, `act=` and URLs, e-mails, tokens, account labels,
  every word that contains a digit, the text after a `|` in a name, field values (Page, Pixel,
  account comboboxes), links and campaign-list rows (never shown to the model). Free names
  without digits are replaced only when the caller passes them in `names=`.
- The model answers with exactly one action: click, expand, close dialog, press Escape, scroll to,
  or give up. Safety is in code, not in the prompt, and when in doubt it refuses:
  - a positive allowlist is the primary gate (`allow` in `config/rpa_forbidden_zones.yaml`, EN/ES/PT):
    expand only a collapsed section card whose name is on a short list (Audience, Placements, Budget,
    Locations, ... and the ES/PT names; never Create new, Share, Advantage+ toggles); links are never targets;
    field labels show only the placeholder; close a real dialog only with Close/Cancel/Not now/Skip/
    Dismiss/Got it/Later/No thanks (never when the dialog mentions discard, unsaved or leaving); click
    only "Show more"/"See more" buttons and a few named tabs; Escape only over a real clean dialog;
    scroll always. Anything else is `not_allowlisted:<label>`, logged for the owner to widen later;
  - the denylist is the second layer, matched on word stems (deleted, eliminara, unpublished) after
    stripping soft hyphens and zero-width characters (publish, delete, remove, discard, terms,
    instant forms, log out, payment, "leave", duplicate, edit, on/off);
  - the check covers every label source of the target and its descendants (text, aria-label,
    title, alt, value, aria-labelledby), the text next to it and the FULL text of the dialog around it;
  - only buttons and tabs are clicked; switches, checkboxes, radios, fields and links never; nothing
    inside a list row; a list page gives up without calling the model;
  - Close works only as "close dialog" inside a visible dialog with no edited fields; Escape needs a
    visible dialog without edited fields;
  - a refused pick never scrolls the page; an allowed one is scrolled instantly, must stand still for
    two frames, is vetted again, and is clicked with `locator.click(position, timeout=1s)` at a point
    checked with `elementFromPoint`;
  - a dialog is always a real dialog (a nested one too); the editor counts as the editor only when it
    holds the name field and covers half the screen; a list page (a wide table or grid) gives up
    before the model is called; shadow DOM targets are refused.
- `ACL_AI_RESCUE=suggest` logs the answer and leaves the page untouched; `act` performs the one
  action and returns, the caller re-runs its own check. Model: `ACL_AI_RESCUE_MODEL`
  (default `claude-sonnet-5-5`), 20 second limit, no retries. No key or SDK means the rung is skipped.
- At most 2 calls per step and 4 per campaign, counted by an object the caller passes in.
- Telemetry: new step `ai_rescue` (mapped to "hold" in the build progress model).
- Tests: `tests/test_ai_rescue.py` with a fake model; fixture `ai_guided_setup_dialog_2026_10_05.html`
  (live capture fb358fda8aab).

## Placements on Meta's layouts of 2026-10-05/06 in the classic ad set editor — ACL-1629

Since 05.10 the placements step failed on several accounts (B90, B91, 9403A, Doc120), and every
failed campaign ended as do-not-publish. Two new layouts, both in the classic editor:

- **Category expanders as plain buttons** (B90, B91): each category row has a
  `<button aria-label="Expand|Collapse">` without `aria-expanded`. The robot set the checkboxes,
  but its "Meta does not offer this placement" check only knew the old expanders, answered
  "collapsed" for ever, and "Messenger Stories" stayed a missing placement. The check now reads
  both expander kinds. A category Meta folds again after a save is reopened; the settle budget
  counts from the first page answer, not from entry (B91: one slow read used the whole budget).
- **A second cause of the same B90/B91 failures: a category box disabled while Meta saves.**
  After a category ("Search results", "Apps and sites") is clicked off, Meta keeps its box
  disabled and still `aria-checked=true` for a few seconds. The step read it once and failed,
  although the box ended in the wanted state (11 of 29 failed runs; every run that also failed
  on "Ads on Facebook Reels" had it). The finish pass now re-reads such a category and clicks it
  again once it is enabled, within the shared reopen cap and time limit; a box that stays disabled
  and checked still fails and is named (`categories_failed`).
- **Final read-back**: after all clicks the step reads every planned sub-item row and the fully-off
  categories (Search results, Apps and sites) from the page once more and passes only if all match
  the plan; a late revert fails the step (`final_readback.mismatch`). A category the plan wants on is
  judged only by its rows: Meta shows its box as unchecked while only some of its placements are on,
  so the finish pass never clicks it (a click would turn all its placements on).
  The ACL-1336 reason is used only when the opened card has no manual-placement entry and carries
  Meta's notice (B90 shows the notice and still offers manual placements).
- **A row disabled while Meta saves** ("Ads on Facebook Reels", 13 runs on 05.10): before a seen
  placement is called missing, the step reads it again, reopens its category and sets it, for up
  to 6 s.
- **Collapsed Placements card in the standalone editor** (9403A): a summary button
  (`aria-pressed=false`) over a hidden body. It was only opened on the single-screen editor path;
  the step now opens it in any editor, and a card that does not open fails as
  `placements_card_not_opened` with an English reason. This is not a fix for 9403A: the opened
  card has no categories at all, only Meta's notice that excluding placements "will no longer be
  available for your ad sets". It is most likely the same cause as ACL-1336 and stays
  do-not-publish; the blocking reason now says "Meta no longer offers manual placement exclusions
  on this ad set (ACL-1336); placements left as Meta set them." The owner decides the policy in
  ACL-1336.
- **Account placement exclusions**: when the card summary still reads "None" after Confirm, the
  step reopens "Placement controls for this ad account" and reads the switch and the four boxes
  back (`excluded_verified_in_dialog`); a lost save stays a failure. When the card has no
  "Account controls" row or "Edit placement controls" button, the report says Meta no longer
  offers them (`account_controls_not_offered`) and the blocking reason names it. If the page call
  itself fails while the row is looked for, the report says it was a read error
  (`account_controls_row_read_error`), not that Meta does not offer the exclusions.
- Tests: `tests/test_bench_placements_1629.py` on two scrubbed captures; bench catalogue rows
  added.
## The same video on several ads gets media on every ad — ACL-1637

06.10, Doc120 (45b9778e65df) and B97 (5f05cf961d37, 152f7df4ef6a): the first ad got the video,
ads 2-4 got none (`same_file_already_uploaded_this_run` / `library_has_no_copy`) and the campaign
ended do-not-publish.

- Cause: a heavy video is compressed first, and the copy that goes to Meta is named
  `<name>.<key>.mp4`. Meta's media library captions the tile with that name, but the "file already
  uploaded this run" path searched only for the original name, so it never found the tile. The
  "do not upload twice" rule then left the ad empty.
- The library lookup of ads 2+ now also searches by the compressed copy's name (found in the
  compression cache, no ffmpeg needed) and looks twice, with a pause, because a fresh upload
  appears in the grid with a delay.
- If the library genuinely has no copy, the file is uploaded again for that ad: once per ad, one
  attempt, and only when the wizard reached the Media step. A wizard that did not open is not
  followed by an upload through the same wizard.
- The per-run failed-upload memo now keeps the failure reason. A file that failed earlier is
  uploaded again only if it never reached Meta (`file_not_landed`, wizard/navigation reasons). A
  file Meta refused (`media_refused_on_ad`), a crop failure, or any failure where Meta saw the
  file is not uploaded again; the ad ends with a named reason.
- An ad that still ends without media now says why in plain English (`library_has_no_copy`,
  `same_file_already_*`, `file_not_landed`, `wizard_not_opened`), is a blocking problem and the
  campaign stays do-not-publish.
- Not changed here: in run 573ab2cf3646 the ad 2 creative step finished at 14:00:17,
  `create_additional_ad` failed at 14:01:38, and nothing was recorded until `step_budget_exceeded`
  at 14:20:38. That 19-minute hang is outside the creative-upload path; it belongs to the
  step-stuck work (ACL-1614) and is handed to its owner.
- Tests: `tests/test_same_video_on_several_ads_gets_media.py`.

## Single-screen editor: default country removal and ads 2-4 on 06.10 layout — ACL-1630

Facebook_5265, three runs on 06.10 ended do-not-publish: `audience_location` failed with
`default_location_left` (left: United States, Open Dropdown) and `ad2_create` with `no_create_control`.

- Locations: the card has only "United States" in every capture - the target country was never
  in it. Meta's new "Open Dropdown" button label was read as a second location (the name of the
  "Remove" row); button labels are no longer part of a location name.
- A country that Enter (no visible suggestions) did not add to the card is no longer a success:
  the robot reads the card after Enter, and a missing country is a failed attempt
  (`audience_location_attempt`, note `not_in_card`), so the retry ladder retypes, instead of a
  "success" followed by `default_location_left`. When every attempt ends `not_in_card`, the closing
  `audience_location` row says `not_in_card` (not `no_location_suggestions`).
- Why Enter added nothing is not known. The search box in the captures carries
  `aria-activedescendant="SA"`, but the attribute is stale: there is no suggestion list and no
  element with id "SA" in the DOM. As a defensive extra, the robot presses the suggestion the box
  points at, only when that suggestion is the country itself ("<country> (Country)", the bare
  country name, or id = the ISO code); a city or region is never pressed (ACL-1390). Whether this
  press adds the country live is unverified.
- The card check counts a country only from its own location row: in the grouped layout a city
  row under the country's heading (e.g. "Riyadh" under "Saudi Arabia") is not the country.
- Ads 2-4: in the captures the ad list rows have empty button slots (no Action menu). When neither
  Create ad nor the ad set Action menu is in the tree, the robot moves the mouse over the ad set
  row (mouse movement only: no click, no keyboard focus) and reads the tree again, up to 3 times,
  stopping as soon as the buttons appear; telemetry `tree.hover` = {tries, woke}. That the buttons
  are drawn on hover is inferred from the empty button slots, not from a live capture. A tab that
  stops answering during the hover is reported as `tab_frozen`.
- The onboarding modal "A streamlined way to create campaigns" was closed by `own_close` before
  the location step in all runs; it is not the cause.
- Bench fixtures `locations_open_dropdown_card_2026_10_06.html`,
  `editor_tree_no_action_buttons_2026_10_06.html`; live check pending.

## Stop ends a job whose robot is stuck inside a step — ACL-1614

05.10, job 1ad447f9: Stop was pressed while the robot sat inside a page call. Stop closed the
Playwright connection from the web thread; the job thread then spun forever inside Playwright
holding the GIL, the whole dashboard slowed to 10-30 s per request, and the job stayed
"cancelling" with its profile slot until the dashboard was restarted.

- Stop only signals now. The connection and the browser are never closed from another thread;
  the job thread closes its own connection when it unwinds. The Multilogin window stays open and
  the profile is never stopped.
- The flow checks the signal in its waits (the shared pause helpers, `before_step`, the
  liveness probe, the sleeps in the Ads Manager flow, simple create and crop step) and raises
  `JobCancelled`, which unwinds through the normal path: a Stop ends within a few seconds.
- If the job thread has not ended within `ACL_CANCEL_DEADLINE_S` (default 60 s), e.g. blocked in
  one call on a frozen tab, the job becomes "Stopped by the user - the robot was stuck in <step>;
  the Multilogin window was left open", its slot and profile lock are released so the queue
  moves on, and the thread is abandoned. If it wakes later it writes no results, errors,
  telemetry or notices and does not touch the slot again.
- The profile lock is not released on abandon (review of PR #622): the hung thread may still
  hold the browser, and two robots must never drive one browser. A job for the same profile
  waits ("Waiting for the stopped robot on this profile to let go") until the old thread exits;
  jobs on other profiles start because the slot is free.
- After a Stop the robot no longer minimises the window or reads the page in cleanup blocks,
  and the ffmpeg compression wait is cancellable (our own ffmpeg child is terminated, never
  Multilogin).
- Known quiet waits (human takeover, teach wait, compression) send a heartbeat, so they never
  raise `step_stuck`.
- A same-profile job waits for the stopped robot BEFORE it takes a queue slot, so a hung profile
  never blocks other accounts; Stop ends that wait.
- Limit: a thread hung forever inside one CDP call (a frozen tab with no timeout) keeps the
  profile lock until the dashboard is restarted; Close profile answers "busy" meanwhile. The
  ffmpeg heartbeat also hides a wedged ffmpeg from `step_stuck` until its compression timeout.
- The page shows "Stopping..." for a cancelling job (never "waiting"/"opening") and the stuck
  reason on a stopped one.
- New signal `step_stuck`: a running job with no telemetry step for `ACL_STEP_STUCK_S` (default
  300 s) records one `step_stuck` row (last step, idle seconds) and sends one Sentry message,
  once per job. The row does not reset the "No new step for N min" clock on the page.
- Tests: `tests/test_acl1614_cancel_hung_job.py`.

## Account map: a free-text Note per account replaces the Setup column — ACL-1620

Owner request 06.10: the Setup column ("ready / needs setup") is not needed; managers want to
write their own note about an account.

- The Setup column is gone. In its place is **Note**: the start of the note on one line with an
  ellipsis, the full text in the tooltip, a faint "add note" when empty.
- Click (or Enter on the focused cell) opens an editor with a resizable textarea and a Close
  button. Close, Esc and a click outside all save; an empty text deletes the note; a failed save
  keeps the editor open with the text and says "save failed".
- Stored per account label on this machine only: `%LOCALAPPDATA%\ACLccount_notes.json`, written
  atomically under the machine lock. It is never in the repo. Up to 10,000 characters per note.
- `GET /api/account-notes` and `POST /api/account-notes` ({account, text}). The same account shows
  the same note in every country; notes survive a report refresh and a dashboard restart.
- Text is escaped on render and written to the page only as text.
- The `ready` / `to_do` fields stay in the row data; only their display is removed.
## Builder creative replacement after a recent refresh — ACL-1651 / PR665

The owner's 2026-10-13 replacement-photo report exposed a missed-search refresh
consumed by the two-second throttle: the search latch stayed set although no
request ran. Misses now share a pending background refresh that waits out that
gap, then searches the current visible query. The ten-second focus limit and one
refresh per miss streak remain; late results do not reopen a closed picker or
change the selected creative or campaign fields. Both Image and Video searches
use the same refreshed list; switching type merely cleared the field/picker and
therefore caused a new focus/search path, rather than performing a separate scan.
Offline browser regressions cover immediate replacement, typing during the wait,
closed pickers and retained campaign settings. No live advertising action or
installed-package delivery is established by these synthetic checks.

## Campaign Builder creative previews no longer freeze the dashboard — ACL-1615

05.10 py-spy: while the Builder loaded video previews, every other request (job status, slots,
the report) waited 10-30 s. `/creative-file` ran on the event loop and walked the whole creatives
root (`rglob("*")`) for every single preview.

- The lookup runs in its own small thread pool, not on the event loop (and not in the report
  pool, so a preview burst cannot starve the report).
- Creatives index: lowercase basename -> relative paths, built with one tree walk per root and
  rebuilt when a directory mtime changes (checked at most every 5 s, immediately on a miss) or
  when an indexed file has disappeared. A path that no longer exists is never returned (publish
  enrichment uses the same finder).
- Behaviour unchanged: path-suffix disambiguation, 404 not found, 409 for duplicates, the
  ACL-1342 realpath / within-root check (done per lookup, so a symlink swapped later is caught).
- Previews are cached by the browser (`Cache-Control: private, max-age=3600`, ETag,
  Last-Modified, 304 on revalidation). Everything else stays `no-store`.
- Video Range requests: the installed Starlette cannot serve them and answered 200 with the whole
  file; `/creative-file` now answers 206/416 for a single range.
- `/api/creatives` and the creative brief already run off the loop; no other route walks the
  disk per creative.
- Tests: `tests/test_acl_1615_creative_index.py`.

## A collapsed Audience card is opened by clicking the card in every editor layout — ACL-1611

05.10, Facebook_9403A (KW_TRL_All_Video_Mix_35_05.10): the ad set's Audience section showed as a
read-only summary card (Locations / Age / Gender / Languages, "Advantage+ on", no Edit button).
The robot looked for the "Further limit the reach of your ads" row inside it, found it 0x0 in the
hidden body five times and stopped. The card was only opened when the single-screen editor was
recognised by its URL; this window (`adsets/edit/standalone`) was not.

- The card is opened in every layout: no summary card means nothing is clicked, an open card is
  left alone.
- The click goes to the card's own title, never to a link or a button inside it ("About
  audiences" is never pressed).
- After the click the robot checks that the card body is shown and holds controls; a card that
  "opened" with an empty body is reported as such instead of passing.
- The step is generic (`simple_create.open_card(title)`), so other collapsed section cards use it
  unchanged; Audience is the first one wired in the non-single-screen layout.
- Telemetry: the existing `simple_card` step now carries `target` and `controls` in its diag
  (`how` was already there). No new step.
- Bench: `audience_card_collapsed_drawer_2026_10_05.html` (real captures of 6ebd77ccaf6a and
  e89bda52e7a5), `tests/test_bench_audience_card_collapsed_drawer.py`.

## A hand-opened window is reopened even when Multilogin is slow to release the profile — ACL-1606

Regression from ACL-1562 (05.10, FacebookElf403): after the robot closed the window through
the Multilogin API, Multilogin still held the profile for a moment, the start answered
"locked", and the robot read that as "opened by hand" again and gave up with the window closed.

- After its own graceful stop the robot marks the profile as its own, so a lock is no longer
  read as a hand-opened window.
- The start is retried for up to ~30 s (1, 1, 2, 2, 3, 3, 4, 4, 5, 5 s) while Multilogin still
  says "locked" / "already running". Any other start error fails at once, without retries.
- Still graceful only: no kill, no other profile touched, no plain browser.
- If the profile is still held after the retries: "Multilogin did not release the profile after
  closing its window — open it again in Multilogin".
- Telemetry: the existing `profile_hand_window` step now carries `start_retries` and
  `start_waited_s` in its diag (no new step).

## At most 2 profiles, a real memory reserve, no duplicate jobs — ACL-1603

Stage 0 of the stable-build plan. On 05.10 "Publish all" for ~20 accounts let 3 profiles
run with 2.4 GB free of 32 GB, Meta tabs froze, and a second "Publish all" queued 10
duplicate jobs.

- Hard cap of **2 profiles at once**, whatever the memory estimate says. Set per machine
  with `ACL_MAX_PROFILES` in the machine `.env` (default 2). The old
  `ACL_MAX_PARALLEL_PROFILES` / `ACL_MAX_PARALLEL_CEILING` are read as fallback names for
  the same cap; a manual number no longer skips the memory check.
- A profile starts only when free memory >= the per-profile use + a **3 GB reserve**
  (`ACL_MEMORY_RESERVE_GB`). Per-profile use is the heaviest recent measurement (not the
  average), floor 3.5 GB (`ACL_PROFILE_MEMORY_FLOOR_GB`), 3.5 GB estimate until a
  profile has warmed up. The check is repeated while a profile warms up (it is counted
  in full). If free memory drops below the reserve during a run, no new profile starts
  (`below_reserve`) until it recovers. Reads (ACL-1580) still share a Publish's slot on
  the same profile and never wait behind the cap for it.
- A job for an account that already has a pending/queued/running/cancelling job is
  refused with "{account} is already in the queue" (HTTP 409, `duplicate: true`). "Publish
  all" skips such accounts and lists them: "Skipped as duplicate (already in the queue)".
- The dashboard shows per account: running, waiting for memory, waiting for a slot, or
  skipped as a duplicate (`/publish-jobs/slots` now also returns `accounts` with a
  `state`). The slot decision rows in telemetry (`job_phase` notes `slot_decision` /
  `slot_queue`) carry `cap`, `reserve_mb`, free memory, measured use and the reason.
- Review round 2: a window left open after a failed run keeps holding its slot until the
  profile is closed (Close profile, a graceful stop, or by hand in Multilogin); the queue
  then says "waiting: N review windows are still open — close them to continue". The first
  profile now also needs profile + reserve free (2810 MB or 400 MB free: it waits, "waiting
  for memory"). The refusal message uses the trimmed account name.
- Review round 3: the waiting line names the kept accounts ("waiting: review windows still
  open for KLD_BFL_B94, Doc21 — close them to continue") with a "Release" link per account
  (`POST /publish-jobs/kept/release`) that stops counting that window without closing it;
  the memory wait says "Waiting for memory: 5.4 GB free, needs 6.5 GB (profile ~3.5 GB + 3 GB
  reserve)". Tests get a fake host memory and a fresh slot queue (`tests/conftest.py`), so no
  test waits on the CI runner's real free memory.
- Tests: `tests/test_acl1603_stable_slots.py`; the ACL-1321/1424 tests now pin the old
  numbers explicitly.

## An account connected here is never hidden by the stats CM — ACL-1588

Owner's decision (05.10): any manager may connect any account for themselves.

- `/api/accounts-overview` no longer skips an account that is in this machine's account
  map because the stats workbook names another CM (FacebookDoc30 / "Dan Dicker" under
  buyer "Evgeni Sedin"). The row carries `owner_hint` ("owner in stats: Dan Dicker"),
  shown under the account name in Accounts. Home and the Builder already take their
  accounts from the map and the country links, so they keep showing it.
- Suggestions for connecting now list every account (stats aliases of all managers and
  sheet accounts of other managers that are not connected here). The selected buyer
  still filters the stats numbers (spend, leads, goals) and nothing else.
- Connect with the optional Country left empty saves the row and sends no
  `/account-links` request (no 400 "A country or asset name is required").
- Connect on a found profile waits for `/mlx-profile-open` at most 75 s (the ad-account
  probe 60 s). On a timeout it says "The profile opened but did not answer — check its
  window in Multilogin (login, checkpoint), then press Connect again." and still saves
  the row with the profile_id, folder_id and instance. The manual form pre-selects the
  last found profile.
- `/mlx-profile-open` runs at most one open per profile_id (a second call answers at once
  with "already being opened") and answers after 70 s with the same message instead of
  hanging; the open itself is never cancelled, killed or restarted. The Connect button of
  that profile stays disabled until the original request settles.
- Tests: `tests/test_acl_1588_any_account.py`, `tests/acl_1588_connect_harness.cjs`.

## Creatives show the concrete variant — ACL-1593

The Creatives breakdown no longer stops at the group number. The variant is the leading
number of the file name: `1_es_2403_post.mp4` is creative 2403, variant 1.

- The robot names ads `"<creative> <variant> - <text>"` (`2403 1 - Pro Bancolombia`,
  `AdDraft.ad_name`). A file name without a leading number gives the name as before.
- `split_ad_title_variant` returns creative, variant and text; `split_ad_title` keeps its
  (creative, text) contract, so every group caller is unchanged. `enrich_structure` adds a
  `VariantId` column.
- Creative breakdown: the group row keeps its totals and expands on click into `1962 · 1`,
  `1962 · 2`, ..., `1962 · no variant` rows with the same metrics; variants add up to the
  group.
- Backfill for ads built before this change: `src/creative_variant.py` reads the robot's
  own `ad_creative_pair` records (campaign + creative number -> variant of the Square
  file). Only an unambiguous pair counts - if one campaign used two variants of the same
  creative, the ad names are not in the journal and the rows stay "no variant". A variant
  typed in the ad title always wins. Only this machine's journal is read.
- Backfill is skipped for a campaign name that appears under more than one account or country in the
  stats (the robot record has no account, so it could belong to either). The journal is pre-filtered
  on `ad_creative_pair` and its map cached by (path, mtime, size); the fill is vectorised. Variants
  are numeric-normalised ("01" = "1").
- No new telemetry step.

## Reads open a closed profile themselves and close what they opened — ACL-1580

"Read spend", "Read all" and the country read no longer refuse a closed Multilogin
profile with "the profile is not open". Owner's decision (05.10): reads open what they
need themselves.

- A closed profile is started through the same safe path as "open profile":
  `multilogin_attach.open(allow_restart=False, allow_kill=False)` — start only, an open
  window is never restarted, no plain browser. A profile that is already open with
  automation is attached to and left open. A hand-opened window is reopened as in
  ACL-1562 and stays open.
- The start takes a place in the same memory-aware profile queue as Publish
  (ACL-1321/1424, `publish_jobs._slots`). While it waits, the account row says
  "Waiting for a free profile slot"; while it starts, "Opening the {account} profile with
  automation". A read whose profile already holds a Publish place shares it
  (`ProfileSlots.acquire(share_profile=True)`) instead of queueing behind a Publish that
  waits for the read's profile lock.
- After the read, a profile the read itself started is closed with the graceful MLX stop
  of that one profile (`stop_profile_on_request`, `allow_kill=False`) and its place is
  released — also when the read failed. A successful "Read spend" waits
  `CLOSE_GRACE_S` (30 s) first, so the country read that follows it (ACL-1282) takes
  the profile over and closes it at its own end instead of opening it a second time.
- A logged-out profile ends the read with "The {account} profile is logged out of
  Facebook — log in inside Multilogin, then Read again" (`error_code`
  `profile_logged_out`), shown as plain text on the row, no retry and no "open profile"
  button.
- Telemetry: `spend_profile_started` and `spend_profile_closed_after_read`, with
  durations. `spend_profile_` steps are in the `cb-build-model` hold rule, and
  `tests/test_build_progress_model.py` now also scans `src/analysis_agent`.
- Review of PR #583:
  - A profile the read started on a slot shared with a Publish of the same profile
    belongs to that Publish: the read never closes it, neither at once nor by timer.
  - Before any close (at once or after the grace), the read checks the queue. If
    another job holds or waits for the profile's slot, or got one since the read
    started it (`ProfileSlots.grants`), the read only releases its own slot and leaves
    the window to that job (`skipped: another_job`).
  - After the slot is granted, the read checks again whether the profile is already
    open. `/mlx-profile-open` does not take the profile lock, so the profile may have
    been opened during the wait. If it is open, the read attaches and does not treat
    the profile as its own.
- Tests: `tests/test_reads_open_a_closed_profile.py` (fakes only, no live Multilogin).

## Platform-neutral tests run on Linux; only marked tests stay on Windows — ACL-1582

Every test ran on Windows runners (6 jobs per run), whose minutes bill at 2x Linux,
although most tests are pure logic, fixtures and Chromium bench pages.

- `windows_only` pytest marker, module-level with a required reason, registered in
  `tests/conftest.py`. 14 of 363 test files carry it (msvcrt locks, 8.3 short paths,
  `install_acl.bat`/cmd, the PowerShell bootstrap and launcher contract, the Windows
  runtime layout, Windows session/screen APIs, `taskkill`, drive-letter paths, backslash
  path traversal of the status screenshot, cross-process lock races); the other 349 run
  on Linux. `python scripts/acl_test_platform.py report` lists the reasons.
- Guard `tests/test_acl_test_platform.py` (AST, also in the fast scope job): an unmarked
  file may not
  - import a Windows module (`msvcrt`, `winreg`, `_winapi`, `winsound`, `win32*`,
    `pywintypes`, `wmi`), also via `pytest.importorskip`, `importlib.import_module`
    or `__import__`;
  - branch on the platform (`os.name`, `sys.platform`, `platform.system()`; `==`, `in`,
    `startswith`; `from sys import platform`, `import os as _o`; a string `skipif`), or
    use an imported `IS_WINDOWS`/`is_windows` helper in a condition or `skipif`;
  - use `windll`, `ProactorEventLoop`, `os.startfile`, `CREATE_NO_WINDOW`, `getattr(ctypes, "windll")`;
  - call `powershell`/`pwsh`, a Windows command as a list or as a `shell=True` string,
    or reference `.ps1`, `LOCALAPPDATA`, `APPDATA`, `USERPROFILE`, `PROGRAMDATA`;
  - hold a Windows path literal (drive letter with a backslash, a UNC share, `%X%` plus a backslash)
    or a bare one-backslash string constant, anywhere; a multi-segment `a\b` string only in an `assert`.
  Round 2 added: a relative backslash path (`..\escape`) anywhere, `platform.system().lower()`,
  Windows env names in any case inside `os.environ`/`os.getenv`. `absolute-forward-slashes` moved to the
  Windows screenshot file (84 + 24 = 108 cases) and `test_config_locations` checks the checkout with `Path.is_relative_to`.
  Data-only lines take `# linux-ok: <why>`.
- Review fixes: the first Linux run failed 18 backslash-traversal cases in
  `test_status_screenshot.py`; they moved to `test_status_screenshot_backslash_windows.py`
  (108 cases before, 90 + 18 after). The two cross-process lock tests
  (`test_processes_preserve_each_update`, `test_multiple_processes_do_not_lose_writes`)
  moved to `test_cross_process_locks_windows.py` (msvcrt locking + os.replace sharing
  violations, ACL-1419). The shard cost model weighs a Chromium-launching file above one
  that starts another process, so Linux shards get an even share of browser files.
- `scripts/acl_test_shard.py --platform linux|windows`; an empty shard now fails.
- `ci.yml`: `linux tests (3.11|3.13, 1..3)` on ubuntu-latest (Chromium with
  `--with-deps`; the Python list comes from the scope job per ACL-1581, so most PRs
  run 3.13 only) and `windows tests (3.13)` on windows-latest (marked files, the smoke
  set, secret scan and `acl_smoke.py`). `ACL verification` requires both.
- `scripts/acl_ci_reuse.py` `REQUIRED_JOBS` and the release gate `verify_promotion`
  need every Linux shard, the Windows job and `ACL verification`. Runs of the old
  layout (`test (py, shard)`) are refused, with a clear message on the tag path.
- Not proven locally: this laptop has neither WSL nor Docker, so the Linux selection
  first runs in CI. A file that fails there only because of Windows paths or CRLF gets
  the marker (the screenshot and lock cases above were exactly that).

## CI: one Python on PRs, superseded PR runs cancelled, installer job stops at 20 min — ACL-1581

The Actions budget ran out for the fourth time on 2026-10-05 (about 4300 Windows minutes
in one day). This is the cheapest slice of ACL-1541.

- `scripts/acl_ci_scope.py` now also decides the Python versions of the `ci.yml` test
  matrix and writes them as the `python` output (a JSON list). A `pull_request` run tests
  Python 3.13 only (3 jobs instead of 6). A PR that touches dependency or bootstrap inputs
  tests 3.11 and 3.13: `requirements*.txt`/`.in`, `constraints*.txt`, `.python-version`, `pyproject.toml`, `setup.py`/`setup.cfg`,
  `pytest.ini`, both `conftest.py`, root `*.bat` installers/launchers,
  `tests/bootstrap_windows_harness.ps1`, `packaging/**`, `.github/workflows/**`,
  `scripts/acl_test_shard.py` and the CI scope/reuse scripts. Push runs
  (stage/main/master), manual runs, an empty diff and any classification error test both.
- `ci.yml` reads the list with `fromJSON(needs.scope.outputs.python || '["3.11", "3.13"]')`;
  job names stay `test (<py>, <shard>)` and `ACL verification` still aggregates
  `needs.test.result`. `REQUIRED_JOBS` is unchanged and now built from
  `acl_ci_reuse.FULL_PYTHON_VERSIONS`, so a one-Python run is never a reuse proof or a
  release proof (`verify_promotion` is unchanged).
- Concurrency in `ci.yml` and `windows-installer.yml`: the group is `pr-<number>` for a
  PR and `<ref>-<run_id>` for anything else, and `cancel-in-progress` is true only for
  `pull_request`. Before, two quick squash merges to stage shared the group
  `refs/heads/stage` and the second cancelled the first task's stage verification.
  `tracker-link.yml` runs only on PRs and already cancelled correctly; unchanged.
- `windows-installer.yml` build/install job: `timeout-minutes` 30 -> 20. Step limits:
  install 3 (new), previous package 12 -> 7, onedir 8 -> 3, bundle test 5 -> 2, installer
  build 10 -> 5, install test 8 -> 3, update cycle 8 -> 7. The numbers come from the last
  91 green runs (2026-10-03..05): job p95 774 s, max 810 s, sum of step p95 884 s. Each step
  limit is at least 1.5x its p95 and 1.25x its max, the job limit at least 1.3x the sum of step p95 and 1.5x
  the job p95, so a hang fails on its own step. The update step
  (420 s) must hold the earlier work (max 137 s), stop-server (30 s), the helper wait and
  the cleanup (30 s), so `test_update_cycle.py` now waits `HELPER_WAIT_SECONDS = 150`
  (was 360 s) and its `finally` cleanup can run; a test reads every `.wait(...)` timeout
  from the script with `ast`.
- Tests: `tests/test_acl_ci_scope.py` (version decision), `tests/test_acl_ci_reuse.py`
  and `tests/test_publish_release.py` (a one-Python run is never a proof; the tag path is
  unchanged), new `tests/test_acl_ci_workflows.py` (concurrency evaluated from the YAML,
  matrix wiring, installer limits). The scope job also runs the new file.
- Not verified on GitHub: the Actions budget was exhausted. After it is restored, a real
  PR should show 3 test jobs and its stage push 6.

## Screen-values probe: reads only by default, every capture is opt-in — ACL-1532

`scripts/probe_screen_values.py` measures whether our captures change a running
Multilogin profile's screen values. Without flags it now only reads (`page.evaluate`,
`Browser.getWindowForTarget`) and takes no capture, so a default run changes nothing.
After review:

- (a) `page.screenshot` and (b) `screenshots.capture` run only with
  `--allow-viewport-reset`. The live run on 9943 (03.10) showed they reset the
  profile's viewport (1919x1079 to 3837x2157 device px).
- (c) CDP `Page.captureScreenshot` with clip and (d) without clip run only with
  `--allow-cdp-capture`. The ACL-1529 review found in headless Chromium that (c) from
  a new session resets another session's device-metrics override; (d) is unmeasured.
  Both are skipped when the window is minimised. The docstring and `--help` say all this.
- (c) and (d) run on their own async connection. Connecting, opening the session, the
  target lookup and the capture each have a 20 s ceiling; a hang aborts the capture and
  detaches the CDP session.
- `--out` warns that the PNGs carry account data. A folder inside the repository is
  refused unless it is under `output/`. The folder is created only after the account
  lookup succeeds. The docstring example uses `output/acl1532_probe`.
- Open tabs are printed as hosts only (Meta paths can carry ids and names). No open tabs
  stops cleanly instead of raising IndexError.
- `tests/test_probe_screen_values.py`: offline, with a fake page and context. It covers
  the default run taking no capture, each flag enabling only its captures, `main`
  passing the flags to `probe`, a timeout that fails if `asyncio.wait_for` is removed,
  and `--out` handling.

## Foreign Meta windows: detect, close safely, name the blocker — ACL-1573 (slice A), ACL-1572

During runs Meta shows windows the robot did not open. Each step knew a few of them on its
own, and an unknown window silently failed the step. ACL-1572 (Doc21, 01.10 308c4f7c8a5a and
04.10 254ee3b3e0c7 / 87d43d6202af / 18d15b75d1eb): `conversion_website_pick` saw
`menu_closed` x8 with `reopened: 0`. The failure screenshots show the "Ad set name -
Template" naming dialog (Add component / Choose existing template / Cancel / Save) over
Conversion location, opened after the one-time `_cancel_naming_template_dialog` check. The
Notifications panel named in ACL-1572 is Meta's pre-rendered, hidden flyout
(`toggleTargetClosed`): it is in all 284 local captures, successful steps included.

- New `src/fb_ads_rpa/foreign_windows.py`: one registry (`KNOWN`) of Meta windows and their
  closers - Notifications panel (Escape, then its own toggle), "Turn off ad blocker",
  onboarding tour, naming template dialog, draft review, surveys - plus the
  network toast. The existing handlers (`_close_ad_blocker_modal`, `_close_onboarding_tour`,
  `_cancel_naming_template_dialog`, `dismiss_publish_draft_dialog`) are called from the
  registry unchanged. Windows a step opens
  (media wizard, objective window, loading) are `owned` and never touched. The media wizard
  and its Replace picker are recognised by structure, not by the title of their current
  screen: the step row (Media / Trim / Crop / Text / Creative generation / Enhancements /
  Translation, `aria-current="step"`) or the ad's `ad_image_video_selector_section` /
  `creative-tool-asset` surface. That structural check runs first, then the registry kinds,
  then the text-only owned kinds (`objective`, `loading`), which match only the window's own
  heading. A "Loading..." line inside the naming or Notifications dialog no longer makes it
  owned. An owned window is never closed by the fallback. The "Creative setup → Site links"
  screen is part of the wizard, so it is no longer a registry entry; the ad-text step still
  cancels it with `_cancel_site_links_modal`. A replay over the captures under C:\acl-live-* gives
  419 wizard and picker windows, all owned, none unknown. Hidden windows
  (`toggleTargetClosed`, `display:none`, `hidden`, zero size) are not windows.
- Before every `_click` / `_fill` of the recorded flow, before the Conversion location menu is
  opened and between the eight Website pick strategies, registry windows are closed. A
  `foreign_window` telemetry row gives kind / title / role / surface (no ids, URLs or the
  campaign name).
- After a failed step (at most once per step per campaign): unknown windows are captured
  (`page_capture`, step `foreign_window`, own quota 3), sent to Sentry with their title
  (`foreign_window: unknown-window`, grouped per title), and closed only with their own
  Close / Not now / Dismiss / Cancel / x or Escape (focused into the window). The step is
  then retried once. Publish, Confirm, Accept, Submit, Create, Continue, Save, Discard,
  anything in a footer / publish surface or outside the window is never pressed. If the
  window stays, the flow's failure becomes `<step>:Unknown Meta window blocked the step:
  <title> (unknown_window)`. The step name before ":" is unchanged, so retry and failure
  policy verdicts are the same as before.
- `conversion_website_pick` diag: `windows_closed` and `reopen_failed`. When the menu does not
  reopen, the guard's fallback runs once and the menu is opened again.
- `_clear_blocking_overlays` also closes the Notifications panel, surveys and the real
  network toast (its "x" has no `aria-label`, which `_close_banners` never recognised).
- Dashboard: `failure_policy` explains `(unknown_window)` failures. The title becomes "Unknown
  Meta window blocked the step: <title>", with an impact and an action line. The build
  progress model holds on `foreign_window`.
- Sentry triage map: `foreign_window` and reason `unknown-window` -> ACL-1573.
- Bench: `foreign_windows_doc21_2026_10_04.html` and `network_toast_2026_10_01.html`;
  `tests/test_foreign_windows_are_closed_or_named.py`.
- Next: slice B (refresh and restore the editor to the same object) and slice C (per-campaign
  checkpoints and resume) attach to `after_failure` returning False.

## CI reuses a full green run of the same tree on promotion — ACL-1565

Every task ran the full Windows suite (6 shards) and the installer build three times
on the same content: on the PR, on the stage push and PR stage→main, and on the main
push. The stage→main promotion alone took about an hour of a task's delivery time.

- `scripts/acl_ci_reuse.py`: the scope job of `ci.yml` and the classify job of
  `windows-installer.yml` look for an earlier run of the same workflow whose commit has
  the same git tree as `HEAD^{tree}`. Candidate commits come from `git log --all`; each
  tree is computed by git, never taken from the API payload. Only a `push` or
  `workflow_dispatch` run of this repository counts (a `pull_request` run tested a
  temporary merge commit whose tree cannot be proven later). It must be `completed` /
  `success`, and every required job of its latest attempt must have run and succeeded:
  `test (3.11|3.13, 1..3)` + `ACL verification`, or `Verify build inputs` +
  `Build, install, repair, and uninstall` + `Installer verification`. A run that itself
  reused (tests skipped), or was partial, cancelled or failed, never counts.
- Then the scope job outputs `windows=false` and `reused_from=<run url>`, the Windows jobs
  are skipped, the required checks `ACL verification` / `Installer verification` still
  conclude success, and the run summary says "Reused full results of run <url> (same
  tree <sha>)".
- Always in full: `workflow_dispatch`, release tags, a missing token, any API or git
  error, no matching run. In practice the PR and the stage push (a squash makes a new
  tree) run in full; the stage→main PR and the main push reuse the stage push run.
- Only the scope/classify job gets `actions: read` (the job's own `GITHUB_TOKEN`, no new
  secrets); the Windows jobs that run PR code keep `contents: read`. The reused run URL
  must be exactly `https://github.com/<repo>/actions/runs/<id>` of that run before it is
  written to `GITHUB_OUTPUT` or the summary.
- Release gate: `verify_promotion` in `scripts/acl_publish_release.py` now checks the
  jobs of the main run named in the tag. If its test matrix did not run, it accepts the
  release only with a provable full run of the release tree (same rules as above),
  otherwise it refuses (an API error while proving counts as no proof). It prints which
  run really tested the tree, so the release log links it. Tracker `check-merge` reads only the aggregate required checks
  and `verify-stage` reads no checks, so the pinned Tracker needed no change.
## A hand-opened profile window is reopened at once, with no conditions — ACL-1562

On 2026-10-04 the "open profile" button for Facebook_9943 answered "The profile is open
by hand — close its window in Multilogin" right after the owner had restored the window:
the ACL-1388/1427 conditions (window at least 15 minutes old, not in the foreground, a
minute with no input, wait up to 2 minutes for that silence) did not pass. Owner's
decision: drop them.

- `hand_window.assess` has no conditions any more: it says "reopen" unless the machine
  switch `FB_RPA_REOPEN_HAND_WINDOW=0` is set (old always-stop behaviour, kept for tests
  and as an emergency opt-out). Removed: `MIN_AGE_S`, `MIN_IDLE_S`, `IDLE_WAIT_S`,
  `wait_until_safe`, `watch_cancel`, the Win32/psutil reads, and the env variables
  `FB_RPA_HAND_WINDOW_MIN_AGE_S`, `FB_RPA_HAND_WINDOW_MIN_IDLE_S`,
  `FB_RPA_HAND_WINDOW_IDLE_WAIT_S`.
- `browser.MultiloginAttachOnlyProvider._reopen_hand_opened` closes the window at once
  on Publish, Read spend, Read all (spend and countries) and the row's "open profile"
  button. Unchanged and mandatory: closing is only the graceful MLX API stop of that one
  profile (`allow_kill=False`); no process is killed, no other profile is touched, no
  plain browser is opened. The login check after the reopen is as before
  (`profile_logged_out`, no retries).
- Visible: before the stop, the job panel and the account-map row say "The <account>
  window was opened by hand — reopening it with automation"
  (`browser.HAND_WINDOW_NOTICE`, `account_spend.hand_note`; the status poll still uses
  the `waiting` key). The "Waiting for the computer to be idle…" texts are gone. The
  Publish hint no longer asks to close a hand-opened window first.
- Telemetry `profile_hand_window` (same step name): `note` `reopened`, `disabled`,
  `mlx_stop_failed`, `still_running` or `restart_failed`; `diag.stop_s` = how long the
  graceful stop took (API call plus waiting for the window's processes to exit).
  `age_s`, `idle_s`, `waited_s`, `wait_outcome` are no longer written.
- If the graceful stop fails or the window outlives it, the action stops with
  `profile_open_by_hand` and the reason in its detail ("Multilogin did not close the
  hand-opened window: …"); nothing is killed. Reads and "open profile" answer
  "Multilogin did not close the hand-opened profile window — close it in Multilogin,
  then Read again" (`code: "open_by_hand"`, still not 409). With reopening switched off on
  the machine they say so instead ("…reopening it with automation is switched off on this
  computer…", `account_spend.HAND_OFF_MESSAGE`); the Publish failure texts name both causes.
- Stop pressed while the profile is being (re)opened no longer lets the first campaign
  start: `publish_draft` checks the job's Stop right after attaching and returns "Stopped
  by the user before the campaign started" (window left open).
- `/mlx-profile-open` (connecting a new profile from search) still leaves a hand-opened
  window alone: it is not an action on a connected account.

## Media step: wait for Meta's media picker to load, name the stall — ACL-1559

Live 2026-10-04 (Facebook_1443, run 2f140fa687fa): Meta's media wizard opened on its
Media step while the media picker still showed only "Loading..." placeholders. The
robot clicked the already-current Media step twice, probed the disabled Next four
times and gave up after ~20 s with the generic `upload_button_missing`.

- `meta_dom.upload_ad_creative`: when the current wizard step is already Media, the
  robot no longer clicks the step and does not press Next. It polls a read-only picker
  check (`_WIZARD_MEDIA_PICKER_JS`, inside the wizard dialog: placeholder count, Upload
  by text or data-surface, "Search media", grid tiles) every 0.25 s for up to
  `MEDIA_PICKER_LOAD_S` = 45 s (capped by the creative step budget). Once Upload is
  there, the existing library search / tile / Upload flow runs unchanged.
- Still loading at the timeout: new reason `media_picker_loading` (diag: `picker`,
  `skeletons`, `waited_s`). It is a navigation reason (`_NAVIGATION_WHYS`): the file is
  not marked as failed for the run. The existing single fresh reopen of the wizard is
  kept. The ad's step reads
  `adN_creative_upload:media_picker_loading - <ad>: <sentence>`, BLOCKING, with its own
  readable problem ("Ad media - Meta's media picker kept loading") and why-text.
- Telemetry: an `ad_creative_upload` row `media_picker:ready|loading|empty` with the
  picker state, `waited_s`, `skeletons_at_start` and `since_open_s`; a `media_picker`
  phase; the picker in the upload trail. No new step names.
- Regression kept: with Creative setup or Text as the current step, Media is still
  clicked (ACL-1348).
- Sentry triage map: `ad_creative_upload` / `media-picker-loading` → ACL-1559.
- Bench: `wizard_media_picker_2026_10_04.html` (fragments `loading` from 2f140fa687fa,
  `loaded` from 232d398471bd), `tests/test_bench_wizard_media_picker_1004.py`.
- Not changed: ACL-1551's busy check still reads the loaded Media step right after a
  pick (Next and Cancel disabled) as busy. The 1551 bench pins that on purpose
  (`media_busy`: "the walk waits while the wizard is busy"), and the walk does not run
  on Media before the pick.

## Run summary carries the run's telemetry as a scrubbed Sentry attachment — ACL-1554

To read a manager's run we had to ask them for `output\rpa_telemetry.jsonl` by hand.
The "Run summary" event (ACL-1447) now carries that run's own step log.

- `run_summary._deliver` (the background send thread, never the run itself) reads the
  journal and attaches `run_telemetry_<run_id>.jsonl` with only the rows of that
  `run_id`. Lines without the run id are not parsed; an unparsable line is skipped.
- Each row goes through `run_telemetry.scrub_diag` and then the summary's own scrub
  (`_EXTRA_SCRUB`: Ads Manager URLs, emails, `act_` ids) on every string at any depth.
  New in `_EXTRA_SCRUB`: the user name in a path (`C:\Users\<name>`, UNC
  `\\srv\Users\<name>`, `/Users/<name>`, `/home/<name>`) becomes `<user>`, up to the next
  path separator, so a name with a space ("Ivan Petrov") goes too; the summary text is
  scrubbed of it as well. In the attachment only, dict keys are scrubbed too (account
  ids, `act_` ids, emails as keys); colliding keys get a `#2` suffix. `run_id` stays as
  is; `job` loses its dashes, like in `acl_sentry.report_step_failure`.
- With no Sentry DSN the journal is not read at all.
- Cap `run_summary.TELEMETRY_MAX_BYTES` = 256 KB. Over it the oldest rows are dropped
  and a final row `{"step": "telemetry_truncated", "dropped_rows": N, ...}` says how many.
  `acl_sentry._attach_telemetry` checks the cap again (before_send does not see
  attachments) and accepts only a `.jsonl` file: screenshots are never attached.
- Context `run_summary.telemetry_attachment`: `ok`, `truncated`, `missing` (no run id,
  no journal, no rows of this run) or `error` (journal unreadable, a row did not scrub,
  or `add_attachment` failed). On `missing`/`error` the summary is sent without the
  attachment.
- Sentry attachment quota for `adopy-7t` was not readable: the stored
  `SENTRY_AUTH_TOKEN` gets 403 on `stats_v2` and on the organization endpoint. Sentry
  rate-limits attachments as their own category and drops only the attachment item
  over quota, so the event itself still arrives.
- Tests: `tests/test_run_summary_telemetry_attachment.py` (real `sentry_sdk`, in-memory
  transport, synthetic rows with a fake account id, Ads Manager URL, email, token and
  user path).
- Live check left: a run on the owner's machine shows the attachment on its Run summary.

## Story not in the library: the media wizard is left with its own control, the other ads are still built — ACL-1551

Live run on Facebook_1443 on 2026-10-04 (232d398471bd, build c1f0e507): ad 1 of
CO_bnc_All_Video_GMBL|TradingYB_35_04.10 was a video post/story pair whose story file is
not in Meta's library. The media wizard stayed open on Crop, the ad set's Action menu did
not open under it, and ads 2-3 were never created (`ad2_create action_menu_not_opened`,
`ad3_create not_tried`).

- Root cause, from the captures: (1) after Next Meta keeps the wizard "busy" - Next and
  Cancel `aria-disabled` (00_wizard_media, 04_ad_creative_upload). The walk counted that
  as four misses (~4 s) and gave up, so it left the wizard on Media; (2) on Crop the
  picker's own footer Save (ACL-1533) did bring Crop back, but Next did not move the
  wizard on: busy, then the same Crop with Next enabled again (02_wizard_crop). Each
  later walk (reuse, retry, force-close) pressed Next again and the second one hung for
  over a minute; (3) the fallback close looked for the corner icon next to "Creative
  setup", which this step row (Media · Trim · Crop · …) does not have, so nothing was
  pressed and the dialog stayed. On 03.10 the same pair passed because Next on Crop
  advanced within about 2.5 s. The 04.10 Crop layout has no Save of its own (footer
  Cancel / Next); the only Save is in the in-wizard Replace picker.
- The walk now waits while the wizard is busy (at most 30 s per walk, it is not counted
  as a miss) and looks for its forward button only inside the wizard's own `role=dialog`.
  Before, the editor footer's Done/Save/Next were candidates too.
- Crop: Next is pressed once per ad. Busy, then the same Crop (or still busy after 25 s)
  means Next did not advance. The robot then never presses Next again. It leaves the
  wizard with its own control: a Save/Done in the wizard footer if Meta shows one (the
  wizard is complete, the story warning stays), otherwise the wizard's own Close in its
  header. Close means the ad is blocking with `crop_wizard_stuck` and the line
  `ad_creative_upload:crop_wizard_stuck - <ad>: Meta's media wizard stayed on its Crop
  step ...`. There is no second upload attempt. Publish, Next and anything outside the
  wizard are never pressed.
- The force-close (`_close_creative_setup_modal`) presses the wizard's own Close before
  the old corner-icon fallback.
- `simple_create.create_next_ad` closes a leftover media wizard with its own Close before
  the ad set's Action menu (`wizard_cleared` in `simple_create_more_ads`). If Close does
  not close it, the reason is `creative_wizard_left_open` ("Meta's media wizard of the
  previous ad stayed open ...").
- Telemetry `ad_creative_wizard`: `closed_by` (the control that closed the wizard),
  `dialog_left`, `exit` (`why`, `tried`, `next_presses`), `busy_s`; note
  `<origin>:closed_by_close` when Close closed it. No new step names.
- `failure_policy.PER_AD_DETAIL`: readable problems "Ad media - Meta's media wizard stuck
  on Crop" and "Ad not created - media wizard left open" (both blocking).
- Bench: `wizard_crop_story_missing_2026_10_04.html` (crop_step, crop_busy,
  in_wizard_picker, media_busy), `tests/test_bench_wizard_exit_1004.py`,
  `tests/test_more_ads_after_a_stuck_wizard.py`.
- Review round 2: a Save/Done already pressed as Crop's forward button is filtered out in
  the page before any click (`_WIZARD_EXIT_JS` takes `{want, skip}`), so it is never
  pressed twice before Close. The `crop_refused` branch in `upload_ad_creative` is removed:
  the retry wrapper already names the Crop reason (`media_refused_on_ad`), and
  `_creative_why` keeps the first reason named.
- The Issue's acceptance criterion "closed by its own Save" assumes Crop has its own Save;
  the 04.10 Crop does not (footer Cancel / Next). The live success path is the picker's
  footer Save, then one Next that closes the wizard.
- Live check left: a 1443 campaign with a pair whose story is not in the library builds
  all its ads.

## Advantage+ audience before the age: verified by controls mode, the Campaign toggle never touched — ACL-1544

Live runs on Facebook_1443 on 2026-10-03 (04509d5eed75, 0e86c42023da): every campaign logged
`advantage_plus_off` ok false `still_on` (`flipped_switch: true`, badge "on" before and after).

- Finding: before the age step the Audience card badge reads "Advantage+ on" by design. Meta
  clears it only when the audience gets a hard condition (age without "Use as a suggestion"),
  which the final step `advantage_plus_audience` checks after the age. The pre-age step never
  passed in any live build (about 257 unique rows, all `still_on`; the acl-live-* telemetry
  files are cumulative copies), so its failure was a false alarm. The captured card was already in controls mode: Locations, Age
  (Minimum/Maximum), Gender, Detailed targeting, Languages and "Switch to recommended setup",
  with no "Further limit the reach of your ads" row.
- Finding: `flipped_switch: true` was a click on the editor header's **Campaign toggle**
  ("In draft"). `_turn_off_advantage_plus_leads` started from the first element whose text
  contained "Advantage+ leads", climbed five levels from the Campaign score panel and clicked
  the first checked switch inside. Captures of the same runs show the toggle on before the
  step and off after (b3a333ff43ab, cb854b07857b); with `flipped_switch: false` it stayed on.
  37 unique runs (28.09-03.10) pressed it.
  It now looks for the label itself, stays inside its row (at most 80 characters of text) and
  never presses a switch named something other than Advantage+ (aria-label "Campaign toggle").
- The pre-age step reads the Audience card mode (`_audience_mode`): controls mode -> ok,
  note `controls_mode`, nothing clicked; recommended setup ("Further limit" row, no controls,
  no "Switch to recommended setup") after the fallbacks -> ok false `still_on`, and the
  recorded flow adds the blocking failure `advantage_plus_off` ("Advantage+ audience - still on
  recommended setup"); neither readable -> ok null, note `unconfirmed`, not blocking (the final
  check after the age still decides). The flow calls the switch-off itself and runs
  `fill_audience_age(..., ensure_manual=False)`.
- Review round 2: the card boundary counts only aria-level 3 card headings (on 1443 the
  rows Locations … Languages are level-4 headings inside the card), and the mode markers
  ("Further limit", "Switch to recommended setup") are read in the collapsed `hidden` body too.
  On the live captures: 57 of 57 `advantage_plus_still_on` read controls, 8 of 8
  recommended-state captures read recommended (before: all 8 unconfirmed). Controls still
  require "Switch to recommended setup": the recommended summary carries the same five labels.
- Bench: `audience_controls_badge_on_2026_10_03.html` (scrubbed cards of both runs, the
  Campaign toggle, the leads row and the collapsed recommended card of b3a333ff43ab/00),
  `tests/test_bench_audience_controls_badge_on.py`.

## Single-screen editor: "Instant form (suggested)" goes back to Website — ACL-1543

Live runs on Facebook_1443 on 2026-10-03 (04509d5eed75 ad 1, 0e86c42023da ad 2): one ad
per campaign stopped on `ad_destination` with `url_field_not_focused` (Website selected,
Browser add-ons on "Instant form (suggested)", Lead Ads Terms card, Lead Gen error #100,
`section_shown: false`), and the campaign ended "do not publish" without saying why.

- Root cause: Meta virtualises off-screen sections of the single-screen editor
  (`display: none !important` inside a `data-virtualized` wrapper). The robot removed
  the `display: none` once at the start of the step, but while the section was off
  screen Meta hid it again, so the Website URL field had no box and never took focus;
  the hidden Browser add-ons could not be read either (`browser_addon_repair`:
  `none_checked: null`).
- The destination step now scrolls the section's wrapper on screen first, then turns
  Instant form off - the "Instant form" opt-in switch when it is on, then Browser
  add-ons "None" - and only then fills and verifies the Website URL (None is set again
  after the URL, as before). Telemetry `destination_instant_form_off` records the
  controls used and the before/after destination state (type, add-on, opt-in; no URL).
- The robot never accepts Lead Ads Terms and never creates an instant form: the press
  guard refuses any press inside the Instant form add-on card and on "View terms",
  "Accept", "Create form" (`click_refused`, note `lead_terms`).
- If Instant form stays on, the reason is `instant_form_not_switched` (the field-level
  reason stays in diag as `field_why`), the ad row reads
  `<tag>_destination:instant_form_not_switched - <ad>: <sentence>` and stays blocking;
  the dashboard problem is titled "Ad destination - Instant form".
- Review round 2: an "Instant form" opt-in switch that stays on is not success - the
  final check, its retry and the classic sweep's read-back refuse it
  (`instant_form_not_switched`, `field_why: instant_form_switch_on`). The press guard
  checks the Instant form card with `closest()` (live "View terms" sits 16 levels below
  it) and tests both the text and the aria-label of a button. The switch is looked up
  inside the destination section only. The new step `destination_instant_form_off` has
  its build-progress rule (ad stage).
- Bench: `ad_destination_instant_form_2026_10_03.html` (scrubbed 04509d5eed75 capture),
  `tests/test_bench_destination_instant_form.py`.

## Crop step: no false "Square changed", the in-wizard Replace picker left through its Save — ACL-1533

Live runs on Facebook_1443 on 2026-10-03 (d54d68303e57, f35a2b31f7d8, 2e98a75f252d,
a39407b72507): every post/story pair ended "do not publish".

- `crop_square_changed` was a false positive. The Crop cards' video media is a `blob:`
  URL that Meta re-mints whenever the step is drawn again; after Replace both cards had a
  new one, so the src comparison said "Square changed" although Square was still on
  Original with the post (1:1 still disabled, no thumbnail ids). `crop_step.square_changed`
  now blocks only on positive evidence: the picked tile's thumbnail id appears on Square,
  Square's ids are replaced by others, its stable media identity (`media_key`: a CDN path
  without its signed query; `blob:`/`data:` have none) changed, or its ratio radios
  changed (another shape).
- `crop_replace_picker_not_closed`: in Meta's in-wizard "Replace media for Stories,
  Status, Reels" the step row's "Crop" is the current step (`aria-current=step`) and
  pressing it does nothing. The way back to Crop is the picker's own Save: the footer's
  primary button after the "Search media" box (where Crop has Next), which already
  brought Crop back after a pick live. It is now the only control pressed there, after a
  pick and, for a story not in the library, with nothing picked; never the header Save,
  Close, Next or Publish (a slot that reads anything but "Save" is not pressed). If Crop
  does not come back, Close and `crop_replace_picker_not_closed` as before. If Save
  without a pick changed the Vertical card (media gone or other ratio radios), the step
  blocks with `crop_vertical_changed_without_story`.
- Review round 2: the Vertical side of Replace needs positive evidence too
  (`crop_step.vertical_replaced`, same rules as Square via `card_changed`: picked thumbnail
  id, Vertical ids replaced, stable `media_key` changed, ratio radios changed - live the
  story made "9:16 (recommended)" disabled). The raw-src check alone passed every video
  Replace (`blob:` re-minted), so a Replace that left the post on Vertical read as "as
  planned"; without evidence the step now blocks with `crop_vertical_replace_unconfirmed`
  (English line in `simple_create.creative_why_text`; the creative step fails, blocking).
  `crop_vertical_changed_without_story` uses the same evidence (thumbnail ids included).
- Bench: `crop_step_pair_video_2026_10_03.html`, `crop_replace_in_wizard_picker_2026_10_03.html`
  (scrubbed, `tests/bench/catalogue.json`), `tests/test_bench_crop_live_1003.py`; the
  in-wizard tests of `tests/test_story_not_placed_is_a_warning.py` follow the live way back.

## Status screenshot is off by default: it resets the profile's viewport — ACL-1535

ACL-1532 measured live (2026-10-03): the status screenshot taken on every account read
(`page.screenshot` in `account_spend._status_shot`, ACL-1430) cleared the viewport
override held by Multilogin; the page went from ~1919×1079 to ~3837×2157 device px and
stayed there. Window size is part of the browser fingerprint.

- `src/analysis_agent/account_spend.py`: `_shoot_status` calls `_status_shot` only when
  `ACL_STATUS_SHOT=1` is set (machine `.env`, loaded by `serve.py` through
  `paths.load_env`; for controlled tests only). Otherwise the read makes no
  `page.screenshot` and no CDP call, and the reading carries the account's last stored
  shot (if it is still on disk), so the row and Copy for group keep working after a Read.
  `_status_shot` itself is untouched (ACL-1529 rewrites it).
- The PNG on a failed read (`_shoot`: grid timeout, wrong period, no totals row; also
  used by `country_split`) is behind the same flag and returns "" without it. The
  HTML/JSON diagnostics (`page_capture`, which never takes an image) are unchanged.
  A grep of the dashboard read paths (spend, country split, status read, open-profile in
  `fb_ads_rpa/browser.py`, turn-off) found no other `page.screenshot` or
  `Page.captureScreenshot` call.
- Map row and live Read: with no stored shot, Copy for group stays disabled; its tooltip
  now reads "Screenshots are switched off for safety".
- Tests: `tests/test_status_screenshot.py` checks with a page spy that a default read
  calls neither `screenshot` nor `new_cdp_session` (successful and failed reads, and
  the `country_split` failure shot), that `ACL_STATUS_SHOT=1` brings the
  old behaviour back, and that the last stored shot is kept.

## Journal, ad-text and team-library file locks use the shared safe lock — ACL-1512

ACL-1495 moved hidden accounts and the targeting registry to
`config.paths.machine_file_lock`. Three more places still had the old pattern
(`msvcrt.locking(LK_LOCK)`, which gives up after 10 x 1 s with EDEADLK, and a `.lock`
file that got `b"0"` written into it on first use):

- `src/acl_log.py` (`append_line`, all run journals): the OS lock is now
  `machine_file_lock(<journal>.lock)`; the per-path thread lock stays. A lock timeout
  surfaces as `JournalWriteError`, like any other write failure.
- `src/analysis_agent/ad_text_registry.py` `_transaction()`: same shape as
  `targeting_registry` (module RLock + shared lock on `ad_text_registry.lock`).
  Its `_save_raw` now replaces the file through `paths._replace_when_free`, like
  targeting (ACL-1419): the multi-process tests below failed in 3 of 58 loaded
  runs before this; the captured failure was `PermissionError [WinError 5]` from a
  plain `os.replace` in an ad-text writer.
- `src/analysis_agent/team_library.py` `_sync_transaction()`: shared lock on
  `team_library_<kind>.lock`.
- `tests/test_shared_file_locks.py`: an AST check that nothing under `src/` calls
  `msvcrt.locking`, `fcntl.flock`/`lockf` or uses `LK_*`/`LOCK_EX` directly; EDEADLK
  and first-use PermissionError reproductions for each store (they fail on the old code
  with the original errors); and one multi-process no-lost-update test per store with
  every core kept busy by burner processes.
- `tests/test_journal_keeps_every_line.py`: the parent-holds-the-lock control test
  now holds the shared lock.

## Spend and country reads reopen a hand-opened profile like Publish does — ACL-1518

On 2026-10-03 07:45 "Read spend" for Facebook_1443 and Facebook_6540 stopped at once
with `profile_open_by_hand (no_automation_port)`, and the row's "open profile"
returned 409, while a Publish on 1443 two minutes later waited for idle and reopened
the window (ACL-1388/1427). The read paths now use the same path.

- `MultiloginAttachOnlyProvider.open` takes `reopen_hand_window=True`: a hand-opened
  window is closed and reopened with an automation port through
  `_reopen_hand_opened`, even with `allow_kill=False`. Same gates (window age, not in
  front, idle), same idle wait (`hand_window.wait_until_safe`, up to 2 minutes), graceful
  MLX API stop only, never kill. `on_hand_wait` is called once when the wait starts.
  Without the flag nothing changes (`allow_kill` decides, as before).
- `account_spend.reopening_by_hand()` turns this on for `_attach_only`; it wraps the
  spend read (`/account-spend/read`, Read all) and the country read
  (`/account-spend/read-countries`). Turn-off and the account probe are unchanged.
- `/account-spend/open-profile` reopens a hand-opened window the same way instead of
  answering 409. If the window stays open by hand, it answers 200 with
  `ok: false, code: "open_by_hand"`. `/mlx-profile-open` (connecting a new profile)
  is unchanged.
- While waiting, `/account-spend/status?account=…` returns `waiting` and the
  account-map row says "Waiting for the computer to be idle to reopen the profile…".
  If the wait ends without idle, or the window is in front, the reading or country
  split fails with `error_code: "open_by_hand"` and the row says "The profile is open
  by hand — close its window in Multilogin, then Read again" (not "failed · retry" or
  "countries failed"). The ACL-1504 "Campaigns not read" block is untouched.
- Read all reads accounts one after another as before; a waiting account holds only
  its own profile lock, so other reads are not blocked.
- Tests: `tests/test_reads_reopen_a_hand_opened_profile.py` (stubbed clock, Win32,
  processes and MLX; no real Multilogin).
## One Copy for group button: account and status on the screenshot — ACL-1520

Owner decision 2026-10-03: the provider's group gets one paste, not a line and a
picture separately.

- Under a problem status (disabled, restricted, blocked, review) the account map has one
  button **Copy for group** instead of **Copy text** (acl-132) and **Copy screenshot**
  (ACL-1430), with the hint "Copy for group — paste it into the provider's group". The
  same markup is drawn by the template and after a live Read (`amapStatusTail`).
- The button puts one PNG on the clipboard: a red header band with large white text
  `<short name> — Account <Status> · <read time>` and the status screenshot below it. The
  image is composed in the browser on a canvas from the stored PNG
  (`/account-spend/status-shot/<id>`); nothing else is requested, sent or stored.
- No screenshot on the last read: the button is disabled with "No screenshot on the last
  read — read the account again". If the clipboard refuses an image, the composed image
  opens in a new tab with "Right-click → Copy image"; if the tab or the image itself
  fails, the old `/view` page of the raw screenshot opens.
- "unreadable" with a screenshot keeps the old Copy screenshot button (there is no
  "Account <Status>" line for it).
- Capture area (point 5): the status screenshot is the whole visible window
  (`full_page=False`, no clip), taken right after the status read and before scrolling to
  the totals. Checked against real captures inside the Multilogin profile: 6540
  (restricted) — Meta's red "We restricted your ad account" banner is above the table and
  is in the shot; Doc20 (disabled) shows no banner, "Account disabled" is in the Delivery
  column. No capture change was needed; a fixture test with a banner page and an order
  test (shot before scroll) now hold it.
- Tests: `test_copy_for_group.py` (composed PNG pixels and header text, narrow shot,
  disabled, fallback tab, /view fallback, nothing sent or stored, banner capture, shot
  before scroll); `test_copy_status_text.py` and `test_status_screenshot.py` updated.

## The country is retried in place and once more at the end of the campaign — ACL-1390

2026-09-30 07:36 UTC (FacebookDoc19, SA_arc_All_Image_PF_35_30.09, run 64cc3d5510b2):
Meta's location search showed no suggestions for "Saudi Arabia" twice, the campaign
ended "the country was not set", although the same account had set Saudi Arabia 25
minutes earlier. Owner decision 2026-09-30: retry in place and once more at the end.

- In place: up to 4 attempts. The first is the text already typed with the old 6 s
  wait (no slowdown when suggestions appear at once); then click back into the field,
  clear it and type slower (8 s); the first word / a shorter prefix (10 s); the full
  name typed slowest with the longest wait (15 s). Only the "<country> (Country)"
  option or the bare country name is picked — a region or city of the same name never
  is (the old "first option" fallback is gone; with no `role=option` rows but an open
  list, Enter stays as before and the rows check decides). Every attempt after a failed
  first one is a telemetry row `audience_location_attempt` (attempt, typed, options,
  outcome). On give-up the search field is left empty.
- Second pass: if `audience_location` still failed, after the rest of the campaign
  (ads and the destination sweep) the robot returns to the same ad set — the ad-set row
  of the left tree in the classic editor, the ad-set section and Audience card in the
  single-screen editor — reads Locations, and runs the location step once more (same
  default-country removal), then reads Locations back. Fixed → the blocking
  `audience_location` is replaced by the warning `audience_location_second_pass`
  shown on the job card; not fixed → the same `audience_location` stop as before.
  Telemetry `audience_location_second_pass` (listed_before / listed_after). No campaign
  replay, no second draft, no Publish.
- Review of PR #393: in the single-screen editor after ads 2+ the second pass runs under
  ACL-1353's `hold_open_ad("simple_create_open_ad_n")` (no reload, no tree open); an
  empty search field is retyped on attempt 1 and the attempt row records what was
  really in the field; keys are sent only when the location search has focus; the
  read-back requires row names equal to the country ("Riyadh, Saudi Arabia" is not).
- Review round 2: the job panel titles the warning "Country set on the second pass"
  (`failure_policy.GUIDE`); a miss opening the ad-set row is recorded on the
  `audience_location_second_pass` row as `open:<why>`, so Robot health does not count
  it as a failed step.

## Screenshot endpoint tests prove a path outside the folder is refused — ACL-1486

The ACL-1430 review found that the tests for `/account-spend/status-shot/<id>`
still passed with the path guards removed: no real `.png` existed outside
`spend_shots`, so a naive lookup would have returned 404 too. Tests only; product
code is unchanged (no escape was found).

- `tests/test_status_screenshot.py` now puts real PNGs next to the screenshots
  folder (parent, a sibling folder named like it, a folder elsewhere) and asks for
  each through `..` segments, backslashes, an absolute path (both slash styles),
  a subfolder walked back out, and URL-encoded / double-encoded forms, on both the
  image and the `/view` page. Each case first asserts the file *would* be found
  without guards.
- The folder check is also tested on its own, with the name check loosened, and a
  non-`status_*` PNG in the folder (a failed-read shot) and a folder named like a
  shot are refused.
- Mutation check: removing the name check, the folder check, the is-file check,
  or both path checks each makes tests fail; with both path checks gone every
  escape route fails at least one test.

## Hidden accounts and targeting file locks never lose an update under load — ACL-1495

`tests/test_hidden_accounts_review.py::test_processes_preserve_each_update` failed at
random on CI with "OSError [Errno 36] Resource deadlock avoided". The ACL-1491
investigation found two product bugs in the machine file lock used by
`src/web/hidden_accounts.py` and `src/analysis_agent/targeting_registry.py`:

1. `msvcrt.locking(LK_LOCK)` tries 10 times, once a second, and then raises EDEADLK.
   A queue longer than 10 s under load is not a deadlock, but the update was lost.
2. On first use every process that saw an empty `.lock` file wrote `b"0"` into it; when
   another process already held byte 0, `flush()` raised PermissionError (Errno 13).

Fix: one shared helper, `config.paths.machine_file_lock(path, timeout=None)`.

- Waits with non-blocking attempts and a growing, jittered pause (5 ms to 100 ms).
  EDEADLK, EACCES and EAGAIN mean "busy, try again". The wait is bounded by
  `paths._LOCK_TIMEOUT` (120 s), after which it raises `TimeoutError` instead of
  hanging the dashboard request.
- The lock file is created by a single atomic `os.open(O_CREAT)` and is never
  written. Windows allows locking a byte past the end of the file, and `flock` has
  no byte ranges, so the file needs no content.
- Both `_transaction()` functions now use it. Writes still go through
  `paths._replace_when_free` (`_atomic_write` for hidden accounts, `_save_raw`
  for targeting).
- `tests/test_file_lock_races.py` reproduces both bugs deterministically (a fake
  EDEADLK from the lock call, and an empty lock file whose byte 0 is held by another
  handle). Both tests failed on the old code with the original errors. The file also
  covers the bounded wait and checks that the lock file is never written.
- `ad_text_registry.py`, `team_library.py` and `acl_log.py` still use the old
  pattern. They are not changed here.

## The dashboard stays responsive while the report is built — ACL-1399

2026-09-30 after a restart the first page built the report inside the request
handler: about a minute in which `/health` and the publish queue did not answer,
and the owner's Publish on Facebook_1443 failed with "Failed to fetch".

- Cold build faster: `config/analysis_rules.py` parsed its JSON file on every
  workbook row (60 256 reads, 69 s of a 106 s profiled build); it is now read once
  and re-checked by mtime at most every 2 s. The unknown-interest log no longer
  rescans the workbook and re-reads its journal per code. Numbers are unchanged.
- The report is never built on the event loop: GET / answers at once with a
  "Loading the numbers…" page, builds in a background thread (report + the page's
  period) and reloads itself when `/report/status` says ready. Every handler that
  reads the report runs in its own thread pool; builds are single-flight. `/health`
  no longer waits for git, `/acl/update/check` runs `git fetch` off the loop, and
  preview / publish start build their drafts in a thread.
- Stop no longer runs the RPA teardown on the server thread: `cancel_job` hands
  `publisher.cancel()` to its own thread. On 2026-09-29 12:54:59 (local) that
  teardown reset the event loop's running-loop mark and the dashboard died.
- Publish that gets no answer (busy server, lost response) no longer freezes the
  tab: the account shows "the server is busy — Retry", and Retry resends with the
  same `request_id`, for which the server returns the job it already created.
  Dismiss frees the account instead; its next Publish reuses that same
  `request_id`, so a submission that did reach the server is not doubled.
  Other accounts may keep publishing while one waits for Retry.
## A failed read says "Campaigns not read", not "0 campaigns" — ACL-1504

2026-10-02, Facebook_1443: Ads Manager did not open within 90 s, and the account
map's expanded row said "IN THE ACCOUNT · 0 CAMPAIGNS · 0 ADS" above the raw
`Page.goto` error, including the Ads Manager address with `act=`. It read as "this
account has no campaigns".

- `src/analysis_agent/account_spend.py`: a reading records whether the campaign list
  itself was reached (`objects_read`; `_read_tree(..., reached=)`). An unparsed table
  counts as not reached. Older records without the field are judged by themselves:
  no rows plus an error means not read.
- When the list was not reached, the reading sent to the screen carries
  `objects_problem`: one short reason (for example "Ads Manager did not open in
  90 s.") and what to do. As in ACL-1437, the reason comes from a local table of raw
  errors. The campaign-card guides are unchanged. The tree error text is scrubbed
  on output (`scrub_tree_error`: URLs, `act=`/`act_` ids, `run_telemetry.scrub`),
  including in records saved before this change.
- The last good tree for the account (any period, `objects_prev`) is kept with a
  reading that did not reach the list. A second failure in a row does not erase it.
- `report.html` (account tree): "campaigns not read · tried …", the reason, what to
  do, and a **Read again** button that presses the row's existing Read spend.
  There is no new reading path. The raw error is under a collapsed "Technical
  details". The last good rows are shown under "last good read · … · read <date> ·
  Meta numbers for …", without turn-off buttons, because their switches may have
  changed. "0 campaigns · 0 ads" appears only when the list was read and was empty.
- Review of PR #505:
  - Read again follows the row's Read spend button whenever its state changes:
    after Read all (`amapReadAllControls`) and after each reading
    (`amapRereadSync`). Before, it stayed grey after Read all until a reload.
  - Bare Meta tokens (`EAA…`) are scrubbed from the technical text too.
  - A not-read attempt for other dates says "not the dates on screen".
## Parallel profiles start at 3.5 GB each and adapt to measured use — ACL-1424

2026-10-01 four Multilogin profiles used 13.6 GB (about 3.4 GB each) and the laptop
was at 87 % memory, while the "Publish all" queue (ACL-1321) assumed 1.5 GB per
profile. Owner decision: start from 3.5 GB and adapt to the free memory.

- `src/web/profile_slots.py`: the per-profile estimate is 3.5 GB
  (`ACL_PROFILE_MEMORY_GB`). Once a running profile has warmed up
  (`ACL_PROFILE_WARMUP_S`, 90 s), the queue measures its Mimic process tree (root
  `chrome.exe` under the Mimic folder with `--client-session-id=<profile id>`, plus
  all its children; the person's own Chrome is never counted) and uses the average
  of the measured profiles instead of the estimate, never below
  `ACL_PROFILE_MEMORY_FLOOR_GB` (1.5). Free memory is read fresh at every decision,
  so opening something heavy holds the next profile and closing it lets it start.
  Ceiling (4) and floor (1) are unchanged; `ACL_MAX_PARALLEL_PROFILES` still skips
  all of this.
- The measurement runs in its own thread (`ProfileUsage`, read-only psutil); a
  decision never waits for it. A probe that fails, hangs past 5 s, or does not find
  the profile falls back to the 3.5 GB estimate.
- Visible: the waiting campaign dot says e.g. "Waiting: 2.1 GB free, a profile uses
  ~3.4 GB". Telemetry: every hold is a `job_phase` row with note `slot_decision`
  (free memory, estimate or measured average, per-profile readings, decision); the
  start keeps the existing `slot_queue` row. `GET /publish-jobs/slots` shows the same
  fields under `next`.
- Review of PR #425 (never overcommit): each running profile keeps its peak
  reading, so a closing or shrinking profile does not pull the average down; a
  profile younger than `ACL_PROFILE_SETTLE_S` (300 s) counts at least the estimate,
  however small it measures; and the growth still ahead of measured profiles
  (`growth_mb`, up to the per-profile figure) is added to what the next start needs.
  Telemetry carries only a list of MB readings (`per_profile_mb`), never profile ids,
  and nested dicts are dropped from the recorded decision. A probe hung for 4x its
  timeout is abandoned and a new one starts, so adaptation does not stop.

## On-screen messages are English — ACL-1462

2026-10-01 the owner saw Russian text in the confirmation shown before Publish. Translated
everything the dashboard shows that was still Russian: the Publish confirmation (locked-
screen warning, tracking-link and targeting warnings, the "Build anyway" button), the
locked-screen notice after a run, the ad-text library's Body hint, the read-failure
tooltip's "screenshot:" label, "Stopped by the user", the targeting-library read error
and the reasons in the "countries failed" tooltip. Code comments, Obsidian note
generators and page-matching keywords stay Russian. A new test fails on any Cyrillic in
the templates outside comments, except the two checks that recognise older stored
errors.

Review of PR #463: the "countries failed" reasons from setting the report period,
the account-map miss, the period mismatch, the refused table and the pivot / section
checks were still Russian; now English. `tests/test_country_split_reasons_are_english.py`
tokenizes `country_split.py` and fails on Cyrillic in any string literal other than
docstrings and `//` comments inside JS blocks.

## One Connect button opens the found profile and connects it — ACL-1457

Each profile found in Multilogin had two buttons, Open and Connect, and they had to be
pressed in that order: only a profile opened by automation can tell the dashboard its
ad account number. The owner asked for one button (2026-10-01). Connect now opens the
profile through the same `/mlx-profile-open` path (attaching if it already runs with
automation; a window opened by hand is still never restarted), reads the number, and
connects. If opening fails, connecting continues and the prompt says to close a
hand-opened window and press Connect again, or to type the number, or to leave it for
later. The "open profile" button on already connected accounts is unchanged.
The button stays disabled until the whole open-and-connect finishes, so a second
click while the profile is still being read does not start a second open
(review fix, `tests/test_one_connect_button.py`).

## A new campaign manager no longer sees the owner's goals — ACL-1452

2026-09-30 a test installation opened as Dan showed "Evgeni — Goals Progress" with
the owner's seven countries and targets. The panel title had the owner's name written
into the page, and a person without saved goals was given the owner's old template
numbers as a start.

- The title names the campaign manager whose goals are on screen.
- A person without saved goals starts with **their own countries** — where they have
  spend, leads or deposits in the workbook, plus countries they linked accounts to on
  this machine's map — each with a target of 10 FTD and no other targets. With no
  countries known, the strip is empty apart from "+ country".
- The starting goals are shown, not written; the first edit saves them together with
  the change, so editing one country does not drop the others. Saved goals are
  untouched (the owner's own seven countries are saved on his machine).
- The starting countries do not depend on the date picker: the panel and the first
  save both take them from the whole workbook, so what is saved is what was shown.
  Saving reads the workbook only while the person has no saved goals, and off the
  request loop. When a team lead edits another manager's goals, that manager's own
  numbers are used, and this machine's account map is not added to them.

## A covered profile window is recorded and woken without the foreground — ACL-1402

2026-09-30, Facebook_1443 (fba2c3f5): `website_selected` → `conversion_event` took
5 minutes, and the build continued the moment the owner opened the account window,
which was covered by other windows (not minimised). Telemetry had no visibility data
after attach.

- `src/fb_ads_rpa/window_visibility.py`: `visibility_diag` — `document.visibilityState`
  and `document.hasFocus()` (bounded probe), the profile window as Windows sees it
  (read-only Win32: `IsIconic`, `IsWindowVisible`, foreground, share on screen and not
  covered by windows above it in z-order; the browser process is found by its CDP
  port), and whether `_disable_background_throttling` succeeded on the tab
  (`browser.THROTTLE_STATUS`). Recorded in every `window_bounds` row (attach and each
  section) and in `stall_unstuck`.
- "Hidden" means what Windows saw: the window is minimised (`minimized`) or its
  on-screen part is covered by other windows (`covered`). A window parked off-screen
  is the robot's normal home for a minimised profile (`offscreen`/`parked`) and is
  not hidden; it only gets a silent keep-alive screencast if the tab has none. The
  page's own `visibilityState == "hidden"` decides only when Windows cannot be read:
  with the window on screen it is just a background tab (review of PR #420, F1/F2).
- `wake_if_hidden`: when a wait runs long and the window is hidden, the CDP helper is
  re-applied, a keep-alive screencast is started if the tab has none, and a minimised
  window goes back to normal off-screen — never `bring_to_front` or any foreground
  call. A tab that does not answer gets no CDP session on itself (ACL-1320); only
  browser-level commands. At most once per 30 s per tab. "Woke" is claimed only when
  an animation frame (`requestAnimationFrame`) arrives within 2 s after the wake (F4).
  Telemetry `window_hidden_woken` (with `reason` and `frames`); job panel, by reason:
  "The profile window was covered by other windows — the robot woke it", "… was
  minimised — the robot restored it off-screen", "The profile tab was hidden — …";
  no frames: "The profile window stayed hidden and did not wake — bring it to the
  front".
- Hooked before every "tab is frozen" decision: `meta_dom._tab_frozen`,
  `browser.page_answers`, the unresponsive branch of `_unstick`, and
  `simple_create._wait_new_ad` (after 20 s; the 120 s Meta-busy limit is unchanged).
  While Meta's "Creating..." dialog is on screen and the page answers, the wait does
  not wake (Meta is busy, F3); in that poll the page is not probed, Windows decides.
- Per-tab state (`THROTTLE_STATUS`, keep-alive tabs, last wake, notices, "woken in
  this run") holds a weak reference next to `id(page)`, so a recycled id in the
  long-lived serve.py never matches a dead tab (F5). A failed browser-pid lookup is
  retried after 15 s instead of disabling Win32 for the process (F6). `_unstick`
  still reloads an unanswering list tab when it cannot be asked after the wake (F7).
- Not done yet: the live experiment with real covered windows (which mechanism
  actually unfreezes an occluded Chrome window) — planned in PR ACL-1402.

## Robot health: success rate of every step per account and build — ACL-1434

A new read-only page, `/robot-health`, linked as "Robot health" in the dashboard
header, and a script, `scripts/robot_health.py`, show how healthy the robot is.
They read only the local run log (`output/rpa_telemetry.jsonl`) and the dashboard
logs: `output/serve*.log` on a hand-run checkout, and the launcher's
`server.stdout.log` / `server.stderr.log` in `%LOCALAPPDATA%/ACL/logs` on an
installed build. No account is opened. The page reads in the server's thread
pool, so it does not hold up progress polling of running jobs.

- Rows are robot steps and columns are accounts plus "all". Accounts are shown
  by a short label only, never the full account name, also in `--json`. A cell is ok/total
  over that column's last N runs (default 10), with the change in points against
  the N runs before. Only wizard outcomes count. Rows with `ok = null`,
  `job_phase`, window/tab/profile/browser state, retries and attempts, and
  always-false diagnostics such as `click_target_covered` and
  `advantage_plus_off` are left out. A retried step counts by its last record.
  A run that stopped earlier does not count for steps it never reached. Steps
  that are only written on failure (`objective_leads`) are marked with `*`.
- A second table shows each step by build. A run's build is the
  "RPA flow version" the dashboard log was under when it first named the run's
  job (the log has times without dates). When stdout and stderr go to separate
  files (`serveN.log` + `serveN.err.log`, or the launcher's pair), the file
  with the job ids is matched to its pair launch by launch, counting from the
  newest. A `build` field in telemetry takes precedence. Runs the log never named, started after the dashboard process
  started, take that process's own build.
- Highlights: a drop of 30 or more points (at least 2 attempts in both windows),
  2 or more failures in a row inside the current window, and a 30-point drop
  from one build to the next. Each links to the failing runs. A run page shows
  the run's steps, its failures marked new / known / known elsewhere, and its
  screenshots from `output/rpa_screenshots/<run>`.
- `python scripts/robot_health.py --top 15` prints the same table as Markdown.
  `--run <id>` prints one run's summary with its capture folders.

## A hand-opened window waits for the computer to go idle — ACL-1427

The Publish click itself resets the "60 s without input" timer of ACL-1388, so the
gate checked a few seconds later almost never passed. Owner decision 2026-10-01:
wait instead of stopping at once.

- `hand_window.wait_until_safe`: when the ONLY failing gate is recent input, wait up
  to 2 minutes (`IDLE_WAIT_S`, per-machine `FB_RPA_HAND_WINDOW_IDLE_WAIT_S`; `0` =
  old immediate stop), re-checking all gates every 3 s. Idle for 60 s with the other
  gates still passing: close and reopen as in ACL-1388. Input for the whole 2 minutes:
  the old `profile_open_by_hand` stop. Any other gate failing (before or during the
  wait) stops at once. The job keeps its profile slot while waiting.
- Stop of the job interrupts the wait within half a second: `RpaPublisher` wraps the
  browser open in `hand_window.watch_cancel`.
- Visible: job panel line "Waiting for the computer to be idle to reopen <account>'s
  hand-opened window — don't use the mouse or keyboard for a minute"; telemetry
  `profile_hand_window` diag gains `waited_s` and `wait_outcome`
  (`no_wait`/`idle`/`timeout`/`gate_failed`/`cancelled`; note `cancelled` on Stop).
- Stop during the wait: the slot result adds "the job was stopped while waiting for the
  computer to be idle", and no Sentry campaign-stop event is sent (it is not a failure).
- With ACL-1444: a reopened window is the robot's profile now, so at the end of a clean
  job it closes itself like any robot profile; while the wait runs, the job keeps the
  profile's slot and "Close profile" answers 409 — Stop is the way out.

## A new Sentry failure starts the triage routine in the cloud — ACL-1330

2026-09-30 owner decision: no polling and no empty runs; the triage agent starts only
when Sentry reports a new failure kind, runs in the cloud with the laptop closed, and
asks the owner on the phone before any release.

- `deploy/sentry-relay/worker.js`: a Cloudflare Worker (free plan) between Sentry and the
  routine's API trigger, because Sentry's webhooks cannot send the `Authorization`
  header `/fire` needs. It checks Sentry's HMAC signature and the event time inside
  the signed body (older than an hour or undated = refused; the unsigned timestamp
  header is only an extra check), passes only the rule "ACL - new failure kind" for
  project `python` and blocking / campaign_stop / job_exception failures, fires once
  per Sentry issue per 24 h, at most 20 times an hour and 30 a day, and sends only a
  short scrubbed text (issue id, title, culprit, step, reason, release, machine, link). Tests: `worker.test.js` (Node, no packages), run from
  `tests/test_sentry_relay.py`.
- `docs/system/ROUTINE_SENTRY_PROMPT.md`: the routine's instructions — payload as data,
  Sentry read-only, tracker from a clean `main` checkout via REST-only `gh`, fix branch
  from `stage`, affected Linux tests, independent reviewer, PR into `stage`, never a
  merge or release, final question "Выпустить для <buyer>?".
- `docs/system/SENTRY_ROUTINE_SETUP.md`: the owner's ~10-minute setup, token rotation and
  switch-off. `scripts/sentry_selftest_event.py` sends one synthetic failure to test the
  chain (`relay_selftest`, listed as known in `config/sentry_triage_map.json`).
- The laptop scheduled task `acl-sentry-triage` is paused (manual "Run now" only).
  Nothing is live until the owner does the setup.

## Copy text for the account provider's group — acl-132

Owner's decision (2026-09-30): when the account map shows a problem status (disabled,
restricted, blocked, under review), a "Copy text" button copies one line for the
provider's WhatsApp group — the account's short name and its status, e.g.
`FacebookDoc20 - Account Disabled` — with the hint "Copy screenshot and account number to
send in the group". The screenshot itself is copied by ACL-1430's button. Nothing is
sent by ACL. Healthy and unread statuses show neither.

- Review fixes (2026-10-02): a problem status without a screenshot now shows "Copy
  screenshot" disabled, with the tooltip "No screenshot on the last read — read the
  account again", so the hint never points at a missing button (the map template and
  the live Read path alike). After a live Read the status cell keeps "Copy text" and
  the hint. A second click within 1.5 s no longer leaves the button stuck on "Copied".
  Tests check that copying makes no request and stores nothing — only the clipboard.
  After a Read the status cell is rebuilt as one markup string, the same markup as the
  map template (fixes a CI regression in the spend-poll test).
## Every campaign sends a run summary to Sentry, labelled with the manager — ACL-1447

2026-10-01. Until now only blocking failures reached Sentry; how a run went on a
buyer's machine (warnings, slow steps, what passed) stayed in the local
`rpa_telemetry.jsonl`, and most events named only the computer (`LAPTOP-UA006`).

- When a campaign finishes — clean, with warnings, do-not-publish, or stopped
  (including a stop while connecting the profile) — `src/fb_ads_rpa/run_summary.py`
  sends one event "Run summary: <result>" at info level (clean, warnings) or warning
  level (do-not-publish, stopped), never error. Fingerprint
  `acl-run-summary` + result: four issues for the whole fleet, not one per campaign.
  Context `run_summary`: result, account, campaign, editor (classic / single-screen),
  steps (step, ok, duration, reason), blocking failures, warnings, total duration,
  number of ads, attempts, run id. Release and machine come from the existing tags.
- Owner decision 2026-10-01: the account display name (short label, e.g.
  FacebookDoc19) and the campaign name are sent, in the summary and as tags `account`
  and `campaign`. Ad account ids, profile ids, Ads Manager URLs, tokens and e-mails
  are still scrubbed (`run_telemetry.scrub` plus URL / e-mail / `act_` rules in the
  summary). The page section attached to failures keeps its placeholders (ACL-1325).
- Every Sentry event carries the tag `manager`: `ACL_MACHINE_LABEL`, else
  `MY_CM_NAME`, else `unset`; read when the event is sent, so a change applies
  without a restart. The setup wizard writes `ACL_MACHINE_LABEL` from the name it
  already asks for; Accounts → This computer → "Your name" changes it.
- Sending runs in a background thread and swallows every error; a run never waits
  for the network. `ACL_SENTRY_RUN_SUMMARY=0` turns summaries off.
- `python scripts/run_summaries.py --from-sentry [--period 7d] [--format json]` lists
  machines with their manager, campaign count per result, last run and release
  (Sentry Discover API, `SENTRY_AUTH_TOKEN` with event:read). `scripts/robot_health.py`
  (ACL-1434) was not on main yet, so the reader is its own script with
  `machines_from_sentry()` for the health table to call.
- Review of PR #450: the `machine` and `machine_id` tags are now set on the global
  scope and again in `before_send`. Before, they lived only in the scope of the
  thread that called `init`, so every summary after the first (sent from a new
  thread) had no machine and was listed as "unknown". `run_summaries` groups by
  `machine_id` and shows the latest label, so two computers of one manager stay two
  rows. The campaign name in the summary goes through the summary scrubber
  (URL / e-mail / `act_`).

## Campaign problems are shown as a short readable list — ACL-1437

2026-10-01 the builder card of CO_bnc_All_Video_FX_Y_35_01.10 showed its most
important message as one long paragraph ("DO NOT PUBLISH AS IS — these steps
failed: conversion_event — optimising for the wrong event … Do not publish:
conversion_event, ad_copy:…"), with the failed steps listed twice.

- `failure_policy.py` now gives every step a short English title, one sentence of
  impact and one "what to do" line (`GUIDE`, `PER_AD_GUIDE`); a test fails if a
  step of `POLICY` / `PER_AD_POLICY` has none. Text only: severities and the robot's
  behaviour are unchanged.
- The job result carries `problems` (step, ad number and name for per-ad steps,
  severity, title, impact, action, technical note, raw code; blocking first) and
  the campaign's `run_id`. The old plain text in `error` stays for older readers
  and Sentry.
- The job panel renders one block: the verdict headline (do not publish as is /
  ready to publish with warnings / not built), the count with "draft saved,
  browser left open", numbered items with a "→" action line, red for blocking and
  amber for warnings, and step codes, notes, run id, campaign id, capture paths
  and the step trail collapsed under "Technical details". Job records without
  `problems` render as before.
- Review fixes: the ACL-1476 `story_not_placed` warning has its own text and
  shows the ad name; a per-ad step without text falls back to the policy reason
  instead of raising (a `KeyError` used to turn a publishable draft into a job
  error). Any part of `error` the list does not repeat (profile stop detail,
  "Recorded flow failed at: …") is shown under "Technical details"; the
  teach-mode marker ("waiting for a Website click") stays visible.

## A problem status on the account map comes with a screenshot and a Copy screenshot button — ACL-1430

2026-10-01 the owner asked for proof to send to support when the map shows a problem
status (FacebookDoc20 disabled, Facebook_6540 restricted). Until now a page screenshot
was kept only when a spend read failed, as a file path in a tooltip.

- When a read finds any status other than `clear`, it saves a viewport screenshot of
  the same Ads Manager page it just read the status from — the tab inside the
  account's Multilogin profile; there is no capture outside Multilogin. Files go to
  `%LOCALAPPDATA%\ACL\spend_shots\status_<account>__<time>.png`, the last 5 per
  account are kept. The shot id and time are stored in the account's status entry.
  A clear read takes no screenshot and removes the button; a failed screenshot never
  fails the read.
- `GET /account-spend/status-shot/<id>` serves a screenshot only by its id (strict
  name, only from that folder, local dashboard only); anything else is 404.
  `/account-spend/status-shot/<id>/view` is the fallback page with the image and
  "Right-click → Copy image".
- On the account map a small "Copy screenshot" button sits next to a problem status;
  its tooltip says when the screenshot was taken. It copies an `image/png` to the
  clipboard; when the browser refuses, it opens the fallback page in a new tab. No
  screenshot, no button.

## Saving a target waits out a briefly held registry file — ACL-1419

`test_multiple_processes_do_not_lose_writes` failed intermittently on Windows CI.
Two causes. Product: `targeting_registry._save_raw` replaced `targeting_registry.json`
with a bare `os.replace`, which Windows refuses (`PermissionError [WinError 5]`)
while the file is open for a moment elsewhere (Defender, the indexer, the previous
writer's handle); the add/edit/remove then failed with that error. Reproduced locally
3 of 18 runs under parallel load. It now uses `config.paths._replace_when_free`, the
same short wait the other machine files already have. Test: each `spawn` worker
imported the test module and with it `src.web.app` (~3 s quiet, much more on a
loaded runner), and the test read `exitcode` right after `terminate()` without
joining, so a slow worker showed up as `assert None == 0`. The worker now lives in
`tests/targeting_worker.py` (registry import only), all workers share one 120 s
deadline and are joined before exit codes are checked; the assertion is unchanged
(three clean exits, 24 targets). `test_save_waits_out_a_briefly_held_registry` pins the wait.
## A brief file hold on Windows no longer loses a spend reading — ACL-1466

The spend and status store (`account_spend.json`) was replaced with a bare `os.replace`.
Windows briefly holds a file after the previous replace or while Defender or the indexer
looks at it, and answered `WinError 5`; the Read result was lost. The concurrent-writes
test caught it ten times since 2026-09-15. The store now replaces through
`config.paths._replace_when_free`, as the account-links file does since ACL-1216; a hold
that does not end still raises.
## Builder: visible Duplicate on ads and campaigns, no repeated buttons — ACL-1459

2026-10-01 the owner sent a screenshot of the builder drawer: duplicating an ad was a
faint icon in the card's corner, duplicating a campaign was a separate button at the
bottom of the drawer, and "+ Add Campaign" stood next to "+ Campaign" for the same
account. Now each ad card has a visible "Duplicate"; "+ Add ad" reads "+ Ad"; "⧉
Duplicate campaign" sits inside the campaign card under its ads and, in the drawer,
opens the copy for editing, as the old bottom button did. Removed: the slot-header
icon, the bottom-of-drawer button and, inside the drawer only, the form's "+ Add
Campaign" (the country page keeps it). Duplication logic is unchanged.
## Buttons no longer look like input fields — ACL-1455

2026-10-01 the owner noted that small buttons and input fields look the same. The
secondary buttons (`.amap-btn`, `.amap-x`, `.acc-btn`) were drawn with the fields'
frame, nearly the same background and the placeholder's grey text. Owner's choice
among three mockups: link-style buttons. They now have blue text and no frame, with
a soft blue background on hover and a focus ring for the keyboard; delete and error
states are red, disabled ones grey. Fields keep their frame — the frame now means
"type here". Primary and other filled buttons are unchanged. One CSS block, both
themes; no markup changes.
## Video pairs keep the post file with a warning when the story cannot be placed — ACL-1476

Owner decision 2026-10-01 (Issue #476) after ACL-1426 went live: two post/story pair
cases no longer hold the campaign as "do not publish".

- `crop_step` — a pair whose wizard showed no Crop step (the single-screen editor walks
  Media → Text → Creative generation → Enhancements; runs e4fd79d26e63, bd7b2f…) and a
  pair whose `_story` file is not in Meta's library (runs a88e3789f7ab, 362762a797c3)
  are warnings `story_not_placed:no_crop_step` / `story_not_placed:not_in_library`
  (`crop_step.warning()`, telemetry `ad_creative_crop` ok=None). The ad keeps the post
  file. In the not-in-library case Original on both cards and Meta AI expansion off are
  still applied and read back; only Replace is skipped.
- Meta's "Replace media for Stories, Status, Reels" opens inside the wizard dialog (step
  row, Close, Save; no Cancel). The way back to Crop is the step row's "Crop", not the
  wizard's Close (which closed the wizard in a88e3789f7ab). If Crop does not come back,
  the step blocks with `crop_replace_picker_not_closed`.
- `meta_dom` — a successful creative step with such a warning adds the ad's warning row
  `<ad>_story_not_placed:<reason> - <ad>: <English line>` (`simple_create.creative_why_text`:
  "Story placement uses the post file: …"); `failure_policy` classifies
  `story_not_placed` as WARNING, so the campaign stays publishable.
- Still blocking: Meta AI expansion left on, `crop_unknown_toggle_on`,
  `crop_vertical_not_story`, `crop_square_changed`, `crop_replace_picker_unscoped`,
  Original not set, `crop_replace_off`.

## Post and Story get the right file in the Crop step — ACL-1426

Owner rules 2026-10-01 (Issue #426): the creative number pairs `_post`/`_square` with
`_story`; the Square card gets the post, the Vertical card the story (Replace) or, without
a pair, the same file; Original on both cards; "Crop and expand (Meta AI)" off.

- `src/fb_ads_rpa/creative_pairing.py` — the table (pair by number regardless of the
  prefix, same media kind, same folder; same prefix wins). A `_story` chosen in the
  Builder with a `_post` sibling puts the post on Square (the ad's media).
- `src/fb_ads_rpa/crop_step.py` — on the Crop step, before the walk's Next: Original on
  each card, expansion unchecked, Replace on Vertical for a pair (library search, tile,
  the picker's own confirm), read-back. Mismatch → named reason (`crop_*`, e.g.
  `crop_vertical_not_original`, `crop_story_not_in_library`, `crop_step_not_shown`), the
  creative step fails with it, no retry, the ad is held for review. Telemetry
  `ad_creative_pair`, `ad_creative_crop`. Switches: `FB_RPA_CROP_STEP=0` (old
  behaviour), `FB_RPA_CROP_REPLACE=0` (pairs blocked with `crop_replace_off`).
- Ads 2–3 in the single-screen editor: the new ad's form gone after the wizard (empty
  Ad section, "Verifying your changes" with the `#100 lead_gen_form_id` preflight
  error) is named `ad_form_gone_after_media` after 30 s instead of 150 polls, and the
  retry loop neither checks again nor opens a second wizard (was ~3 + 3 + 2 min per ad).
- Builder: `/api/creatives` carries `story_label` ("Story: <file>" / "Story: same file,
  Original"), printed under "Ad N" on the preview card. `serve.py` restart needed.
- Review of PR #440 (round 2):
  - H1: every success return of the creative step (`upload` True, the retry loop's
    media check, the three known-file "media already on ad" returns, the known-file
    reuse) first asks `crop_step.outcome()` through `meta_dom._crop_gate`. A Crop
    failure stored by the retry or give-up close-walk, or a pair whose wizard never
    showed Crop (`crop_step_not_shown`), fails the step with that reason (telemetry
    `ad_creative_upload` with `where`) instead of being erased by `set_plan(None)`.
  - M1: the walk recognises the Crop step by its heading "Select media crop for
    placements" as well as by the step row; a single file whose Crop step was never
    handled gets one warning row `ad_creative_crop` / `crop_step_not_shown`.
  - M2: Meta AI expansion fails closed — "AI expansion"/"generative" labels count as
    expansion, a box outside the cards is switched off (`shared:expand_off`), and any
    other checked box or switch on the step blocks with `crop_unknown_toggle_on`.
  - M3: Replace is proven by the picked tile's thumbnail ids on the Vertical card
    (mismatch → `crop_vertical_not_story`; no ids on either side → warning row
    `as_planned_thumb_unconfirmed`, as on the library-pick path).
  - L1: the picker's Done/Select/... is pressed only inside the picker's own dialog;
    without one (or inside the wizard's) → `crop_replace_picker_unscoped`.
  - L4: `ad_form_gone_after_media` and `crop_*` reasons carry an English sentence
    (`simple_create.creative_why_text`) in the step diag `text`.

## Every step of the creative wizard is captured, read-only — ACL-1426 (phase A)

Live runs 04c8c214d2ba and 0bd520418dbe (Facebook_1443, 2026-10-01): after a
library pick the robot walks the Trim → Crop → Text → Image generation →
Enhancements → Translation wizard with its forward button, so the Crop step keeps
Meta's defaults (Vertical 9:16 with "Crop and expand (Meta AI)"), and no capture of
any step after Media existed (the editor page is cut at 4 MB before the wizard).

- While `_walk_creative_wizard` sees the wizard after a library pick or an upload
  (both editors, all ads), each new step is captured before the forward press:
  the scrubbed wizard block in `output/rpa_pages/<run>/NN_wizard_<step>`
  (`wizard_trim`, `wizard_crop`, `wizard_text`, `wizard_image_generation`, …), the
  read step state (step row, current step, headings, buttons, checkbox/radio
  states) in `context.json`. At most 18 per run, on their own quota — the
  failure-capture limit is untouched; no Sentry section is remembered for them.
- One telemetry row per walk, `ad_creative_wizard` (note `<origin>:closed|left_open`,
  diag `sequence`, per-step `at_s`/`s`/`pressed`).
- Read-only: no presses, scrolls or waits added; the press sequence is the same with
  capture on and off, and capture failures never reach the walk
  (`src/fb_ads_rpa/wizard_capture.py`). Machine switch `FB_RPA_WIZARD_CAPTURE=0`
  turns the captures off (the telemetry row stays).

## Ads 2-3 in the single-screen editor: media found, Creating... waited out — ACL-1412

Live run d1755a59e139 (Facebook_1443, 2026-09-30) left ad 2 without media, link
and text and stopped ad 3 at `dialog_open`.

- Before opening the creative, checking the media or setting the destination of
  ads 2+, the robot scrolls the new ad's Media block back on screen (the editor had
  unmounted the ad form, so "Set up creative", the attached media and Destination
  were not in the page). Only scrolling: no presses, no reloads, still inside
  `hold_open_ad`. The media entry used is logged: `ad_creative_upload` note
  `media_entry:<set_up_creative|picker_open|media_on_ad|media_block_*>`.
- Meta's "Creating..." dialog after Create ad is waited out for up to 120 s
  (`META_BUSY_WAIT_S`) whether or not the page answers; any other dialog stops the
  ad at once (`adN_create:dialog_open`). Meta's media picker left over the new ad
  is closed with its own Cancel. `simple_create_more_ads` telemetry gains
  `progress_s`, `dialog.open_s`, `dialog_kind` and `media_picker`.
## Placements pass when Meta no longer offers a placement, and in the single-screen editor — ACL-1445

2026-10-01 campaigns ended "do not publish: placements" on Doc19 and on the tester's
machine although the placements were right. Three causes, each now a bench test
(`tests/test_bench_placements.py`).

- Meta stopped offering "Messenger Stories" on some accounts during the day. The robot
  has to switch it off, and it treated the placement's absence as a failure. Rule
  (`fill_placements`, `_settle_category`, `_CATEGORY_END_JS`): a placement to switch OFF
  that is absent counts as "not offered" only when all of these hold:
  - all five categories are present;
  - its own category (`_SUBITEMS_BY_CATEGORY`) is open;
  - the category's list is scrolled to a proven end: the last row is shown, no row of
    the category is hidden, the inner scroll is at the bottom, nothing is loading, and
    the next category is shown;
  - the list and that proof have not changed for 1.5 s (polled for up to 6 s while
    the end is unproven; rows that appear meanwhile are set at once);
  - every placement to switch ON in that category was rendered;
  - the placement never appeared anywhere in the step (`subitems_seen`).

  Otherwise the absence stays a failure, as before. A tail that never mounted is never
  judged; 8e9acb2 / ACL-1220 recorded "Threads feed" and "Facebook Notifications"
  mounting only after scrolling. A placement to switch ON is never "not offered". The
  step's telemetry line names the not-offered placements (`subitems_not_offered`, note
  "not offered: …"). Read-back still checks every offered placement.
- Single-screen editor (cb854b07857b, 30.09): the open Placements card showed only a
  "Loading..." progressbar, with no "Account controls" to anchor on, and the step
  reported `placements_card_not_found`. The card is now found by its own anchor
  (`data-surface` ending in `adset_placement_static_pgd_section`). Its body is waited
  for (20 s), the card is tagged again once it has loaded, and placements are then set
  and read back there as in the classic editor. A body that never loads is reported as
  `placements_card_loading`.
- Single-screen editor (SA_ARC_All_Video_IPO_35_01.10, tester): the placements list was
  collapsed to "Placements · Included: 7 · Allowed with limited spend: 0 · Excluded:
  13". The counts are not proof of which placements are included, so the summary is not
  accepted. The step opens it and reads the rows back; if it cannot open it, the step
  fails and reports the counts (`placements_summary`). The fixture was rebuilt from the
  screenshot text, as there is no capture.
## The robot finds Meta's "Add text option" next to the text generation section — ACL-1344

28.09–01.10 the second primary text was not added on most video ads
(`ad_copy_second_text: add_control_not_found`, Doc21 four runs in a row, the
tester's machine 01.10 twice). Every live capture of the block has the control:
`#add-bodies-option-button` > `div[role=button]` with `data-surface`
`.../message_field/lib:dco-add-asset-button`, text "Add text option" — the
same in the classic and the single-screen editor (bench captures
cc8fcfa17ae9 01.10 and 3100d06ad51e 30.09). The old finder looked for it by
text only and dropped anything inside a "text generation zone"; when Meta
draws "Advantage+ creative text generation" in the same `message_field`, the
zone grew from that heading up to the parent it shares with the button and
swallowed it. Every failure on 01.10 had `zones 3–7` and the generation
section on the page; every success had no section.

- The button is taken by its `data-surface` (in Meta's slot), and a
  generation zone stops below anything that holds our field's controls.
  Nothing inside Meta's generation section is pressed or counted as ours.
- If Meta redraws the new option box right after the robot marks it, the box
  is taken again instead of failing with `option_box_not_focused` (29.09–30.09
  live, up to two retries; `reclaims` in telemetry).
- Not found still means a warning, not a blocked ad (owner decision); the ad
  creative block is captured once per ad for the next diagnosis.
- Tests: `tests/test_bench_add_text_option.py` on both real pages, as captured
  and with the real 29.09 generation section in place — find, press, type
  text 2, read both texts back. Meta's new box itself is still emulated.

## Finished clean profiles close themselves; a Close profile button for the rest — ACL-1444

2026-10-01. With "Publish all", every finished account left its Multilogin
window open ("Browser left open for manual review"), 0.6–1.5 GB each; on a
tester's laptop 4 of 6 windows were idle, memory 92 %, and waiting accounts
could not start.

- End of a job (`publish_jobs._let_go_of_profile` → `RpaPublisher.finish_job`):
  when every campaign is publishable, the job has no errors, it was not
  stopped, no other job is waiting for the same profile, and the draft is
  saved (fail closed: anywhere but the campaign list — classic `/edit`,
  single-screen `simple_create`, any other page — Meta's autosave settles and
  "All edits saved" must show), the robot stops
  the profile gracefully through the MLX API — `allow_kill=False`, this
  profile only. Only a profile the robot started or attached to over its
  automation port and that still runs the same process (ACL-1320/1388
  markers); a hand-opened window is never closed. Any problem keeps the window
  open as before. The job panel says "Closed the profile — all campaigns are
  ready"; telemetry `profile_closed_after_job` (ok True = closed, False = the
  MLX stop failed, None = kept on purpose; note = why: `kept_problem`,
  `kept_switch`, `kept_unsaved`, `hand_opened`, …).
- Per-machine switch `FB_RPA_KEEP_PROFILES_OPEN=1` keeps every window open
  (old behaviour).
- Dashboard: "Close profile" on a finished job's panel →
  `POST /publish-jobs/{job_id}/close-profile`, the same graceful MLX stop.
  Refused (409) while any job of that profile is pending/queued/running or a
  read holds the profile lock; the lock is held during the stop.
- Profile queue: slots count jobs, released when the job ends (unchanged); the
  memory decision now sees the freed memory once the job releases its slot.
- Tests: `tests/test_a_finished_clean_profile_closes_itself.py` (46);
  `tests/conftest.py` sets `FB_RPA_KEEP_PROFILES_OPEN=1` for all other tests.

## Connect Multilogin issues the token in the right workspace — ACL-1441

2026-10-01. "Connect Multilogin" (Accounts → Connect) asked for the automation
token in the login's default workspace. For a team member whose default is
their own Free workspace, Multilogin answered "403 Request is not allowed",
although the same login may issue tokens in the team workspace that holds the
profiles (seen live: ndays6443 → own Free workspace; KLD profiles in
1ejadmedia, role `user`).

- `src/web/multilogin_auth.py` `sign_in()` now: `POST /user/signin` →
  `GET /user/workspaces` (`workspace_id`, `name`, `role`) → switch with
  `POST /user/refresh_token {email, refresh_token, workspace_id}` →
  `GET /workspace/automation_token?expiration_period=1mo` in that workspace.
  Field names checked against the Multilogin X API documentation (Postman
  collection 28533318/2s946h9Cv9).
- Which workspace: the one the person picked → the slot's remembered one →
  the only one → the only one whose plan has automation
  (`GET /workspace/restrictions` → `automation_available`). Otherwise the
  form shows a picker (name, role, "no automation") and Continue re-sends the
  sign-in with the choice. No token is issued before the choice.
- The slot remembers the workspace in machine settings
  (`MULTILOGIN_WORKSPACE_<slot>`, `MULTILOGIN_WORKSPACE_ID_<slot>`); the
  connection line shows it. The password is still never stored.
- Named errors: "This workspace's plan has no automation — choose the
  workspace with your profiles"; "Your role in this workspace cannot issue
  tokens — ask the workspace owner"; a failed switch names the workspace.
- Every Multilogin call from the sign-in carries a normal browser
  User-Agent (Cloudflare answers 1010 to the python-requests one).
- If the workspace list is unavailable, the old behaviour stays: token in the
  default workspace.

## Ads in the account tree get their workbook numbers — ACL-1395

2026-09-30 on Facebook_6540 (CO_bnc_All_Video_FX_Y_35_28.09) the campaign row had
its workbook numbers and both ads under it showed only dashes. Two causes.

- The account read took the name cell's whole text, so Meta's label came with the
  name: "1741 - Pro Bancolombia Unpublished edits Unpublished edits". Nothing in the
  workbook matched it. The read now takes the name from the cell's own tooltip
  (`data-tooltip-content`, present on every row in the captured Ads Manager pages)
  and strips trailing labels by one list, `META_NAME_LABELS` in
  `src/analysis_agent/account_spend.py`: "Unpublished edits", "In draft", "In
  review", "Draft" — only at the end of a name, whole and exact, never leaving it
  empty. The turn-off lookup compares names the same way. The tree applies the same
  list to readings saved before the fix (`amapTreeName`, a copy kept equal by test).
- Several ads with one name in one campaign used to get a dash. Owner decision
  2026-09-30: each shows the workbook totals for that name once (not multiplied),
  with a quiet "shared by N ads" mark and the tooltip "The workbook cannot tell ads
  with the same name apart; these are the totals for all of them". Campaign rows are
  unchanged; two campaigns with one name keep the dash.

## Every Meta layout the robot has met is an offline test — ACL-1433

2026-10-01 Doc19 placements failed on code that passed the same morning: a Meta
variation, learned only on the next live run. The bench now holds every layout
the robot has met, so a robot change is checked against all of them before merge.

- `tests/bench/CATALOGUE.md` (from `tests/bench/catalogue.json`,
  `scripts/bench_catalogue.py --write`): per robot step every layout seen live,
  with its fixture, test, expected result and status; captures seen but not cut
  are listed with the reason. `tests/test_bench_catalogue.py` fails when a
  fixture is not catalogued, a named fixture/fragment/test is missing or the
  Markdown is stale.
- `scripts/capture_to_fixture.py`: a live capture folder (`page.html`, possibly
  truncated at 4 MB, or `block_*.html`, plus `context.json`) becomes a scrubbed,
  trimmed fixture (named blocks only; ids, `act=`, URLs, e-mails, phones,
  tokens, media file names, campaign and profile names replaced; render ids to
  `syn_N`) checked with the secret scanner's rules, plus an optional test stub.
  Documented in `tests/fixtures/ads_manager/README.md`.
- Harvest of 29.09–01.10 captures: 10 new fixtures (wizard steps of five step
  rows incl. Trim/Crop, Doc19 and single-screen placements, five destination
  layouts and the ad error block, editor gone under the robot, languages chips,
  ad copy without Headline with the first real "Add text option", conversion
  card with a heading button group, default-only Locations, single-screen
  Audience Advantage+ on). Each test runs the robot's own step or reader.
- Live bugs found, kept as strict xfail for the RPA track: Doc19 fails
  placements because Meta no longer offers "Messenger Stories"; the
  single-screen Placements card is not found (anchored on "Account controls").
- Review checklist (`.github/pull_request_template.md`,
  `docs/system/AGENT_CONTEXT.md`): every robot fix adds its live capture to the bench.

No robot code changed.

## The test suite runs in parallel — ACL-1397

2026-09-30. The full suite (3800+ tests) ran one test at a time: 82 min on the
owner's laptop while other sessions shared it, 22–23 min for the test step in CI
per Python version. It now runs with pytest-xdist:
`python -m pytest tests/ -q -n auto --dist loadfile` (CI and
docs/repository/WORKFLOW.md). Locally on the same laptop: 20 min with 8 workers.

- `--dist loadfile`: each test file stays on one worker, so the 80+ files that
  start their own Chromium start it once, not once per worker.
- CI: the hosted Windows runner (private repository) has 2 CPUs, and xdist
  alone left CI at 23–31 min. The suite is now also split into 3 jobs per
  Python version by `scripts/acl_test_shard.py` (every test file in exactly one
  shard, checked by `tests/test_acl_test_shard.py`).
- `test_a_stopped_profile_never_spins_the_robot` reserved a "free" port and
  released it before Chromium took it; under parallel runs another worker got
  it first (ECONNREFUSED, CI 3.13). Chromium now picks the port itself
  (`--remote-debugging-port=0`) and the test reads it from `DevToolsActivePort`.
- `tests/conftest.py` sets `ACL_LIBRARY_REPO/TOKEN/BRANCH` to empty strings in
  every worker, so a machine `.env` with the live team library can never be
  picked up by a test run.
- Fixed waits replaced with waits for the event itself: the build-percent
  layout check (`test_campaign_build_percent_browser`, a known flake) reads both
  boxes in one settled frame; the drawer tests wait for the intercepted preview
  request instead of sleeping 100–500 ms; a publish log in
  `test_meta_campaign_agent` goes to `tmp_path`, not the repository `output/`.
- No product code changed; no test is serial-only.
## A disabled ad account no longer shows as "no warning" — ACL-1391

2026-09-30 the account map showed "no warning" for FacebookDoc20 while Ads Manager
wrote "Account disabled" in the Delivery column of its campaign. Two causes:

- The status read looked only for banner sentences. A disabled account can have no
  banner at all, only the Delivery cell. The robot already recognised that shape
  before publishing (ACL-1313) with its own list of signs, and the two lists had
  drifted apart. The status read now also asks the robot's detector: "Account
  disabled" as a whole Delivery cell reads as `disabled`, "Verification required
  to advertise" as `restricted`. A test fails if the robot learns a kind of block
  the status column has no word for. The help link "Why is my ad account
  disabled?" still reads as nothing. Every sign in every frame is weighed, not
  the first one on the page: a verification banner above an "Account disabled"
  cell reads as `disabled`. Meta's lines come out space-joined, and a broken
  link to the robot's detector is logged once instead of passing silently.
- The last successful status stayed on screen after later reads failed: Doc20's
  "no warning" was from 22.09, and every read after it had failed. The map now
  prints the read date (dd.mm.yy) under the status, and a status followed by a failed read
  is greyed out and marked "not refreshed", with the failed attempt in its tooltip.

No new visits to accounts: the fix applies on the next Read a person asks for.

## The robot closes and reopens a hand-opened profile window when it is safe — ACL-1388

2026-09-30 06:49 UTC a "Publish all" run stopped Facebook_6540 at the start with
`profile_open_by_hand`: its Multilogin window had been opened by hand, and since
ACL-1320 the robot never stops such a window (stopping one once lost a Facebook
session Multilogin had not saved yet). Owner decision: close it automatically, but
only when it is safe.

- `src/fb_ads_rpa/hand_window.py` decides. All must hold: the profile's browser has
  been running at least 15 minutes (`MIN_AGE_S`, the youngest main process decides),
  the profile window is not the foreground window, and there was no keyboard/mouse
  input on the machine for 60 seconds (`MIN_IDLE_S`, `GetLastInputInfo`). Any failed
  read, no process, or not Windows means "not safe". Per-machine overrides:
  `FB_RPA_HAND_WINDOW_MIN_AGE_S`, `FB_RPA_HAND_WINDOW_MIN_IDLE_S`;
  `FB_RPA_REOPEN_HAND_WINDOW=0` restores the old always-stop behaviour.
- `browser.py` applies it at all three hand-opened places (`no_automation_port`,
  `unowned_port`, `mlx_lock`), only when publishing (spend reading keeps the stop).
  Safe: graceful MLX API stop of only this profile (no kill — a failed API stop keeps
  the old stop and closes nothing), wait for its processes to exit, start with
  automation. A start that fails after the close is `profile_restart_failed`. The
  existing login check on the way to the campaigns list stops a logged-out profile
  with `profile_logged_out`, without retries. Not safe: the same
  `profile_open_by_hand` stop and message as before.
- Visible: job panel line "Closed the hand-opened window of <account> and reopened
  it for the robot", telemetry `profile_hand_window` (note = outcome or refusal
  reason, diag = where, age_s, idle_s) and a Sentry breadcrumb with the same data.

## Ads 2-3 in the single-screen editor: media found, Creating... waited out — ACL-1412

Live run d1755a59e139 (Facebook_1443, 2026-09-30) left ad 2 without media, link
and text and stopped ad 3 at `dialog_open`.

- Before opening the creative, checking the media or setting the destination of
  ads 2+, the robot scrolls the new ad's Media block back on screen (the editor had
  unmounted the ad form, so "Set up creative", the attached media and Destination
  were not in the page). Only scrolling: no presses, no reloads, still inside
  `hold_open_ad`. The media entry used is logged: `ad_creative_upload` note
  `media_entry:<set_up_creative|picker_open|media_on_ad|media_block_*>`.
- Meta's "Creating..." dialog after Create ad is waited out for up to 120 s
  (`META_BUSY_WAIT_S`) whether or not the page answers; any other dialog stops the
  ad at once (`adN_create:dialog_open`). Meta's media picker left over the new ad
  is closed with its own Cancel. `simple_create_more_ads` telemetry gains
  `progress_s`, `dialog.open_s`, `dialog_kind` and `media_picker`.

## Updates install only what CI and the publisher vouch for — ACL-1357

Audit S06: the git update gate (`scripts/acl_update_gate.py`, and its PowerShell
copy in `Install ACL.bat`) accepted a release tag that merely contained "success"
and any `/actions/runs/` link, and the packaged ACL.exe trusted `release.json`
(with the installer SHA-256) from the same GitHub Release it downloaded.

- Git gate: the run link must be in this repository and the GitHub API must
  confirm `ci.yml`, `head_sha` = the tag commit, branch `main`, event
  push/workflow_dispatch, completed/success. A fabricated or foreign run is
  refused. A run that cannot be checked (no token, no network) is reported (log +
  Sentry) and, in warn mode, falls back to the old tag check;
  `ACL_UPDATE_GATE=enforce` refuses it.
- Packaged client: `release.json.sig` (Ed25519 over the exact bytes) is checked
  against keys pinned in `src/web/release_signing.py` before the hash is
  believed; an unknown key, a bad or malformed signature, or a signed manifest of
  another tag (or with no tag) is refused and not cached. The owner's public key
  `428a74e4f8e6e616` is pinned (Issue #357); a missing signature is still only
  reported (log + Sentry `kind=update_trust`) until enforcement: `ENFORCE`,
  build var `ACL_ENFORCE_RELEASE_SIGNATURES=true` or `ACL_RELEASE_SIGNATURES=enforce`.
  With a key pinned, CI refuses to publish an unsigned release.
- Installer gate stage `$AclGateEnforce` in `Install ACL.bat` mirrors
  `GATE_ENFORCE`; a test keeps them equal.
- CI signs with `scripts/acl_sign_release.py` and secret `ACL_RELEASE_SIGNING_KEY`
  (environment `release`). Owner steps and the GitHub protection checklist:
  `docs/RELEASE_TRUST.md`.

## The robot builds campaigns in Meta's single-screen editor — ACL-1327

2026-09-26 Meta started opening a new editor on some accounts
(`/adsmanager/manage/simple_create`: campaign, ad set and ad on one screen). The
robot knew only the step-by-step editor, so those accounts stopped. Owner decision:
support the new editor rather than look for a way back to the old one.

- `src/fb_ads_rpa/simple_create.py` recognises the editor by URL or markup at any
  step, scrolls to the Ad set / Ad sections instead of pressing Next, opens the
  collapsed cards and sets the Leads objective inline. The existing card handlers
  (name, budget, conversion, audience, age, locations, languages, interests,
  placements, identity, creative, destination, copy) run inside the one page; the
  draft is kept by Meta's autosave (telemetry `simple_create_draft`). Publish is
  never pressed.
- Named stops: `lead_terms_not_accepted` (a person accepts the Lead Ads Terms),
  `simple_create_layout` (unknown variation, or `FB_RPA_SIMPLE_CREATE=0`),
  `objective_inline` (the Leads objective could not be set on the page), and
  `simple_create_more_ads` — only the first ad is built; ads 2–3 are ACL-1353.
- Shipped in `acl-release-2026-09-29b` (the content of PR #340 through PR #376 and
  PR #378).
- Live check, Facebook_1443, 2026-09-29/30: four campaigns built end to end
  (campaign, ad set, ad 1, draft saved) in 5–21 minutes; the owner confirmed the
  campaign and ad set setup in Ads Manager on 2026-09-30. Because ads 2–3 are
  missing, the robot itself marks each of these drafts "do not publish as is"
  (`simple_create_more_ads` is blocking); the owner accepted closing this task
  on the one-ad result, with ads 2–3 in ACL-1353. On Facebook_9943 no 29.09 run
  got past the campaign name (an SSL error on reload, network trouble before the
  objective, a profile that failed to start); one affected account is what this
  task asked for.

## Sentry events carry the failing page section; a read-only triage digest — ACL-1325

2026-09-26 a new "Suggest an audience" Audience layout on one account broke "Further
limit the reach" and was found only because the run was on the owner's machine: the
Sentry event had step and reason, not the page. Owner decisions 2026-09-26: attach a
scrubbed copy of the failing section — yes; screenshots — no; react immediately; the
digest goes to the owner's Claude chat.

- Blocking-failure and campaign-stop events get `section_<step>.html` (≤ 200 KB): the
  card the failing step works on (plus an open dialog), taken in the page at the
  failure and rebuilt from a whitelist when sent — campaign/account/ad set/ad names,
  URLs, `act=`/`*_id=`, e-mails, phones, 6+ digit runs, tokens replaced; `js_*` ids →
  `syn_N`; text cut at 300 chars. Tag `page_snapshot=section`, context
  `page_snapshot`. Off per machine: `ACL_SENTRY_PAGE_SNAPSHOT=0`. Nothing new on disk.
  Review of PR #328: a failure on the campaigns list no longer sends the whole page —
  the open dialog, otherwise the list region, capped in the page (inside the
  Multilogin browser only; ids/URLs/contacts/tokens removed). A section never goes
  with an event of another run or campaign; profile stops carry none. The page is asked
  with a 5 s bound; a hung tab gives no section.
- `scripts/sentry_triage.py`: new/regressed issues since a cursor, latest event and
  section per issue, grouped by `step:reason`, matched to tasks in
  `config/sentry_triage_map.json`, printed as JSON + markdown. Never writes to Sentry
  or GitHub; `--apply` only records the cursor; section files are saved only into an
  explicit `--attachments-dir` (latest 50 issue folders kept), otherwise the outline is
  built in memory and nothing is written; no token → "skipped", exit 0.
- `docs/system/SENTRY_TRIAGE.md`: the routine (Sentry alert + desktop scheduled task
  every 10 minutes) and the owner's one-time setup. No alert rule or schedule is
  created by the code.

## "Publish all" runs only as many profiles at once as the machine's memory allows — ACL-1321

On the owner's laptop three Multilogin profiles with Ads Manager already used 85–89 %
of the memory and made every step 1.2–2.7× slower (2026-09-25/26); "Publish all" with
six accounts started all six. Owner decision 2026-09-26: the dashboard decides from
free memory, with a floor of one, a ceiling, and a per-machine override.

- Publish jobs now take a place from a profile queue (`src/web/profile_slots.py`)
  before the profile lock. Before each start: nothing running — start (floor);
  `ACL_MAX_PARALLEL_PROFILES=N` in the machine `.env` — exactly N at once, memory not
  asked; otherwise at most `ACL_MAX_PARALLEL_CEILING` (4) and only while free physical
  memory ≥ `ACL_PROFILE_MEMORY_GB` (1.5) + `ACL_MEMORY_RESERVE_GB` (1.5). A profile
  started less than 90 s ago is counted as not yet using its memory; an unreadable
  memory probe allows two. Settings are read at every decision (no restart).
- Waiting jobs are `queued`, start in submission order, and re-check when a job ends,
  when a job is cancelled, and every 2 s (memory can free up without a job ending). A
  job whose profile is busy waits for its profile without holding a place, and takes
  it first when it frees. Cancelling a queued job ends it at once.
- Telemetry: one `job_phase` row `slot_queue` per job — wait time, the memory reading
  and the decision that let it start, and why it had to wait. `GET /publish-jobs/slots`
  shows the current mode, what runs, what waits and the next decision.
- Dashboard: a queued campaign shows a still grey ring with a dot ("Waiting for a free
  slot (N ahead)" / "Waiting for the Multilogin profile"), the Publish-all list shows
  each unfinished account ("queued — waiting for a free slot (N ahead)" / "building
  the draft"), and the account panel shows "queued" with a Stop button.

## A campaign opens within two minutes, even when its tab freezes — ACL-1320

Run `cf563b4ba594` (2026-09-26, FacebookDoc19): the first campaign opened in 19.2,
16.3 and 1.3 minutes in three runs on the same profile process. For about 14 of the
19 minutes the campaigns-list tab did not answer at all, and the robot sat in calls
that have no effective timeout — `locator.count()`, `is_visible(timeout)` (Playwright
ignores that timeout), `page.evaluate`, CDP `send` — while the stall guard, checked
only at the start of an attempt, fired at 108 s and 224 s instead of 60 s. The
workspace hypothesis (a profile running from the inactive Multilogin workspace) was
ruled out by the log: the cause is a frozen renderer of a long-lived tab.

- Opening a campaign has a hard budget (`meta_dom._open_campaign`): about 45 s per
  round while nothing is visibly loading, 120 s in total. A round without the
  objective dialog goes back to the list and presses Create again (no more 75/180-s
  rounds and no reload on this path); while "Loading creation" is visibly spinning on
  a tab that answers, the round may use the rest of the budget. On the third round,
  or after the second "tab did not answer", Create is pressed in a fresh tab of the
  same profile (the frozen tab is closed); if the fresh tab does not answer either, the
  campaign stops with `profile_unresponsive`, and the publisher restarts the profile
  once through the MLX API with the token of its own workspace (A = Inhouse, B = KLD)
  and retries the campaign. Attaching to an already-open profile checks its tab first
  and does the same. Never a plain browser. Telemetry: `campaign_open_budget`,
  `fresh_tab`, `profile_restarted_unresponsive`.
- Every wait on the list asks first, with a driver-enforced 3-s timeout, whether the
  tab answers (`_tab_frozen`); page reads go through `_eval_bounded`. A frozen tab
  hands over to the stall guard at once, and the guard itself, the step log, the
  screenshot and the window put-back never open a CDP session on a frozen tab
  (measured on real Chromium: `new_cdp_session` on a hung renderer blocked 197 s).
- The objective wait gives a woken tab 18 s of fresh time, counts a slow poll as a
  busy page rather than an empty one, and hands a tab that twice did not answer back
  to the budget.
- The campaigns list must show a visible Create and no route spinner within 30 s of
  the goto, else it is reloaded once (`campaigns_list_ready`); the tab is woken before
  the first Create without bringing the window forward — it stays parked off screen
  (decision of 2026-09-19); the guard's reload re-applies the throttling fix.
- A stall leaves evidence: a PNG (bounded Playwright path only), a page capture and
  the tab's `JSHeapUsedSize` / `Nodes`, at most four per run (`stall_evidence`).

Carried from the final review of PR #314: a parked window that was later widened is
still recognised as our parking (the spot is remembered), so its off-screen place is
never taken for its home; the second wrong-ad check after re-reveal and the
"name field unreadable → still type" path are pinned by tests; a failed ad-name step
no longer also fails that ad's copy with `wrong_ad_open` (the copy is checked against
the name actually in the field); an exception from building a job's drafts reaches
Sentry like one from publishing, and the `job_exception` tags are tested; the test
telemetry guard proves the journal is temporary before the first write; Home computes
the workbook's activity once per workbook revision, and "A, B is placed in a country"
reads "A, B are placed in countries".

After the live run of the candidate on 2026-09-26 15:21–15:57 UTC (new accounts
Facebook_1443, _6540, _9943; the build was pulled back to the released one):

- A stopped profile no longer spins the publish thread. On a gone tab every
  Playwright call fails in milliseconds; the liveness probe answered "could not ask"
  and the wait loops went round again with the GIL held until the dashboard stopped
  answering. `page_liveness` now says "the tab is gone" (closed page, disconnected
  browser, or a closed-connection error seen once), the objective wait and the campaign
  opener end with `browser_closed` at once, and no poll round is shorter than its step.
- A Facebook login page (`profile_logged_out`) and a broken proxy (`profile_network`:
  `ERR_SOCKS_*`, `ERR_PROXY_*`, `ERR_TUNNEL_*`, `ERR_CONNECTION_*`,
  `ERR_SSL_PROTOCOL_ERROR`, a Chrome error page, three failed navigations or a recent
  network error of the same profile) are diagnosed before any fresh tab or restart and
  stop the account's job with a message for the operator; a failed MLX restart is
  `profile_restart_failed`. Only a genuinely unresponsive tab gets a fresh tab / restart.
- A profile opened by hand (no automation port, not the robot's own Mimic process) is
  never stopped: the job fails at once with `profile_open_by_hand` — "Close its window
  (so Multilogin saves the session), then press Publish again."
- Meta's onboarding tour ("A streamlined way to create campaigns", pager "1 of 3") is
  closed by its own X only, never Next; Meta's new single-screen editor
  (`/adsmanager/manage/simple_create`) stops the campaign with `simple_create_layout`
  instead of failing later.

Independent review of PR #323 (request-changes) addressed: a round no longer cuts a
slow "Loading creation" (once loading was seen, only idle time after it or the budget
ends the wait) and the last Create of a round gets at least 25 s; 120 s is a hard
ceiling (steps inside the open read its deadline); `profile_network` is decided from
facts of the current tab, and from history only with two unresolved errors after the
last successful navigation off a normal Meta page; a busy tab is kept (5-s probe,
consecutive failures, a 10-s confirmation before a fresh tab); a slow goto in a fresh
tab keeps the tab; the tour is recognised by its own text and never inside the editor;
after a failed name step only our name or "New Leads Ad" is accepted; the robot-profile
mark needs a port owned by the profile's process; an attach-time restart failure is
`profile_restart_failed`, attach-time stops reach Sentry; no profile-id prefix in
telemetry.

## Sentry receives only real campaign failures — ACL-1318 (phase 1)

Every telemetry row with `ok=False` used to become a Sentry event, so the list of
issues was dominated by diagnostics that are not failures of a campaign
(`advantage_plus_off: still-on`, `click_target_covered: silent`, `create_took`,
`conversion_website_pick`, `locations_open`, per-attempt rows, `ad_level_summary:
silent`) and real ones such as `ad2_copy: copy-fields-not-revealed` were buried. Now
`src/fb_ads_rpa/sentry_signal.py` sends an event only for (a) a hard failure that ends
a campaign attempt (one per attempt, `failure_kind=campaign_stop`) and (b) each
blocking failure of a campaign's final result (`failure_policy.split`, one per failure,
once per campaign, `failure_kind=blocking`). An exception that ends a publish job's
work (caught in `publish_jobs._run_rpa`) is sent with its stack through the same
scrubber (`failure_kind=job_exception`); before this it reached no one. Each
event is titled `step: reason`; the reason is the machine token inside the result text
(`(copy_fields_not_revealed)`, `…:row_not_found`), else a known kind, else the reason
the step's telemetry rows gave, or that of an explicitly listed helper step
(`conversion_website` ← `conversion_website_pick`, `adN_create` ←
`create_additional_ad`). The fingerprint is step without the ad number + reason, so
campaign names and numbers never split an issue. The event carries campaign, account
label, run id, job id, attempt and the scrubbed diag of the step's last telemetry row.
Everything else stays in local telemetry. Replaying the 25.09 telemetry: 77 events
become 18.

## Each campaign in the builder shows where its build is — ACL-1315

While campaigns were being built, the only report was the job list at the bottom of
the builder drawer, and after a dashboard restart it kept saying "running". Now every
campaign sent to a publish job shows a dot in place of its readiness sign: grey and
pulsing while it is built or waits for its turn, orange and pulsing when the build has
been silent too long or has hit something only a person can fix (account restricted
by Meta, Lead Generation terms not accepted, all retries used up), solid orange when
the campaign was built with blocking errors or not built at all, green when the draft is
ready ("Publish was not clicked"). A job the server no longer knows (404 after a restart) becomes an empty
ring without a pulse — unknown, not running. Jobs are remembered in localStorage, so
the dots come back after F5.

The facts come from `GET /publish-jobs/{id}/progress` (`src/web/publish_progress.py`):
the campaign's result, the robot's last step from the tail of `rpa_telemetry.jsonl`
(512 KB read per poll) and how long the job has been silent; the page decides the
colour. Rows count only for their own job: every telemetry row written by a job's
thread carries the job id, rows of another job are skipped even when the campaign
name is the same, and rows older than the job never colour a campaign. "Silent too
long" is 3 minutes up to the audience step and 7 minutes from placements on, where a
creative upload legitimately takes up to about six (25.09: 370, 356, 329 s). The dot
key on the page is account + name, so same-named campaigns of two accounts do not mix.

## The live robot ships: port of the live branch and ACL-1313 — ACL-1284

The robot that has been building the test campaigns since 2026-09-17 ran from a
separate branch (`local/live-2026-09-19`) on the owner's laptop, not from `main`.
This port merges that branch, together with the ACL-1313 hardening
(`codex/acl-1313-six-campaigns`), into a branch cut from `main`, so that buyers get the
same robot the owner has been watching.

What ships. The live robot's waiting and recovery: a stuck Meta load, a Chrome error
page or an editor block that never rendered is reloaded and retried instead of
failing the step; the objective dialog, the device section and the ad text field are
waited for as long as Meta is visibly loading. The audience: collapsed Locations and
Languages rows are opened by hover, their arrow, Enter on the row button or a click on
the value, with the target scrolled into view first and never clicked through a panel
that covers it; a country row far below the window is scrolled to before its Close is
pressed; interest groups are re-read until Meta has drawn them. The ad level: the text
is pasted whole, healed when the editor is blocked or loses its tail, and the
destination is set before it; the ad-set row is found in the editor tree and Create no
longer lands in the left menu. Pacing between actions is roughly halved
(`FB_RPA_SLOW_PACING=1` restores it) and the profile window can live off screen or on a
chosen monitor. ACL-1313 adds the fixes from the six test campaigns of 2026-09-24/25: a
blocked ad account stops its campaign, banners are closed by their own X, a minimised
profile window never hangs the run, a language Meta preselected counts, media Meta
carried into the next ad is not "no media", and success-path screenshots are opt-in.
The dashboard's period picker closes on Esc and on an outside click (`9c211c5`).

The second merge of `codex/acl-1313-six-campaigns` (up to `c8d9d42`) added: a busy
tab says why Publish does nothing (`c8d9d42`); a "Turn off ad blocker" modal or a
"connection lost" toast is closed and the page reloaded (in the editor only with "All
edits saved"); an editor block reading "component failed to load" is healed by a
reload and the "Extensions / Site links" window is closed by its Cancel, never Save;
Meta's route spinner (`AdsPERouteLoadPlaceholder`, no "Loading" text) counts as
loading; a placements category that Meta collapses again is reopened; and media Meta
already shows is confirmed on the ad itself whatever step the wizard is on
(`c27ee46`). A blocked ad account (`account_blocked`) is never retried. Two
environment variables: `FB_RPA_SHOT_ALL=1` brings back the success-path screenshots
and page captures, and `FB_RPA_WINDOW_LEFT` / `FB_RPA_WINDOW_TOP` park the profile
window at a chosen place (default: off the left edge of the screen). The live
`read_back_by_label` bypass of the age read-back is removed (it confirmed the age by a
number from a neighbouring field, ACL-1250); the Age reader treats "Use as a
suggestion" as part of the Age field (`dbaa58d`). `enrich_draft` does not force a
second library pull within 60 s of the previous one (`LIBRARY_FRESH_S`). Account spend
reads a campaign's ads once the tree list has settled and captures the page when the
links are ambiguous (`c0fd8e4`).

Review of PR #314 fixed in the same port: a profile window parked off screen on
attach is never maximised onto the owner's screen and is minimised again at the end,
each step on its own; screenshots ask the window (not the document, which focus
emulation keeps "visible") whether it is minimised and never call the unbounded CDP
screenshot on one; a reload on the second or third ad reopens that ad before it is
named; "Start authentication" stops the campaign only next to "Review N error" or the
code #3858385; tests no longer write into the live telemetry journal. From the live
runs of the candidate: a collapsed Age row is opened by its own row and an open Age
field is never collapsed by the old header-click fallback (run `5e29e66da497`); the
last country's disabled X still counts as its row; an already selected "Further
limit" radio is not a failure; the last Create of the objective retry gets a full
wait; the ad-set naming-template dialog is cancelled; `objective_wait` says what was
loading; and a wait longer than 60 s wakes the tab and reloads it only when it is
stuck (`stall_unstuck`, once per step, never on the second or third ad). Re-review:
a window minimised mid-run keeps its real home, and a home that is exactly our
parking spot is replaced by a visible point (0, 0); any other off-screen position is
taken for a monitor to the left or above and kept.

What does not ship. ACL-1311 (Multilogin Team access, PR #312) is reverted in the port
until the two-machine token check.

Owner decisions of 2026-09-25 this follows: one PR instead of four slices (PR #299 is
absorbed); in `meta_dom.py` the live fallbacks are kept and the ACL-1250 / ACL-1225 /
ACL-1226 guards from `main` are applied on top of them — every fallback looks for its
target only inside the field its label belongs to, the country summary must match
exactly, and the age is read inside the Age field; the media library is variant A
(videos with the yellow triangle are accepted, decision of 2026-09-24) with two
guarantees kept as tests: a different file is never "our creative", and processing
that never ends is a failure; tag and release once `main` is green.

Two things had to be adjusted to keep those decisions true on the live pages. The Age
reader from ACL-1250 returned nothing on the live layout (two "Age" labels, and
"Minimum age" / "Maximum age" labels between the label and the values); the live
robot had never read it either — all 162 ages of 2026-09-19..25 were confirmed only by
a bypass that the guard removes. It now tries each label on its own and treats labels
that name a part of the same field as part of it (fixture cut from the 2026-09-24
run). And three gaps in the live media path broke the first guarantee: a similar name
("2012_story.mp4" for "2012.mp4") counted as ours, a tile selected before the robot
arrived was never compared by thumbnail, and the outer upload retry turned a refusal
about the ad's own media into success. The media tests written for the strict #238
variant were rewritten for variant A; where the live code differs by design, the test
says so.

Approved by the owner on 2026-09-25 and fixed in the port as well: the library's
Account tab (`aria-selected=true` whenever it is open) no longer counts as a selected
tile — it made every undo after a pick that did not take look dirty, the likely cause
of the live `library_pick_left_selection` refusals; a library grid that is not on
screen reads as "unknown", so an undo after a click without a visible grid never
allows an upload on top; and "Video rejected" on the ad's media is a failure
(`media_rejected`). An unreadable Media state stays non-failing, since media presence
is checked separately.

## Multilogin is connected in the dashboard, not in the installer — ACL-1303

The wizard no longer mentions Multilogin at all. Setup is six steps instead of seven,
finishes without a token, and never writes one; the person connects in the dashboard,
on the Accounts screen, when they sit down to work. The Readiness and Finish screens
say so, because without that connection the robot cannot open a profile and the first
Publish dies on "TOKEN_A is not set".

What sent the step back out of the installer was its own verification. On the owner's
machine, with a token the robot had been using all week, it answered "Multilogin
returned HTTP 403". That turned out not to be Multilogin: the wizard checked through
`urllib` with no `User-Agent`, and Cloudflare refused the request before it arrived
(error 1010). With an ordinary header the same call returns 401 — meaning both stored
tokens are dead, although the token itself says it is good for another two weeks.

So Multilogin appears to invalidate an automation token when the same workspace signs
in again, and the team shares one login. Ten installs in a row would have left exactly
one person — the last — with a working robot, and the other nine would have found out
days later, on their first campaign. A dashboard that connects on demand survives that:
whoever was knocked out signs in again, sees the result on the spot, and knocks out
whoever connected before them, which is no worse than the account allows.

The rest of this section is how the step got here before it was removed; the machinery
it describes is gone from the wizard, and the sign-in itself lives on in
`src/web/multilogin_auth.py`, where the dashboard uses it.

### Before that: the installer asked for a Multilogin sign-in, not a token nobody has


Walking a clean install from a published release on 2026-09-24 stopped at step 5 of 7:
the wizard asked for a "Multilogin access code". Those are not handed out — whoever
signs in obtains one — so a new campaign manager has nothing to type there and no way
to get it. Meanwhile the dashboard, one screen later, already knew how: sign in with a
Multilogin email and password, receive an automation token, store only the token.

The owner never hit this because his token was already in the machine settings from
earlier work; it took walking the install as a new person to see it.

The step now asks for the email and password, signs in, and keeps the token. Pasting a
token is still there, clearly as the second option, for anyone who was handed one. The
password is used for one request and is cleared from the field as soon as the token
arrives; nothing writes it to the machine settings, and `machine_config.py` has no
notion of a password at all.

Pasting did not work in the wizard's fields. Tkinter binds paste to the Latin letter
"v", so with a Russian keyboard layout the event carries "м" and the binding never
matches — and there was no right-click menu either, leaving nothing but typing the
address by hand. The whole team types in Russian. The shortcuts now key off the
physical key (Windows keycodes for A, C, V, X) rather than the letter, and a right-click
menu offers Cut, Copy and Paste.

Three things the owner found while walking the built installer, all on the same screen.
The two accounts are named the way the dashboard names them — Inhouse and KLD, from
`mlx_instance_labels()` and overridable through `ACL_MLX_LABEL_A`/`_B` — rather than
carrying a second set of names ("Main", "Second") for the same two things. The
paste-a-token fields are hidden behind "I was given an access code instead": minting a
token requires being the workspace owner, so pasting one is the rare case, and showing
the fields to everyone made the step taller than the window. And the window's Next and
Back buttons were packed after the page body, so on a laptop they were squeezed off the
bottom and the person was stuck with no way forward; the footer is now reserved first
and the default window is taller.

Signing in lives in one place now (`src/web/multilogin_auth.py`) instead of two: the
wizard runs before the dashboard exists, so it cannot call the route, and a second copy
of the same logic is a thing that gets fixed once and forgotten once. The MD5 the
Multilogin API requires, and the address of that API, are stated once.

Review found one real defect in the first version of this fix, on the very machine being
set up: signing in to one account marked **both** slots verified. A typo sitting in the
second account's box would then have been written to the machine settings as a working
token, under a green "verified". Only the slot that just signed in counts now; the other
still has to be tested. Two more branches — a refused sign-in, and a sign-in that yields
no automation token — had no test behind them, and deleting either left the whole suite
green while the wizard finished "ready" with no token at all and the first Publish died
on "TOKEN_A is not set". Both are pinned now, in the wizard and in the dashboard route.

Two more things the same screen was getting wrong. Slot A and slot B are two different
Multilogin accounts — here Inhouse and Agency — and both fields read "Multilogin access
code", the second distinguished only by "(optional)"; they are now named. And signing in
is not the same permission as being allowed to mint an automation token: only the
workspace owner has the second. A team member's correct password gets through the first
step and no further, so the wizard now says exactly that and points at the owner,
instead of reporting a success that leaves the robot unable to open a single profile.

## A browser test that passed while the code it covered threw — ACL-1296

The Playwright fixture in tests/test_country_refresh_review.py built its page from a
slice of report.html that ended at `amapMoney`, so `amapSplitNote` and
`amapApplyReading` were never in it. When a test drove a successful country split,
`amapRefreshSplitNote` threw `ReferenceError: amapSplitNote is not defined`, its own
`.catch` swallowed it, and the test stayed green — the path it existed to cover never
ran once. On the real page the function is defined, so nothing was broken for anyone
using the dashboard; what was broken was our evidence that it works.

The slice now ends at `amapPaintTree` and the fixture stubs the two functions beyond it
(`amapPaintTree`, `amapSpendFailed`), which this file does not test. Ten checks that
used to search the source text for phrases — "does the function body contain
`theirs === ours`" — now execute the code instead: the note after a resumed read
("countries read for $10.00, not the $41.50 above"), equal amounts producing no
accusation, half a cent counting as no difference the way the re-read rule counts it, a
never-read / running / failed split saying nothing, a split that does not remember its
spend saying exactly that, the refresh rewriting the note exactly once, a reading that
is still running leaving it alone, and both rows of a two-country account carrying the
same note. Breaking the repaint on purpose turns five of them red; the source greps it
replaced stayed green through the same break.

Review then mutation-tested the result and found four properties that no check could
fail on: that a finished split refreshes the note at all (breaking the call left the
tooltip explaining the old money forever — the ACL-1288 symptom, silently restored),
that the two pollers hand on the split they received, that the server half compares the
amounts as written rather than as numbers (with numbers, an account at 41.502 against
41.50 is accused of a lag the rule deliberately ignores), and that the refresh walks
every row of its account and no other. Each is now pinned, and each mutation turns
exactly one test red.

Fixing that surfaced a real drift: the server writes a single-day reading as one date,
while the rebuilt title always wrote `from…to`. The old implementation preserved it by
accident, because it kept the existing text and only replaced the tail. Building from
data made the tooltip change under the reader on a reading of the same dates, so
`amapReadTitle` now writes one day as one day.

`amapRefreshSplitNote` also stopped rebuilding the cell's title by splitting the live
one on `' · countries '` and now builds it from the reading it already holds, through a
shared `amapReadTitle`. Splitting on a separator the template writes couples the two
across a change either one can make alone.

## The country split no longer lags one press behind the money — ACL-1288

ACL-1282 decided whether to read the country split at the moment the spend read
started, which meant comparing against the *previous* amount. The press on which the
money actually changed read the new spend and left the split alone; only the next press
noticed. On finished dates nobody sees it — the first read takes both — but on today's
dates the screen showed a fresh spend beside a breakdown explaining the old one, and
looked perfectly consistent doing it.

The decision now travels with the finished reading: `/account-spend/status` returns
`plan` when the reading is `ok`, and both the row and the "Read all spend" queue use
that instead of an answer from the POST that started the read. `/account-spend/read`
no longer answers about the split at all — there is nothing to compare at that moment,
because the record it writes says "reading" and carries no amount. While a read is in
flight no decision is made, which is the same rule stated once instead of twice.

Moving the decision to the status route made a reload capable of building a report on
its own: `amapResumeReadings` resumes a read that was in flight, through the same poll,
and would have acted on the plan it now receives. Reports inside a live ad account are
something a person asks for, so the poll carries a `pressed` flag and only a press
starts the split; a resumed poll shows what was read and stops there.

The decision is also computed from the reading and split the route already has in hand
instead of reading both stores a second time. That removes a doubled read on a poll
that runs every three seconds, and a real race: between the two reads a new press could
land, and the plan would then be computed against a "reading" record with no amount —
the same "decided against a different reading than the one on screen" fault this change
exists to remove.

The account row's reading tooltip now says which spend its country split explains, and
says so plainly when that is not the amount above it — from the server on page load and
from the same poll afterwards, because the live repaint rebuilds that cell and would
otherwise wipe the only notice on screen. Both halves compare the amounts as they are
written, which is the rule that decides whether to re-read them (to half a cent), so the
screen cannot accuse itself of a lag the rule ignores.

Measured over five presses on a single-country account with the money changing on the
fourth: the split is read on press 0 (there is none yet) and on press 3 (the amount
differs), and on no other press. Before this change it was read on press 0 and press 4
— one press after the money moved.

## The tree name gets its own line where eleven columns do not fit — ACL-1277

The remaining half of ACL-1277: on a laptop with the Builder drawer docked open the
campaign name in the tree had about 91px at 1280 and 115px at 1366. Measured with the
real fragment: the ten columns other than the name need 660px by their content alone,
while the table gets 582px at 1280 and 668px at 1366. There is no width to
redistribute — the table does not fit its own contents, and the account row is
squeezed whatever the tree does.

Owner's decision (2026-09-23), choosing between hiding the derived columns and moving
the name: hide nothing. Where the name column is narrower than 210px — which leaves the
name under about 134px once the caret, the level mark and the on/off switch have taken
their share — the tree row puts the name on its own full-width line and the numbers on
the line below.

The numbers on that line do not keep the columns. The first attempt did, and review
caught what that means with honest cell padding: every numeric cell had about 2px of
text room and rendered as "$…", "1..", "6..". Which header such a number stands under
does not matter, because at that width the header row itself reads "S… A… L… F…". So
the second line lists the numbers in order, each with its own label — SPEND, META
SPEND, LEADS, FTD, CPL, CPA, CR, DELIVERY, META — and a cell with no value is not
shown at all, because "CPL" alone promises a number that does not exist. The workbook's
"—" counts as no value here: an ad usually has no workbook row of its own, and the line
would otherwise read "SPEND — LEADS — FTD — CPL — CPA — CR —". In the single-line
layout the "—" stays, because there it sits in its own column and costs nothing while
saying "we looked and found nothing". The indent belongs to the whole row, so an ad's
numbers sit to the right of its campaign's: it is the only thing that says which row a
numbers line belongs to. The labels
come from `data-label` through CSS and never exist in the single-line layout, where the
word is already in the table head.

A workbook that could not be read now says so in words, once per tree, instead of six
dashes per row: with the dashes hidden, a broken pipe looked exactly like "this row has
no workbook numbers", and the reason lived only in the tooltip of a cell that is no
longer there. The stacked row is variable-height by design — about 28px with nothing to
show, 40px with Meta's numbers and about 55px once the workbook numbers arrive, against
34px in the single-line layout — so an open tree reflows a beat after a reading lands.

The switch is made from the measured column, not from the window width: the width is
eaten by the sidebar, the docked drawer and the country block's own padding, none of
which the window knows about. The Builder drawer is now listened to as well
(`acl:builder-drawer`): it widens the page with its own padding and fires no `resize`,
so the tree kept the widths measured before it opened and its numbers ran past the
right edge of the table — the exact case this ticket is about. The class goes on the
whole tree, so ads drawn later — when a campaign is expanded — get the same layout.
Above that width the row stays a single line, columns and all: the extra height is not
free.

The test fixture was measuring a table that was not the product's: it put widths on the
cells and left them the browser's 1px padding, while the page gives every cell
0.42rem/0.85rem and the tree copies that padding to both sides of its own cells. Every
width in this 93-test file was therefore 26px per cell too generous — which is how the
"numbers keep their columns" version looked acceptable. The fixture now builds the
template's `<colgroup>` and applies the page's own cell padding.

Measured after the change at 582px (honest padding): the campaign name box goes from
91px to 506px and the ad's from 75px to about 490px, and every number is readable in
full. At 982px the layout is untouched, columns aligned as before. Six tests in
tests/test_the_account_tree_on_screen.py cover the switch, that every number says what
it is and that empty ones vanish, that the labels are the table head's own words, that
they stay invisible in the wide layout, the untouched wide layout itself, and ads
expanded after the layout was chosen.

## One button reads the account: spend always, countries when needed — ACL-1282

The account row had "Read spend" and "Countries". Both asked the same account about
the same period and each one opened it separately. They are now one button. It reads
the spend, and then, in the same profile session, the country split only when the
split would answer something the spend does not: the account is linked to more than
one country, there is no split for these dates, or the stored split is older than the
spend reading. Otherwise nothing else is touched — the split builds a report inside
Ads Manager, and that price must not be paid for every single spend number.

The decision is made on the server (`country_split.why_split_is_needed`, called from
`_split_plan`) and travels back with the reading, because the page has neither the
account links nor the two stores. It is taken *before* the spend read starts:
`start_read` replaces the stored reading with a "running" record that carries no
amount, so a decision taken after it has nothing to compare and would skip the split
exactly when the money moved.

Staleness is about the numbers, not the clock. Each split records which spend reading
it explains (`spend_at`, `spend`, `spend_currency`), and it is re-read only when that
amount or currency has changed. The first version of this change compared timestamps
— "the split is older than the spend reading" — and never settled: a press that read
only the spend still stamped a new time on the reading, so the next press called the
split stale and built a report. Measured by review: three Ads Reporting reports per
six presses on a single-country account with nothing changing. Re-reading the same
amount now changes nothing; a changed amount brings the split back, including an
"empty" split once money appears. Splits stored before this change do not say which
spend they explain and count as stale — read once, then settled.

The account's country count no longer decides on its own. It decides where there is
nothing to compare: with no successful spend reading for these dates, an account under
more than one country takes the split straight away, because its total says nothing
about where the money went. With a matching spend reading it is left alone — the same
report would give the same answer. (The issue's wording said "more than one country"
always needs a split; kept literally, one "Read all spend" run would build a report
for every multi-country account, every run, forever — five of fourteen accounts sit
under two countries.) Countries are counted the way the map displays them, so "KW" and
"Kuwait" are one country and not a permanent multi-country verdict.

"Read all spend" follows the same rule per account, inside the same queue: the
account is open now, and a second pass would be a second touch of a live account.

A failed split no longer discards a good spend reading: the shared button returns to
"Read spend" and the failure appears in the reading cell as "countries failed" with
the reason in its tooltip. It is rendered from the server too, so it survives a page
reload, and the country totals are refreshed even after a failure — a stale total
next to a failure message would be a number that no longer exists.

With two buttons instead of three the actions column drops from 19% to 13% and the
name column gains those 6%: 28%. In tree rows at 1280 with the Builder drawer docked
open the campaign name box goes from about 65px to 91px (the ad's from 49px to 75px);
at 1366 it is 115px. At that narrowest width the two buttons still wrap onto a second
line, exactly as the three did before. That is better, not solved — ACL-1277 keeps the
rest, and the next step there is a tree grid that drops the trailing button column,
which tree rows never use, in favour of the name.

A failed split shows the reader whatever the reading wrote, and several of those
sentences are still Russian (they pre-date this change, which is what first puts them
in the account row). Only the orphaned-reading text is translated here; the rest is
ACL-1287.

## Account tree reads cleanly: name, Meta result, column widths — ACL-1275 / ACL-1277

Three display problems in the tree the owner reported on 2026-09-22. Long campaign
and ad names wrapped onto a second line; they now stay on one line and end with an
ellipsis, with the full name in the tooltip. The "Setup" column means "is this
account ready" on the account row but carries Meta's own result on tree rows; the
tree value is now labelled "Meta" and its tooltip says whose number it is, so the
two are not read as the same thing. The trailing button column, which tree rows
never use, was the widest column (23%) while the name had 19%; widths are now name
22%, Setup 9%, buttons 19%, and "Remove" is the "×" control; no numeric column was
narrowed. Measured at 1280, 1366 and 1920 with the real fragment and the Builder
drawer closed: rows and the three buttons stay on one line, and the name column
gains 29px at 1280 and 49px at 1920.

With the Builder drawer docked open (400px) the table is 582px at 1280 and 668px at
1366; the name then shows about 7-10 characters, and the account row's buttons wrap
to a second line exactly as they did before this change. Eleven columns do not fit
that width; the case is not solved here. ACL-1282 (Read spend and Countries merged
into one button) frees the next chunk, and a tree grid of its own is the fallback if
that is not enough. With the drawer open the "Meta" source label hides itself so the
number it labels stays visible; the source remains in the tooltip.

Two ads under one campaign showing the same name is real account data, not a read
bug: Meta's own embedded structure for FacebookDoc19 lists distinct ad ids with
identical names (2 in GMBL, 3 in Day). The existing "—" with "several ads share
this name" stays as designed.

## Latest read shown, marked, when the dates on screen have none — ACL-1271

The account map showed a Read result only for exactly the period on screen: open
1–22 September after reading 1–21 and the row said "—", "0/1 spend read", with no
tree. Now, when an account has no reading at all for the dates on screen, the row
shows its latest successful reading for other dates: muted and italic, with those
dates under the amount and in the tooltip ("not the dates on screen"), and the
campaign/ad tree is painted from it with the same note in its heading. Such amounts
are not added to the country total and not counted in "N/M spend read". A failed
reading for the dates on screen keeps its "failed · retry" and shows the marked
fallback under it (owner's decision: one failed attempt must not hide the numbers and
the tree); a running reading is shown as before. Read spend / Read all spend read these accounts for the dates on screen as usual.

## STAGE badge and "not on stage" count in the header — ACL-1267

Owner's decision (2026-09-22): the process stays as it is — working branch -> stage
-> main, and campaign managers get updates only from a release tag on main. The
dashboard run from source is the version the owner works on, so its header shows a
STAGE badge with how many changes in it are not on the project's `stage` branch yet
("+119 not on stage"), and the browser tab title starts with "STAGE ·". Commits are
counted without merges and without copies already on stage by the same patch; the
project remote is found by its URL, and the count is refreshed by a background
fetch every 10 minutes, so rendering never waits for the network. If the count
cannot be determined the badge says "stage unknown" with the reason in its tooltip.
Packaged builds (what campaign managers run) show no badge.

## Read buttons say what they read — ACL-1266

Account managers read the map's "Read" buttons as "connect the account". On the
owner's decision the buttons now name the action: "Read spend", "Read all spend",
"Read all spend in <country>"; Read all while busy says "Reading spend N of M…"
(the per-account busy label stays the short "reading…" so three buttons still fit
the actions cell at 1280–1366px), and the country counter reads "N/M spend read" both from the
server and after a read in the page. "Connect" was rejected on purpose: the
button reads spend for the dates on screen, and "connected" already means the
account/profile link. Labels only; behaviour unchanged.

## Workbook numbers per campaign and ad in the account tree — ACL-1256

The account row shows spend, leads, FTD, CPL, CPA and CR from the stats workbook;
the tree under it (campaigns and ads read from Ads Manager) showed only Meta's own
spend and results, pushed to the right edge. Now every tree row stands under the
account columns: Spend (workbook), Account spend (Meta), Leads, FTD, CPL, CPA, CR
(workbook), Status (Meta delivery), Setup (Meta result). Column widths are taken
from the account row itself and follow window resizes.

Numbers come from `/account-attribution`: the same selection as the account row
(country, workbook alias, period) and the same formula, so campaigns add up to the
account row (verified on Doc20, Doc19 and Doc21). A campaign is the workbook's
`Adset Name`, an ad its `Ad Title`; names are matched with whitespace collapsed.
Rows the workbook does not know, or that share a name with another row (e.g.
several ads with one name in a Doc19 campaign), show a dash with the reason in
the tooltip, never guessed numbers.

## Country-attributed read part without noise labels — ACL-1264

The same cell's `in <country>: …` part (spend the accounts attribute to the
country, acl-357) now follows ACL-1258: amounts via `fmt_summary_amount`, no
`currency unknown`, no account names. Sign-only amounts from different accounts
are still not added up; the reason stays in the value's tooltip.

## Country read summary without noise labels — ACL-1258

The collapsed country row showed read totals without a currency code as
`currency unknown 1,044.82 (FacebookDoc20 - Mios Darwiti)`. On the owner's request
the country summary now shows only the amount: a 3-letter code or Meta's own sign
stays (`KWD 7.00`, `$1,044.82`), otherwise a bare number. Such amounts are still
never added together and stay separate values. The per-account cell keeps its
`currency unknown` label. Server (`fmt_summary_amount`) and page script
(`amapSummaryMoney`) format the same way.

## Ads under campaigns of an id-less account table — ACL-1255

Some ad accounts (FacebookDoc19, Saudi Arabia, since 2026-09-18) show the new Ads
Manager table without object ids. The account tree read their campaigns by name but
no ads, because ads are requested per campaign id. The campaign list page embeds
Meta's own `campaign_structure_tree` data with each campaign's id and ad names; the
tree now takes the id from there (only for names that map to exactly one id), reads
that campaign's ads through the usual filtered list, and keeps them only when every
ad name belongs to that campaign's structure. The recovered id is stored as
`data_id`, never as `id`, so turn-off stays unavailable for such rows. Accounts with
ids are unchanged. Read only: no clicks, no editor.

Live check on 2026-09-22 found the ads list of such an account still unread: the
campaign ids were recovered, but ad names in that table are plain text (not links)
and the page carries no `level=` hints. The id-less parser now takes a row's name
from the tooltip inside a row with a switch when no name links exist, and, without
`level=` hints, takes the level from the name column heading (`Campaign`, `Ad set`,
`Ad`). An unknown heading still refuses the list.

## Audience field confirmation — ACL-1250 / PR #247

Merged into master on 2026-09-21 after independent review, full Windows tests and
installer verification. A nearby budget number or saved-audience summary can no
longer falsely confirm age/country settings, and opening a collapsed field cannot
select an adjacent field's remove control. Nested text/pencil layouts stay supported.
The real Windows/Multilogin pilot remains explicitly deferred in ACL-1215. This
record describes source behavior, not a released package or completed adoption.

## Adoption checkpoint — 2026-09-21

ACL-1248 changes repository organization and delivery governance; it adds no product
feature. Current product code includes the reviewed installer (#245), update helper
(#246) and shared library (#244). Their remaining setup/pilot requirements are listed
in PROJECT.md and the original Issues. Code integration is not a released installer.

Existing detailed release history remains in [RELEASES.md](../RELEASES.md); original
specifications remain at their established locations. Preserve their dates and scope.
Add future implemented changes and reasons here, linking the actual ticket and PR.
