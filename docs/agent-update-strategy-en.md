---
kind: spec
name: agent-update-strategy-en
agent: architect
task: "[[global-rollout-rpa-agent]]"
created: 2026-07-22
status: draft
version: 1
tags:
  - architecture
  - deployment
  - rpa
  - multilogin
  - windows
related:
  - hub-agent-architecture-en.md
  - agent-update-strategy.md
---

# Keeping the RPA agent's code up to date on every employee's machine

English version of `agent-update-strategy.md`.

## Context

The company wants to move Agent 10 (RPA campaign publishing via
Multilogin + Playwright) from "only works on my machine, while I have
Multilogin open" to a model where **every employee** launches
campaigns from their own computer through their own, already logged-in
Multilogin profiles, while the dashboard and shared database stay
centralized (on a domain).

This is a separate concern from `service-architecture.md` (that spec
is about per-customer Docker instances of the analytics tool, one
SQLite per customer). This document is about an **internal**
multi-user workspace for a single company, with shared data and
distributed RPA execution.

Model: **hub + local agents**.

- **Hub** (on the domain) — the shared dashboard + shared DB + job
  queue API ("run campaign X for account Y").
- **Local agent** — a process on each employee's machine: polls the
  hub, runs Playwright/Multilogin locally with that person's own
  accounts, sends results and screenshots back to the hub.

The key open question with this split: **how does the agent's code
stay current across N machines**, given that development (as in this
session) moves fast and breaks often on live runs. Below are two
options that were considered, with recommendations and rollout
checklists.

## Recommendation

1. Start with **Option 1 (git-based)** for the pilot — nothing needs
   to be built from scratch, it reuses the GitHub repo that already
   exists.
2. Before investing in **Option 4 (Intune)** — ask IT exactly one
   question (see below, "Precondition"). If the answer is "yes, Intune
   is already used for internal LOB apps" — move there after the
   pilot. If there's no such infrastructure, Option 4 is shelved and
   Option 1 remains the primary mechanism longer.
3. Don't run both mechanisms at once on the same set of machines — at
   any given stage there should be one source of truth for updates, or
   they will fight each other.

---

## Option 1: Git-based self-update on launch

### How it works

Each employee has a local clone of the repo on their machine. A
launcher script (`start_agent.ps1`) runs before the agent starts:

1. `cd` into the repo folder.
2. `git fetch origin`.
3. Compare local `HEAD` to `origin/<branch>`.
4. If behind — `git pull --ff-only` (fast-forward only, no merge
   conflicts on the user's machine).
5. If `requirements.txt` changed (compare its hash to the saved one)
   — `pip install -r requirements.txt --quiet`.
6. If the list of Playwright browsers changed — `playwright install`.
7. Launch the agent process.

```powershell
# scripts/start_agent.ps1 (sketch)
Set-Location "$PSScriptRoot\.."
git fetch origin agent-stable
$behind = git rev-list HEAD..origin/agent-stable --count
if ([int]$behind -gt 0) {
    $dirty = git status --porcelain
    if ($dirty) {
        Write-Warning "Local changes present -- update skipped, ping the team channel."
    } else {
        git pull --ff-only origin agent-stable
    }
}
python -m src.fb_ads_rpa.agent
```

### Requirements

- Git installed on every machine, with read access to the private
  GitHub repo — preferably via a **personal** fine-grained access
  token or deploy key per person, rather than one shared token for
  everyone, so access can be revoked individually.
- The branch machines pull from must be a separate, stable branch
  (e.g. `agent-stable`), **not** the feature branches used for
  day-to-day RPA development (as in this session, where things break
  constantly on live runs). This requires a promotion process: tested
  → merged into `agent-stable` → automatically rolled out to every
  machine.
- Personal secrets and config (`.env`, a specific person's Multilogin
  `folder_id`, their slice of `ad_account_map.csv`) must live
  **outside** the tracked git tree — e.g. in
  `%APPDATA%\fb_agents\local.env`, loaded on top of the repo's
  defaults. Otherwise `git pull`/`reset` will eventually wipe them out.

### Safety rules

- Never run `git reset --hard` if there are uncommitted local changes
  the agent doesn't own itself — check `git status --porcelain`
  first; if dirty, stop and report rather than silently discarding.
- If `git pull` fails (no network, expired token, conflict) — the
  agent should still start with whatever code is already there
  locally, and report the failure to the hub. Never block an
  employee's ability to work just because the update check failed.

### Rollout rings

1. **Ring 0** — your machine, feature branches (as today).
2. **Ring 1** — the `agent-stable` branch, a pilot group of 2-3
   people.
3. **Ring 2** — the whole company, once Ring 1 has run cleanly for a
   few days.

### Pros / cons

**Pros:** cheapest to build, reuses the existing GitHub setup,
transparent (`git log` shows exactly what's deployed).

**Cons:** requires git + working GitHub auth on every machine; no code
review/signing gate before rollout — anyone with push access to
`agent-stable` can instantly ship to every machine at once (mitigate
with branch protection + required review).

### Minimal implementation checklist

- [ ] Create a protected `agent-stable` branch (branch protection:
      only via an approved PR).
- [ ] Write `scripts/start_agent.ps1` with the logic above.
- [ ] Move all secrets/personal config out of the tracked tree into
      `%APPDATA%\fb_agents\` — document the format in a separate
      `docs/agent-local-config.md`.
- [ ] Add a `.gitignore` entry so nobody accidentally commits their
      personal config.
- [ ] Document the manual recovery path for when it breaks: "delete
      the repo folder and re-clone" as the last resort.
- [ ] Issue personal PATs/deploy keys instead of one shared token.

---

## Option 4: Rollout via Intune (device management)

### Precondition: confirm with IT first

Before investing here, get an answer to one question:

> Is the company's device fleet actually enrolled in Intune (or
> another MDM) for deploying/updating internal applications, or is
> the AzureAD account on the machine only about SSO/M365, with no
> app-deployment policies configured?

Being AzureAD-joined does **not** by itself mean Intune-managed —
those are different things. If the company has no process today for
deploying internal line-of-business apps via Intune, this option
should be shelved until that infrastructure (and a dedicated IT
resource) exists.

### How it would work (typical Intune LOB app flow)

1. Package the agent as a Win32 app (`.intunewin` format) using the
   Microsoft Win32 Content Prep Tool — bundling the Python runtime (or
   relying on a company-standard image), the repo code, the Playwright
   browsers, and a silent installer script.
2. Upload it to Intune as a Line-of-Business app, assign it to an
   Azure AD security group (e.g. "Campaign Managers").
3. Configure Intune detection rules (e.g. checking a version via a
   registry key or a version file) so Intune knows when a redeploy is
   needed.
4. New versions: build a new `.intunewin` package, bump the version,
   upload — Intune silently pushes it to every assigned device on
   their next check-in (typically within a few hours), with no user
   action required.
5. Multilogin and any other prerequisites (browsers, drivers) would
   need to be deployed as separate/dependent Intune apps unless
   they're already part of the standard company laptop image.

### Pros / cons

**Pros:** zero-touch for end users, leverages IT's existing device
trust and security posture, updates reach people even if they'd never
remember to run a manual pull, works for people who've never run a
command line in their life.

**Cons:** noticeable lead time (a packaging pipeline + an IT process,
likely needs the IT team involved, not just you), a slower iteration
loop during active development (device check-in intervals, not
instant like a git pull), needs IT buy-in/bandwidth that may not be
readily available right now.

### Minimal implementation checklist (once IT confirms Intune is viable)

- [ ] Get IT sign-off + a named contact who owns Intune app
      deployments.
- [ ] Write a silent-install PowerShell script (Python dependencies +
      Playwright browsers + copying agent code to a fixed path).
- [ ] Package with the Win32 Content Prep Tool → `.intunewin`.
- [ ] Define a detection rule (e.g. checking the contents of a
      `VERSION` file).
- [ ] Assign to a pilot AAD group first, validate, then expand to
      everyone.
- [ ] Document the release process: who bumps the version, who
      uploads to Intune, how rollback works.

---

## Bottom line for this document

This plan assumes Option 1 ships first regardless — it doesn't depend
on any external team. Whether Option 4 is pursued at all depends
entirely on IT's answer to the Intune precondition question above —
recommend raising that as a single message to IT before scoping any
Intune work further.

## Related open questions (out of scope for this document)

These came up during discussion but remain unresolved — noted here so
they aren't lost during further planning:

- Hub hosting (dashboard + shared DB): Azure vs another cloud provider
  vs existing company infrastructure — not decided.
- Dashboard login model: Google/Microsoft SSO vs a custom
  login/password system — not decided.
- Rollout scale: a small pilot team vs the whole company at once —
  not decided.
- Mapping "user → their accounts/Multilogin profiles" (needs a table
  in the shared DB that doesn't exist today — `ad_account_map.csv`
  isn't tied to users at all).

#architecture #spec #deployment #rpa #multilogin #windows #git #intune
