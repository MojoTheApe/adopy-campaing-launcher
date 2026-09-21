# ACL-1248: audited adoption and cutover

Issue: https://github.com/MojoTheApe/adopy-campaing-launcher/issues/248

## Current facts and target

Prepared from `master` at `79caea74d4f88c559d79caf6fc86ef9b7b43f9e8`.
The frozen historical audit retains its original source `6e871edd425afe1c74b955d7ba7b9793e06a7022`.
Master Repo source `e1473c428872757519355af1a7753cf3a0ce0f12` is proposed 1.0.0,
not a published release. Its compatible Tracker is
`5f211d3095a392a16c2c01a65eb02ff8719a7243` (TT-61 / PR #62).
The active old engine is `adb2ce04fff017d1059031f4298effc020d0e2e5`.
No `main` or `stage` exists at intake. Registry identity stays `acl`.
Target: package delivery, stage enabled, existing installer/updater preserved.

The pristine generated files are in `.workflow/template-base.json`. They are the
unmodified template, not a disguised copy of ACL's customized files. ACL identity,
modules, guards, CI and runtime procedures are deliberate local adaptations.
`.workflow/tools/` contains pinned exported helpers; the guarded client is a local
exception. `scripts/` paths remain compatible entry points. No product script or
retained legacy Tracker package is deleted just to fit the template.

## What remains before activation

- The original PR gate is resolved: #180 is closed, with its safe remaining
  optimization retained in ACL-1174; its loading/boxless-row fixes already exist.
  #247 passed independent review and full Windows/installer checks, then merged at
  `79caea74d4f88c559d79caf6fc86ef9b7b43f9e8`. #222 is closed as superseded; its residual
  keyboard discrepancy remains ACL-1252. The author's separate hover/Enter work is
  ACL-1251 and its live checkout was preserved. Do not reopen historical completions
  or import the old combined source. Reports are preserved in the original PRs.
- Refresh this migration's own final-head checks and independent review after the
  base update. Earlier head `34d2096` passed all required Windows and installer
  checks. #247 passed 2346 tests on each Windows version (35595745155) and the full
  installer cycle (35595745062); those runs verify its source, not a changed migration
  head. Installer verification corrections are now in master. No CI waiver applies.
- Complete the historical reconciliation below, registry companion review and real
  authenticated board verification. Do not create a second ACL registration.
- Establish an isolated Windows stage installation, its data/configuration boundaries,
  exact-candidate checks and the retained deferred pilot. There is no stage proof yet.
- Verify private Master Action access and actual GitHub branch controls. Current API
  responses require a supported paid plan for private protection; no change is made.
- Record independent full-candidate review, required successful checks and agreed
  one-time adoption integration route. A draft PR against master uses the base
  branch's legacy metadata policy for linkage only; it does not pass new admission.

`python scripts/acl_adoption.py audit --remote` reads selected PR/CI prerequisites;
it performs no writes and is not a substitute for the full checklist above.

## Historical completion

Preserve labels, Issue states, aliases, release membership and accepted code history.
Enabling the new policy without receipts would change completion counts and dependency
readiness. The snapshot `historical-completion.json` records the original states for
review, not fabricated review/stage/package evidence.

For each old completed item link the original merge proof and distinguish accepted
code completion from actual package delivery. ACL-1240 is accepted as code merged;
no verified release is implied. ACL-1239 retains its company-sign-in requirement in
ACL-1242 and must remain unfinished. ACL-1241's shared-library setup remains separate.
The owner explicitly chose to preserve 74 historically completed tasks and apply the
new rules to new work. [The reviewed audit](HISTORICAL_COMPLETION.md) lists that exact
set. The candidate config contains the compatible Tracker's bounded manifest; the
registry patch contains the identical block. Fresh projection preserves 74 of 157
completed tasks and 7 of 10 release tasks, without a delivery receipt. The UI says
Completed before adoption; old installations have not been verified. Changed scope
or restarted work loses this exception. Unfinished tasks remain unfinished.

Before cutover verify the exact reviewed list against fresh Issues; do not regenerate
or extend it automatically. Finish compatible Tracker publication and compare the
live board after switching. No Issue relabelling or synthetic receipts are needed.

## Coordinated cutover (after blockers are resolved)

Use the bounded [bootstrap procedure](BOOTSTRAP.md). The first reviewed migration
must integrate into **master before renaming it**: the prepared PR checker verifies
that exact original route and the preserved legacy config. Renaming first would
make its real linkage check fail. Do not change source_branch or weaken the checker
to hide that failure.

The bootstrap keeps ordinary project-client writes blocked until genuine activation.
After matching main/stage/config and Tracker publication exist, the procedure uses
the exact clean pinned shared engine for a narrowly scoped ACL-1248 evidence flow.
Its normal review, current-check, stage and unchanged-content admission still apply.
No active flag or historical receipt is used to authorize unverified work. The
activation candidate then follows stage -> main; only after actual evidence is
recorded do normal project-client operations resume. Keep the descriptor draft while
the selected standard remains unpublished, and keep ACL-1248 unfinished until its
remaining actual acceptance is met. Runtime authorization is separate from this plan.

## Existing source installations and rollback

Old clients fetch `origin/master`; deleting that channel would strand them. The new
source client uses `origin/main`. Keep/recreate master at its recorded original SHA
as a temporary **compatibility channel**, with no independent development. Before
any compatibility update verify the exact main release, its successful full CI,
annotated tag and package/pilot evidence. Fast-forward master to that same published
main release only, never to an unreviewed candidate or stage. Use an explicit normal
push of the verified SHA; stop on non-fast-forward or unexpected master divergence.
Old clients can then install the bridge code, whose next fetch uses main. Retire the
compatibility ref only after source installations have migrated. GitHub web redirects
alone do not redirect clients' `git fetch origin master`.

New source installation commands and release verification in this candidate target
main and therefore must not be presented as active before cutover. Packaged employees
continue using immutable release assets, not Git branch names. Keep release tags and
published artifacts immutable. Never roll employee data back merely to revert code.
If cutover fails, stop new admissions/releases, preserve both refs and state evidence,
and restore the recorded registry/policy only through a reviewed compatible recovery;
do not reset other agents' work or claim a failed cutover succeeded.

## Ownership and impact

Master Repo MR-9 selects the compatible audited-history extension from Task Tracker
TT-61 / PR #62. Package/stage and historical IDs retain their existing semantics.
The ACL registry/fallback companion is reviewed in Task Tracker TT-63 / draft
PR #64 (`7a7975011b564a09bdfe325dd8efeb228e454f2e`), matching the patch under
`adoption/`. It remains unmerged and is not read by the live server automatically. Existing source/layout
history is preserved in `history/PRE_ADOPTION_*`; current rules are authoritative here.
