# Sentry triage: reacting to a new failure kind from any install — ACL-1325

Every ACL install reports campaign stops, blocking failures and job exceptions to
Sentry (org `adopy-7t`, region de; ACL-1318). This page describes how the owner's
Claude session notices a **new kind** of failure from any buyer's machine, works out
what changed on the Meta page, and prepares a fix for independent review — without
ever deploying anything.

Owner decisions (2026-09-26, issue comments):

- blocking-failure and campaign-stop events carry a **scrubbed copy of the failing page
  section** (DOM of the card the step works on) — yes;
- **screenshots — no** (they show account, campaign and page names);
- the agent reacts **immediately** when a new failure kind appears, not on a daily
  schedule;
- the digest goes to the **owner's Claude chat** for now.

## 1. What an event carries

| Where in the event | Content |
| --- | --- |
| title / fingerprint | `step: reason`; fingerprint `acl-step / <step without ad number> / <reason>` |
| tags | `step`, `reason`, `failure_kind` (`blocking`, `campaign_stop`, `job_exception`), `account` (short label only), `machine`, `release`, `page_snapshot=section` when a snapshot is attached |
| contexts | `run` (run id, campaign, job, attempt), `failure.why`, scrubbed `diag`, `page_snapshot` (parts, `captured_at_step`, `same_step`, bytes, truncated) |
| attachment | `section_<step>.html`, at most 200 KB |

The attachment is built in `src/fb_ads_rpa/page_capture.py`:

1. At the failing step (`_shot` → `page_capture.capture` → `remember_section`) the page
   returns the card the step works on — Audience, Conversion, Placements, Destination,
   Ad creative + Ad setup, … (`section_titles_for(step)`), plus an open dialog (e.g.
   "Reach your preferred audiences"). Unknown step → the editor section around the
   focus, then the editor, capped. Outside the editor (the campaigns list: `create`,
   `objective_*`, `stall_*`) → the open dialog if there is one, otherwise the list
   region (`[role=main]` / the table container: toolbar + table), capped in the page
   at 400 000 characters. Never the whole page (`document.body`), never a screenshot.
   Owner clarification 2026-09-26: only what is inside the Multilogin browser is
   captured (work accounts in a closed environment), so other campaign names in the
   list may stay; ids, URLs, contacts, tokens and uuids are always removed (step 2).
   The copy is built by a bounded walk in the page that stops at the cap, so a huge
   list does not become a long task. Scripts, styles, svg, images and all attributes
   except `role`, `aria-*`, `data-surface`, `placeholder`, `type`, `checked`,
   `disabled`, `id` are dropped in the page already. The page is asked with a 5 s bound
   (`wait_for_function`, as `meta_dom._eval_bounded`): the call comes from stall
   evidence on a tab that may hang, and a tab that does not answer gives no section
   instead of a hung publish thread. The raw copy is kept in process memory only
   (16 entries), never on disk.
2. When the event is sent (`sentry_signal`), `scrub_section_html` rebuilds the markup
   from a whitelist and replaces: the run's campaign name, account label (whole and
   word by word), ad set and ad names → `<campaign>`, `<account>`, `<name>`; URLs →
   `<url>`; `act=` / `*_id=` values → `<hidden>`; e-mails, phone numbers, digit runs
   of 6+, tokens and uuids (`run_telemetry.scrub`); Meta render ids `js_*` → `syn_N`
   (references stay linked). Each text is cut at 300 characters, the whole file at
   200 KB with tags closed and `truncated: yes` in its header.
   A section is attached only to an event of the same run and the same campaign it
   was taken in; without a run id only the exact step of the same named campaign
   counts (no "last section" fallback). Profile stops (the profile did not attach or
   re-attach) carry no section, and each campaign starts with the previous one's run
   forgotten.
3. `acl_sentry.report_step_failure(..., attachment=...)` adds it with
   `scope.add_attachment` and a second size check (`before_send` does not see
   attachments). A failed attachment never loses the event.

Machine setting (machine `.env`, `%LOCALAPPDATA%\ACL\.env`):

```
ACL_SENTRY_PAGE_SNAPSHOT=0   # stop attaching page sections (default: on)
```

Page names (the Facebook Page in "Identity") are not known to the run and are not
replaced; the Identity card is only taken for identity steps. Ad copy typed by the
robot is creative text, not personal data, and is kept (it is needed to diagnose
copy failures), cut at 300 characters per text.

## 2. The triage script

`scripts/sentry_triage.py` — read-only:

```
python scripts/sentry_triage.py                  # since the saved cursor (first run: 24 h)
python scripts/sentry_triage.py --since 2h       # explicit window
python scripts/sentry_triage.py --format md      # markdown only
python scripts/sentry_triage.py --apply          # also record the cursor (its only write)
python scripts/sentry_triage.py --attachments-dir "%LOCALAPPDATA%\ACL\sentry_triage"
                                                 # also save the section files there
```

It lists new (`is:unresolved firstSeen:>cursor`) and regressed
(`is:regressed lastSeen:>cursor`) issues, reads each issue's latest event (tags,
contexts) and downloads its `section_*.html` attachment into memory, groups issues
by `step:reason` (`ad2_copy` and `ad3_copy` are one group), marks each group with a
known tracker task from `config/sentry_triage_map.json` or as **NEW KIND**, and
prints JSON (for the
agent) and markdown (for the owner) — including the headings and control labels of
the attached section, so a new Meta layout is visible without opening the file.

It never writes to Sentry or GitHub. Locally it writes nothing by default: the
section outline is built from the downloaded bytes in memory. `--apply` writes only the
state file (the cursor). Section files are saved only into an explicitly named
`--attachments-dir` (as `sentry_issue_<id>\section_<step>.html`); there the latest 50
issue folders are kept and older `sentry_issue_*` folders are removed (nothing else in
that folder is touched). Without `SENTRY_AUTH_TOKEN`, without network or without access
it prints `"skipped": "<why>"` and exits 0.

State: `%LOCALAPPDATA%\ACL\sentry_triage_state.json` (`cursor`, last seen `lastSeen`
per issue). Overrides: `SENTRY_ORG`, `SENTRY_BASE_URL` (default
`https://de.sentry.io`), `SENTRY_PROJECT`, `--state`, `--map`, `--attachments-dir`.

When a task is filed for a new kind, add a row to `config/sentry_triage_map.json`
(`step`, `reason` — fnmatch patterns — `task`, `note`); remove it when the task is
closed so a recurrence shows up as new again.

## 3. The routine (what the triage session does)

Trigger (ACL-1330, owner decision 2026-09-30: no polling, no empty runs):

- **Sentry alert** "ACL - new failure kind" (rule 1303794: a new issue, or an issue
  that changed from resolved to unresolved) → a signed webhook to the relay
  `deploy/sentry-relay/worker.js` → `POST /fire` of the **cloud Claude Code routine**
  `ACL Sentry triage` with a short scrubbed text of the alert. One run per Sentry issue
  per 24 h; the owner's laptop can be closed. The routine follows
  [ROUTINE_SENTRY_PROMPT.md](ROUTINE_SENTRY_PROMPT.md) and ends by asking the owner in
  the Claude app "Выпустить для <buyer>?". Setup: [SENTRY_ROUTINE_SETUP.md](SENTRY_ROUTINE_SETUP.md).
- The same rule still **e-mails** the owner (fallback when no phone push arrives).
- The laptop's Claude desktop scheduled task `acl-sentry-triage` is **paused**: manual
  "Run now" only, never re-enabled on a schedule. It is the fallback when the cloud path
  is down.

### 3.1 The relay and why it is a Cloudflare Worker

The routine's API trigger needs `Authorization: Bearer <routine token>` plus the header
`anthropic-beta: experimental-cc-routine-2026-04-01` and a JSON body `{"text": "..."}`
(https://code.claude.com/docs/en/routines.md). Sentry cannot send that: an Internal
Integration's webhook (alert action) and the legacy WebHooks plugin post to a URL with
Sentry's own headers only (`Sentry-Hook-Resource`, `Sentry-Hook-Timestamp`,
`Sentry-Hook-Signature`), no custom header, and the routine token cannot go into the URL.
So a relay is needed. Candidates compared:

| Host | Verdict |
| --- | --- |
| **Cloudflare Worker (free plan)** | **Chosen.** No card, set up in the dashboard by pasting one file (no CLI, no build), secrets stored encrypted, KV for dedupe, `waitUntil` lets it answer Sentry within its 1-second limit and call `/fire` afterwards. |
| GitHub Actions `repository_dispatch` | Same problem: GitHub's dispatch API also needs an `Authorization` header, which Sentry cannot send. A workflow would also start ~20–60 s later and need the routine token as a repository secret. |
| Google Apps Script web app | Anonymous web apps are closed by the Adopy Google Workspace policy. |
| Vercel / Netlify functions | Work, but need a project deploy from a repository and more clicks; nothing gained over a Worker. |

What the relay does (`deploy/sentry-relay/worker.js`, tests `worker.test.js`):
verifies `Sentry-Hook-Signature` (HMAC-SHA256 of the raw body with the integration's
client secret; constant-time compare) and, because Sentry signs only the body and not
the `Sentry-Hook-Timestamp` header, takes the event time from the signed body (Sentry's
`received`, else `datetime` / `timestamp`) and refuses an event older than one hour,
more than 15 minutes in the future, or without a time (the header, when present, must
also be within 15 minutes); a replay inside the hour hits the per-issue dedupe below;
accepts only `event_alert` for the rule `ACL - new failure kind`, project `python` and
`failure_kind` blocking / campaign_stop / job_exception; fires once per Sentry issue
within `DEDUPE_HOURS` (24) — a "pending" mark is written before the call so a second
alert a second later does not start a parallel run, and removed if `/fire` fails so the
next alert can retry; at most `MAX_FIRES_PER_HOUR` (20) fires per hour, below the
routine's 30/hour and the account's 100/hour limits, and `MAX_FIRES_PER_DAY` (30) a day
(the DSN ships with every install, so a flood of made-up issues is capped); without the KV binding or the
routine settings it fires nothing. The text it sends carries only the Sentry issue id,
title, culprit, the tags `failure_kind`, `step`, `reason`, `release`, `server_name`,
`machine`, `account`, `environment`, the rule name and the issue link; every value is
scrubbed (URLs with or without a scheme, e-mails, `act=` / `*_id=` values, tokens
including Meta `EAA…` access tokens, uuids, long digit runs, phone
numbers; `<`/`>` removed so the text cannot close the routine's payload block) and cut
to 200 characters, the whole text to 2 000. The event's user, contexts, request and the
page section never leave Sentry through the relay — the routine reads them with the
read-only Sentry tools. KV is eventually consistent across Cloudflare locations; two
deliveries of the same alert through different locations within a few seconds could in
theory both fire — Sentry's 5-minute action interval per issue makes that unlikely.

Self-test: `python scripts/sentry_selftest_event.py` sends one synthetic failure
(`step=relay_selftest`, a new `reason` each time, so a new issue and a real alert). The
routine recognises the step and only answers that the chain works;
`config/sentry_triage_map.json` lists the step as known (ACL-1330) so the laptop digest
does not report it as a new kind.

For each group marked NEW KIND (or a known kind that regressed after its task was
closed), the session (cloud routine: see the prompt for the cloud-specific commands):

1. Reads the evidence: the digest (with the outline of the section snapshot), the
   event tags (release, machine, account label) — or the same via the Sentry MCP tools
   (`search_issues`, `get_sentry_resource`). When the full section is needed (step 3),
   re-runs the script for that window with
   `--attachments-dir "%LOCALAPPDATA%\ACL\sentry_triage"` to save the files.
2. Checks the tracker for an existing task (GitHub Issues in
   `MojoTheApe/adopy-campaing-launcher`, `python scripts/task_tracker.py guide`).
   Files a new task (English, per repo rules) with: what broke, since which release,
   on how many machines/accounts, the Sentry links, the snapshot outline, the
   suspected cause — or adds a comment with the new evidence to the existing one.
   Adds the `step`/`reason` row to `config/sentry_triage_map.json` in the fix branch.
3. If the cause is clear from the snapshot (a renamed label, a new card layout, a new
   dialog): creates a worktree **from the current work base** (never switches branches
   in a shared checkout), cuts a fixture from the snapshot following
   `tests/fixtures/ads_manager/README.md` (the snapshot is already scrubbed — still
   re-check names and run `python scripts/acl_scan_secrets.py`), writes a test that
   fails on the current code, fixes, runs the targeted suite, commits, and asks for
   an **independent review** (a separate reviewer agent/session, per
   `docs/BRANCHING.md`). It does not push to release branches, does not merge, does
   not publish a release, does not restart a live dashboard, and never touches a live
   ad account.
4. Posts a short digest to the owner's chat: what broke, on whose machine
   (machine label + account label), since which release, the task link, and what is
   being done (fix branch + review pending / needs a live capture / buyer action).
5. Runs `python scripts/sentry_triage.py --apply` to record the cursor only after the
   digest is posted.

Buyer-side stops (logged out, proxy, profile open by hand, account restricted) are
mapped to tasks already and only appear in the digest as counts — they need the buyer,
not code.

## 4. Setup the owner does once

Nothing below is created automatically.

### 4.1 Sentry read token

1. Sentry → avatar (bottom left) → **User Settings** → **Personal Tokens**
   (`https://de.sentry.io/settings/account/api/auth-tokens/`) → **Create New Token**.
2. Scopes: `org:read`, `project:read`, `event:read` (nothing with `write` or `admin`).
3. Put it into the machine `.env` (`%LOCALAPPDATA%\ACL\.env`), never into the repo:
   `SENTRY_AUTH_TOKEN=sntryu_...`
4. Check: `python scripts/sentry_triage.py --since 7d --format md` prints a digest,
   not `skipped`.

Downloading attachments needs the role set in **Settings → (organization) →
Security & Privacy → Attachments Access**; the owner's role must be at or above it.

### 4.2 Sentry alert rule "a new failure kind appeared"

1. Sentry → **Alerts** → **Create Alert** → **Issues** ("Create an alert based on
   issues") → **Set Conditions**.
2. Environment: `production`; project: the ACL project.
3. **WHEN** (any of): "A new issue is created"; "The issue changes state from resolved
   to unresolved".
4. **IF** (optional, all of): "The event's tags match `failure_kind` `is in`
   `blocking,campaign_stop,job_exception`" — manual reports (`kind=report`) then do
   not page.
5. **THEN**: "Send a notification to Member → <owner>" (e-mail / Sentry mobile push).
   Optional: Settings → Integrations → **WebHooks** (legacy plugin) with a URL the
   owner controls, then add "Send a notification via WebHooks".
6. Action interval: **5 minutes**. Name: `ACL - new failure kind`. **Save Rule**.

(Sentry's newer "Monitors & Alerts" UI has the same triggers under **Alerts → Create
Alert → Issue alert**; the wording of the conditions is the same.)

### 4.3 Cloud routine and relay (ACL-1330)

Step by step, about 10 minutes: [SENTRY_ROUTINE_SETUP.md](SENTRY_ROUTINE_SETUP.md)
(routine with an API trigger, the Cloudflare relay, the Sentry internal integration as a
second action of rule 1303794, a self-test, the phone; how to rotate the tokens and how
to switch it off).

### 4.4 Laptop fallback: desktop scheduled task (paused)

Since 2026-09-30 the task `acl-sentry-triage` below is **paused** and is run by hand
only ("Run now"), e.g. when the cloud path is down. Do not put it back on a schedule.
The text below is how it was created.


Open the Claude desktop app in the `C:\fb_agents` project (the chat where the digest
should arrive — completion notices of a scheduled task go to the session that created
it), and say:

> Create a scheduled task `acl-sentry-triage`, cron `*/10 8-21 * * 1-6` (local time),
> with this prompt: "In C:\fb_agents run
> `python scripts/sentry_triage.py --format both`. If it says skipped or there are no
> groups with new_kind=true and no regressed issues, stop without output. Otherwise
> follow docs/system/SENTRY_TRIAGE.md section 3 for each new group: file or update the
> tracker task with the evidence, prepare a fix branch with a failing-then-passing
> test in a new worktree when the cause is clear, ask for independent review, never
> push, merge, release, restart the dashboard or touch a live account. End with a
> digest: what broke, machine and account label, release, task link, what is being
> done. Then run `python scripts/sentry_triage.py --apply`."

Notes: scheduled tasks run only while the Claude desktop app is open (a missed run
runs on next launch); ten minutes is the reaction time between a new issue on any
machine and the digest. For a cloud routine instead (`/schedule` in Claude Code), the
routine has no access to the machine `.env` or local snapshots — give it the Sentry
MCP tools and the repository instead of the script.

## 5. Tests

- `deploy/sentry-relay/worker.test.js` (`node --test deploy/sentry-relay/worker.test.js`,
  Node 20+, no packages; run from pytest by `tests/test_sentry_relay.py`) — signature
  and replay checks, filtering by resource / rule / project / failure kind, one fire per
  issue (also while the first fire is in flight), the hourly cap, retry after a failed
  fire, nothing fires without KV or routine settings, scrubbing of titles, culprits and
  tags, the `/fire` request shape. `tests/test_sentry_relay.py` also checks the
  self-test sender and its map row.

- `tests/test_sentry_page_snapshot.py` — scrubbing on every recorded fixture seeded
  with names/ids/urls/e-mails/phones/tokens/scripts, the 200 KB cap with closed tags,
  card selection in offline Chromium (the "Suggest an audience" Audience card whose
  heading also holds buttons), attachment present on blocking and campaign-stop
  events, absent on clean runs, for another run and with `ACL_SENTRY_PAGE_SNAPSHOT=0`,
  and delivered to a fake Sentry transport by the real SDK.
- `tests/test_sentry_triage.py` — recorded Sentry API responses: grouping, known-task
  mapping, attachment download and outline, cursor only with `--apply`, read-only
  requests, pagination, graceful skip without token / access / network.
