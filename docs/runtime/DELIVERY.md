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

After the admitted configured route (temporary direct main; Stage when re-enabled),
obtain successful full Windows CI on the exact main
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

The verified release repository is `MojoTheApe/acl-releases`; the source Actions
variable `ACL_RELEASES_REPO` selects it. On 2026-10-15, read-only setup inspection
confirmed the names `ACL_RELEASES_TOKEN`, `ACL_RELEASE_SIGNING_KEY` and
`ACL_UPDATE_TOKEN` exist; no secret values were read. Existing stable publications
include signed `acl-release-2026-10-08b`. Keep the read-only updater token scoped to
that release channel; source/library access remains separate. Actual permission,
trust and download success are proven by a real run, not a secret-name inventory.

Standing owner approval of ACL integration/main merge includes package delivery
unless explicitly source-only/skip-release. The agent records `batch begin --deliver` with
`.github/workflows/complete-delivery.yml` and job `Verify published Windows package`.
After actual main admission and successful exact-main CI, this durable workflow
uses the existing reusable publisher and then downloads previous/current published
installers through the configured updater trust path. It verifies actual isolated
Windows update/restart/recovery and preserved data before a native-log-validated
Tracker receipt records the frozen tasks’ delivery. Use [the repository runbook](../repository/POST_MAIN_DELIVERY.md)
for exact scope, pending/failure recovery and the separately recorded source/live
activation boundary. `workflow_dispatch` with the same successful CI run resumes
an existing authorized request; it grants no new scope. Missing/failed checks never
produce Delivered. Preserve immutable tags/releases and inspect incomplete drafts.

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
