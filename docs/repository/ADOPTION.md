# ACL-1248: audited adoption and cutover

Issue: https://github.com/MojoTheApe/adopy-campaing-launcher/issues/248

## Current facts and target

Prepared from `master` at `6e871edd425afe1c74b955d7ba7b9793e06a7022`.
Master Repo source `546e6718d350410597876053a4fe0115fbbc1d6e` is proposed 1.0.0,
not a published release. Its compatible Tracker is
`ea969826398a808925bbd907ca8719445177ab59`.
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

- Resolve pre-adoption PRs #180, #222 and #247 under their original rules. #247 is
  proposed to replace the remaining #222 work; verify this before closing #222.
  Preserve any additional changes still in other working copies. Do not merge #222
  wholesale over newer reviewed corrections. Old green CI on a conflicting PR is
  not verification against current master.
- Pass hosted Actions on the final candidate. The intake runs ended `startup_failure`
  before jobs started; on 2026-09-21, run 35587298320 executed real Windows jobs.
  Each Python version passed 2344 tests but failed the bootstrap fixture, which still
  created origin/master while the candidate installer reads origin/main. This fixture
  is corrected; the new exact-candidate Windows and installer results remain required.
  Installer #245 and updater #246 were merged with an owner-approved exception limited
  to those exact candidates. It is not reused here.
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
Agree and review the disposition of historical completion before enabling policy:
record legitimate evidence for qualifying merged work, or explicitly retain delivery
pending where no verified package exists. Never invent old stage tests, reviewer
approval or non-code completion to keep a count green. Preserve the original accepted
record even where the new package-readiness view is pending. This is a cutover blocker,
not an implicit bulk relabel operation. No receipts are synthesized by this PR.

## Coordinated cutover (after blockers are resolved)

1. Freeze source/PR admission briefly, refresh master and rebase/review this candidate
   if its base changed. Account for all live worktrees and original PR dispositions.
   Complete the companion Task Tracker registry/fallback review, but do not publish
   its main/stage config while the source repository still uses master.
2. Record original master SHA, settings, registry version and state-branch SHA for
   recovery. Agree the exact first integration route for this migration in the Issue;
   it has not itself passed a pre-existing stage. Never fabricate a stage receipt.
3. Coordinate the source branch rename to main and explicitly verify the migration
   PR's base and branch settings. Preserve original master history. Establish stage
   from the current main content, merge the reviewed source through the agreed
   bootstrap route, and verify actual resulting trees/checks. No force push or
   arbitrary stage reset. Keep all new feature PRs paused until config/state agree.
4. Publish the existing ACL registry's main base and reconciled configuration through
   Task Tracker's maintained reviewed procedure; initialize the Stage label. Confirm
   a fresh authenticated board shows the original IDs/modules and intended columns.
   Existing registration is an update, not a second onboarding.
5. Record genuine evidence references in `.workflow/adoption.json`, set its state to
   active in a reviewed commit and run validation/checks. Update the temporary warning
   in AGENTS.md and the migration guard instructions. Keep `delivery.json` draft until
   published-standard and operational readiness requirements are actually fulfilled;
   the active/draft distinction must stay explicit. Remove allow-draft CI only then.
6. Verify a complete new task through stage/main and a real package install/update.
   Follow the existing release/backup/recovery safeguards; no release occurs merely
   from branch rename. Do not mark ACL-1248 complete until its full criteria are met.

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

Master Repo needs no contract change: package/stage and historical IDs are supported.
Task Tracker needs the existing ACL registry/fallback update and completion evidence
review, not an engine redesign. The companion patch is prepared under `adoption/`.
It is not read by the live server or applied automatically. Existing source/layout
history is preserved in `history/PRE_ADOPTION_*`; current rules are authoritative here.
