# Team7 GitHub task tracker

The tracker at https://tracker.team7.cloud uses the existing ACL task-wall UI
with **GitHub Issues as the task source**. It is a read-only view: edit tasks,
assignees and labels through the GitHub link in each expanded card.

`status:*`, `area:*` and `track:*` labels control the corresponding fields.
All open and closed issues are fetched across all pages; pull requests are
excluded. New issues without board metadata still appear under Other tasks.
Duplicate ACL IDs receive an issue-number suffix so both remain visible.
Closed issues without an explicit shipped/merged label display Closed, not
an inferred release status. Multiple valid status labels display as Idea with
an explanation in the card. Statuses are not inferred from unrelated commits.

`docs/task-board-snapshot.md` supplies only existing area/capability names and
descriptions. Task titles, bodies, subtasks, assignees and status come from
GitHub. The original Obsidian CLI remains available and is not synchronized
back from this site.

## Deployment on Pixie

- Application directory: `/opt/acl-tracker`.
- Container: `acl-tracker-tracker-1`, managed by Docker Compose.
- Nginx receives only rendered public files and a password hash, all read-only.
- No host port is published; the existing Traefik reverse proxy routes the
  hostname and manages its Let's Encrypt certificate and HTTP→HTTPS redirect.
- The root page and `status.json` both require HTTP Basic authentication.
  `/healthz` contains only `ok` and is intentionally available for health checks.
- A host systemd timer runs the one-shot sync every 60 seconds. The browser
  polls sync status every 60 seconds, so a GitHub change can take approximately
  two minutes to reach an already-open tab.
- The sync command uses the **existing authenticated GitHub CLI inside the
  OpenClaw container**. No GitHub token is copied into the tracker. The timer
  therefore depends on that container, its CLI path and its existing login.
- A failed request preserves the last rendered board and marks sync delayed.
  The page also marks stale data if no successful sync has happened for three
  minutes. Sync errors never publish command stderr or credentials.

The deployed Nginx image is pinned by digest in `/opt/acl-tracker/.env`:
`nginx@sha256:dc5069ad14f19660b141b21236140b91656bf89bbc3e2417c70ae650cd66104c`.
The floating image in the Compose default is for an initial setup only; resolve
and pin an official image before subsequent deployments.

## Update

Copy `scripts/acl_board.py`, `scripts/acl_tracker.py`, and
`docs/task-board-snapshot.md` into matching paths under `/opt/acl-tracker`.
Copy this directory's `compose.yaml` and `nginx.conf` to that directory's root.
Keep `.local/users`, `.env` and `public/` on the server across updates.

```sh
systemctl start acl-tracker-sync.service
cd /opt/acl-tracker
docker compose config --quiet
docker compose up -d
docker compose exec tracker nginx -t
systemctl list-timers acl-tracker-sync.timer
journalctl -u acl-tracker-sync.service -n 10 --no-pager
```

For a clean setup, create the directories, supply a password file in
`.local/users`, install the two systemd unit files into `/etc/systemd/system/`,
run `systemctl daemon-reload`, perform a successful initial sync, start the
container, then enable `acl-tracker-sync.timer`. The service file describes
the existing Pixie GitHub CLI location; adapt it for another host. Do not store
the plain password or a GitHub credential in source control.

Stopping this tracker uses only its own resources:

```sh
systemctl disable --now acl-tracker-sync.timer
cd /opt/acl-tracker
docker compose down
```

The shared Traefik service and other applications do not require a restart.

## Validation of the first deployment

Based on master `1ed565660a2da90ae7ef8603f74c8c71f61467e1`.
Implementation and self-review: Codex. No independent review claimed.

- `python -m pytest --noconftest tests/test_acl_tracker.py tests/test_acl_board.py tests/test_board_snapshot.py -q`: **76 passed**.
  These are isolated stdlib board tests. `--noconftest` excludes the unrelated
  dashboard fixture bootstrap that imports pandas. No Windows or full ACL
  application suite was run for this standalone Linux deployment.
- Live GitHub sync: **65 issues**, with matching issue links in the deployed HTML.
- Valid HTTPS certificate; HTTP redirects to HTTPS with 308.
- Root page and status endpoint: 401 without credentials; 401 with an incorrect
  password; authenticated requests succeed.
- Browser check of the exact deployed HTML: filter shows 18 review tasks;
  expanding a card exposes subtasks and the correct GitHub issue link.
- Docker health check passes; recurring systemd sync succeeds.

This deployment does not merge code into master or promote a desktop ACL release.

## Empty updates and UI refresh

On 9 September 2026, the server logged nine successful zero-issue syncs among
otherwise populated updates. The previous code accepted that response and
published an empty board. It now rejects a zero-issue result when a populated
board already exists, retaining both the HTML and the last successful count,
timestamp and digest. The status endpoint reports `ok: false` and the safe
error code `empty_response`; a later valid sync recovers automatically. Invalid
page envelopes are rejected too. The upstream reason for the intermittent
empty responses has not been established; the safeguard does not claim to
detect every possible partially incomplete, nonempty response.

New, empty repositories can still initialize. Deliberately clearing an existing
board requires a one-off sync with `--allow-empty`; do not add that flag to the
recurring timer. Other safe error codes distinguish fetch, render and write
failures without publishing command stderr or secrets.

`All` clears the status filter and opens all areas. It does not change GitHub
labels. Manual expansion and selected status survive reloads. The refresh digest
covers the rendered board and browser code, excluding the sync timestamp, so
UI deployments also reach existing tabs without causing a reload every minute.

Validation for the reliability change: 81 board/tracker tests passed. A separate
Chrome test with 64 current issues verified All, status filters, repeated
reloads, card/area persistence, rejected empty updates and the existing green
palette. Backend regression tests reproduced the old empty-board overwrite
before the fix and verify preservation followed by automatic recovery.
