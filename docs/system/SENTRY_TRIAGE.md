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
   focus, then the editor, capped. Scripts, styles, svg, images and all attributes
   except `role`, `aria-*`, `data-surface`, `placeholder`, `type`, `checked`,
   `disabled`, `id` are dropped in the page already. The raw copy is kept in process
   memory only (16 entries), never on disk.
2. When the event is sent (`sentry_signal`), `scrub_section_html` rebuilds the markup
   from a whitelist and replaces: the run's campaign name, account label (whole and
   word by word), ad set and ad names → `<campaign>`, `<account>`, `<name>`; URLs →
   `<url>`; `act=` / `*_id=` values → `<hidden>`; e-mails, phone numbers, digit runs
   of 6+, tokens and uuids (`run_telemetry.scrub`); Meta render ids `js_*` → `syn_N`
   (references stay linked). Each text is cut at 300 characters, the whole file at
   200 KB with tags closed and `truncated: yes` in its header.
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
python scripts/sentry_triage.py --apply          # also record the cursor (the only write)
```

It lists new (`is:unresolved firstSeen:>cursor`) and regressed
(`is:regressed lastSeen:>cursor`) issues, reads each issue's latest event (tags,
contexts) and downloads its `section_*.html` attachment into
`%LOCALAPPDATA%\ACL\sentry_triage\<issue>\`, groups issues by `step:reason`
(`ad2_copy` and `ad3_copy` are one group), marks each group with a known tracker task
from `config/sentry_triage_map.json` or as **NEW KIND**, and prints JSON (for the
agent) and markdown (for the owner) — including the headings and control labels of
the attached section, so a new Meta layout is visible without opening the file.

It never writes to Sentry or GitHub. Without `SENTRY_AUTH_TOKEN`, without network or
without access it prints `"skipped": "<why>"` and exits 0.

State: `%LOCALAPPDATA%\ACL\sentry_triage_state.json` (`cursor`, last seen `lastSeen`
per issue). Overrides: `SENTRY_ORG`, `SENTRY_BASE_URL` (default
`https://de.sentry.io`), `SENTRY_PROJECT`, `--state`, `--map`, `--attachments-dir`.

When a task is filed for a new kind, add a row to `config/sentry_triage_map.json`
(`step`, `reason` — fnmatch patterns — `task`, `note`); remove it when the task is
closed so a recurrence shows up as new again.

## 3. The routine (what the owner's Claude session does)

Trigger — whichever comes first:

- **Sentry alert** "a new issue is created" / "issue regressed" → e-mail (and
  optionally a webhook) to the owner, who opens the Claude chat; or
- **Claude desktop scheduled task** every 10 minutes during working hours, which runs
  the script and stays silent when nothing is new.

For each group marked NEW KIND (or a known kind that regressed after its task was
closed), the session:

1. Reads the evidence: the digest, the downloaded section snapshot, the event tags
   (release, machine, account label) — or the same via the Sentry MCP tools
   (`search_issues`, `get_sentry_resource`).
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

### 4.3 Claude routine (desktop scheduled task)

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

- `tests/test_sentry_page_snapshot.py` — scrubbing on every recorded fixture seeded
  with names/ids/urls/e-mails/phones/tokens/scripts, the 200 KB cap with closed tags,
  card selection in offline Chromium (the "Suggest an audience" Audience card whose
  heading also holds buttons), attachment present on blocking and campaign-stop
  events, absent on clean runs, for another run and with `ACL_SENTRY_PAGE_SNAPSHOT=0`,
  and delivered to a fake Sentry transport by the real SDK.
- `tests/test_sentry_triage.py` — recorded Sentry API responses: grouping, known-task
  mapping, attachment download and outline, cursor only with `--apply`, read-only
  requests, pagination, graceful skip without token / access / network.
