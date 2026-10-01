# System history

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
