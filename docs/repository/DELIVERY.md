# ACL delivery contract

Read [ADOPTION.md](ADOPTION.md) for actual cutover evidence and the one-time
activation gate before any live operation. Main/stage and the shared board exist;
the descriptor remains draft because the pinned standard has no published release.

`delivery.json` is the project-specific delivery description. `tracker/config.json`
is the matching Tracker configuration. The standard and engine use exact commit
pins; neither automatically follows main. Populate actual procedures and verify
registration before changing adoption from draft to ready.

Execution schema 2 explicitly selects Review -> Approved -> Stage -> Merged.
Independent source approval releases the review lane; one separate Coordinator
integration claim covers a frozen batch of 1..batch_limit tasks. The recommended
limit is five, independent of Worker capacity. Preserve each member's author,
source PR/current approval and scope; verify the combined stage candidate before
unchanged promotion. Ship-all snapshots intake and processes sequential batches,
rechecking remaining approvals after each main change. A single task may ship
without filling the batch. Execution-v1 commands remain in force only through the guarded source cutover;
follow the old pinned guide for that route. Completion and runtime authorization remain project-specific.

## Path from request to completion

Idea -> Backlog -> To Do are planning steps under the owner's direction. An
explicit development request starts In Progress and a ticket branch. The author
implements, self-checks and creates the linked PR, then sets Review. A separate
subagent reviews; fixes stay on the same branch and require fresh approval.
Execution schema 2 keeps one task per source PR, independently reviewed into
Approved. Approval releases source review ownership; it does not merge or deliver.
A separate Coordinator freezes intake and one batch of up to five. Assemble exact
approved source tips in the reserved integration branch, acquire independent
combined review, run complete applicable checks and merge only that candidate
into permanent stage. All members become Stage and share exact-tree target
verification. Promote the same members/content into main, then record per-task
Merged. Preserve each source Worker/Reviewer and source branch. See
[BATCH_INTEGRATION.md](BATCH_INTEGRATION.md) for current commands and guarded adoption.

The source-adoption child completes its existing execution-v1 stage/main route
before live state changes. Existing legacy/v1 consumers keep their pinned lifecycle.
No direct source merge, added promotion fix, stage reset or silent member removal
is allowed. After stage entry retain the claim and same-member rebuild through
actual author fixes, independent review and fresh verification. Package delivery,
release authorization, installation/update and recovery stay separate.

Run the pinned client's guide, `check-pr N`, `record-review`, `check-merge N`,
`stage`, `verify-stage` and `merged ... --reviewed-complete --apply` at the matching
lifecycle steps. Commands reserve/record evidence; they do not run review or deploy.
Exact-content CI reuse is allowed only for applicable proven jobs. Actual server
branch protection must be verified separately from agent admission.

Merged records inclusion in main. A local manual-pull project completes at that
verified merge. VPS/n8n/package profiles require their actual delivery and checks,
recorded with `record-delivery`. Failed or pending delivery remains unfinished.
The explicitly selected n8n-source profile completes the Git source task after
the admitted main merge. n8n transfer/publication is a separate process and must
never be claimed from Git evidence. Existing runtime acceptance criteria survive
adoption. For repository-only work, label stage evidence as Git checks; consult the project runtime guide when runtime verification is required;
branch-to-environment connections require their own explicit setup.
A partially implemented ticket stays active. Never substitute a closed Issue or
non-code completion for missing code/delivery evidence.

## Procedure references

Use `docs/runtime/DELIVERY.md` for actual environment identities, access,
release authorization, rollback and backup. The descriptor links those procedures. A disabled
stage is an explicit project choice. Production delivery is a separate operation
using the project's maintained mechanism; Git storage gives no new deploy access.
For n8n distinguish workflow publication from server upgrade. For local manual
pull, no installer or automated update mechanism is required.

## Onboarding proof

Record the shared workspace project ID and a durable verification reference after
labels, branch configuration, registry publication and a fresh live board have been
verified. A generated directory or local registry edit is not a connected project.
Document pending setup honestly. Never put credentials or sensitive logs in these
files or in published Tracker evidence.
