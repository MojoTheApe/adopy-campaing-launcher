# Isolated Windows staging

The permanent stage branch is a source candidate, not proof of a working environment.
ACL needs an isolated Windows test installation for that exact reviewed source tree.
The target is not provisioned or verified by this migration's draft descriptor.

1. Obtain an actual stage merge after independent review and required checks. Build
   from that exact stage content; keep the commit/tree and artifact identity.
2. Run the existing Windows Python 3.11/3.13 suite and packaged build/install/repair/
   uninstall workflows against it. Execution-v2/v3 combined batch and actual stage/main contexts require the full
   applicable matrix/installer; successful exact-tree jobs may be reused with the
   verified links from the scope summary. Do not treat a metadata PR's skipped Windows jobs as an installation test.
3. Use a separate Windows test account/machine or isolated runner. Give the application
   its own ACL_CONFIG_DIR, workbooks, Creatives/workspace and dashboard port. Verify
   the paths before starting. Never reuse an employee installation, production OAuth,
   update channel, Multilogin profile or live ad account. Disable external telemetry
   and production auto-update in that target. A second checkout alone is insufficient.
4. Complete the agreed functional pilot and update/restart/compatible recovery tests
   using authorized isolated resources. Unit fixtures do not replace a required real
   Windows scenario. Record unperformed portions; do not set stage verified until the
   agreed acceptance is satisfied.
   The Windows installer workflow runs `packaging/windows/test_update_cycle.py`
   on a fresh GitHub-hosted runner: two actual packages of the candidate source,
   frozen update helper, update/restart, and recovery after a deliberately mismatched
   installer/expected-version pair. It verifies actual health, current.json, all
   application shortcuts and preservation of synthetic machine/workspace/creative
   files. Its `ACL-update-recovery-*` report identifies source/tree and installer
   hashes. This supplies isolated package-mechanism evidence, not a published GitHub
   release/download-channel check or the separately deferred live Multilogin pilot.
5. Record `stage` and `verify-stage` evidence with the pinned Tracker for the actual
   target/tree and report. Promote the same tasks/content into main only after success.
   Changed main/content invalidates the evidence and requires reconciliation/recheck.

CI artifacts from stage are test candidates and are not published as the latest stable
release. Promotion to main still needs successful exact-main release CI and the stable
release procedure; stage evidence cannot masquerade as installed-package delivery.

## Which version is on screen

A dashboard run from source shows a STAGE badge in its header and "+N not on stage":
the number of its commits that are not on the project's `stage` branch (fetched in
the background from the remote whose URL is the project repository). Packaged builds
installed from a release show no badge. The badge tells the owner which version they
are looking at; it is not stage verification and does not replace the isolated
Windows check above. The count leaves out commits that reached stage as the same
patch; a commit brought to stage with conflict resolution or squashed keeps being
counted, so treat the number as a guide, not an exact audit.
