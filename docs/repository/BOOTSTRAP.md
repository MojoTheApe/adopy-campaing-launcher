# One-time ACL-1248 bootstrap

This is the reviewed implementation plan for the first adoption, not evidence that
it ran and not independent authority to deploy. Ordinary work stays frozen during
the short coordinated cutover. Existing manual Multilogin pilot deferrals remain
recorded in ACL-1215; they are not a successful stage or package-update report.

## Before the coordinated operation

Finish all old PR dispositions; refresh #249 on current master and obtain passing
checks and independent review for that exact head/base. Review Task Tracker #64 and
its exact matching ACL fallback. Record the source master/default/settings and
tracker-state SHAs, Tracker candidate/deployed SHAs and registry configuration for
recovery. Obtain any missing authorization for the concrete server rollout. Confirm
an isolated Windows target can supply the required stage install/update/restart/
recovery evidence. No employee data, production credentials or ad action is needed
for isolated package checks. If these prerequisites are unavailable, keep preparation
draft and report the specific missing step rather than partially switching projects.

## Initial integration uses the old rules

1. Record the exact one-time route and intended pause in ACL-1248. Linkage must use
   its real status and remaining scope; never clear a blocker merely to make a check
   green. Review + Blocked is the correct handoff while cutover cannot proceed.
2. Once the coordinated source operation is ready and its actual blockers are
   resolved or explicitly deferred within their authorized scope, review/check #249
   against **master** using the preserved legacy policy. Merge the prepared source
   into master first. Verify the first parent and resulting tree against the reviewed
   candidate. Leave ACL-1248 unfinished: this is a source integration milestone.
3. Rename that integration branch to main and verify the actual GitHub default/ref
   settings. Recreate master at the recorded **pre-migration** source SHA as the
   temporary compatibility channel described in ADOPTION.md. Keep source history,
   stable tags and other worktrees. No force push or branch reset.
4. Create permanent stage from the exact prepared main commit; run its full Windows
   and installer workflows. Branch existence and install/repair/uninstall are facts,
   not a completed update/recovery pilot or an invented stage receipt.
5. Verify main's actual tracker/config.json matches the reviewed Tracker companion.
   Merge/publish that companion through its maintained guarded procedure under the
   applicable runtime authorization. Confirm all six snapshots, the original ACL
   IDs, the seven columns and the unchanged bounded historical list. Keep new work
   paused until the registry, source and live board agree.

Renaming before step 2 cannot work: scripts/acl_pr_check.py deliberately admits only
this prepared PR against the original master and exact legacy config. The stage
rule is not retroactively imposed on this first old-policy source merge, and that
merge must never be described as already stage-verified.

## Bounded evidence maintenance before client activation

The prepared project client intentionally refuses writes; do not turn its guard off
or revive retired legacy mutation commands. Initial maintenance instead uses the
existing **canonical shared-engine interface**, from a clean checkout of exact
`5f211d3095a392a16c2c01a65eb02ff8719a7243`, with an explicit `--config` pointing at
ACL's matching, reviewed tracker/config.json. This is the explicit migration route,
not permission for other projects/tasks or an unrestricted alternate workflow.
Verify that config against authoritative main and the shared fallback before writes.

During the pause, permit only label initialization and the normal evidence/admission
commands for the single task ACL-1248, its existing working branch
codex/acl-1248-master-adoption and the recorded activation/promotion PRs. Read the
pinned engine's guide; run previews first. Do not start other tasks, change release
scope, mutate historical tasks or fabricate review/deployment results. The shared
engine still validates actual task links, route, head/base/tree, checks and receipts.

1. Initialize the configured Stage label with canonical `init`. Keep ordinary client
   writes blocked. Record genuine registry/branch/initial-stage check reports in the
   Issue; reports must distinguish completed evidence from deferred scenarios.
2. Prepare an activation commit on the same working branch, brought forward from
   actual main. Populate .workflow/adoption.json only with genuine evidence references,
   retain delivery.json as draft while the standard is unreleased, and update the
   temporary agent guidance. Reference the durable activation PR/report for review
   and CI evidence so a self-referential commit SHA is not invented.
3. Open that activation PR to stage. Its active candidate uses the standard PR checker,
   while installed main still has the prepared guard. Obtain independent review and
   current checks. Through the pinned canonical interface run record-review with the
   real report and check-merge immediately before merging. No bootstrap waiver of
   the new candidate's admission is granted.
4. After the actual stage merge, record stage for that PR. Perform the required
   isolated checks on its exact stage content and record verify-stage with the real
   report. Earlier prepared-main checks do not verify a changed activation tree.
   Failed/missing verification stops promotion; the installed main client stays guarded.
5. Promote stage to main with the same task set and unchanged verified content, normal
   independent review/checks and check-merge. Verify the actual merge tree and parents,
   then the fresh live board. Only now do ordinary active-client operations resume.
   Do not delete stage, main or the working branch while corrections may be needed.

These commands record operations; they do not perform a deployment or spawn a review.
Do not mark ACL-1248 complete merely because client activation works. Its package/
end-to-end acceptance and any unresolved criterion remain explicit. A stable release,
secrets provisioning or employee installation still needs its own actual scope and
verification. No old or new task receives a fabricated installation receipt.

## Stop and recover

On unexpected source/config/state drift, stop before the next mutation and inspect.
Before server publication, preserve the current service. After publication, use its
recorded compatible backup and guarded rollback; source/registry recovery must agree
with the refs that actually exist. Keep Issues, execution/delivery state, stable tags
and employee data intact. A failed activation keeps main's prepared guard and the
original task unfinished; fix the same working branch, then re-review and verify.
