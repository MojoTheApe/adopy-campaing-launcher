# Delivery policy contract v1

Explicit `completion_policy: {"schema_version": 1}` adds accepted-scope completion
and protocol-5 activation. Read [the completion contract](COMPLETION_POLICY.md).
Source/export support does not activate existing projects. All keeps unfinished
Merged tasks visible; historical acceptance does not claim Delivered.

Optional [task context](TASK_CONTEXT.md) adds explicit requirements protocol `v2:`
and independent source compatibility under matching execution v2/v3. New scope
ignores only the permitted terminal journal. Old hashes/receipts keep whole-body
meaning; shared delivery fingerprints and actual runtime gates are unchanged.
Outer state 4 fences incompatible writers. Existing activation is a separate
official-config/owned-idle operation; installing this engine grants no adoption.

Execution policy schema 2 keeps this delivery evidence contract and uses
[Approved tasks and frozen batches](BATCH_INTEGRATION.md) for admission/ownership.
Its source PRs target main for independent approval and stay unmerged; only the
reserved combined branch enters configured Stage or main. Stage and promotion include the frozen
batch ID as well as the exact task set. Legacy/v1 routes below remain in force.

The optional [parallel execution contract](PARALLEL_EXECUTION.md) extends this
policy with a repository-wide review/integration owner. In that mode review
evidence also identifies its lane/session, and all tasks (including report-only
work) follow the independently reviewed PR route. Legacy behavior below remains
unchanged when `execution_policy` is absent.

Execution schemas 2/3 also support explicitly disabled staging at a compatible
pin, preserving Approved and all frozen combined-review gates. Read the current
[batch contract](BATCH_INTEGRATION.md#optional-stage-and-authorized-delivery--tt-127).
The direct-main route records a real admitted `main_batches` receipt and never
pretends Stage was tested. Target/completion/required checks remain explicit.

This is an opt-in extension to tracker config schema 3 (also accepted in schema 2).
The Master Repo template generates this block from its project descriptor:

```json
{
  "schema_version": 1,
  "staging": {"enabled": true, "branch": "stage", "target": "example/stage"},
  "completion": "verified-production",
  "delivery_target": "example/production",
  "required_checks": ["descriptor", "tracker-link", "tests"]
}
```

Store it as `delivery_policy` in `tracker/config.json`. `project.base_branch` is
main. Completion is `reviewed-merge`, `verified-production` or
`verified-stable-package`. Targets identify actual configured environments or the
local repository, not secrets. Required check names are exact GitHub Actions job
names. The latest run for every required name must pass on the reviewed PR head.
Use one semantic `stage` column between review and merged iff staging is enabled.
The application derives columns from config, not from a universal fixed board.

## Commands and evidence

Use the project client `python scripts/task_tracker.py COMMAND`, or in the engine
checkout `python -m tracker --config PROJECT_CONFIG COMMAND`. Evidence commands
preview by default; add `--apply` to persist. Evidence is written to the project's
existing `tracker-state` branch using the shared compare-and-swap store, not to the
published site. Reports/history/targets are excluded from public task snapshots.
Repository readers can still read the state branch; never place secrets in reports.

1. `check-pr N`: validate links, classifications, route and naming.
2. `record-review --pr N --evidence-file review.json --apply`: record the actual
   independent review after reading it. It must name distinct agent identities,
   current head/base/tree SHAs, verdict and a durable report:

   ```json
   {"kind":"independent-review","implementer":"author-session","reviewer":"review-agent-session",
    "head_sha":"40-hex-sha","base_sha":"40-hex-sha","tree_sha":"40-hex-sha",
    "verdict":"approved","report":"Durable review report reference"}
   ```

   The tool binds this to the task set and policy. Identity strings are an
   attestation by the running agent, not cryptographic proof of independence.
3. `check-merge N`: read-only admission immediately before the GitHub merge.
   The command does not merge. Keep normal GitHub branch protections enabled and
   re-check if the head/base changes. Post-merge commands verify actual Git parents.
4. For staging, merge the source PR, then `stage --pr N --apply`. After actually
   deploying/testing stage, `verify-stage --evidence-file stage.json --apply`:

   ```json
   {"kind":"stage-verification","result":"passed","target":"example/stage",
    "source_sha":"actual-stage-merge-sha","tree_sha":"actual-stage-tree-sha",
    "checks":["Actual smoke/behavior check and result"],"report":"Durable result reference"}
   ```

   `failed` records a failure. A retry of the same candidate preserves its proof;
   a changed candidate needs new proof. Create stage -> main, link the same tasks,
   run `check-merge`, and merge. The main parent and resulting content must match
   the passing stage receipt. No extra product ticket is needed for promotion.
5. `merged EX-12 --pr N --reviewed-complete --apply`: existing completeness and
   ancestry checks plus the new admission evidence. Records Merged only for fully
   delivered code scope; it does not deploy or claim production success.
6. Where delivery is required, run the project's maintained release procedure,
   verify the configured target, then `record-delivery EX-12 --evidence-file
   delivery.json --apply`:

   ```json
   {"kind":"delivery-verification","result":"passed","target":"example/production",
    "merge_shas":["actual-admitted-main-merge-sha"],
    "checks":["Installed version and intended behavior verified"],"report":"Durable result reference"}
   ```

   Use `failed` for a failed attempt. The receipt covers all code merges for each
   supplied task and must match its adopted policy. Repeating merge recording
   preserves an already verified receipt. Release/dependency/UI completion all use
   the same evidence; a Merged label alone cannot bypass it. Non-code `complete`
   still requires its existing Research/Operations classification and verified
   result; it is not an alternative for code implementation.

## New project registration

Export the small client, not the Tracker application:
`export-template --output NEW_DIR --repository owner/repo --name "Project"
--id-prefix EX --base main --engine-revision FULL_SHA --delivery-policy policy.json`.
Run client `init` for project labels. Prepare the shared registry change with
`register-project --workspace workspace.json --project-config NEW_DIR/tracker/config.json
--id example --apply`. Repeating identical registration is safe; conflicts in
identity/prefix/config fail without replacing existing registration. Review and
publish that registry change through the Tracker's maintained release procedure.
Verify the project appears with the expected board and successful fresh sync before
claiming it is connected. A local registry write is not live publication.

## Adoption and compatibility

Do not turn this on implicitly during an engine upgrade. Audit historical IDs,
labels, PRs, completion and release state first. Existing open PRs finish under the
old rules before activation; the migration must report them as blockers. Existing
completed tasks need an explicit evidence reconciliation plan before opting in;
otherwise the stricter policy correctly shows their completion as unverified.
Preserve Issues, release history, custom modules and local deployment controls.
Changing target, checks or completion changes the policy fingerprint, so old proof
cannot authorize a changed policy. Pair any contract change with a Master Repo
impact assessment and a linked compatibility change.

### Staging-only policy migration

`delivery-policy migrate --previous-config ORIGINAL.json --previous-revision
FULL_SHA --report-file cutover.md` previews a narrow staging toggle. It verifies
the exact original config from the official immutable repository revision and
its ancestry to current main. Every non-Stage setting, task identity, execution
capacity, delivery target, completion and required check stays equal. The Stage
target may change to/from the explicitly disabled identity. New configs retain
all historical-completion tasks/provenance, with only that manifest's policy
changed in the explicitly reviewed candidate diff. This command does not edit
config, create approvals or modify Issues/PRs.

Publish compatible engine writers and the single snapshot service before live
adoption. Reserve actual owned maintenance, drain Workers/Reviewers, pending
operations, running intake/batch and unpromoted Stage using their real owners.
Queued Review and Approved entries may remain; the preview verifies their exact
durable queue membership. Preserve the original config and a private state copy.
After the exact reviewed candidate config is published on current main, add
`--apply --actor Maintainer-XXXX --session ACTUAL_RUN --claim MAINTENANCE_ID`.
The operation requires idleness, actual maintenance ownership and shared-file
SHA compare-and-swap. It keeps the fence until supported maintenance resume.
Coordinate writer/config/service agreement before reopening admission.
One preview/apply audit memoizes unchanged read-only GitHub proofs per snapshot
and distinct admitted PR. It checks main again before the write; a concurrent
state change still fails CAS instead of reusing proof across separate commands.

Original delivery receipts and archived batches remain intact. An additive
audited reconciliation binds old/new fingerprints, immutable previous config,
exact current Issue scope, original receipt digest and original admitted main
source/tree identities. Original verification/target/report and pending/failed
state are preserved. Genuine prior success can remain complete under the same
delivery requirements; pending never becomes verified. Changed/reopened scope
cannot borrow the audit. A later actual delivery retains this reconciliation in
history and records fresh current-policy evidence. Reports/claims/reconciliation
details remain private; the public projection contains only the effective policy
and scope digest needed for truthful completion.

Historical pre-batch pending package records may be preserved as unfinished
history instead of being re-admitted as modern batches. The migration recognizes
only the original minimal `policy`/`sources`/`state`/`recorded_at` pending format,
with original official merged stage-to-main PR metadata and no
modern batch/request/admission binding or batch PR marker. Their original receipt
bytes and old policy stay intact; they receive no policy reconciliation, current
delivery authority or completion. The private `retained_legacy_pending` audit
lists each task, original source identities, receipt digest and retention reason.
Legacy v1 review/promotion metadata alone does not make a source a modern batch.
This corroborates only the historical PR identity; it does not reconstruct old
tree/ancestry admission or verify the retained source evidence. Each distinct old
PR metadata read is memoized within this one inspected snapshot.
Any verified/process/non-code record, verification/history/reconciliation data,
known current or archived batch membership, or malformed batch marker retains
the full existing proof requirements. Missing compatible verification is a
reported limitation, never a reason to demote success or silently skip admission.

Closed-completed historical Issues without any status label can remain unchanged
only when they are absent from every durable task/receipt, ownership, queue,
approval, batch/request, historical-completion and retained task-history binding.
Open or tracked work with no status remains an error. No Issue is relabeled,
reopened, archived or made complete. The private `retained_untracked_closed`
audit records the unchanged historical Issue scope and reason, with no admission,
delivery or completion receipt. This retention-only audit is excluded from the
ownership scan on later Stage toggles; genuine task history remains binding.
An old `todo_added` sort timestamp is preserved as non-authoritative metadata;
actual Todo labels, `todo_order`, runs and ownership remain binding. It does not
make a closed historical Issue a current task. Missing or unsupported
identity-container shapes fail closed.

For this bounded operator repair, use a clean independently reviewed merged
TT-129/TT-131 engine checkout with the exact reviewed consumer config and the normal
`delivery-policy migrate` preview/apply commands above. Existing e775-compatible
writers and snapshot readers accept the additive private audit unchanged. No
portable helper, client/config pin or service publication changes for this repair;
permanent engine upgrades remain separate reviewed work. Do not patch a cached
runtime or migrate another consumer merely as a test.

An existing explicit delivery request can also carry through this audit when
its full archived main batch and every exact task receipt reconcile together.
Its original permission/producer/target remains intact; the additive audit binds
the old/new policies, original request digest, main PRs and exact source heads.
This lets the old-route adoption batch finish its already authorized publication
after the new staging policy activates. A missing, cancelled, partial or changed
request is never created or broadened by migration.

Queued Worker handoffs keep their order and owners. Old Approved proof is
preserved as history and cannot enter a new-policy intake. Its recorded
Coordinator uses normal `batch return` to the original author, followed by a
fresh claim/handoff/current-policy independent review/approval; no work is
silently approved or discarded. An already applied migration is idempotent.
Old configs are rejected by the recorded policy marker. Re-enabling Stage uses
the same procedure, including prior Stage reconciliation to current main before
the next batch. Other delivery-policy changes need their own explicit design.

New execution-state adoption records its exact delivery-policy fingerprint.
Preexisting unmarked execution-v2/v3 state predates optional Stage and is accepted
only with its historically supported enabled Stage until the inspected toggle
records that marker. Passing a no-Stage config or combining a delivery change
with execution-capacity migration cannot initialize or bypass it. Genuine old
staged and execution-v1 consumers retain their supported compatibility route.

### Retrospective legacy package confirmation

The separate explicit [`legacy-delivery` operator](LEGACY_DELIVERY_RECONCILIATION.md)
can recover only audited original minimal pending pre-batch records retained by
the genuine staging-only migration. It requires new owner authorization, exact
current task scope, independent full-scope completion and actual official native
signed package installation proof. Migration retention alone grants no authority.
It preserves original producer task scope and original receipts/history and uses
one selected-row CAS without stopping unrelated work. Preview is read-only;
missing evidence or unfinished business acceptance stays pending. This operator
changes no exported helper, ordinary admission/projection or consumer pin.

### Authorized native delivery continuation

After the owner's one integration-and-delivery instruction, the actual
Coordinator uses `batch begin --deliver --delivery-workflow PATH --delivery-job
EXACT_NAME` as documented in the batch contract. Ordinary source/main PRs and
batches created without this request never acquire delivery authorization.
The Coordinator still verifies complete scope and records the real admitted
main merge with ordinary `merged`; an automatic producer cannot approve tasks
or invent this handoff. The project owns the release/deploy, target acceptance,
retry and rollback implementation. Tracker supplies portable request/evidence
validation, not a generic command executor.

The exported recorder needs the same repository token with Actions/Issues read
and contents write, plus the existing portable policy helpers:

```sh
python scripts/tracker_delivery_receipt.py resolve --repository OWNER/REPO \
  --merge-sha MAIN_MERGE_SHA --ci-run SUCCESSFUL_MAIN_CI_RUN --output request.json
python scripts/tracker_delivery_receipt.py record --request request.json \
  --evidence delivery.json --verification-run RUN_ID --verification-job JOB_ID --apply
```

Resolve checks the official successful native main CI at that revision, actual
archived authorized batch/main PR, exact task set and per-task Merged receipts.
No matching authorized batch is ineligible; a retained/unfinished Coordinator
handoff returns temporary exit 75 so a project can use bounded retries or an
explicit resume. A pending release does not need another owner approval.

The exact configured trusted native verification workflow/job must finish
successfully and its real job log must contain one
`TRACKER_DELIVERY_RECEIPT <compact JSON>` matching the supplied passed
`delivery-verification`: exact configured target, sole admitted merge SHA,
actual acceptance checks and durable report. The project emits that line only
after its real installation/deploy/runtime acceptance. An uploaded arbitrary
manifest is not native success. The recorder re-resolves authorization/tasks,
checks exact official producer/job/revision/log content and persists through
CAS. Same-proof retries are idempotent; cancelled requests, wrong producers,
other revisions/targets, incomplete task handoffs, changed policy and replacing
existing verified proof fail closed. It uses no private-engine access secret.
Partial native reruns may reuse a successful verifier from an earlier actual
attempt when only downstream receipt recording reruns. The recorder inspects
native attempt membership and requires the latest actual configured verifier;
any newer verifier supersedes older proof, including failed, skipped or running
verification. Projects retain request/evidence artifacts from their real producer
attempts instead of deriving their names from the latest overall run attempt.

See [TT-127](https://github.com/MojoTheApe/task-tracker/issues/127) and its
Master companion [MR-28](https://github.com/MojoTheApe/master-repo/issues/28).

### Owner-approved historical completion

An adoption may preserve an explicitly audited set of completed tasks instead of
inventing old delivery receipts. This is optional and never enabled by an engine
upgrade. With the owner's recorded approval, use the candidate config and original
pre-delivery config to propose a manifest:

```sh
python -m tracker --config candidate/tracker/config.json audit-history \
  --legacy-config original/tracker/config.json --source-revision ORIGINAL_FULL_SHA \
  --approved-by "Owner decision in adoption issue" --report "Durable audit reference"
```

This reads current Issues and writes nothing. Review its complete task list and
place only `historical_completion` in the candidate project config and matching
registry fallback config. Commit through the adoption PR. The manifest records
repository, exact target policy, original config hash/revision, audit date, owner
approval reference, report and each task's public ID and accepted-scope hash.
Attestations are reviewed records, not cryptographic proof of owner identity.
Never regenerate or extend the list automatically at cutover or an engine upgrade;
report changed tasks and obtain a new explicit reconciliation decision if needed.

Only original completed, unblocked, unarchived tasks qualify. A new/incomplete
task, changed body/title/identity/state or changed functional/workflow labels cannot
borrow that acceptance. Release membership, priority, milestone and comments do
not change accepted scope; normal release rollover therefore preserves history.
Restarting a listed task through `work` permanently supersedes historical acceptance
before the status write. Direct GitHub changes are compared with the audited state;
the snapshot reader cannot detect an unobserved edit that was fully reverted.
Use a follow-up ticket for new work, or `work` after explicitly reopening with its
new scope. Any delivery record, including failed/revoked/old-policy records,
supersedes the historical exception.

The UI badge says **Done · Accepted before adoption** for accepted historical
Merged tasks that are still complete. Their always-visible task details say
**Completed before adoption** and explain the absence of current-policy delivery
verification. **Delivered** is reserved for actual verified delivery. This is a
completion display label;
the underlying state stays `historical`, distinct from `verified`, with no new
receipt. Such tasks count in completion and dependencies,
but produce no review, stage, merge or installation evidence. Admission commands
do not consult this exception. Release publication still requires the explicit
`--published` confirmation and all existing scope, metadata and dependency checks.
Unlisted work follows every new requirement. A policy change invalidates the
manifest and requires explicit reconciliation; it is not silently grandfathered.

## Optional instruction-only process route

The separate top-level `process_instructions` setting is mirrored exactly as
`delivery.json.workflow.process_instructions`. It requires the independent-review
delivery policy and never changes its fingerprint or inherits on export. Read
[PROCESS_INSTRUCTIONS.md](PROCESS_INSTRUCTIONS.md) for the fixed eligible paths,
complete Git proof, actual process check names, independent ownership and exact
merged evidence. Eligible one-task sources target main and complete from their
verified process merge receipt; ordinary changes keep this document’s delivery
flow. Legacy uses its actual task slot and review attestation; execution-v1/v2/v3
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
