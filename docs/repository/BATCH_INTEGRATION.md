# Approved tasks and frozen integration batches (execution schemas 2/3)

With explicitly adopted [task context](TASK_CONTEXT.md), unchanged Approved sources
can use an existing bounded independent compatibility lane after forward main
changes. Original source review/checks remain unchanged; separate exact-bound
compatibility proof permits current intake without an invented author edit.
Changed scope/content or actual conflicts retain normal author return. Frozen
batch recovery and all combined checks stay mandatory. Other projects preserve
the original conservative main-bound approvals described below.

## Optional Stage and authorized delivery — TT-127

Stage is optional at a compatible explicit engine pin. Keep the same execution
schema/capacities and set `delivery_policy.staging.enabled` to false, mirrored by
`delivery.json.workflow.staging: false`; remove only the semantic Stage column.
The sequence becomes Review → Approved → reviewed frozen batch → main → verified
delivery. Individual source PRs still target main for approval and cannot merge.
Only the official reserved `codex/batch-<32hex>` branch with the identical
`Tracker batch: <ID>` line can select the `batch` route. Admission resolves its
actual Coordinator reservation, exact members, source approvals/scope/main,
bounded composition, independent combined review and required CI. A matching
branch name or PR marker alone cannot admit or authorize anything.

For this route, open the combined PR into main and use the existing
`batch review-acquire`, `record-review`, `check-merge`, real GitHub merge and
`merged ... --pr N --apply --reviewed-complete` commands. No Stage entry,
verification or fabricated promotion receipt is used. Actual merge parents and
source inclusion remain mandatory; interrupted Merged label writes retain the
lane for the same retry. Post-main acceptance can still be attested only by the
retained actual independent integration Reviewer. After main, finish this batch;
new product fixes require their own task/batch. Existing staged/legacy/v1 flows
retain their behavior.

When the owner authorizes integration **and delivery**, freeze that permission
explicitly while beginning the batch:

```sh
python scripts/task_tracker.py batch begin --intake INTAKE_ID \
  --actor Coordinator-XXXX --session ACTUAL_RUN --deliver \
  --delivery-workflow .github/workflows/complete-delivery.yml \
  --delivery-job 'Verify published Windows package'
```

Workflow/job above are examples of an actual project producer. The durable
`delivery_request` binds the batch/member set, target, policy, actual requester
and exact native verification workflow/job. Fixes may refresh approvals within
the same frozen member set and still need independent combined review. A batch
without this request keeps its ordinary integration behavior. Permission cannot
be added by resuming an existing batch or inferred from an arbitrary main push.

The Coordinator completes the genuine Merged handoff; the project-owned trusted
workflow performs its release/deploy and acceptance. The exported
`scripts/tracker_delivery_receipt.py` can then resolve and record the request
using a same-repository token, without cloning the private engine. See
[native delivery evidence](DELIVERY_POLICY.md#authorized-native-delivery-continuation).
Merge never means Delivered. Pending/failed receipts remain unfinished, and a
new batch cannot advance main while an earlier authorized delivery to the same
target is incomplete. Source implementation/review can continue.
Instruction-only main admission also waits so it cannot invalidate an in-flight
exact-main publication. Those tasks may retain their ordinary queued handoff.

Retry the exact authorized publication after an operational failure. If the
owner explicitly cancels a failed/abandoned delivery request, its actual retained
Coordinator can run `batch delivery-cancel --batch-id ID --claim ID --actor
Coordinator-XXXX --session ACTUAL_RUN --report-file decision.md`. A replacement
actual Coordinator must inspect the stopped owner and additionally supply
`--previous-claim ID --stopped --reason ...`. This records the cancellation and
ownership history, preserving all task receipts and their pending/failed state;
it permits a new reviewed fix batch without claiming completion. It is a new
decision on abandoned work, not another routine release approval.

Changing an existing staging policy needs the explicit fenced, queue-preserving
[policy migration](DELIVERY_POLICY.md#staging-only-policy-migration). Publish
compatible writers/service, drain real owners, inspect preview, and apply only
after the exact reviewed candidate config is on main. No consumer is activated
by this source change. Re-enabling Stage uses the same guarded procedure and an
explicit owner decision; a first release does not flip configuration implicitly.

Master companion: [MR-28](https://github.com/MojoTheApe/master-repo/issues/28).

## Parallel source reviews (explicit execution schema 3)

```json
"execution_policy": {"schema_version": 3, "implementation_limit": 5,
                     "review_limit": 5, "batch_limit": 5}
```

Mirror the exact object in `delivery.json.workflow.execution`. Schema 3 accepts
implementation_limit 1–16, source review_limit 1–5 and batch_limit 1–5. Every
source lane belongs to a distinct actual Reviewer run; the shared CAS reservation
rejects a sixth source when five are occupied. The existing single Coordinator,
frozen intake/batch and one independent integration review stay bounded separately.
Source reviews and approvals continue while the integration reviewer holds a
batch. Schemas 1/2 still require review_limit 1 and keep their prior lifecycle.

Commands and exact-content checks below are unchanged. `execution review-acquire`
selects a source by `--pr`; resume, `review-return`, takeover and `batch approve`
select only its claim. `record-review` identifies the matching PR, lane and actual
session. Uncertain approval/return writes hold only that source lane. A takeover
invalidates only that source's prior review, preserving other lanes. Reusing a
Reviewer run for concurrent source/integration lanes is refused. Source reviews
are stored privately in `source_reviews` keyed by PR; `review` holds only the one
integration lane. Public snapshots expose neutral owners and occupancy without
private claims, sessions, paths, evidence or reports. Instruction-only reviews
count toward source capacity and every held process lane blocks intake/batch
creation; genuine process admission still requires idle stage/intake/integration.

For explicit schema-2 to schema-3 adoption, keep implementation/batch capacities
unchanged, commit matching consumer config/descriptor/pins and publish compatible
writers plus the snapshot service. Preview `execution migrate
--preserve-review-queue`, then apply the same command with `--apply` while all
workers, source/integration reviewers, intake/batch and stage owners are idle.
The migration validates every queued Review's original ready handoff and each
current Approved's complete source scope/review/checks against current main. It
preserves exact queue order, approvals, Issues/PRs, actor/task/completion history,
delivery receipts and historical acceptance. The adoption history lists preserved
task IDs. A stale Approved source must be returned/reviewed under its prior rules
before migration. Unrecorded Reviews, busy owners or changed capacities fail
closed. Existing explicitly requested closed-handoff preservation remains opt-in
and never makes those sources ready. No hand editing or automatic state clearing
is permitted. Upgrading an engine, merging this implementation or changing a
registry fallback alone does not activate consumers or publish a service.

The independently integrated [TT-93](https://github.com/MojoTheApe/task-tracker/issues/93)
introduced the source pool. [TT-97](https://github.com/MojoTheApe/task-tracker/issues/97)
extends its ceiling to five with companion
[MR-22](https://github.com/MojoTheApe/master-repo/issues/22).

Each task keeps its genuine Worker, isolated branch/claim and independent source
review. Approval releases the review slot. A separate Coordinator owns a frozen
integration batch through configured Stage and main; one Reviewer reviews its combined
content. Approved is waiting for integration, not completion. The web UI remains
read-only. Commands reserve work and record evidence; they never merge or deploy.

## Explicit adoption

```json
"execution_policy": {"schema_version": 2, "implementation_limit": 5,
                     "review_limit": 1, "batch_limit": 5}
```

Mirror this object in `delivery.json` at `workflow.execution`. It requires the
existing delivery policy and exactly one Approved column between Review and Stage,
or Review and Merged when Stage is disabled. Implementation and batch capacity are independent;
both accept 1–16, review capacity is exactly one. ACL initially uses five per
batch. Approved backlog has no size limit; one urgent task is a valid batch.
Legacy and execution-schema-1 projects keep their existing behavior.

Except for the explicitly audited queued transition below, finish existing
PRs/claims/review/stage work under their original rules. Publish
the reviewed compatible snapshot service, commit matching consumer pins/config
and registry fallback, then preview/apply `execution migrate` while idle. It
preserves delivery receipts, historical completion and actor/task history. It
refuses live Approved work, active intake/batches and any old claim/review/stage.
No engine upgrade or skill installation activates this mode. Keep all writers on
the new pin; an old configuration cannot mutate the new policy's state.

For an explicitly selected execution-schema-1 to schema-2 upgrade, the supported
`execution migrate --preserve-review-queue` option can retain already handed-off
Review tasks while all implementation/review owners and stage are idle. Capacity
must stay unchanged, and every Review Issue must exactly match one durable ready
queued source PR on its original Worker branch. Preview lists preserved tasks;
apply changes only policy/approval-batch sections and migration history, without
editing Issues, PRs, authors or handoffs. Existing stage-targeted source PRs become
approval-only; direct merging is refused. They still need fresh current-main
review/checks before Approved. A source base may be the reconciled stage revision
only when its tree equals current main. New source PRs should target main, so
individual approval can proceed while a batch occupies stage. Busy owners,
unrecorded Reviews, drafts, wrong source branches or active stage block migration.

Only when explicitly requested, add `--preserve-closed-review-handoffs` together
with `--preserve-review-queue` to retain an original nondraft, closed, unmerged
source. All the same capacity, idle ownership, exact queue, branch, source route
and singleton checks still apply. Both base/head repositories must be official,
and the handoff must match its recorded Worker, session and implementer history.
Preview writes nothing. Apply preserves task/actor/completion records, queue order,
Issues and PRs, adding only normal policy adoption and one history event. The
event and result list actual sorted `{issue, pr}` pairs in
`preserved_closed_review_handoffs`; no source becomes ready or Approved. Default
migration still rejects closed sources. Closed entries cannot acquire source
review, record review evidence, approve, enter intake or pass batch admission;
author resolution remains separate. New eligible work can proceed without
consuming those historical entries. This does not reopen or close PRs, clear
authority or authorize service publication.

## Individual review and approval

Use `python scripts/task_tracker.py` in a consumer, or `python -m tracker` in this
repository. The [claim, identity and handoff rules](PARALLEL_EXECUTION.md#roles-and-claims)
still apply: one actual run has one Worker/Reviewer identity; use separate real
agent runs and never relabel a Worker as its own Reviewer.

1. `work`/`start` claims one task in its dedicated branch/worktree. One source PR
   links one task, targets **main**, and remains unmerged. Include current main in
   the source and retain all configured source checks. Direct source admission to
   main is refused; only the reserved combined candidate enters the configured integration branch.
2. `execution handoff ID --pr N ...` releases implementation capacity after the
   durable Review handoff. `execution review-acquire --pr N ...` reserves the
   actual source review slot: the shared single lane in schema 2, or one of the
   explicitly configured independent source slots in schema 3.
3. Review exact head/base/tree and complete task scope. `record-review` uses the
   existing evidence fields, plus `execution_lane` and `review_session`. Scope,
   policy and current main are also bound by the command.
4. `batch approve --actor Reviewer-XXXX --session ACTUAL_RUN --claim REVIEW_ID`
   checks current review, source CI and scope, journals the transition, verifies
   Approved and releases the review slot. Retry the same claim after uncertainty;
   ownership stays reserved until labels and state agree. The source PR stays open.

Before approval, `execution review-return` returns the task to its author. After
approval, its Coordinator uses `batch return --issue ID --actor Coordinator-XXXX
--session RUN --report-file fixes.md`. The author takes a fresh claim on the
original branch, checks, hands off, and gets a new independent approval. Scope,
head, baseline, membership or policy changes require fresh relevant evidence.
Changing a label alone cannot approve, reserve or admit a task.
If the task belongs to a running frozen intake, its current Coordinator supplies
`--claim INTAKE_ID`; a member of the active batch requires `--claim BATCH_ID`.

## Ship a fixed intake

On an authorized request, the Coordinator freezes currently eligible Approved
tasks. Source CI and dependencies remain mandatory. Ineligible tasks are reported
with reasons and remain unfinished. New approvals wait for the next intake.

```sh
python scripts/task_tracker.py batch intake --all-approved \
  --actor Coordinator-XXXX --session COORDINATOR_RUN
# Or select the explicit fixed set using --issues ID ID instead of --all-approved.
python scripts/task_tracker.py batch begin --intake INTAKE_ID \
  --actor Coordinator-XXXX --session COORDINATOR_RUN
```

The returned batch ID is its private Coordinator claim. Default selection takes
up to `batch_limit` remaining intake members; `--issues` can select a smaller
subset, never new members. Only one integration batch exists repository-wide.
When enabled, Stage must already contain current main's tree. Members, exact source tips,
approvals, scope, main baseline, policy and real review identities are frozen.

Assemble those tips with merge commits in the reserved `codex/batch-<ID>` branch
from current main. Preserve the source branches. Ordinary extra implementation
commits are rejected by admission. Conflict resolution changes code: return
affected tasks to their authors and review them again. Review also inspects the
merge commits and resulting tree; these attestations do not authenticate GitHub
users or replace branch protection.

Create the integration PR into stage (or main when Stage is disabled) with all member IDs in the title, exactly one
`Tracker issues: ID, ID` line, and one `Tracker batch: BATCH_ID` line. Then:

```sh
python scripts/task_tracker.py batch review-acquire --pr N \
  --actor Reviewer-YYYY --session REVIEW_RUN --coordinator Coordinator-XXXX
```

The Reviewer must be independent of every member's historical implementers and
the Coordinator run. Record review of the exact combined candidate. Evidence
uses the actual Coordinator as `implementer`, and adds `batch_id` and ordered
`source_heads` from the frozen manifest to the existing fields. Preserve each
member's own Worker/Reviewer; integration review is additional evidence.

For staging-enabled delivery, run the complete applicable combined CI, `check-merge N`, merge into stage, then
`stage --pr N --apply`. Every frozen member becomes Stage. Verify the actual
configured target/tree with `verify-stage`. Promote permanent stage to main with
the same member and batch lines. `check-merge` requires the passed exact combined
tree and unchanged main baseline. Preserve permanent branches and local release,
installation, backup and rollback controls.

After the real main merge, use `merged ID ID --pr MAIN_PR` dry run, then
`--apply --reviewed-complete` after verifying every member's complete scope.
The intake records completion of that batch; the Coordinator claim is released
only after durable merge/label writes. `batch close-sources --batch-id BATCH_ID`
can close superseded source PRs only after verifying their exact inclusion in an
admitted merge still in main. Branches/reviews/history remain intact.

GitHub can automatically mark the main-targeted source PRs MERGED when the
promotion includes their head commits. This is expected: completion accepts them
only after a real promotion merge, with unchanged frozen source heads, scope,
policy and independent review, and verifies each tip is an ancestor of that exact
main merge. Approval, intake and pre-main admission still require open unmerged
sources. Cleanup treats already merged or closed included sources as satisfied;
it closes only sources that remain open and unmerged. Retry normal `merged` after
an interrupted label write; ownership stays held until every member finishes.

For a blocked batch on an older engine, invoke a clean reviewed fixed-engine
checkout explicitly with `python -m tracker --config /absolute/consumer/tracker/config.json`
from that engine checkout, then the same `merged` and `batch close-sources`
arguments. The consumer client enforces `tracker/engine.json` even with
`TASK_TRACKER_HOME`; pointing it at a different revision will fail. Never patch
the old `.tracker-runtime` or edit durable state/claims to bypass completion.
An explicit engine invocation preserves the project's configuration and existing
policy; it does not migrate state or adopt a new consumer pin. Coordinate future
writer pins through the project's normal adoption procedure.

Reconcile stage with main, then `batch begin --intake SAME_ID ...` for the next
bounded set. Revalidate its source approvals against the new main; stale tasks
return for author updates and fresh review. Ten eligible tasks ship as two batches
of five; new arrivals do not extend them. Package/production delivery still needs
its separate verified receipt. Main merge alone never publishes or installs.

## Recovery

`execution show` is private: inspect held claims, intake, member versions and
reports before retrying. Every reservation uses shared-file SHA compare-and-swap;
uncertain GitHub writes retain ownership. Never infer abandonment from time.

- Before stage, `batch cancel ... --claim BATCH_ID --report-file reason.md` can
  release the batch while unchanged members remain Approved in the intake.
  It refuses an actual stage merge even when its Stage receipt was interrupted.
- After stage, keep the Coordinator claim. `batch return --issue ID ... --claim
  BATCH_ID --report-file fixes.md` returns only the affected author and invalidates
  candidate review/verification. Other members and integrated content stay held.
  The author gets a fresh claim/approval. `batch refresh ... --claim BATCH_ID`
  binds fresh member proofs/current main to the same set, preserving old history.
  Build a new candidate, independently review, verify stage and promote unchanged.
- `batch review-return ... --claim REVIEW_ID --report-file reason.md` releases
  candidate review for rebuilding while retaining Coordinator ownership. It does
  not implement or silently return all member tasks.
- If main changes, refresh all affected source approvals and the held candidate.
  No new task joins a batch, including during fixes. Do not reset stage, discard
  already integrated work, drop a member or add unreviewed promotion changes.
  This version deliberately supports same-member rebuilds; selective exclusion
  after stage requires a separately reviewed recovery implementation.
- A stopped Coordinator transfers with `batch takeover --previous-claim BATCH_ID
  --stopped --reason ... --actor Coordinator-NEWID --session NEW_RUN`. The old
  session cannot mutate the held batch. A stopped Reviewer uses the existing
  `execution review-takeover`; old approval is invalidated, exact-content stage
  proof stays subject to the new independent candidate review.
- If the Coordinator stops before/between batches, the same takeover command
  accepts the running intake ID and preserves its remaining selection. Pending
  member author-return journals transfer with a held batch. A standalone pending
  author return uses `batch takeover --issue ID --previous-claim RETURN_ID
  --stopped --reason ...` before resuming the same report under the new claim.
- A stopped Reviewer during a partial Review/Approved label transition retains
  its narrowly scoped approval-recovery journal on takeover. Its replacement
  records a fresh independent review; only that owned recovery permits the two
  transitional labels, and approval reconciles them before releasing the slot.
- With no held batch, `batch intake-cancel --claim INTAKE_ID ... --report-file ...`
  cancels only remaining intake selection; actual task statuses/history stay.

Public snapshots show Approved count, configured batch limit, held member IDs,
Coordinator and batch state, with separate implementation/review capacity. They
exclude session/claim IDs, source manifests, filesystem paths and reports.

Tracked by [TT-77](https://github.com/MojoTheApe/task-tracker/issues/77) and
[MR-14](https://github.com/MojoTheApe/master-repo/issues/14). Consumer adoption and
shared-service publication require their own verified operational handoff.

## Optional instruction-only process route

The separate top-level `process_instructions` setting is mirrored exactly as
`delivery.json.workflow.process_instructions`. It requires the independent-review
delivery policy and never changes its fingerprint or inherits on export. Read
[PROCESS_INSTRUCTIONS.md](PROCESS_INSTRUCTIONS.md) for the fixed eligible paths,
complete Git proof, actual process check names, independent ownership and exact
merged evidence. Eligible one-task sources target main and complete from their
verified process merge receipt; ordinary changes keep this document’s delivery
flow. Legacy uses its actual task slot and review attestation; execution-v1/v2
uses the real source review lane. V2 process sources cannot become Approved or
enter intake/batches. A process lane blocks new intake/batch claims, and held
stage/intake/integration blocks process acquisition. Idle stage gaps can bridge
only current retained verified process merges under the pinned proof, with full
normal checks/review/stage verification and unchanged frozen main afterward.


## Explicit post-main application acceptance recovery

An admitted application batch can retain its integration lane after main while
its original acceptance checkboxes remain unchecked. Editing those checkboxes
changes frozen scope. Instead, the actual retained independent integration
Reviewer may use `record-acceptance --pr MAIN_PR --actor Reviewer-ID --session
ACTUAL_SESSION --claim RETAINED_LANE --evidence-file acceptance.json` (preview;
add `--apply` to journal). This does not edit an Issue, finish a task, change a
policy or publish a package. The Coordinator and member implementers cannot
attest their own acceptance. Do not use it for partially implemented outcomes.

The document has `kind: post-main-acceptance`, a concrete `report`, exact
`batch_id`, promotion `pr`, actual `merge_sha`, `head_sha`, combined `tree_sha`,
`policy`, frozen ordered `issues` and `source_heads`, original `issue_scopes`
keyed by decimal Issue number, retained `reviewer`, `review_session`,
`execution_lane`, `coordinator` and `coordinator_session`. `criteria` is keyed
by exactly the members with unchecked work. Each value lists every unchecked
line in original order, including its one-based `line`, literal `criterion`,
`result: passed` and concrete nonempty `evidence`. Fenced examples are excluded.
The receipt validates the admitted promotion, source inclusion, original review,
passed stage/tree and required checks. It cannot waive any of them.

After recording, use ordinary `merged ... --pr MAIN_PR` preview and then
`--apply --reviewed-complete`. Completion rechecks the current receipt and task
scope before all writes and each task write; uncertain writes retain normal
ownership and resume behavior. The same evidence is retained for idempotent
completion and exact-source cleanup. Missing/partial/stale evidence fails closed.
Original task bodies, approvals and reviews remain unchanged. Parent/sibling
outcomes outside the frozen batch remain unfinished. Public snapshots exclude
acceptance reports, sessions and private ownership.

For an already admitted batch blocked on an older consumer pin, use a clean,
independently reviewed fixed-engine checkout with the exact original consumer
config: `python -m tracker --config /absolute/original/tracker/config.json
record-acceptance ...`, then normal `merged` commands. Do not patch cached
runtime code, change config or adopt a new policy. This explicit engine invocation
is recovery only; permanent consumer-pin adoption is separately tracked/reviewed.
