# Setup: a manual problem report becomes a GitHub ticket (ACL-1230)

What you get: when a campaign manager presses **Report a problem** in the dashboard, the
report arrives in Sentry as its own issue, and Sentry creates a GitHub issue in
`MojoTheApe/adopy-campaing-launcher` with the label `support-ticket`. No GitHub token
exists on any buyer machine and we run no service of our own: the only credential is the
one Sentry's GitHub integration holds, configured once by the owner.

```
dashboard "Report a problem" ──► Sentry issue (tag kind=report) ──► alert rule ──► GitHub issue
        (scrubbed text + machine facts)                         (Sentry's GitHub integration)
```

## What the application already sends (no code change needed)

`acl_sentry.report_problem()` marks every manual report with the tag `kind` = `report` and
a fingerprint `acl-report` / `manual` / `<report id>` (ACL-1273), so each report is its own
Sentry issue; the same id is the tag `report_id`.

**Prerequisite: ACL-1273 must be merged and released first.** Before it the fingerprint was
constant, all reports from all machines collapsed into one Sentry issue and only the first
would ever create a ticket. Text, version, machine label, period on screen and last
failed steps are scrubbed of ids, tokens and profile numbers before they leave the
machine. Automatic step failures carry other `kind` values and never match the rule below.

## Owner checklist (live setup, ~5 min, done in Sentry — not by an agent)

Never paste a token into a chat, an Issue or a repository file.

1. Sentry → **Settings → Integrations → GitHub** → install for `MojoTheApe`, allow only the
   repository `adopy-campaing-launcher`. (Needs the organisation owner on the GitHub side.)
2. In the repository create the label **`support-ticket`** (Issues → Labels → New label).
3. Sentry → project `python` → **Alerts → Create Alert → Issues**:
   - Name: `ACL - manual problem report`
   - Environment: all
   - **When**: *A new issue is created*
   - **If**: *The event's tags match* `kind` **equals** `report`
   - **Then**: *Create a new GitHub issue* → repository `MojoTheApe/adopy-campaing-launcher`,
     labels `support-ticket`. Sentry fills the title (`Report: …`) and the body (a link to the
     Sentry issue plus event details) itself.
   - Action interval: the shortest Sentry offers (5 minutes); it does not matter for a
     new-issue trigger.
4. Keep the repository **private**: the ticket title carries the first 400 characters of the
   buyer's text and the body may carry the machine and manager labels. The scrubber removes
   ids, tokens and profile numbers, not arbitrary personal data a buyer types.
5. Verification (one synthetic report from a test machine, not a live account): press
   **Report a problem**, write `synthetic ticket check`, send. Expect within a minute a
   GitHub issue labelled `support-ticket` whose body links the Sentry issue.

## Honest limits

- The dashboard says "Queued. Delivery is not confirmed" and shows the first 8 characters
  of the Sentry event id. That id is not proof that the team saw the report; the GitHub
  ticket is. If no ticket appears after a few minutes, check the alert rule and the
  integration — the report is most likely still in Sentry (a report the scrubber rejects is dropped on the
  machine although the dashboard already showed a reference).
- One report = one Sentry issue = one GitHub ticket (with ACL-1273). The rule fires on *new*
  issues only, so SDK retries of one event do not create duplicates, and two identical texts
  are two reports. Automatic failures do not match the tag.
- Read status is not built (ACL-1231): the team closes or comments on the GitHub ticket.
