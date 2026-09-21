# ACL-1248: audited adoption and cutover

Issue: https://github.com/MojoTheApe/adopy-campaing-launcher/issues/248

## Intake provenance

Prepared from `master` at `79caea74d4f88c559d79caf6fc86ef9b7b43f9e8`.
The frozen historical audit retains its original source `6e871edd425afe1c74b955d7ba7b9793e06a7022`.
Master Repo source `e1473c428872757519355af1a7753cf3a0ce0f12` is proposed 1.0.0,
not a published release. Its compatible Tracker is
`5f211d3095a392a16c2c01a65eb02ff8719a7243` (TT-61 / PR #62).
The original engine was `adb2ce04fff017d1059031f4298effc020d0e2e5`.
No `main` or `stage` exists at intake. Registry identity stays `acl`.
Target: package delivery, stage enabled, existing installer/updater preserved.

The pristine generated files are in `.workflow/template-base.json`. They are the
unmodified template, not a disguised copy of ACL's customized files. ACL identity,
modules, guards, CI and runtime procedures are deliberate local adaptations.
`.workflow/tools/` contains pinned exported helpers; the guarded client is a local
exception. `scripts/` paths remain compatible entry points. No product script or
retained legacy Tracker package is deleted just to fit the template.

## Verified cutover and activation gate — 2026-09-21

- Original PR #180 and #222 are closed. Their remaining work is preserved in
  ACL-1174, ACL-1251 and ACL-1252. Reviewed PR #247 merged as `79caea74`.
- Prepared migration PR #249 passed independent review, full Windows CI
  [35600689330](https://github.com/MojoTheApe/adopy-campaing-launcher/actions/runs/35600689330)
  and real installer/update/restart/recovery
  [35600689604](https://github.com/MojoTheApe/adopy-campaing-launcher/actions/runs/35600689604),
  then merged at `09eb8af3542d1eb64649ece179f79ad343efd563`, tree
  `21ef1a66e804d006c0fc43bcc31892909f5f5ef1`.
- GitHub default is now `main`; permanent `stage` was created at that exact commit.
  `master` remains at pre-migration `79caea74d4f88c559d79caf6fc86ef9b7b43f9e8`.
  Automatic branch deletion is disabled. Existing clones and stable tags were preserved.
- Initial stage `09eb8af` passed full Windows CI
  [35603056418](https://github.com/MojoTheApe/adopy-campaing-launcher/actions/runs/35603056418)
  and real package update/restart/recovery
  [35603056536](https://github.com/MojoTheApe/adopy-campaing-launcher/actions/runs/35603056536).
  Downloaded report source/tree match that exact stage commit. This is initial
  prepared content; PR #253 still requires its own changed-tree stage verification.
- Tracker PR #64 merged and was deployed as `0dadc889e18b027c8ca98b888407d492848f2f19`
  using the maintained guarded installer on the existing server. Fresh snapshots
  preserve all 160 ACL task identities, six modules, 74 historical completions,
  Release 7/10 and all other five project registrations. ACL now has Stage before
  Merged. New tasks remain subject to package delivery. See
  [TT-63](https://github.com/MojoTheApe/task-tracker/issues/63) for the actual report
  and private rollback location. No historical receipt was invented.
- The owner excluded browser/UI verification. Server-side digests, fresh snapshots,
  original identity/classification, unchanged authentication/secret/configuration
  and bind mounts, active event synchronization and HTTP 401/403 restrictions were
  checked. No authenticated browser test is claimed.
- Required private Master Action access works in actual consumer runs. GitHub's
  private branch-protection API reported a plan restriction; normal agent admission
  remains required, but is not a claim of server-enforced protection.

The active candidate follows the ordinary working-branch -> stage -> main route.
Its exact independent review, current CI and actual stage package report are linked
from [ACL-1248](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/248).
Earlier prepared-source tests do not verify a changed activation tree. Keep ordinary
admissions paused until the real passing stage receipt, unchanged-content main
promotion and normal `merged` operation record that promotion and release the slot.
The marker alone is not proof that these later steps have occurred. Keep the
Master descriptor draft while its selected standard is unpublished. Stable release
publication/download-channel acceptance and the separately deferred Multilogin pilot
are not claimed by the isolated same-candidate package tests.

`python scripts/acl_adoption.py audit --remote` reads selected PR/current-main CI
prerequisites; it never writes or replaces the normal Tracker admission/evidence.

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
registry patch contains the identical block. The frozen intake projection preserved 74 of 157
completed tasks and 7 of 10 release tasks; the published cutover now has 160 tasks, without a delivery receipt. The UI says
Completed before adoption; old installations have not been verified. Changed scope
or restarted work loses this exception. Unfinished tasks remain unfinished.

At cutover the exact reviewed list was verified against fresh Issues and the
published board. Do not regenerate or extend it automatically. No Issue relabelling or synthetic receipts are needed.

## Retained one-time cutover procedure

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
activation candidate then follows stage -> main. Ordinary admissions resume only
after all applicable acceptance is met and the normal `merged` operation records
the promotion and frees the stage slot. A source merge alone is insufficient;
unresolved acceptance keeps the coordinated operation paused. Keep the descriptor draft while
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
The ACL registry/fallback companion is reviewed in Task Tracker TT-63 /
PR #64 (`7a7975011b564a09bdfe325dd8efeb228e454f2e`), matching the patch under
`adoption/`. It was published as merged commit `0dadc889e18b027c8ca98b888407d492848f2f19`
through its guarded installer; source merge alone did not publish it. Existing source/layout
history is preserved in `history/PRE_ADOPTION_*`; current rules are authoritative here.
