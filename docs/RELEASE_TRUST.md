# Trusted updates (ACL-1357, audit S06)

Whoever can publish an ACL update can run code on every buyer machine next to
live ad accounts. This page says what the client code now checks, how signing
is switched on in stages, and which GitHub settings only the owner can change.
Code alone does not close S06: without the settings below, anyone with write
access can still push to `main` and tag it.

## What the code checks

### Git channel (`Install ACL.bat`, `start_dashboard.bat`, the dashboard update button)

This is the channel buyers use today: `Install ACL.bat` clones the repository
and `start_dashboard.bat` fast-forwards to `origin/main` when an update is
requested. The gate is `scripts/acl_update_gate.py` (and its PowerShell twin
`Get-AclRelease` in `Install ACL.bat`).

The candidate must be the exact target of an annotated `acl-release-*` tag
whose annotation links to a run in `MojoTheApe/adopy-campaing-launcher`, and
the GitHub API must confirm that run:

| Field | Required value |
|---|---|
| `repository.full_name` | `MojoTheApe/adopy-campaing-launcher` |
| `path` | `.github/workflows/ci.yml` |
| `head_sha` | the tagged commit |
| `head_branch` | `main` |
| `event` | `push` or `workflow_dispatch` |
| `status` / `conclusion` | `completed` / `success` |

These are the same conditions `scripts/acl_publish_release.py` checks before a
packaged release.

The gate has three outcomes, staged like the signatures below:

| Outcome | When | warn mode (now) | enforce mode |
|---|---|---|---|
| verified | the API confirms the run | update | update |
| rejected | the run is readable and a field above differs; or the run is 404 while the same token can read the repository (a made-up link); or the tag links no run of this repository | **refuse** | **refuse** |
| unverified | no token; 401/403; network error; 404 while the repository itself is not visible to the token | update only if the tag text records `success` (the old check); log + Sentry `kind=update_trust`, `gate=unverified`; the dashboard note says why | **refuse** |

An attacker controls neither the buyer's token nor the network, so cannot
push a real check into the unverified path. Enforcement only tightens:
`GATE_ENFORCE = True` in `scripts/acl_update_gate.py` together with
`$AclGateEnforce = $true` in `Install ACL.bat` (a test keeps the two equal), or
`ACL_UPDATE_GATE=enforce` on the machine (the environment or the machine
`.env`; `Install ACL.bat` reads the environment). Switch it on when Sentry
shows no `gate=unverified` events for a release.

The API call needs a token. The gate uses, in order:
`ACL_UPDATE_GATE_TOKEN` (process environment, then the machine `.env` at
`%LOCALAPPDATA%\ACL\.env`), the Git credential this clone already uses for
github.com, then `GH_TOKEN` / `GITHUB_TOKEN`. The credential is read with every
prompt disabled — terminal, Git Credential Manager window and askpass
programs (`GIT_ASKPASS`, `SSH_ASKPASS`, `core.askPass`) — so a missing one
never opens a window. The token needs read access to Actions of the
repository (`repo` scope for OAuth/GCM credentials, `Actions: read` for
fine-grained tokens).

Machines that end up **unverified**: SSH remotes (`git@github.com:`, no
HTTPS credential), fine-grained tokens without `Actions: read` (403/404), and
GCM holding several GitHub accounts (it would ask which one; the gate forbids
the question).

#### Unsticking a machine

In warn mode such a machine still updates; its dashboard note says "This
machine could not verify its CI result (…)". To make it verify (and before
switching to enforce), give it a token:

1. Create a fine-grained token for `MojoTheApe/adopy-campaing-launcher` with
   `Actions: read` and `Metadata: read`, with an expiry.
2. Add `ACL_UPDATE_GATE_TOKEN=<token>` to `%LOCALAPPDATA%\ACL\.env` on that
   machine (never to the checkout's `.env`, never to git).
3. Or fix the Git credential instead: switch the remote to HTTPS
   (`git remote set-url origin https://github.com/MojoTheApe/adopy-campaing-launcher.git`)
   and sign in once with `git fetch`; with several GCM accounts, run
   `git config --global credential.https://github.com.username <account>`.
4. Check: `.venv\Scripts\python.exe scripts\acl_update_gate.py` prints
   `update gate OK: … verified` (or "BLOCKED" with the reason). If a
   refused update stuck a machine in enforce mode, remove
   `ACL_UPDATE_GATE=enforce` from its environment only after the reason is
   understood.

Checked on 2026-09-29 against the live API: the current release
`acl-release-2026-09-27` (run 36301767639) passes; older tags whose CI ran on a
feature branch (e.g. `acl-release-2026-09-03e`) would not, which only matters
if `main` were rolled back to them.

### Packaged channel (`ACL.exe`, `src/web/release_updates.py`)

`release.json` names the installer, its size and SHA-256, and the client
refuses an installer whose bytes differ. New: the release also carries
`release.json.sig`, an Ed25519 signature over the exact bytes of
`release.json`, and the client verifies it with a public key pinned in
`src/web/release_signing.py` (`PINNED_PUBLIC_KEYS`) before it believes
anything in the manifest. A signed `release.json` of another tag is refused
too.

| Signature state | warn mode (now) | enforce mode |
|---|---|---|
| valid, pinned key | install | install |
| no `release.json.sig` | install, log + Sentry warning | **refuse** |
| build has no pinned key yet | install, log + Sentry warning | **refuse** |
| signed by an unknown key | **refuse** | **refuse** |
| bad signature (manifest or hash changed) | **refuse** | **refuse** |
| unreadable signature file | **refuse** | **refuse** |
| installer bytes differ from the signed hash | **refuse** | **refuse** |

The warning goes to the log and, where Sentry is configured, as a
`kind=update_trust` event with the signature state; the update check response
also carries `signature: {status, mode}`.

Enforcement can only be switched on, never off: `ENFORCE = True` in
`src/web/release_signing.py`, or per build with the repository variable
`ACL_ENFORCE_RELEASE_SIGNATURES=true` (written into the bundled channel file),
or per machine with `ACL_RELEASE_SIGNATURES=enforce`.

Limits: builds installed before this change have no verifier at all, and
builds without a pinned key only warn. Protection starts with the first build
that pins the key and becomes complete once enforcement is on. Even with a
pinned key, two gaps remain: anyone who can write releases can still hold
buyers on an older signed release that is newer than theirs (no expiry), and
until enforcement is on a release whose `.sig` was deleted is still accepted
with a warning. Windows
Authenticode signing of the installer is still missing (separate task: needs a
code-signing certificate or Azure Trusted Signing).

## Switching signing on (owner, in this order)

1. **Create the key** on a trusted machine, outside any checkout:
   `python scripts/acl_sign_release.py keygen --out <path outside the repo>`.
   It prints the line for `PINNED_PUBLIC_KEYS`. Keep an offline backup of the
   private key (password manager / hardware token).
2. **Store the private key** as the repository secret
   `ACL_RELEASE_SIGNING_KEY` (PEM text), then delete the file. On GitHub Free
   a private repository has no environment secrets or protection rules, so
   the key stays a repository secret until the plan upgrade; then move it to
   the `release` environment (see the checklist).
3. **Pin the public key**: a reviewed PR that sets `PINNED_PUBLIC_KEYS` in
   `src/web/release_signing.py`. Never commit the private key. Done in
   PR #370: key id `428a74e4f8e6e616` (owner key, Issue #357). The public key
   is not a secret — it can only check signatures — and
   `scripts/acl_scan_secrets.py` does not flag it.
4. **Release** normally. The release workflow signs `release.json`
   (`Sign release.json with the publisher key`); it fails if the key is set
   but not the pinned one. Check the release has `release.json.sig`:
   `python scripts/acl_sign_release.py verify --manifest release.json`.
5. **Require signatures in CI**: once a key is pinned, `acl_sign_release.py
   sign` already fails when the secret is missing, so a deleted secret cannot
   silently publish unsigned releases. The repository variable
   `ACL_REQUIRE_RELEASE_SIGNATURE=true` additionally makes the requirement
   explicit in the workflow.
6. **Enforce on clients** once managers run a build with the pinned key and no
   `update_trust` warnings arrive for a release: set
   `ACL_ENFORCE_RELEASE_SIGNATURES=true` (per build) or `ENFORCE = True` (in
   code, via PR).

Key rotation: ship a build that pins both keys, switch the secret, remove the
old key in a later build.

## GitHub settings checklist (owner)

The audit saw `master.protected=false` and no environments. Check each item
with the commands at the end.

**Plan** — do this first
- [ ] `MojoTheApe` is a personal account on GitHub Free with a private
  repository: `GET /repos/.../rulesets` answers 403 "Upgrade to GitHub Pro".
  Branch protection, rulesets and protected environments for a *private*
  repository need a paid plan (Pro for a personal account, Team for an
  organization). Upgrade the plan; do not make the source public to get them.
  Until then every item below except "Actions" and "People and tokens" is
  unavailable, and anyone with push access can still put a commit on `main`
  whose CI passes.

**Release branches** (`main`, `master` while it is live, `stage`) — one ruleset
- [ ] Require a pull request before merging; at least 1 approval by someone
  other than the author; dismiss stale approvals on new commits; require
  approval of the most recent push.
- [ ] Require review from code owners for `.github/workflows/**`,
  `scripts/acl_update_gate.py`, `scripts/acl_publish_release.py`,
  `scripts/acl_sign_release.py`, `src/web/release_*.py`, `Install ACL.bat`,
  `start_dashboard.bat`, `requirements.txt` (add a `CODEOWNERS` file).
- [ ] Require status checks to pass (the jobs of `ci.yml` and
  `delivery-check.yml`), branch up to date before merging.
- [ ] Block force pushes and deletions.
- [ ] Bypass list empty; if an emergency bypass is needed, only the owner
  account, and only "for pull requests".

**Release tags** (`acl-release-*`) — tag ruleset
- [ ] Only release managers may create them; block update and deletion (a
  published tag never moves).
- [ ] Enable immutable releases for the repository that hosts the releases,
  if the plan offers it, so assets cannot be replaced after publication.

**Release environment** `release` (after the plan upgrade)
- [ ] Create it, then add `environment: release` to the `publish` job of
  `.github/workflows/release.yml` (left out on purpose: on Free it would give
  no protection, and a release run must not depend on an unverified setting).
- [ ] Required reviewers (not the person who pushed the tag; enable "prevent
  self-review").
- [ ] Deployment tags: only `acl-release-*`.
- [ ] Move `ACL_RELEASE_SIGNING_KEY`, `ACL_RELEASES_TOKEN` and
  `ACL_UPDATE_TOKEN` to environment secrets of `release`.

**Actions**
- [ ] Default `GITHUB_TOKEN` permission: read-only (workflows ask for more
  explicitly).
- [ ] Do not allow Actions to create or approve pull requests.
- [ ] Allow only GitHub-owned and verified actions (or pin by SHA).
- [ ] Require approval for workflows from outside collaborators and forks.

**People and tokens**
- [ ] Two-factor authentication required for everyone with write access;
  publishers use passkeys or hardware keys.
- [ ] Review collaborators with write/admin/maintain; remove the ones who no
  longer publish.
- [ ] `ACL_UPDATE_TOKEN` (shipped inside installers, treat as public): a
  fine-grained token limited to a releases-only repository, `Contents: read`,
  with an expiry and a rotation date.

**Verify**
```
gh api repos/MojoTheApe/adopy-campaing-launcher/branches/main/protection
gh api repos/MojoTheApe/adopy-campaing-launcher/rulesets
gh api repos/MojoTheApe/adopy-campaing-launcher/environments
gh api repos/MojoTheApe/adopy-campaing-launcher/actions/permissions/workflow
```
Acceptance (S06): a writer without the required reviews and checks cannot
produce a trusted update, and the client refuses a replaced artifact or
manifest and an unknown signer.
