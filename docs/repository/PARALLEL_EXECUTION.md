# Parallel execution contract v1

This document describes execution policy schema 1. Schemas 2/3 use
[Approved tasks and frozen integration batches](BATCH_INTEGRATION.md), sharing the
Worker claim/handoff and independent identity rules but releasing source review
at Approved and reserving integration separately. Installing a newer engine does
not switch modes.

Two workers can implement independent tasks in separate agent runs, branches and
worktrees. One reviewer processes ready tasks sequentially and keeps the same lane
through review, fixes, stage (where configured), checks and main integration.
Waiting in Review does not consume an implementation slot. A dependent task waits
for its dependency's configured completion, including delivery when required.

This is explicit per-project adoption. Neither a shared engine upgrade nor a new
installed skill enables it. `tracker/config.json` accepts:

```json
"execution_policy": {"schema_version": 1, "implementation_limit": 2, "review_limit": 1}
```

It requires the existing [delivery policy](DELIVERY_POLICY.md). Master Repo mirrors
the same object in `delivery.json` at `workflow.execution`. Limits are repository
wide across all cooperating platforms/sessions. Implementation capacity accepts
1–16; review capacity is exactly 1. Capacity changes require an idle migration,
not rewriting the live claims. One source PR contains one task in this version.
Research/operations can use a PR containing their report; `complete` cannot bypass
the independent review lane. Legacy projects retain their existing commands.

## Adoption order

1. Finish the adoption PR under the old rules. Inventory active Issues, branches,
   claims, pending batches, reviews and stage work. Preserve completion evidence:
   execution-only adoption/capacity changes leave delivery receipts and historical
   manifests valid. Changes to the actual delivery policy still require its
   existing explicit evidence reconciliation.
2. Update all writers, generated PR helpers, exact engine pins and the snapshot
   service to a compatible reviewed revision. Deploying that service is a separate
   maintained release step; merging this source does not update the live website.
   Preserve the old config and a private copy of shared state for recovery.
3. Commit the matching opt-in config through the project's authorized adoption.
   Preview `execution migrate`, then `execution migrate --apply`. It refuses live
   In Progress/Review/Stage tasks, legacy claims/running batches and active stage.
   It preserves history and delivery data while using state schema 2. Preview makes
   no writes; repeating an already applied migration is safe.
4. Verify a fresh board snapshot and use only the pinned compatible client. Older
   state-aware clients reject schema 2. Do not resume older writers or remove the
   policy as a rollback: reconcile all work while idle before restoring anything.

## Roles and claims

The coordinator uses neutral names such as `Coordinator-A7F2`, `Worker-B3K8` and
`Reviewer-C9M4` (role plus 4–32 alphanumeric characters). Generate a distinct opaque
session ID for each actual agent run, retain it on resume, and never rename a run
to claim independence. A session can register only one worker/reviewer actor.
GitHub Assignee remains a real account and is never overwritten with these names.
The web drawer displays coordinator, worker, reviewer and work state separately.

The tracker reserves work; the coordinator uses the host's actual delegation
capabilities. It does not spawn a model or open chats. Start at most the configured
number of workers and launch an independent reviewer when a task is ready and the
review lane is free. Workers do not independently launch reviewers. If independent
agent runs are unavailable, explain the serial implementation fallback and obtain
a real independent reviewer through available project tooling. Never fabricate one.

Use `python scripts/task_tracker.py` in a consumer, or `python -m tracker` in the
engine checkout. Read the current task and applicable instructions before each
claim. The following identity/path values are illustrative:

```sh
python scripts/task_tracker.py start EX-12 \
  --actor Worker-B3K8 --session run_b3k8 --coordinator Coordinator-A7F2 \
  --branch work/ex-12-search --worktree /worktrees/ex-12
```

`work` accepts the same flags for immediate existing/new requests. `start` returns
a private `claim.id`; reserve before the worker edits code. Give each worker only
its task, worktree, ID and necessary context. The second claim repeats this with a
different task/run/branch/worktree. A third claim waits when both slots are full.
Do not use the legacy `queue begin/next/release/cancel` operations in this mode;
claim the owner's explicitly selected tasks individually.

`execution show` is a private diagnostic containing paths, claims and reports.
It must not be published. Actor IDs are attestations by cooperating agents, not
new authentication principals. GitHub permissions and merge protections remain
authoritative; direct GitHub writes or forged sessions are not prevented by these
records. No credentials or secrets belong in shared state or reports.

## Handoff, review and integration

Push a ready source PR with the exact task link and existing naming/check rules.
For execution-enabled projects, `work/<ticket-id>-<short-name>` and the existing
`codex/` prefix are accepted; retain an explicit host/user branch requirement.
Write the actual scope, checks/results, PR, base and remaining risks in a handoff
file and keep the Issue Specifications current. Then:

```sh
python scripts/task_tracker.py execution handoff EX-12 --pr 45 \
  --actor Worker-B3K8 --session run_b3k8 --claim CLAIM_ID --report-file handoff.md
python scripts/task_tracker.py execution review-acquire --pr 45 \
  --actor Reviewer-C9M4 --session review_c9m4 --coordinator Coordinator-A7F2
```

Handoff journals before changing the label, verifies Review, then releases only
that implementation claim and enqueues the task. The one review reservation is
atomic across sessions; it rejects every recorded implementer session, including
previous owners. The coordinator chooses a ready task (normally oldest first);
queue order is visible but strict FIFO is not required. Save the returned review
`id`. `record-review` evidence keeps the existing exact head/base/tree/report and
recorded `implementer`, plus the lane owner's `reviewer`, `review_session` and
`execution_lane`. It rejects stale or foreign evidence. Resume an existing lane
with `review-acquire` and its `--claim`, actor/session and PR.

Run normal `check-pr`, independent review, required checks, `record-review` and
`check-merge` immediately before merging. Stage-enabled projects still use working
branch → stage → main, exact content verification and the existing one-candidate
rule. The reviewer finishes the entire lane before taking another task. After a
verified main merge, run `merged ID --pr MAIN_PR` dry run, then
`--apply --reviewed-complete`. Only after receipts and labels succeed is the lane
released. Production/package verification remains a separate existing requirement;
the lane does not publish, install or falsely claim delivery. Before the next
task's approval, update against the new main and repeat affected checks/review.

## Failures, fixes and stopped agents

- Uncertain writes keep the claim. Inspect `execution show` and Issues, then
  repeat the same operation. For an uncertain start, use `start --resume` with the
  original flags and `--claim`. For an uncertain handoff, repeat its exact PR/report
  and claim. Do not infer success, free capacity or abandonment from elapsed time.
- A worker that cannot proceed uses `execution stop ID --actor ... --session ...
  --claim ... --report-file ...`. It journals the handoff, moves the task to blocked
  Todo and releases its claim. Resolve the report before unblocking/reclaiming it.
- If a worker has actually stopped, the coordinator can use `execution takeover
  ID --previous-claim OLD --stopped --reason ...` with new worker/session and
  explicit branch/worktree/coordinator fields. Inspect/reuse the prior work. This
  rotates ownership and records both implementers; old claims cannot resume,
  stop or hand off. Pending handoff/stop writes must be reconciled first.
- The reviewer requests fixes with `execution review-return --actor ... --session
  ... --claim LANE_ID --report-file ...`. Before a source merge this frees the lane
  and returns the task to Todo. The author takes a fresh implementation claim.
  Add `--keep-lane` to retain integration ownership; this is mandatory after the
  source has entered stage. The worker still consumes an implementation slot while
  fixing it and hands off a ready source PR from the same working branch. That
  handoff resumes the same reviewer and invalidated proof must be repeated.
- If the reviewer stops, use `execution review-takeover --previous-claim OLD
  --stopped --reason ...` with a new independent reviewer/session/coordinator. It
  preserves the occupied lane, rotates the claim and invalidates approval. A
  replacement can review the actual merged source in stage or main with `record-review`;
  unchanged exact-content stage verification remains subject to normal gates.

Reservations use GitHub contents SHA compare-and-swap. Label changes are separate
GitHub writes, so recovery deliberately retains ownership until verification.
Stopping the previous process before takeover is essential: a claim is not a
remote process kill switch. Never force-clear another lane or hand-edit shared
state to bypass a failed check.

## Compatibility and publication

`export-template --execution-policy FILE` exports the explicit policy alongside
`--delivery-policy FILE`; absence strips any engine-owned execution setting.
Schema-1 projects and snapshots remain supported. Schema-2 public snapshots expose
capacity, neutral roles and review position, but omit session/claim IDs, filesystem
paths, reports and actor history. The UI is still read-only.

Tracked by [TT-67](https://github.com/MojoTheApe/task-tracker/issues/67) and companion
[MR-11](https://github.com/MojoTheApe/master-repo/issues/11). This change enables
adoption; it does not migrate ACL or any other existing project.

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
