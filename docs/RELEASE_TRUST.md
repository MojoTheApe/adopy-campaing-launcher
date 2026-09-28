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
packaged release. The words in the tag no longer matter: a made-up link, a run
of another repository, workflow, branch or commit is refused.

The API call needs a token. The gate uses, in order: `ACL_UPDATE_GATE_TOKEN`,
the Git credential this clone already uses for github.com (read with prompts
disabled), then `GH_TOKEN` / `GITHUB_TOKEN`. No token, no network or any API
error means **not verified**: the update is skipped and the buyer keeps the
running version. The token needs read access to Actions of the repository
(`repo` scope for OAuth/GCM credentials, `Actions: read` for fine-grained).

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
that pins the key and becomes complete once enforcement is on. Windows
Authenticode signing of the installer is still missing (separate task: needs a
code-signing certificate or Azure Trusted Signing).

## Switching signing on (owner, in this order)

1. **Create the key** on a trusted machine, outside any checkout:
   `python scripts/acl_sign_release.py keygen --out <path outside the repo>`.
   It prints the line for `PINNED_PUBLIC_KEYS`. Keep an offline backup of the
   private key (password manager / hardware token).
2. **Store the private key** as the secret `ACL_RELEASE_SIGNING_KEY`
   (PEM text), preferably as a secret of the `release` environment, then delete
   the file.
3. **Pin the public key**: a reviewed PR that sets `PINNED_PUBLIC_KEYS` in
   `src/web/release_signing.py`. Never commit the private key.
4. **Release** normally. The release workflow signs `release.json`
   (`Sign release.json with the publisher key`); it fails if the key is set
   but not the pinned one. Check the release has `release.json.sig`:
   `python scripts/acl_sign_release.py verify --manifest release.json`.
5. **Require signatures in CI**: repository variable
   `ACL_REQUIRE_RELEASE_SIGNATURE=true` — a release without the key fails.
6. **Enforce on clients** once managers run a build with the pinned key and no
   `update_trust` warnings arrive for a release: set
   `ACL_ENFORCE_RELEASE_SIGNATURES=true` (per build) or `ENFORCE = True` (in
   code, via PR).

Key rotation: ship a build that pins both keys, switch the secret, remove the
old key in a later build.

## GitHub settings checklist (owner)

The audit saw `master.protected=false` and no environments. Check each item
with the commands at the end.

**Plan**
- [ ] Branch protection and rulesets for a *private* repository need a paid
  plan (GitHub Team for an organization, Pro for a personal account). If the
  settings below are unavailable, upgrade the plan. Do not make the source
  public to get them.

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

**Release environment** `release` (used by `release.yml`)
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
