# Workflow reference

This local exact-pin reference describes supported routes; it does not select
any of them. Read TRACKER_AGENT_WORKFLOW.md (or the source selected guide) and
actual tracker/config.json for this project's primary path. All numerical limits
below are illustrative configurations, not capacity granted to the reader.
Shared executable policy, state and parser rules belong to Tracker; normative
common policy belongs to Master; product/runtime safeguards belong to the project.

Full exact local contracts:
- [DELIVERY_POLICY.md](DELIVERY_POLICY.md)
- [PARALLEL_EXECUTION.md](PARALLEL_EXECUTION.md)
- [BATCH_INTEGRATION.md](BATCH_INTEGRATION.md)
- [TASK_CONTEXT.md](TASK_CONTEXT.md)
- [COMPLETION_POLICY.md](COMPLETION_POLICY.md)
- [PROCESS_INSTRUCTIONS.md](PROCESS_INSTRUCTIONS.md)
- [MAINTENANCE_AND_CI_EVIDENCE.md](MAINTENANCE_AND_CI_EVIDENCE.md)
- [CI_REUSE.md](CI_REUSE.md)
- [LEGACY_DELIVERY_RECONCILIATION.md](LEGACY_DELIVERY_RECONCILIATION.md)
- [PORTABLE_CONTRACT.md](PORTABLE_CONTRACT.md)
- [CI_RETRY.md](CI_RETRY.md)
- [DIRECT_WORKFLOW.md](DIRECT_WORKFLOW.md)
- [EXECUTOR_ACTIVITY.md](EXECUTOR_ACTIVITY.md)
- [PROCESS_SUPPORT.md](PROCESS_SUPPORT.md)

---

Reference case: legacy, Stage disabled.

## Selected route: legacy ownership

One actual implementation owner holds the project's durable legacy slot. Do not
steal it or infer abandonment from elapsed time. `work TASK_ID --resume` is only
an explicit same-task continuation after the earlier execution stopped; it never
authorizes concurrent implementation. Related tasks may share a legacy PR when
the actual complete scope and project controls permit it.

After implementation/self-check, set Review. For an explicit partial handoff,
record branch, PR, checks and remaining outcomes in Specifications, then use
`queue release TASK_ID`. Partial tasks stay Review and never complete a parent.
The implementation author invokes a separate non-implementing Reviewer for the
actual complete issue and exact source head/base. A reviewer who edits code cannot
provide final independent approval. Missing independent review stays a limitation.

```sh
python scripts/task_tracker.py work TASK_ID
python scripts/task_tracker.py set TASK_ID --status review
# Explicit inter-stage handoff only, after recording the real PR and remaining work:
python scripts/task_tracker.py queue release TASK_ID
```

The legacy `queue order`, `queue begin` and `queue next` commands operate on a
frozen To Do selection. Later additions wait. Dependencies override manual order,
priority, entry time and Issue number; `next` never pulls Backlog into that batch.
Read [legacy queue and recovery](WORKFLOW_REFERENCE.md).

Record actual independent source/combined review with `python scripts/task_tracker.py record-review
--pr PR_NUMBER --evidence-file review.json --apply`. The document names distinct
implementer/Reviewer identities, approved verdict, exact head/base/tree and durable
report; execution routes additionally bind the real review lane/session. A command
records a genuine review; it cannot manufacture independence or complete scope.

## Selected integration: Stage disabled

The reviewed working source enters `main`. There is no Stage column, Stage entry, `stage` or
`verify-stage` requirement in this selected route. Preserve every actual review,
composition and CI gate; never manufacture Stage proof. Retain the actual task scope through main.

---

Reference case: legacy, Stage enabled.

## Selected route: legacy ownership

One actual implementation owner holds the project's durable legacy slot. Do not
steal it or infer abandonment from elapsed time. `work TASK_ID --resume` is only
an explicit same-task continuation after the earlier execution stopped; it never
authorizes concurrent implementation. Related tasks may share a legacy PR when
the actual complete scope and project controls permit it.

After implementation/self-check, set Review. For an explicit partial handoff,
record branch, PR, checks and remaining outcomes in Specifications, then use
`queue release TASK_ID`. Partial tasks stay Review and never complete a parent.
The implementation author invokes a separate non-implementing Reviewer for the
actual complete issue and exact source head/base. A reviewer who edits code cannot
provide final independent approval. Missing independent review stays a limitation.

```sh
python scripts/task_tracker.py work TASK_ID
python scripts/task_tracker.py set TASK_ID --status review
# Explicit inter-stage handoff only, after recording the real PR and remaining work:
python scripts/task_tracker.py queue release TASK_ID
```

The legacy `queue order`, `queue begin` and `queue next` commands operate on a
frozen To Do selection. Later additions wait. Dependencies override manual order,
priority, entry time and Issue number; `next` never pulls Backlog into that batch.
Read [legacy queue and recovery](WORKFLOW_REFERENCE.md).

Record actual independent source/combined review with `python scripts/task_tracker.py record-review
--pr PR_NUMBER --evidence-file review.json --apply`. The document names distinct
implementer/Reviewer identities, approved verdict, exact head/base/tree and durable
report; execution routes additionally bind the real review lane/session. A command
records a genuine review; it cannot manufacture independence or complete scope.

## Selected integration: Stage enabled

The working branch enters permanent `stage`, then unchanged verified content is
promoted to permanent `main`. Both PRs carry the same actually reviewed task set. Retain the original
source branches for fixes; never delete permanent branches or discard another
owner's Stage. Only one task set/composition owns Stage at a time.

Run `check-merge` for the actual admitted PR immediately before its GitHub merge.
After the source/combined PR really merges into `stage`, record `stage --pr
PR_NUMBER --apply`, perform the real configured Stage verification and record
`verify-stage --evidence-file stage.json --apply`. Then admit and merge the exact
promotion into `main`. If main/content changes, return fixes to the claimed
original author(s), obtain fresh relevant review/checks and Stage verification;
old evidence cannot approve changed content. Merge/squash requires the reviewed
first parent and identical reviewed resulting tree; rebase merge is unsupported.

---

Reference case: execution 1, Stage disabled.

## Selected route: execution schema 1

There are 2 implementation slots and exactly one
shared review/integration lane. Each actual Worker owns one task in a dedicated
branch/worktree; one source PR contains exactly one task. Its independent Reviewer
retains the same lane through review, author fixes and main integration. Waiting
Review work consumes no implementation slot. Claims use distinct actual neutral
Worker/Reviewer/Coordinator runs and durable sessions; never rename a Worker to
claim independence. GitHub Assignee remains a real account.

Before first use, verify that the explicit execution policy is durably activated
through its inspected idle `execution migrate` preview/apply procedure. Publish
compatible writers/service before a live gate; preserve existing owners, queues
and evidence. Selected config limits grant no capacity until state matches. An
already adopted project does not migrate again merely because work resumes.

Use distinct actual actor/session values and the returned claim; placeholders
below must be replaced with real task, branch, worktree and PR records:

```sh
python scripts/task_tracker.py work TASK_ID --actor Worker-W001 --session ACTUAL_WORKER_RUN --coordinator Coordinator-C001 --branch codex/TASK_BRANCH --worktree WORKTREE_PATH
python scripts/task_tracker.py execution handoff TASK_ID --pr PR_NUMBER --actor Worker-W001 --session ACTUAL_WORKER_RUN --claim ACTUAL_WORKER_CLAIM --report-file handoff.md
python scripts/task_tracker.py execution review-acquire --pr PR_NUMBER --actor Reviewer-R001 --session ACTUAL_REVIEW_RUN --coordinator Coordinator-C001
```

Workers hand off to their Coordinator; the Coordinator dispatches independent
review after the real lane is reserved. Do not use legacy `queue begin/next/release`
or direct `set --status review` as execution handoffs. A separate genuine Reviewer
is mandatory; unavailable tooling is not self-approval. Before any post-handoff
author edit, use the supported owned return, reacquire the original branch/task,
fix, self-check, hand off and obtain fresh relevant independent approval.
Read [the exact selected ownership/recovery contract](PARALLEL_EXECUTION.md).

Record actual independent source/combined review with `python scripts/task_tracker.py record-review
--pr PR_NUMBER --evidence-file review.json --apply`. The document names distinct
implementer/Reviewer identities, approved verdict, exact head/base/tree and durable
report; execution routes additionally bind the real review lane/session. A command
records a genuine review; it cannot manufacture independence or complete scope.

## Selected integration: Stage disabled

The reviewed working source enters `main`. There is no Stage column, Stage entry, `stage` or
`verify-stage` requirement in this selected route. Preserve every actual review,
composition and CI gate; never manufacture Stage proof. Retain the actual task scope through main.

---

Reference case: execution 1, Stage enabled.

## Selected route: execution schema 1

There are 2 implementation slots and exactly one
shared review/integration lane. Each actual Worker owns one task in a dedicated
branch/worktree; one source PR contains exactly one task. Its independent Reviewer
retains the same lane through review, author fixes and main integration. Waiting
Review work consumes no implementation slot. Claims use distinct actual neutral
Worker/Reviewer/Coordinator runs and durable sessions; never rename a Worker to
claim independence. GitHub Assignee remains a real account.

Before first use, verify that the explicit execution policy is durably activated
through its inspected idle `execution migrate` preview/apply procedure. Publish
compatible writers/service before a live gate; preserve existing owners, queues
and evidence. Selected config limits grant no capacity until state matches. An
already adopted project does not migrate again merely because work resumes.

Use distinct actual actor/session values and the returned claim; placeholders
below must be replaced with real task, branch, worktree and PR records:

```sh
python scripts/task_tracker.py work TASK_ID --actor Worker-W001 --session ACTUAL_WORKER_RUN --coordinator Coordinator-C001 --branch codex/TASK_BRANCH --worktree WORKTREE_PATH
python scripts/task_tracker.py execution handoff TASK_ID --pr PR_NUMBER --actor Worker-W001 --session ACTUAL_WORKER_RUN --claim ACTUAL_WORKER_CLAIM --report-file handoff.md
python scripts/task_tracker.py execution review-acquire --pr PR_NUMBER --actor Reviewer-R001 --session ACTUAL_REVIEW_RUN --coordinator Coordinator-C001
```

Workers hand off to their Coordinator; the Coordinator dispatches independent
review after the real lane is reserved. Do not use legacy `queue begin/next/release`
or direct `set --status review` as execution handoffs. A separate genuine Reviewer
is mandatory; unavailable tooling is not self-approval. Before any post-handoff
author edit, use the supported owned return, reacquire the original branch/task,
fix, self-check, hand off and obtain fresh relevant independent approval.
Read [the exact selected ownership/recovery contract](PARALLEL_EXECUTION.md).

Record actual independent source/combined review with `python scripts/task_tracker.py record-review
--pr PR_NUMBER --evidence-file review.json --apply`. The document names distinct
implementer/Reviewer identities, approved verdict, exact head/base/tree and durable
report; execution routes additionally bind the real review lane/session. A command
records a genuine review; it cannot manufacture independence or complete scope.

## Selected integration: Stage enabled

The working branch enters permanent `stage`, then unchanged verified content is
promoted to permanent `main`. Both PRs carry the same singleton task. Retain the original
source branches for fixes; never delete permanent branches or discard another
owner's Stage. Only one task set/composition owns Stage at a time.

Run `check-merge` for the actual admitted PR immediately before its GitHub merge.
After the source/combined PR really merges into `stage`, record `stage --pr
PR_NUMBER --apply`, perform the real configured Stage verification and record
`verify-stage --evidence-file stage.json --apply`. Then admit and merge the exact
promotion into `main`. If main/content changes, return fixes to the claimed
original author(s), obtain fresh relevant review/checks and Stage verification;
old evidence cannot approve changed content. Merge/squash requires the reviewed
first parent and identical reviewed resulting tree; rebase merge is unsupported.

---

Reference case: execution 2, Stage disabled.

## Selected route: batch execution schema 2

There are 2 implementation slots and one bounded
Coordinator integration claim. There is one shared review lane. Source approval releases it; an independent integration review then holds that lane through main. Source review waits while integration review owns it.

A normal source PR contains exactly one task, targets `main` for approval and
stays unmerged. `execution handoff` releases its implementation slot after the
verified Review handoff. `execution review-acquire --pr PR_NUMBER` reserves its
actual independent source lane; `record-review` binds exact head/base/tree, full
requirements and real Reviewer/session/claim. `batch approve` verifies source CI,
records Approved and releases that source lane. Approved is not integration or
completion. Never merge an ordinary approval-only source PR directly.

The authorized Coordinator uses `batch intake` to freeze eligible Approved tasks
and `batch begin --intake INTAKE_ID` to claim a bounded composition of up to
2 members. New approvals wait for a later intake. One
actual independent integration Reviewer uses `batch review-acquire --pr PR_NUMBER`
and `record-review` for the exact combined content and every frozen source tip.
The combined PR and any promotion include the complete identical frozen task set
and its real `Tracker batch: BATCH_ID` line. Singleton source identity never limits
the number of members in a reserved integration/promotion PR.

Before first use, verify that the explicit execution policy is durably activated
through its inspected idle `execution migrate` preview/apply procedure. Publish
compatible writers/service before a live gate; preserve existing owners, queues
and evidence. Selected config limits grant no capacity until state matches. An
already adopted project does not migrate again merely because work resumes.

Use distinct actual actor/session values and the returned claim; placeholders
below must be replaced with real task, branch, worktree and PR records:

```sh
python scripts/task_tracker.py work TASK_ID --actor Worker-W001 --session ACTUAL_WORKER_RUN --coordinator Coordinator-C001 --branch codex/TASK_BRANCH --worktree WORKTREE_PATH
python scripts/task_tracker.py execution handoff TASK_ID --pr PR_NUMBER --actor Worker-W001 --session ACTUAL_WORKER_RUN --claim ACTUAL_WORKER_CLAIM --report-file handoff.md
python scripts/task_tracker.py execution review-acquire --pr PR_NUMBER --actor Reviewer-R001 --session ACTUAL_REVIEW_RUN --coordinator Coordinator-C001
```

Workers hand off to their Coordinator; the Coordinator dispatches independent
review after the real lane is reserved. Do not use legacy `queue begin/next/release`
or direct `set --status review` as execution handoffs. A separate genuine Reviewer
is mandatory; unavailable tooling is not self-approval. Before any post-handoff
author edit, use the supported owned return, reacquire the original branch/task,
fix, self-check, hand off and obtain fresh relevant independent approval.
Read [the exact selected ownership/recovery contract](BATCH_INTEGRATION.md).

Record actual independent source/combined review with `python scripts/task_tracker.py record-review
--pr PR_NUMBER --evidence-file review.json --apply`. The document names distinct
implementer/Reviewer identities, approved verdict, exact head/base/tree and durable
report; execution routes additionally bind the real review lane/session. A command
records a genuine review; it cannot manufacture independence or complete scope.

## Selected integration: Stage disabled

Only the reserved combined batch PR enters `main`. There is no Stage column, Stage entry, `stage` or
`verify-stage` requirement in this selected route. Preserve every actual review,
composition and CI gate; never manufacture Stage proof. Approved and frozen batch admission remain required.

---

Reference case: execution 2, Stage enabled.

## Selected route: batch execution schema 2

There are 2 implementation slots and one bounded
Coordinator integration claim. There is one shared review lane. Source approval releases it; an independent integration review then holds that lane through main. Source review waits while integration review owns it.

A normal source PR contains exactly one task, targets `main` for approval and
stays unmerged. `execution handoff` releases its implementation slot after the
verified Review handoff. `execution review-acquire --pr PR_NUMBER` reserves its
actual independent source lane; `record-review` binds exact head/base/tree, full
requirements and real Reviewer/session/claim. `batch approve` verifies source CI,
records Approved and releases that source lane. Approved is not integration or
completion. Never merge an ordinary approval-only source PR directly.

The authorized Coordinator uses `batch intake` to freeze eligible Approved tasks
and `batch begin --intake INTAKE_ID` to claim a bounded composition of up to
2 members. New approvals wait for a later intake. One
actual independent integration Reviewer uses `batch review-acquire --pr PR_NUMBER`
and `record-review` for the exact combined content and every frozen source tip.
The combined PR and any promotion include the complete identical frozen task set
and its real `Tracker batch: BATCH_ID` line. Singleton source identity never limits
the number of members in a reserved integration/promotion PR.

Before first use, verify that the explicit execution policy is durably activated
through its inspected idle `execution migrate` preview/apply procedure. Publish
compatible writers/service before a live gate; preserve existing owners, queues
and evidence. Selected config limits grant no capacity until state matches. An
already adopted project does not migrate again merely because work resumes.

Use distinct actual actor/session values and the returned claim; placeholders
below must be replaced with real task, branch, worktree and PR records:

```sh
python scripts/task_tracker.py work TASK_ID --actor Worker-W001 --session ACTUAL_WORKER_RUN --coordinator Coordinator-C001 --branch codex/TASK_BRANCH --worktree WORKTREE_PATH
python scripts/task_tracker.py execution handoff TASK_ID --pr PR_NUMBER --actor Worker-W001 --session ACTUAL_WORKER_RUN --claim ACTUAL_WORKER_CLAIM --report-file handoff.md
python scripts/task_tracker.py execution review-acquire --pr PR_NUMBER --actor Reviewer-R001 --session ACTUAL_REVIEW_RUN --coordinator Coordinator-C001
```

Workers hand off to their Coordinator; the Coordinator dispatches independent
review after the real lane is reserved. Do not use legacy `queue begin/next/release`
or direct `set --status review` as execution handoffs. A separate genuine Reviewer
is mandatory; unavailable tooling is not self-approval. Before any post-handoff
author edit, use the supported owned return, reacquire the original branch/task,
fix, self-check, hand off and obtain fresh relevant independent approval.
Read [the exact selected ownership/recovery contract](BATCH_INTEGRATION.md).

Record actual independent source/combined review with `python scripts/task_tracker.py record-review
--pr PR_NUMBER --evidence-file review.json --apply`. The document names distinct
implementer/Reviewer identities, approved verdict, exact head/base/tree and durable
report; execution routes additionally bind the real review lane/session. A command
records a genuine review; it cannot manufacture independence or complete scope.

## Selected integration: Stage enabled

The reserved combined batch branch enters permanent `stage`, then unchanged verified content is
promoted to permanent `main`. Both PRs carry the complete frozen member set and batch ID. Retain the original
source branches for fixes; never delete permanent branches or discard another
owner's Stage. Only one task set/composition owns Stage at a time.

Run `check-merge` for the actual admitted PR immediately before its GitHub merge.
After the source/combined PR really merges into `stage`, record `stage --pr
PR_NUMBER --apply`, perform the real configured Stage verification and record
`verify-stage --evidence-file stage.json --apply`. Then admit and merge the exact
promotion into `main`. If main/content changes, return fixes to the claimed
original author(s), obtain fresh relevant review/checks and Stage verification;
old evidence cannot approve changed content. Merge/squash requires the reviewed
first parent and identical reviewed resulting tree; rebase merge is unsupported.

---

Reference case: execution 3, Stage disabled.

## Selected route: batch execution schema 3

There are 2 implementation slots and one bounded
Coordinator integration claim. There are 2 independent source review slots and one separate integration review lane. Every held lane needs its own actual non-implementing Reviewer run; a source lane cannot release another lane. Source review can continue during integration review.

A normal source PR contains exactly one task, targets `main` for approval and
stays unmerged. `execution handoff` releases its implementation slot after the
verified Review handoff. `execution review-acquire --pr PR_NUMBER` reserves its
actual independent source lane; `record-review` binds exact head/base/tree, full
requirements and real Reviewer/session/claim. `batch approve` verifies source CI,
records Approved and releases that source lane. Approved is not integration or
completion. Never merge an ordinary approval-only source PR directly.

The authorized Coordinator uses `batch intake` to freeze eligible Approved tasks
and `batch begin --intake INTAKE_ID` to claim a bounded composition of up to
2 members. New approvals wait for a later intake. One
actual independent integration Reviewer uses `batch review-acquire --pr PR_NUMBER`
and `record-review` for the exact combined content and every frozen source tip.
The combined PR and any promotion include the complete identical frozen task set
and its real `Tracker batch: BATCH_ID` line. Singleton source identity never limits
the number of members in a reserved integration/promotion PR.

Before first use, verify that the explicit execution policy is durably activated
through its inspected idle `execution migrate` preview/apply procedure. Publish
compatible writers/service before a live gate; preserve existing owners, queues
and evidence. Selected config limits grant no capacity until state matches. An
already adopted project does not migrate again merely because work resumes.

Use distinct actual actor/session values and the returned claim; placeholders
below must be replaced with real task, branch, worktree and PR records:

```sh
python scripts/task_tracker.py work TASK_ID --actor Worker-W001 --session ACTUAL_WORKER_RUN --coordinator Coordinator-C001 --branch codex/TASK_BRANCH --worktree WORKTREE_PATH
python scripts/task_tracker.py execution handoff TASK_ID --pr PR_NUMBER --actor Worker-W001 --session ACTUAL_WORKER_RUN --claim ACTUAL_WORKER_CLAIM --report-file handoff.md
python scripts/task_tracker.py execution review-acquire --pr PR_NUMBER --actor Reviewer-R001 --session ACTUAL_REVIEW_RUN --coordinator Coordinator-C001
```

Workers hand off to their Coordinator; the Coordinator dispatches independent
review after the real lane is reserved. Do not use legacy `queue begin/next/release`
or direct `set --status review` as execution handoffs. A separate genuine Reviewer
is mandatory; unavailable tooling is not self-approval. Before any post-handoff
author edit, use the supported owned return, reacquire the original branch/task,
fix, self-check, hand off and obtain fresh relevant independent approval.
Read [the exact selected ownership/recovery contract](BATCH_INTEGRATION.md).

Record actual independent source/combined review with `python scripts/task_tracker.py record-review
--pr PR_NUMBER --evidence-file review.json --apply`. The document names distinct
implementer/Reviewer identities, approved verdict, exact head/base/tree and durable
report; execution routes additionally bind the real review lane/session. A command
records a genuine review; it cannot manufacture independence or complete scope.

## Selected integration: Stage disabled

Only the reserved combined batch PR enters `main`. There is no Stage column, Stage entry, `stage` or
`verify-stage` requirement in this selected route. Preserve every actual review,
composition and CI gate; never manufacture Stage proof. Approved and frozen batch admission remain required.

---

Reference case: execution 3, Stage enabled.

## Selected route: batch execution schema 3

There are 2 implementation slots and one bounded
Coordinator integration claim. There are 2 independent source review slots and one separate integration review lane. Every held lane needs its own actual non-implementing Reviewer run; a source lane cannot release another lane. Source review can continue during integration review.

A normal source PR contains exactly one task, targets `main` for approval and
stays unmerged. `execution handoff` releases its implementation slot after the
verified Review handoff. `execution review-acquire --pr PR_NUMBER` reserves its
actual independent source lane; `record-review` binds exact head/base/tree, full
requirements and real Reviewer/session/claim. `batch approve` verifies source CI,
records Approved and releases that source lane. Approved is not integration or
completion. Never merge an ordinary approval-only source PR directly.

The authorized Coordinator uses `batch intake` to freeze eligible Approved tasks
and `batch begin --intake INTAKE_ID` to claim a bounded composition of up to
2 members. New approvals wait for a later intake. One
actual independent integration Reviewer uses `batch review-acquire --pr PR_NUMBER`
and `record-review` for the exact combined content and every frozen source tip.
The combined PR and any promotion include the complete identical frozen task set
and its real `Tracker batch: BATCH_ID` line. Singleton source identity never limits
the number of members in a reserved integration/promotion PR.

Before first use, verify that the explicit execution policy is durably activated
through its inspected idle `execution migrate` preview/apply procedure. Publish
compatible writers/service before a live gate; preserve existing owners, queues
and evidence. Selected config limits grant no capacity until state matches. An
already adopted project does not migrate again merely because work resumes.

Use distinct actual actor/session values and the returned claim; placeholders
below must be replaced with real task, branch, worktree and PR records:

```sh
python scripts/task_tracker.py work TASK_ID --actor Worker-W001 --session ACTUAL_WORKER_RUN --coordinator Coordinator-C001 --branch codex/TASK_BRANCH --worktree WORKTREE_PATH
python scripts/task_tracker.py execution handoff TASK_ID --pr PR_NUMBER --actor Worker-W001 --session ACTUAL_WORKER_RUN --claim ACTUAL_WORKER_CLAIM --report-file handoff.md
python scripts/task_tracker.py execution review-acquire --pr PR_NUMBER --actor Reviewer-R001 --session ACTUAL_REVIEW_RUN --coordinator Coordinator-C001
```

Workers hand off to their Coordinator; the Coordinator dispatches independent
review after the real lane is reserved. Do not use legacy `queue begin/next/release`
or direct `set --status review` as execution handoffs. A separate genuine Reviewer
is mandatory; unavailable tooling is not self-approval. Before any post-handoff
author edit, use the supported owned return, reacquire the original branch/task,
fix, self-check, hand off and obtain fresh relevant independent approval.
Read [the exact selected ownership/recovery contract](BATCH_INTEGRATION.md).

Record actual independent source/combined review with `python scripts/task_tracker.py record-review
--pr PR_NUMBER --evidence-file review.json --apply`. The document names distinct
implementer/Reviewer identities, approved verdict, exact head/base/tree and durable
report; execution routes additionally bind the real review lane/session. A command
records a genuine review; it cannot manufacture independence or complete scope.

## Selected integration: Stage enabled

The reserved combined batch branch enters permanent `stage`, then unchanged verified content is
promoted to permanent `main`. Both PRs carry the complete frozen member set and batch ID. Retain the original
source branches for fixes; never delete permanent branches or discard another
owner's Stage. Only one task set/composition owns Stage at a time.

Run `check-merge` for the actual admitted PR immediately before its GitHub merge.
After the source/combined PR really merges into `stage`, record `stage --pr
PR_NUMBER --apply`, perform the real configured Stage verification and record
`verify-stage --evidence-file stage.json --apply`. Then admit and merge the exact
promotion into `main`. If main/content changes, return fixes to the claimed
original author(s), obtain fresh relevant review/checks and Stage verification;
old evidence cannot approve changed content. Merge/squash requires the reviewed
first parent and identical reviewed resulting tree; rebase merge is unsupported.

---

Reference case: modern direct, Stage disabled.

## Selected route: modern direct execution schema4

There are 1 implementation slots,
1 independent source review slots and one integration
reservation. Role, Stage, delivery target and capacity remain separate choices.
Select/adopt execution4 + task-context2 + completion1 as one guarded bundle under
actual owned idle maintenance; config/export grants no capacity until it matches
outer7 state. Optional retry6 stays inside7 without resetting any history/budget.
Do not separately activate a partial modern bundle. Old projects retain their own
selected schema, limits, Stage and choices until their reviewed owned migration.

A tracked source has exactly one real Issue, current semantic readiness, one
actual Worker/session/claim and a dedicated branch/worktree. Use task-context
assess, then work; never set In Progress by editing labels. After checks use
execution handoff, then execution review-acquire with a different real Reviewer.
The source handoff retains its addressable branch/worktree and exact scope/report
through pending writes, fixes and stopped-owner recovery; neither a new claim nor
an old status label releases it. Review-plan names the actual full scope and
head/base/tree/main/policy. Independently inspect it and record-review with the
actual required assessments; a hash or command is not semantic review.

```sh
python scripts/task_tracker.py task-context assess TASK_ID --actor Worker-W001 --session ACTUAL_WORKER_RUN --coordinator Coordinator-C001 --branch codex/TASK_BRANCH --worktree WORKTREE_PATH
python scripts/task_tracker.py task-context assess TASK_ID --actor Worker-W001 --session ACTUAL_WORKER_RUN --coordinator Coordinator-C001 --branch codex/TASK_BRANCH --worktree WORKTREE_PATH --evidence-file readiness.json --apply
python scripts/task_tracker.py work TASK_ID --actor Worker-W001 --session ACTUAL_WORKER_RUN --coordinator Coordinator-C001 --branch codex/TASK_BRANCH --worktree WORKTREE_PATH
python scripts/task_tracker.py execution handoff TASK_ID --pr SOURCE_PR --actor Worker-W001 --session ACTUAL_WORKER_RUN --coordinator Coordinator-C001 --branch codex/TASK_BRANCH --worktree WORKTREE_PATH --claim ACTUAL_WORKER_CLAIM --report-file handoff.md
python scripts/task_tracker.py execution review-acquire --pr SOURCE_PR --actor Reviewer-R001 --session ACTUAL_REVIEW_RUN --coordinator Coordinator-C001
python scripts/task_tracker.py direct review-plan --pr SOURCE_PR --actor Reviewer-R001 --session ACTUAL_REVIEW_RUN --claim ACTUAL_REVIEW_CLAIM
python scripts/task_tracker.py record-review --pr SOURCE_PR --evidence-file review.json --apply
```

The actual source Reviewer uses `python scripts/task_tracker.py direct begin --pr SOURCE_PR` with
that same Reviewer/session/claim. This retains the source lane and acquires the
one serialized integration reservation; it does not create Approved or a batch,
merge, publish or deploy. Other direct/capture/batch/process/Stage work must wait
for that held reservation. Source review may continue within its separate limit.

## Selected integration: Stage disabled

The actually reserved reviewed singleton (or true combined batch) enters main. No Stage entry or receipt is manufactured.

Keep the genuine Reviewer/reservation through actual main, durable accepted-scope
receipt, reconciled real task status and atomic archive/release. The maintained
merged command performs this order; there is no early direct finish command.
Unknown writes/crashes retain ownership and reconcile the same operation. Pending
package/production/publication remains unfinished after a permitted source handoff.
Changed source/scope or real conflicts require the actual owned author return,
fresh work/checks/handoff/review. Unchanged direct sources after forward main
changes use the held independent Reviewer and actual semantic compatibility plus
current native combined-candidate checks; no empty commit or path-disjoint guess.
Read [modern commands, migration and recovery](DIRECT_WORKFLOW.md).

---

Reference case: modern direct, Stage enabled.

## Selected route: modern direct execution schema4

There are 1 implementation slots,
1 independent source review slots and one integration
reservation. Role, Stage, delivery target and capacity remain separate choices.
Select/adopt execution4 + task-context2 + completion1 as one guarded bundle under
actual owned idle maintenance; config/export grants no capacity until it matches
outer7 state. Optional retry6 stays inside7 without resetting any history/budget.
Do not separately activate a partial modern bundle. Old projects retain their own
selected schema, limits, Stage and choices until their reviewed owned migration.

A tracked source has exactly one real Issue, current semantic readiness, one
actual Worker/session/claim and a dedicated branch/worktree. Use task-context
assess, then work; never set In Progress by editing labels. After checks use
execution handoff, then execution review-acquire with a different real Reviewer.
The source handoff retains its addressable branch/worktree and exact scope/report
through pending writes, fixes and stopped-owner recovery; neither a new claim nor
an old status label releases it. Review-plan names the actual full scope and
head/base/tree/main/policy. Independently inspect it and record-review with the
actual required assessments; a hash or command is not semantic review.

```sh
python scripts/task_tracker.py task-context assess TASK_ID --actor Worker-W001 --session ACTUAL_WORKER_RUN --coordinator Coordinator-C001 --branch codex/TASK_BRANCH --worktree WORKTREE_PATH
python scripts/task_tracker.py task-context assess TASK_ID --actor Worker-W001 --session ACTUAL_WORKER_RUN --coordinator Coordinator-C001 --branch codex/TASK_BRANCH --worktree WORKTREE_PATH --evidence-file readiness.json --apply
python scripts/task_tracker.py work TASK_ID --actor Worker-W001 --session ACTUAL_WORKER_RUN --coordinator Coordinator-C001 --branch codex/TASK_BRANCH --worktree WORKTREE_PATH
python scripts/task_tracker.py execution handoff TASK_ID --pr SOURCE_PR --actor Worker-W001 --session ACTUAL_WORKER_RUN --coordinator Coordinator-C001 --branch codex/TASK_BRANCH --worktree WORKTREE_PATH --claim ACTUAL_WORKER_CLAIM --report-file handoff.md
python scripts/task_tracker.py execution review-acquire --pr SOURCE_PR --actor Reviewer-R001 --session ACTUAL_REVIEW_RUN --coordinator Coordinator-C001
python scripts/task_tracker.py direct review-plan --pr SOURCE_PR --actor Reviewer-R001 --session ACTUAL_REVIEW_RUN --claim ACTUAL_REVIEW_CLAIM
python scripts/task_tracker.py record-review --pr SOURCE_PR --evidence-file review.json --apply
```

The actual source Reviewer uses `python scripts/task_tracker.py direct begin --pr SOURCE_PR` with
that same Reviewer/session/claim. This retains the source lane and acquires the
one serialized integration reservation; it does not create Approved or a batch,
merge, publish or deploy. Other direct/capture/batch/process/Stage work must wait
for that held reservation. Source review may continue within its separate limit.

## Selected integration: Stage enabled

The working singleton (or actual combined batch) enters permanent Stage, receives the exact configured Git/runtime verification, then its unchanged subject/members promote to main. Record stage only after the actual admitted merge, verify-stage for the actual target, and check-merge for promotion. Git-only n8n Stage never authorizes n8n runtime changes.

Keep the genuine Reviewer/reservation through actual main, durable accepted-scope
receipt, reconciled real task status and atomic archive/release. The maintained
merged command performs this order; there is no early direct finish command.
Unknown writes/crashes retain ownership and reconcile the same operation. Pending
package/production/publication remains unfinished after a permitted source handoff.
Changed source/scope or real conflicts require the actual owned author return,
fresh work/checks/handoff/review. Unchanged direct sources after forward main
changes use the held independent Reviewer and actual semantic compatibility plus
current native combined-candidate checks; no empty commit or path-disjoint guess.
Read [modern commands, migration and recovery](DIRECT_WORKFLOW.md).

---

Reference case: modern batch, Stage disabled.

## Selected route: modern batch execution schema4

There are 1 implementation slots,
1 independent source review slots and one integration
reservation. Role, Stage, delivery target and capacity remain separate choices.
Select/adopt execution4 + task-context2 + completion1 as one guarded bundle under
actual owned idle maintenance; config/export grants no capacity until it matches
outer7 state. Optional retry6 stays inside7 without resetting any history/budget.
Do not separately activate a partial modern bundle. Old projects retain their own
selected schema, limits, Stage and choices until their reviewed owned migration.

A tracked source has exactly one real Issue, current semantic readiness, one
actual Worker/session/claim and a dedicated branch/worktree. Use task-context
assess, then work; never set In Progress by editing labels. After checks use
execution handoff, then execution review-acquire with a different real Reviewer.
The source handoff retains its addressable branch/worktree and exact scope/report
through pending writes, fixes and stopped-owner recovery; neither a new claim nor
an old status label releases it. Review-plan names the actual full scope and
head/base/tree/main/policy. Independently inspect it and record-review with the
actual required assessments; a hash or command is not semantic review.

```sh
python scripts/task_tracker.py task-context assess TASK_ID --actor Worker-W001 --session ACTUAL_WORKER_RUN --coordinator Coordinator-C001 --branch codex/TASK_BRANCH --worktree WORKTREE_PATH
python scripts/task_tracker.py task-context assess TASK_ID --actor Worker-W001 --session ACTUAL_WORKER_RUN --coordinator Coordinator-C001 --branch codex/TASK_BRANCH --worktree WORKTREE_PATH --evidence-file readiness.json --apply
python scripts/task_tracker.py work TASK_ID --actor Worker-W001 --session ACTUAL_WORKER_RUN --coordinator Coordinator-C001 --branch codex/TASK_BRANCH --worktree WORKTREE_PATH
python scripts/task_tracker.py execution handoff TASK_ID --pr SOURCE_PR --actor Worker-W001 --session ACTUAL_WORKER_RUN --coordinator Coordinator-C001 --branch codex/TASK_BRANCH --worktree WORKTREE_PATH --claim ACTUAL_WORKER_CLAIM --report-file handoff.md
python scripts/task_tracker.py execution review-acquire --pr SOURCE_PR --actor Reviewer-R001 --session ACTUAL_REVIEW_RUN --coordinator Coordinator-C001
python scripts/task_tracker.py direct review-plan --pr SOURCE_PR --actor Reviewer-R001 --session ACTUAL_REVIEW_RUN --claim ACTUAL_REVIEW_CLAIM
python scripts/task_tracker.py record-review --pr SOURCE_PR --evidence-file review.json --apply
```

Tracked engineering stays approval-only into main. Use batch approve to record
real Approved and release that source lane, then the genuine Coordinator intake
and frozen batch begin with up to 2 real members. One member
is still a batch. Its actual integration Reviewer is a subrole of the single
reservation, independent of every implementer. The combined PR and promotion
retain every frozen member and the real Tracker batch line. Never replace those
members with a singleton or use direct begin for an ordinary batch source.

## Selected integration: Stage disabled

The actually reserved reviewed singleton (or true combined batch) enters main. No Stage entry or receipt is manufactured.

Keep the genuine Reviewer/reservation through actual main, durable accepted-scope
receipt, reconciled real task status and atomic archive/release. The maintained
merged command performs this order; there is no early direct finish command.
Unknown writes/crashes retain ownership and reconcile the same operation. Pending
package/production/publication remains unfinished after a permitted source handoff.
Changed source/scope or real conflicts require the actual owned author return,
fresh work/checks/handoff/review. Unchanged direct sources after forward main
changes use the held independent Reviewer and actual semantic compatibility plus
current native combined-candidate checks; no empty commit or path-disjoint guess.
Read [modern commands, migration and recovery](DIRECT_WORKFLOW.md).

---

Reference case: modern batch, Stage enabled.

## Selected route: modern batch execution schema4

There are 1 implementation slots,
1 independent source review slots and one integration
reservation. Role, Stage, delivery target and capacity remain separate choices.
Select/adopt execution4 + task-context2 + completion1 as one guarded bundle under
actual owned idle maintenance; config/export grants no capacity until it matches
outer7 state. Optional retry6 stays inside7 without resetting any history/budget.
Do not separately activate a partial modern bundle. Old projects retain their own
selected schema, limits, Stage and choices until their reviewed owned migration.

A tracked source has exactly one real Issue, current semantic readiness, one
actual Worker/session/claim and a dedicated branch/worktree. Use task-context
assess, then work; never set In Progress by editing labels. After checks use
execution handoff, then execution review-acquire with a different real Reviewer.
The source handoff retains its addressable branch/worktree and exact scope/report
through pending writes, fixes and stopped-owner recovery; neither a new claim nor
an old status label releases it. Review-plan names the actual full scope and
head/base/tree/main/policy. Independently inspect it and record-review with the
actual required assessments; a hash or command is not semantic review.

```sh
python scripts/task_tracker.py task-context assess TASK_ID --actor Worker-W001 --session ACTUAL_WORKER_RUN --coordinator Coordinator-C001 --branch codex/TASK_BRANCH --worktree WORKTREE_PATH
python scripts/task_tracker.py task-context assess TASK_ID --actor Worker-W001 --session ACTUAL_WORKER_RUN --coordinator Coordinator-C001 --branch codex/TASK_BRANCH --worktree WORKTREE_PATH --evidence-file readiness.json --apply
python scripts/task_tracker.py work TASK_ID --actor Worker-W001 --session ACTUAL_WORKER_RUN --coordinator Coordinator-C001 --branch codex/TASK_BRANCH --worktree WORKTREE_PATH
python scripts/task_tracker.py execution handoff TASK_ID --pr SOURCE_PR --actor Worker-W001 --session ACTUAL_WORKER_RUN --coordinator Coordinator-C001 --branch codex/TASK_BRANCH --worktree WORKTREE_PATH --claim ACTUAL_WORKER_CLAIM --report-file handoff.md
python scripts/task_tracker.py execution review-acquire --pr SOURCE_PR --actor Reviewer-R001 --session ACTUAL_REVIEW_RUN --coordinator Coordinator-C001
python scripts/task_tracker.py direct review-plan --pr SOURCE_PR --actor Reviewer-R001 --session ACTUAL_REVIEW_RUN --claim ACTUAL_REVIEW_CLAIM
python scripts/task_tracker.py record-review --pr SOURCE_PR --evidence-file review.json --apply
```

Tracked engineering stays approval-only into main. Use batch approve to record
real Approved and release that source lane, then the genuine Coordinator intake
and frozen batch begin with up to 2 real members. One member
is still a batch. Its actual integration Reviewer is a subrole of the single
reservation, independent of every implementer. The combined PR and promotion
retain every frozen member and the real Tracker batch line. Never replace those
members with a singleton or use direct begin for an ordinary batch source.

## Selected integration: Stage enabled

The working singleton (or actual combined batch) enters permanent Stage, receives the exact configured Git/runtime verification, then its unchanged subject/members promote to main. Record stage only after the actual admitted merge, verify-stage for the actual target, and check-merge for promotion. Git-only n8n Stage never authorizes n8n runtime changes.

Keep the genuine Reviewer/reservation through actual main, durable accepted-scope
receipt, reconciled real task status and atomic archive/release. The maintained
merged command performs this order; there is no early direct finish command.
Unknown writes/crashes retain ownership and reconcile the same operation. Pending
package/production/publication remains unfinished after a permitted source handoff.
Changed source/scope or real conflicts require the actual owned author return,
fresh work/checks/handoff/review. Unchanged direct sources after forward main
changes use the held independent Reviewer and actual semantic compatibility plus
current native combined-candidate checks; no empty commit or path-disjoint guess.
Read [modern commands, migration and recovery](DIRECT_WORKFLOW.md).

## Retained legacy operations and recovery

Legacy queue selection uses `queue order`, `queue begin`, `queue next` and
`queue show`, and explicit `queue release TASK_ID` after a documented partial
handoff. Execution policies use claims/handoff and their selected review lanes
instead. Legacy `timing start/stop --kind reviewing` with a real Reviewer/session,
or `--kind merging` with a real Coordinator/session, observes actual activity and
grants no ownership/approval/capacity. Waiting Review is not actual review; stale
clocks freeze and unknown old durations are never fabricated. Do not change a
consumer pin merely to show a clock.

All adoption, stopped-owner takeover, staging-only reconciliation, native delivery,
post-main acceptance and legacy historical recovery procedures remain in the
linked local contracts. Compatibility is selected explicitly, not inferred from
a reference example or an installed skill. No source edits, deploys, state repairs
or operational messages are authorized by reading this reference.
