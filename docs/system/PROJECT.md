# Adopy Campaign Launcher — current system

ACL is a Windows desktop-installed application with a local web dashboard for
campaign managers. It reads performance data, prepares campaigns and drives
Multilogin's browser through controlled automation. It is not the shared Task
Tracker server, even though a legacy Tracker implementation remains in this repo.

## Components and current behavior

- `src/web`, `serve.py`: local dashboard, account/report views, campaign builder,
  machine configuration and update endpoints.
- `src/analysis_agent`: statistics workbook reading, reports and reconciliation.
- `src/campaign_bridge`, `src/upload_agent`: campaign input/output preparation.
- `src/fb_ads_rpa`, `src/meta_campaign_agent`: Ads Manager interactions, verified
  profile identity, draft preparation, queues and browser lifecycle.
- `src/creative_agent`, `src/knowledge_agent`: creative analysis and domain references.
- `src/analysis_agent/team_library.py` and related library modules: shared targets/ad texts in
  the separately provisioned private team library; PR #244 replaced Google Sheets
  for that library. Existing optional Google integrations are not all removed.
- `packaging/windows`: packaged ACL.exe, first-run setup, per-user installer and
  detached update helper. `src/web/release_updates.py` offers verified release updates.
- `config/paths.py`: machine-specific paths. User configuration, account maps and
  credentials belong outside Git, normally under `%LOCALAPPDATA%/ACL`.

Read [distribution boundaries](../DISTRIBUTION.md), [product/design context](../../PRODUCT.md),
[design guidance](../../DESIGN.md) and existing specs for subsystem details.
`content_machine`, `jira_agent` and legacy `fb_publish_agent` are not automatically
part of the ACL runtime; preserve the tested distribution boundary.

## Delivery state at adoption

PR #245 (installer) and #246 (updater) are merged at the recorded source baseline.
The source install path still exists alongside the packaged path. There is no
published GitHub Release or verified real Windows upgrade/restart/rollback at this
checkpoint. Hosted Windows checks are unavailable; local test reports do not prove
live operation. Company Google sign-in remains ACL-1242 and part of ACL-1239's scope;
shared-library provisioning remains ACL-1241. Do not mark either parent complete
because its code exists. Media reuse remains governed by its explicit feature flags
and outstanding acceptance, not by adoption.

The owner selected a test version on stage and stable installed releases from main.
Those branches/configuration are prepared by ACL-1248 and not activated at this
checkpoint. This change organizes engineering; it does not change campaign behavior,
credentials, library setup or employee installations.

## Maintaining this document

Describe implemented behavior and limitations here when changing the system. Preserve
plans under original specs, label them as plans, and record what/why and ticket/PR
links in CHANGELOG.md. Process-only changes belong in ../repository/CHANGELOG.md.
