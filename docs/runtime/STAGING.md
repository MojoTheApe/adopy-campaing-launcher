# Isolated Windows staging

The permanent stage branch is a source candidate, not proof of a working environment.
ACL needs an isolated Windows test installation for that exact reviewed source tree.
The target is not provisioned or verified by this migration's draft descriptor.

1. Obtain an actual stage merge after independent review and required checks. Build
   from that exact stage content; keep the commit/tree and artifact identity.
2. Run the existing Windows Python 3.11/3.13 suite and packaged build/install/repair/
   uninstall workflows against it. Stage/main pushes always use full checks; do not
   treat a metadata PR's skipped Windows jobs as an installation test.
3. Use a separate Windows test account/machine or isolated runner. Give the application
   its own ACL_CONFIG_DIR, workbooks, Creatives/workspace and dashboard port. Verify
   the paths before starting. Never reuse an employee installation, production OAuth,
   update channel, Multilogin profile or live ad account. Disable external telemetry
   and production auto-update in that target. A second checkout alone is insufficient.
4. Complete the agreed functional pilot and update/restart/compatible recovery tests
   using authorized isolated resources. Unit fixtures do not replace a required real
   Windows scenario. Record unperformed portions; do not set stage verified until the
   agreed acceptance is satisfied.
5. Record `stage` and `verify-stage` evidence with the pinned Tracker for the actual
   target/tree and report. Promote the same tasks/content into main only after success.
   Changed main/content invalidates the evidence and requires reconciliation/recheck.

CI artifacts from stage are test candidates and are not published as the latest stable
release. Promotion to main still needs successful exact-main release CI and the stable
release procedure; stage evidence cannot masquerade as installed-package delivery.
