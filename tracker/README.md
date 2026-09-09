# Portable GitHub task wall

A read-only English UI for GitHub Issues. Python 3.11+ standard library; authenticated `gh` CLI for sync and agent commands. No frontend build, database, AI API, or hosting vendor is required.

1. Configure `tracker/config.json`: project name, repository, base branch and module catalog. Keep the six workflow IDs stable. The final label can be `In main`, `In master`, or `Merged`.
2. Read `AGENT_WORKFLOW.md`. Add links to it in the repository's `AGENTS.md` and `CLAUDE.md`.
3. Run `python -m tracker init` to create labels and the configured empty release milestone. It never assigns existing tasks to a release.
4. Run `python -m tracker sync --output /path/to/private/public` and serve that directory behind authentication and HTTPS. Both `data.json` and HTML contain private Issue content: protect the entire directory, except a separate content-free health check.
5. Repeat sync every minute using the host scheduler. Use one sync process per output directory. A failed API request, malformed pagination or unexpected empty response preserves the last successful board and exposes a safe error in `status.json`. Only an explicit `--allow-empty` clears an existing board.

The browser polls status and replaces data only when digests match. It preserves filters, expanded tasks/groups, focus and scroll. UI code changes trigger a reload with the same saved context. Unavailable or stale data cannot show a confirmed green release-ready state.

Credentials remain in `gh` on the sync host, never in web files. `--gh-command-json` accepts a command array for an existing container or another host-specific adapter. Use a read-capable account for sync and suitable Issue/Contents permissions for agent commands. Do not embed tokens in command arguments or repository config.

## Reuse in another repository

```sh
python -m tracker export-template --output /tmp/new-project-tracker --repository OWNER/NEW_REPO --name "New Project" --base main
```

The export contains the portable package, generic config, shared instructions and GitHub templates. It contains no task snapshot, old ACL mappings, credentials or deployment paths. Copy these into the new repository, merge instructions with its existing rules, edit the module catalog, initialize labels and configure its hosting/scheduler. Export does not contact GitHub.

## Migration and rollback

`migration-plan --mapping FILE --output PLAN` reads fresh GitHub Issues and produces a reviewable before/after label plan. `migrate --plan PLAN` validates the whole batch and applies individual label additions/removals; `--rollback` reverses it. Save the plan privately. A retry accepts only states within the planned labels and rejects unrelated concurrent changes. Issue numbers, titles, bodies, checklists, states and milestones are retained. Create a new plan if the scope has changed.

ACL's one-time mapping is in `docs/tracker-v2-mapping.json` outside this portable package. The old Obsidian board renderer remains a separate historical export tool; its snapshot is not an input to this UI.

## Serving behind an existing reverse proxy

Mount only generated public files read-only in a web server. Set `Cache-Control: private, no-store`; protect `/`, `/data.json` and `/status.json` with the same authentication. The UI's inline JS/CSS and Google Fonts require the corresponding existing CSP allowances; external fonts are optional and system font fallbacks work offline. Do not expose directory listings or the sync host's credentials/config directories. The sync command is also available through `scripts/acl_tracker.py` in ACL for existing systemd units.
