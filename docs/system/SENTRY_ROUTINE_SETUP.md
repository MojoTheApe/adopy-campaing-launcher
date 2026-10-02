# Setup: a new Sentry failure starts the cloud triage routine (ACL-1330)

What you get: when Sentry reports a **new failure kind or a regression** from any buyer's
machine, a Claude Code routine starts in the cloud within a minute (your laptop can be
closed), investigates, files or updates the tracker task, prepares a fix PR into `stage`
with an independent review, and asks you in the Claude app: "Выпустить для <buyer>?".
Nothing runs on a schedule and nothing runs when Sentry is quiet.

```
Sentry rule "ACL - new failure kind"  ──webhook──►  relay (Cloudflare Worker)  ──/fire──►  cloud routine
      (also still e-mails you)          signed        checks signature, filters,      Bearer    one session per
                                                      one run per issue per 24 h      token     Sentry issue
```

Why a relay: Sentry's webhooks cannot send an `Authorization` header, and the routine's
trigger URL needs one. The relay is one file, `deploy/sentry-relay/worker.js`, on the
Cloudflare Workers free plan (no card). Why this host and not another — see
[SENTRY_TRIAGE.md](SENTRY_TRIAGE.md) section 3.1.

About 10 minutes. You create every account, token and setting yourself; no agent enters
credentials. Keep two browser tabs open: claude.ai and Cloudflare. Never paste a token
into a chat, an Issue or a file in the repository.

## A. The routine (claude.ai, ~3 min)

1. Open **claude.ai/code/routines** → **New routine**. Name: `ACL Sentry triage`.
2. **Instructions**: paste the prompt from
   [ROUTINE_SENTRY_PROMPT.md](ROUTINE_SENTRY_PROMPT.md) — everything between
   `----- prompt start -----` and `----- prompt end -----`. In the model selector pick
   the strongest model you have.
3. **Repositories**: add `MojoTheApe/adopy-campaing-launcher` **and**
   `MojoTheApe/task-tracker` (the tracker engine the task client loads). If a repository
   is not in the list, install/allow the Claude GitHub App for it when prompted.
4. **Environment**: keep **Default** (network **Trusted**). No environment variables are
   needed. *(Optional, later: to let the routine run `scripts/sentry_triage.py` too, make
   a separate environment with network **Custom** = `de.sentry.io` + "Also include default
   list", and the variables `SENTRY_AUTH_TOKEN` (a read-only token: `org:read`,
   `project:read`, `event:read`), `SENTRY_ORG=adopy-7t`, `SENTRY_PROJECT=python`,
   `SENTRY_BASE_URL=https://de.sentry.io`. Environment variables are visible to anyone who
   uses that environment — keep it personal.)*
5. **Select a trigger** → **API**.
6. **Connectors**: keep **Sentry** (read-only use is enforced by the prompt), remove all
   the others. If Sentry is not connected yet: claude.ai/customize/connectors → Sentry →
   connect with your Sentry login, then come back.
7. **Create**. Open the routine → menu next to its name → **Edit** → in **Select a
   trigger** open the **API** trigger → copy the **URL** (ends with `/fire`) → **Generate
   token** → copy the token. It is shown **once**; keep this tab open until step 11.

## B. The relay (Cloudflare, ~4 min)

8. Sign up / log in at **dash.cloudflare.com** (free plan).
9. **Workers & Pages** → **Create** → **Start with Hello World** → name
   `acl-sentry-relay` → **Deploy** → **Edit code**. Replace the whole editor content with
   the content of `deploy/sentry-relay/worker.js` from the repository (GitHub → the file →
   **Raw** → select all → copy) → **Deploy**. Note the worker address
   `https://acl-sentry-relay.<your-subdomain>.workers.dev`. Opening it in a browser shows
   `sentry-relay ok`.
10. **Storage & Databases** → **Workers KV** → **Create** a namespace
    `acl-sentry-relay-fired`. Then the worker → **Settings** → **Bindings** → **Add** →
    **KV namespace** → variable name `FIRED`, namespace `acl-sentry-relay-fired` → save.
    (Without it the relay refuses to fire — that is how it guarantees one run per issue.)
11. The worker → **Settings** → **Variables and Secrets** → **Add**:
    - `ROUTINE_FIRE_URL` — type **Text** — the URL from step 7;
    - `ROUTINE_TOKEN` — type **Secret** — the token from step 7;
    - `SENTRY_PROJECT` — type **Text** — `python`.
    **Deploy**. Optional variables (defaults are fine): `ALERT_RULE`
    (`ACL - new failure kind`), `FAILURE_KINDS` (`blocking,campaign_stop,job_exception`),
    `DEDUPE_HOURS` (`24`), `MAX_FIRES_PER_HOUR` (`20`), `MAX_FIRES_PER_DAY` (`30`).

## C. Sentry (~2 min)

12. Sentry (adopy-7t.sentry.io) → **Settings** → **Developer Settings** → **Custom
    Integrations** → **Create New Integration** → **Internal Integration** → **Next**.
    Name `ACL routine relay`; **Webhook URL** = the worker address from step 9; turn on
    **Alert Rule Action**; permissions: leave all at **No Access**; no webhook
    subscriptions. **Save Changes**. Copy the **Client Secret** shown on the integration
    page.
13. Cloudflare → the worker → **Variables and Secrets** → **Add** `SENTRY_CLIENT_SECRET`,
    type **Secret**, value = the client secret → **Deploy**.
14. Sentry → **Alerts** → rule **ACL - new failure kind** (id 1303794) → **Edit Rule** →
    under **THEN** → **Add action** → "Send a notification via **ACL routine relay**".
    **Keep** the existing e-mail action: it is the fallback. **Save Rule**.

## D. Test (~1 min + a minute of waiting)

15. On the laptop, in the ACL folder (once this change is in your checkout):
    `.venv\Scripts\python.exe scripts\sentry_selftest_event.py`. It sends one synthetic
    failure (`relay_selftest`, a new reason each time, so Sentry opens a new issue).
    No browser, no campaigns.
16. Within about a minute:
    - Sentry e-mails you about the new issue (as before);
    - Cloudflare → the worker → **Logs** (turn on **Workers Logs** if asked) shows
      `fired for issue <id>: https://claude.ai/code/session_…`;
    - claude.ai/code/routines → `ACL Sentry triage` shows a new run, and the Claude app
      → **Code** shows the same session. Its answer should be "Проверка прошла: Sentry →
      релей → облачная routine работает …".
    If the log says `ignored: …`, the reason is in the text (other rule, other project,
    failure kind not watched). `bad signature` → step 13 value is wrong. `stale or undated
    event` → the event inside the alert is more than an hour old (a replayed or late
    alert) or carries no time; send a fresh self-test. `routine is not configured` → step
    11. `fire failed … HTTP 401` → the token in step 11 is wrong or was regenerated.

## E. The phone

17. Install the Claude app (iOS / Android), sign in with the same account, open **Code**,
    and allow notifications when the phone asks. Routine runs appear in **Code**; you can
    answer "да"/"нет" there.
18. Push for cloud routine runs: the documentation (2026-09-30) describes phone pushes
    for Remote Control sessions, not for routine runs, so check it on the test in step 15.
    If no push arrives, the fallback is the Sentry e-mail that every new failure kind
    already sends you (step 14 keeps it): open the Claude app → **Code** → the newest
    `ACL Sentry triage` run. Write down in Issue #330 whether the push arrived.

## Switching it off, rotating tokens

- **Pause everything quickly**: Sentry → the rule → **Edit Rule** → remove the "ACL
  routine relay" action (the e-mail stays). Or claude.ai/code/routines → the routine →
  the on/off switch at the top.
- **Stop the trigger for good**: routine → **Edit** → API trigger → **Revoke**; and/or
  Cloudflare → the worker → **Settings** → **Delete**.
- **Rotate the routine token**: routine → **Edit** → API trigger → **Regenerate** →
  Cloudflare → `ROUTINE_TOKEN` → **Edit** → paste → **Deploy**. The old token stops
  working immediately.
- **Rotate the Sentry secret**: Sentry → the integration page → rotate the client secret
  (or delete and recreate the integration, then redo step 14) → Cloudflare →
  `SENTRY_CLIENT_SECRET` → **Deploy**.
- **Update the relay code** after a change in `deploy/sentry-relay/worker.js`: Cloudflare
  → the worker → **Edit code** → replace → **Deploy**. Variables and the KV binding stay.

Limits to know: the routine can be fired 30 times per hour, the account 100 per hour;
the relay stops at 20 per hour (`MAX_FIRES_PER_HOUR`) and 30 per day
(`MAX_FIRES_PER_DAY`), and fires once per Sentry issue per 24 hours (`DEDUPE_HOURS`).
Each run uses your Claude subscription like any other session.

A flood: the Sentry DSN ships with every install, so anyone holding it can create new
Sentry issues. If claude.ai/code/routines shows many `ACL Sentry triage` runs you did not
expect, or the worker log shows `daily limit reached`, pause the relay action in the
Sentry rule (first item above), then look at the new issues in Sentry before turning it
back on.

Known limits: the Sentry connector also has write tools (resolve / assign an issue,
Seer); the routine is told to use it read-only, but that is enforced by the prompt, not
by the connector. Sentry does not sign the `Sentry-Hook-Timestamp` header, so the relay
judges the age of an alert by the event time inside the signed body (Sentry's
`received`, else the event `datetime`) and refuses an event older than one hour or
without a time.
The laptop scheduled task `acl-sentry-triage` stays paused (manual "Run now" only).
