# Windows installation, stable releases and recovery

Profile: package. Target identities are the isolated Windows stage installation and
the stable ACL installer/update channel. Neither Git storage nor a source merge
changes employee installations. Read ../repository/ADOPTION.md during the transition.

## Existing maintained mechanism

Use ../../packaging/windows/README.md, ../SETUP-NEW-PC.md and ../RELEASES.md.
Packaged ACL.exe uses src/web/release_updates.py and packaging/windows/updater.py:
validate manifest/version/size/hash, serialize an update, avoid active campaign
jobs, run the installer, verify the new process/version, and recover the previous
version/shortcuts when the known failure permits it. Keep machine data/configuration.
Source checkouts retain their own gated update path; do not replace it with packaged
logic. The adoption changes their stable branch reference to main, with the old-master
bridge described in the cutover runbook.

## Stable release

After verified stage promotion, obtain successful full Windows CI on the exact main
release commit. Record scope and limitations in the release ledger. Create the
existing annotated acl-release-* tag with its genuine SHA/success/run URL under the
project's release authorization. scripts/acl_publish_release.py verifies tag, HEAD,
current main and the successful exact-main CI through GitHub. If that main run reused
an earlier full run of the same tree (ACL-1565), it also requires a successful push or
workflow_dispatch run with the full test matrix on a commit whose git tree equals the release tree. The full
matrix is the ACL-1582 layout: every `linux tests (py, shard)` job, `windows tests (3.13)` and
`ACL verification`; runs of the older all-Windows layout are refused. Never repoint tags,
replace releases, accept stale evidence or treat a merge waiver as release authority.
The release workflow builds/tests the package, stages its assets as a draft and
publishes only verified complete artifacts. Preserve the existing checks.

The configured release repository and tokens are operational setup, not committed
values. At migration intake no Actions variables/secrets are configured in ACL and
no release has been published there. Confirm the actual intended release repository
and permitted access through the existing owner setup; never invent credentials or
bake a source-repository write token into an employee installer. Do not expose token
values in logs. Product update and shared-library access are distinct permissions.

Complete the agreed Windows installation/update/restart/recovery check before recording
verified package delivery. Keep a real report for the exact release. `record-delivery`
records evidence; it does not install, publish or infer success. Employees can update
later; completion does not assert every workstation is current. Required unfinished
business acceptance (including company sign-in) stays unfinished.

## Data and staging

Use STAGING.md. Preserve %LOCALAPPDATA%/ACL, local workbooks, creatives and machine
credentials. A code rollback must not overwrite newer business data. Keep recovery
material and the prior usable installation until verified replacement. Unknown
partial actions need inspection before retry. Do not use production accounts or
shared browser sessions for a test. The standing manual-pilot deferral is a recorded
limitation, never a claim that installation passed or a release approval.
