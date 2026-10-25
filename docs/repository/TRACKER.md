# Selected Tracker workflow

Repository: `MojoTheApe/adopy-campaing-launcher`; integration branch: `main`;
public task prefix: `ACL`. These instructions describe only this project's
selected config. Read [../tracker/config.json](../../tracker/config.json), this guide, AGENTS.md and any
project-owned AGENT_CONTEXT.md before work, including a resumed chat. GitHub Issues
are the task source of truth; the web workspace is read-only. Keep product/runtime,
installers, LIVE_FIRST, deployment authorization, backups and rollback safeguards.

`python scripts/task_tracker.py guide` renders this selected path from the exact pinned engine.
[All supported routes and recovery](WORKFLOW_REFERENCE.md) are
local reference material, not additional active routes. Engine/skill installation
or export never adopts a policy, changes capacity or grants delivery permission. The exact shared engine revision is recorded in [tracker/engine.json](../../tracker/engine.json).
Master owns common normative policy; Tracker owns commands, parser and executable
semantics; the project owns product/runtime knowledge. Fetching latest upstream
instructions is unnecessary. Engine commands require Python 3.11+; the exported
read-only helpers retain their declared portable runtime floor.

## Read, classify and claim the task

Every development request, including an immediate fix, needs a real Issue and
verified In Progress before code. Read its complete Description, Specifications,
original requirements, checklists, relevant code/decisions, related completed work
and dependencies. Assess current relevance at start/resume; age or disjoint paths
alone prove neither freshness nor semantic compatibility. Unchanged authorized
work proceeds without a routine extra owner approval; material outcome or
unresolved authorization changes need their decision.

The agent authors a plain-English `## Description` explaining the purpose and
expected result, followed by `## Specifications` with verified context, behavior,
scope, constraints, acceptance criteria, checks and current handoff. Put technical
commands/paths/revisions and dated history in Specifications. Read both sections
in full. Use the [task template](../../tracker/task-template.md) and `--body-file` for every new
`create`/`work --title`; empty/placeholder sections are rejected. An Idea may state
known facts and open decisions. Preserve original body, IDs, labels, criteria and
history; check concurrent edits before writes. Retrospective rewrites first need
a reviewed sample and exact preservation of original requirements.

Planning creates classified Idea/Backlog work; owner-selected To Do does not
start implementation. New accepted backlog work defaults to Medium priority.
Select one configured module by product outcome and one Type (Bug, Feature,
Improvement, Research or Operations); Operations is not a generic module. Module
creation uses maintained `module add` with stable boundaries/examples/reason.
Resolve ambiguous classification before implementation. Design, Blocked and
explicit Release membership are independent flags; High priority implies no
Release inclusion. Preserve project IDs, offsets, legacy aliases and Issue URLs;
foreign prefixes are rejected and PR numbers are not Issue IDs.

Use `work TASK_ID` for existing immediate work; new work uses `work --title TITLE
--body-file task.md --module MODULE --type TYPE`. An uncertain Issue creation is
journaled before POST and must be reconciled, never blindly repeated. Do not edit
code if tracking/start could not be verified. Investigation may continue. A
changed scope/new outcome belongs in its existing clarified task or a new task as
appropriate; do not duplicate semantic work. After the actual claim, use an isolated
working branch/worktree from current `main`. The default branch
is `codex/public-id-lowercase-short-name`; opted-in execution also supports `work/`,
subject to the actual host/user branch rule. PR titles identify the real task ID(s),
with exactly one task in an execution source and all frozen members in a reserved
integration/promotion. Keep the original branch for owned author fixes.

Use one explicit `needs: TASK_ID`, `needs: #NUMBER` or Issue URL per dependency.
External/unverified references remain unresolved. Record an actual blocker with
`set TASK_ID --blocked --reason "Actual obstacle and required resolution"`, keep
history and remove the flag when resolved. Never invent a blocker or infer an
external task's completion from a same-numbered local Issue. Check actual scope
and dependency completion before claiming new work; already started dependents
are not silently rolled back by this guide.

## Selected route: batch execution schema 3

There are 5 implementation slots and one bounded
Coordinator integration claim. There are 5 independent source review slots and one separate integration review lane. Every held lane needs its own actual non-implementing Reviewer run; a source lane cannot release another lane. Source review can continue during integration review.

A normal source PR contains exactly one task, targets `main` for approval and
stays unmerged. `execution handoff` releases its implementation slot after the
verified Review handoff. `execution review-acquire --pr PR_NUMBER` reserves its
actual independent source lane; `record-review` binds exact head/base/tree, full
requirements and real Reviewer/session/claim. `batch approve` verifies source CI,
records Approved and releases that source lane. Approved is not integration or
completion. Never merge an ordinary approval-only source PR directly.

The authorized Coordinator uses `batch intake` to freeze eligible Approved tasks
and `batch begin --intake INTAKE_ID` to claim a bounded composition of up to
5 members. New approvals wait for a later intake. One
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

## Deliver the process separately from the product

This rule applies regardless of installation/deployment profile. Instructions and
proven isolated task/workflow tooling have their own checks and delivery; a consumer
installer, application deploy or n8n publication is not inherited from the profile
for a process-only outcome. Product/build/install/update changes retain applicable
checks and actual product delivery. Split mixed scope; a label or path is not proof.

For canonical published support upgrades, use the read-only `process-support
PR_NUMBER` command and its genuine independent full-scope assessment. Unknown content
or scope is not a waiver. Keep ordinary source/batch/Stage/main admission and checks;
only the real admitted main outcome can establish process completion. Do not request
consumer product delivery for a proven process-only outcome. Preserve explicit
publication scope and any existing requested delivery. Schema1 instruction admission
is unchanged. For isolated manual verification tooling, the same command previews
its bounded candidate proof; after ordinary admitted main completion, an actual
independent assessor supplies `--evidence-file` and the exact successful official
manual workflow run. Preview before `--apply`. This supplements retained source
review/acceptance without taking over another developer or rewriting historical
proof. Workflow syntax alone never proves isolation; unknown/mixed effects and
any genuine publication request retain product delivery.
Read [process support](PROCESS_SUPPORT.md).

## Read the workspace lifecycle

Review contains unfinished review. Actual approved waiting work appears in Delivery
as Ready for integration; its native approved key and ownership remain unchanged.
Integrating means the current held composition/Stage/main operation. Awaiting Release
requires a genuinely pending required publication after main acceptance. Queue
length, occupied slots and confirmed executor activity are separate facts. Waiting
age comes from the actual current approval timestamp, never from Issue updates or
an active-work clock. Missing history remains unknown. The UI grants no admission;
use the existing current source/scope/main/dependency and delivery checks.

A genuine author return waits in Backlog as Needs fixes (native To Do). Only its
actual implementation claim shows Fixing in In Progress. Fresh handoff ends that
correction cycle and requires current independent Review, then the selected
Delivery and Done gates. Explicit Paused wins; stop/takeover/resume retain genuine
origin without inventing running activity. Old missing origin stays neutral.
Publication-only failure remains in Delivery; an integration-only finding does
not return every frozen source member. Source corrections use the selected owned
author-return command and preserve frozen intake, Stage and pending delivery.

The active Coordinator may continue corrections of the owner's already selected
task set when actual capacity/admission allow, without another routine approval.
A free slot does not launch an agent or restart a stopped run. New unselected
work, explicit pause resumption and materially changed outcomes retain their
actual owner decisions. Source approval alone does not authorize integration;
already authorized integration includes the actual required delivery. No scheduler,
new order/limit, automatic merge or live policy activation is introduced.

## Pause without losing the current phase

A confirmed stopped executor uses `pause stop TASK_ID --kind implementing|reviewing|merging`
with its actual current identity/claim, `--stopped` and a durable `--report-file`;
preview then apply. The task displays Paused in its saved category and releases
only that executor slot. Keep native phase, branch/worktree, source/history,
frozen selection, requested delivery and real blockers. Silence means Connection
lost after prior confirmation (otherwise Activity unconfirmed); it never frees
ownership. Resume explicitly with the saved pause ID, a genuine available slot
and fresh authority after current scope/source/dependency checks. A frozen batch
Coordinator resumes with a genuinely new run/session before its combined Reviewer.
Readers and all writers must be verified compatible before owned idle `pause activate`;
a pin/publication alone does not activate outer8. Never run this migration while
old writers or actual owners are active. See [task pause](TASK_PAUSE.md).

## Selected instruction-only exception

Only the pinned complete-diff classifier can prove the eligible one-task
instruction-only route. It targets `main`, retains its
genuine task/author ownership and independent exact head/base/tree/scope review,
and runs the exact process checks. It bypasses ordinary Stage/package delivery
only for actual eligible proof; mixed automation/config/runtime/build changes keep
the ordinary route. A batch-mode process source uses its real source Reviewer lane
through main and never enters Approved/intake/batches. Held Stage/intake/integration
and pending exact-target delivery still block conflicting admission. A post-main
acceptance edit needs fresh independent review under retained ownership.
Read [instruction-only eligibility](PROCESS_INSTRUCTIONS.md).

## Verify, integrate and finish the actual outcome

Every implementation PR has exactly one machine-readable `Tracker issues:` line
containing only its real local task IDs/Issue URLs. Use the selected PR template
and actual source/batch membership rules above. Do not use automatic Issue-closing
keywords in PR text or commits. Keep branch/PR links, full scope, exact head/base,
checks and remaining work in the Issue's Specifications.

Use `check-pr PR_NUMBER` for early linkage feedback. Use `check-merge PR_NUMBER`
once for final admission immediately before merge; it includes linkage and all
actually adopted review/CI/Stage gates. A link check alone does not authorize a
merge. Run required tests and actual branch protections; accurately report
unavailable hosted checks and apply only the existing project's exception rules.
A new head/base/task set/policy needs fresh relevant independent approval. No
self-review, timeout, elapsed age or stale check can substitute.

After the actual main merge, preview `python scripts/task_tracker.py merged TASK_IDS --pr
MAIN_PR_NUMBER`; apply only after every selected task criterion is genuinely
reviewed/complete with `--apply --reviewed-complete`. Use the full frozen membership
for a reserved batch, and assess every legacy task separately. Partial milestones
never finish a parent. Retain owned post-main acceptance recovery when original
criteria remain unchecked; do not edit a frozen checkbox as a scope workaround.
Read the retained-reviewer record-acceptance procedure in the local delivery contract.

Main merge is pending completion until the actual stable package release and installation/acceptance verification. A source tag or upload alone is insufficient. Required target: `ACL stable Windows installer and update channel`. Pending/failed delivery stays unfinished. A merge never publishes,
installs or verifies an external business result. With actual integration/delivery authorization, the Coordinator persists batch begin --deliver with the exact real native workflow/job; no merge/push can invent this permission. The project-owned producer and exported delivery recorder verify the unchanged authorized target/revision.

Actual verified non-code Research/Operations uses `complete --verified-non-code`
only where legacy ownership permits it. Execution-enabled projects use their
independently reviewed report PR; non-code resolution cannot bypass review or CI.
Archive/restore preserve task status/history; cancelled or archived work is not
completion. Release membership uses `--release`/`--no-release`; actual publication
rollover is previewed with `release close --name NAME` and requires
`--apply --published` only after a genuinely completed nonempty scope. Interrupted
rollover resumes its original journal; release history is separate from Archive.

Reread actual Issue labels and verify the next successful project snapshot after
workflow operations. If stale/delayed, report verification pending and inspect
service evidence; do not rewrite labels or rerun CI merely to force refresh.
Preserve last-good data and project isolation. CAS conflicts/uncertain writes
require fresh inspection and supported same-owner recovery; never overwrite
other owners, discard history, mutate private state or claim release by timeout.
Read [delivery and exact evidence](DELIVERY_POLICY.md) and
[maintenance/recovery](MAINTENANCE_AND_CI_EVIDENCE.md).

## Confirm actual executor activity

Held ownership is not a heartbeat. Read [executor activity](EXECUTOR_ACTIVITY.md)
for the shared `activity report` and foreground `activity run --exec` commands.
Only the actual owner/session/claim reports its running, waiting or stopped state.
Report once a minute while running and explicitly before waiting. Five minutes
without confirmation freezes the work clock and shows activity unconfirmed;
it never frees a claim. Do not run a detached heartbeat or impersonate another
executor. Missing producers and historical gaps remain unknown. Installing a
pin does not restart existing agents or select a new execution policy.

The separately labeled current-stage clock includes waiting, not measured work.
A recorded new stage starts its clock; explicit pause freezes it and supported
resume continues it. Backlog has no clock; missing entry facts stay unknown.
Activity loss does not restart a stage or release ownership. Confirmed
Work/Review/Merge totals remain in task details. Compatible successful owned
launch/resume commands seed only that caller's first report; minute renewal still
requires the actual executor or its real foreground child wrapper.
