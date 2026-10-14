# Approved tasks and batches of at most five

The prepared [ACL-1875 source-review pool](SOURCE_REVIEWS.md) selects execution
schema 3 with five independent source reviews and the same single integration
Coordinator/Reviewer. Its own source finishes the current v2 route before actual
live migration retained in parent ACL-1874. The dated v1-to-v2 adoption evidence
and commands below remain historical; they are not the v2-to-v3 migration procedure.

Parent [ACL-1566](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/566)
records actual migration, fresh shared-board verification and remaining deferred scope.
Source child [ACL-1747](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/747)
prepares the reviewed source and completes its existing execution-v1 stage/main route.
**Source preparation is not live activation.** Preserve the dated v1 activation
record in [PARALLEL_EXECUTION.md](PARALLEL_EXECUTION.md).

The later [ACL-1750](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/750)
source prepares the separate instruction-only route. It changes policy and CI and
therefore uses the ordinary old-policy stage route after ACL-1747; keep its genuine
claim/review/handoff distinct. Both adopted-source handoffs have completed normally.
The required [ACL-1753](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/753)
compatible-pin refresh follows its own old-policy source route before the coordinated
live v2 cutover. The original nine queued source tasks remain preserved.

## Selected contract and reading map

ACL-1747 originally selected reviewed Master
`cd79a7699cb108c517e5729590a57cab85ab1083` (MR-14 / PR15) and Tracker
`e6013f8ad641bd99061f7db82bceeb705f8c150a` (TT-77 / PR78). ACL-1750 advances
the source to reviewed, merged Master `d24db185ebdff2cadcecf89f5b28061d6cd19564`
(MR-16 / PR17) and Tracker `d6ce39bede04851c37bb4d13cc3bed0f2dcd2f76`
(TT-79 / PR80), including the instruction-only admission contract. ACL-1753 selects
reviewed merged Master `fe9241152cb466dfbafa3bcd3070f877c1b1b9c4` (MR-18 / PR19)
and Tracker `086aaa98f2e807d640105be2929714d0b7331f21` (TT-85 / PR86), adding the
explicit preserved-closed-handoff cutover option without changing live approval guards.
At the ACL-1753 source cutover both ACL
execution objects explicitly selected schema 2, implementation_limit 5,
review_limit 1 and batch_limit 5. The shared skill was installed locally from
Master `d24db185ebdff2cadcecf89f5b28061d6cd19564` with a backup; each other agent environment needs
its own installation. Installing the skill does not migrate project state.

Read the pinned Tracker's `docs/BATCH_INTEGRATION.md` and `guide` for exact commands.
`python scripts/task_tracker.py` is the guarded project client. Exported portable
admission helpers remain in `.workflow/tools/` with `scripts/` compatibility paths.
The required batch helper travels with the reviewed pin; v2/v3 validation
refuses a missing implementation or compatibility entry point.

Each task keeps one source PR, its actual Worker/branch and independent Reviewer.
Ordinary batch source PRs target main and remain open. Their direct merging is refused.
The separately opted-in [instruction-only route](PROCESS_INSTRUCTIONS.md) instead
holds independent source review through direct main admission; no batch is created.
For ordinary batch work, individual review checks the complete current scope,
main/base, content and required source CI. `batch approve` verifies the report and
moves Review -> Approved, releasing
the source-review lane. Approved is waiting for integration, not completion.

On an authorized "ship all Approved" request, freeze the currently eligible intake.
New approvals wait for another intake. Select sequential batches of at most five;
one urgent task is valid and the Approved backlog has no size cap. Capacity for
implementation and batch size are separate settings. Only one held integration
batch may occupy permanent stage. A separate actual Coordinator run owns it;
do not rename one Worker run to manufacture a Coordinator or Reviewer.

## Assemble and ship

Use real opaque actor/session IDs and the actual claim IDs returned by the engine;
the examples below are placeholders, not evidence of executing agents.

```sh
python scripts/task_tracker.py batch intake --all-approved \
  --actor Coordinator-XXXX --session ACTUAL_COORDINATOR_RUN
python scripts/task_tracker.py batch begin --intake INTAKE_ID \
  --actor Coordinator-XXXX --session ACTUAL_COORDINATOR_RUN
python scripts/acl_batch.py --actor Coordinator-XXXX \
  --session ACTUAL_COORDINATOR_RUN --claim BATCH_ID --worktree /absolute/new/worktree
# Inspect the plan; the same arguments with --apply assemble the frozen tips.
```

`acl_batch.py` reads the live private claim through the exact pinned client, verifies
ownership, matching policy, frozen membership, current main and unchanged open source
PR heads. It requires the official ACL origin and a clean checkout, fetches exact tips,
rechecks ownership and creates a dedicated linked worktree on `codex/batch-BATCH_ID`.
Merge commits preserve every source tip. It prints the combined PR title/body and
candidate head/tree; it never pushes, merges into stage/main, publishes or deploys.
An existing unrelated directory/branch is preserved. A conflict aborts only its own
unfinished merge, retaining prior assembled work and the claim. Return the affected
task to its author; no unreviewed conflict resolution belongs in the batch branch.
The helper accepts matching v2/v3 batch policy. In v3 unrelated source-review
owners may continue during assembly; an existing integration review still blocks
reassembly. Exact composition, frozen membership and guarded rebuild proof remain
required.

Push the reviewed candidate branch and create its PR into stage with all member IDs
in the title, one `Tracker issues: ID, ID` line and one `Tracker batch: BATCH_ID` line.
Acquire `batch review-acquire`; the actual independent Reviewer must have implemented
none of the members and must be a different run from the Coordinator. Review combined
scope, composition, merge commits and final tree in addition to each source's review.
Record the exact report using `record-review`, with execution_lane, review_session,
batch_id and ordered source_heads from the frozen manifest. Preserve actual member
Workers and source Reviewers; the combined report names the actual Coordinator as
the integration implementer.

Complete applicable combined CI is mandatory: Linux Python 3.11/3.13 shards, Windows
3.13 tests, descriptor, linkage and installer/build/install/repair/uninstall plus the
real isolated packaged update/restart/recovery report. Even a docs-only combined
batch requires this context. The scope classifier permits per-job reuse only of the
same tested Git tree. A green source's different tree proves no combined candidate.
Manual/release rules remain unchanged; report reused jobs and skipped source jobs
accurately. See [WORKFLOW.md](WORKFLOW.md) and [STAGING.md](../runtime/STAGING.md).

Run `check-pr` and `check-merge` immediately before the actual stage merge. Record
`stage --pr N --apply`, verify the actual configured stage target/tree, then promote
permanent stage to main with identical members/batch lines and unchanged content.
Run promotion admission again; no extra fix or member joins it. After the real main
merge and full task-scope review, use `merged ID ID --pr MAIN_PR` dry run then
`--apply --reviewed-complete`. The engine journals each member and releases the
integration claim only after completion. `batch close-sources --batch-id BATCH_ID`
closes superseded source PRs only with actual inclusion evidence, preserving branches.
Reconcile stage/main and revalidate the next intake members against current main.
After admitted instruction-only main merges, the engine's read-only
`stage_base_compatible` verifies actual process receipts and a bounded current
first-parent/scope/policy/tree chain before permitting the next batch. This proof
changes no branch refs; unexplained or mixed divergence remains blocked. See
[PROCESS_INSTRUCTIONS.md](PROCESS_INSTRUCTIONS.md).

## Fixes and recovery

Before approval, Reviewer returns review and the author takes a fresh implementation
claim before any edit. After approval, Coordinator uses `batch return` with the held
batch/intake claim where applicable. The original author claims, fixes, checks and
hands off for fresh independent approval. Changed main/base/scope/head or policy
requires fresh relevant evidence. Labels and timeouts cannot reserve or transfer work.

Before actual stage entry, `batch cancel` preserves real author claims/status/history;
begin a smaller subset of the same intake to leave a failed member for later. After
stage entry retain the Coordinator lock and same members. Return only affected authors,
refresh after fresh approval, rebuild, independently review and reverify exact stage.
After `batch refresh`, use a **new** isolated path with
`acl_batch.py --rebuild --previous-head EXACT_OLD_HEAD --apply` and the same owning
actor/session/claim arguments. The helper validates the retained previous manifest
and exact generated head, saves a local evidence ref, and assembles a fresh detached
candidate from frozen main and current approved tips. The previous local branch,
worktree and index stay intact. Appending new tips to the old graph retains superseded
tips and fails the pinned composition gate; it is not a recovery method.
If a detached rebuild failed without publication, the remote still holds an earlier
candidate: the helper must find a retained same-member manifest whose exact composition
matches that head. The latest refreshed manifest alone does not establish its history.
An unknown old implementation commit is rejected; keep the failed worktree as evidence.

Inspect the printed `replacement.push_argv`, recheck the actual held claim/current
sources, then run that exact atomic push. It creates a remote evidence branch at the
old head and replaces **only the owned generated integration ref**, with explicit
expected-old-head and expected-absent-evidence leases. If either lease fails, neither
remote update happens: inspect the changed ownership/ref, preserve evidence and stop.
For an unpublished partial assembly, the old head must match the owned local branch;
the generated remote ref must still be absent. The printed atomic push uses an
expected-absent lease for its first publication and archives the partial head too.
Further rebuilds archive each previous remote candidate while keeping the original
local branch/worktree and all earlier evidence intact.
Never use an unqualified force push. The old evidence refs are retained; source
branches, main, permanent stage and release tags are never rewritten by this operation.
The rebuilt candidate needs fresh combined review, CI and exact stage verification.
If the baseline changed, use the documented claimed reconciliation first. Never force-reset/delete stage or
invent new members in a rebuilding batch. Selective post-stage exclusion needs a
separately reviewed supported recovery implementation.

Inspect private `execution show` before retry/takeover. Only supported explicit
stopped-session transfers change ownership. `batch review-return` keeps the
Coordinator lock; source fixes do not silently release integrated content. Keep
private manifests/claim/session IDs and raw reports out of public board snapshots.

## Guarded source and state cutover

2026-10-08 intake: main `48bb8ee4898bc5845bd7a4cb33a052ce2f4cb496`; nine durable
queued Reviews (Issues406,433,526,529,642,651,710,728,739), no Worker/Reviewer before
ACL-1747's claim, and last stage candidate PR744 promoted. Preserve all their PRs,
authors, task scope and handoffs; none is automatically approved or completed.

1. ACL-1747 uses an access-controlled copy of the genuine old main config (execution
   schema 1, capacity5/review1) for its old-rule claim/review/admission/stage/verification/
   promotion/Merged commands. The old a4ea engine cannot read ACL's >1MiB state.
   The reviewed TT-73 operator at `c89dd82289d2c6b3e27a85736729bbeaf7461a77` supplies
   that already-reviewed read fix while retaining the unchanged old policy; this is
   bounded transition support, not a fabricated configuration or a v2 admission bypass.
2. Independently review source/base/full child scope, run required CI, admit its source
   into stage under that old config, verify the actual exact-stage Windows package
   mechanism. Verify the shared-service publication in step 3 before main promotion;
   then promote unchanged into main and complete its normal old-policy
   Merged handoff. ACL-1750 completed its separate old-policy source handoff; the
   required ACL-1753 compatibility refresh also completes its own ordinary
   old-policy stage/main/Merged route before coordinated live migration. Original queued work stays
   idle and preserved through the cutover.
   The new head config is prepared but cannot mutate old v1 state as v2.
3. Publish the reviewed compatible shared Tracker service through its maintained
   guarded deployment. It preserves authentication, last-good snapshots and the single
   event writer. If this operation is not authorized or verified, do not claim rollout
   or activate consumer state. Keep the maintained ACL registry fallback aligned via
   a reviewed Tracker companion; config merge alone is not live-board proof.
4. Verify no active Worker/Reviewer, unpromoted stage, active batch/intake or unrecorded
   Review. Save a private before-state snapshot. From the reviewed merged ACL checkout
   and exact new engine, preview both explicit preservation flags:
   `execution migrate --preserve-review-queue --preserve-closed-review-handoffs`.
   Inspect its exact retained queue and `preserved_closed_review_handoffs` pairs,
   then apply the same options. The second flag requires the first. It supports only
   unchanged-capacity v1->v2 with verified durable one-task handoffs, including the
   original closed unmerged PR 407 for Task 406 and PR 439 for Task 433. These stay
   unfinished; no Issue/PR is reopened or automatically approved. Review acquisition,
   approval and batch admission still require a current open ready source. Never
   clear, edit or overwrite state to pass.
5. Compare private before/after state: only execution policy/new approval-batch sections
   and migration history may change. Preserve Issues/labels/source PRs/authors/handoffs,
   delivery/history, six modules, 71 aliases, +1000 IDs and the 74-task manifest.
   Existing stage-targeted queued sources become approval-only; they still require
   current-main author reconciliation, CI and independent review before Approved.
6. Verify a fresh digest-covered shared ACL board with Approved, limit5, distinct
   ownership, retained queue and correct package/delivery projection. Browser verification
   is excluded by the existing owner decision; report server-side snapshot evidence
   and preserved access controls accurately. The owner selected the shortest cutover
   and cancelled extra tools and demonstration work. Do not create such tasks or
   manufacture a real-batch receipt; retain unresolved scope in the parent and resume
   useful application work only under its separate authorization.

The genuine pristine upstream template baseline stays legacy at its recorded inputs;
v2 is an explicit local opt-in. Draft standard validation remains because VERSION1.0.0
is unpublished. The delivery fingerprint stays
`302d13abcb245858eb810380630d4a2845b1d0f516981281bb4c9dfa950dae21`.
Main merge does not publish a stable installer, update employee machines, verify the
release/download channel or perform the deferred live Multilogin pilot. Frozen
compatibility master, backups/recovery, machine data and draft-only advertising remain
under their maintained procedures and actual authorization.
