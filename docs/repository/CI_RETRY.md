# CI accounting and bounded full retries

Version3.5.0 candidate, TT-152; publication and actual project activation remain
separate. Read [portable artifacts](PORTABLE_CONTRACT.md), [native proof](CI_REUSE.md)
and [owned maintenance](MAINTENANCE_AND_CI_EVIDENCE.md). The current project keeps
its actual config until an explicit reviewed selection and owned activation.

## Read-only accounting first

`python scripts/tracker_ci_accounting.py --repo OWNER/REPO --from YYYY-MM-DD
--to YYYY-MM-DD` prints the human report; `--json` emits report2. `--snapshot FILE`
replays a saved schema1 GET observation without contacting GitHub. Generic live
capture uses `--save-snapshot FILE --project-root CHECKOUT` (use `--help` for exact
flags). Raw captures belong outside that checkout, use mode0600, cannot overwrite
an existing file and must never be committed. The project adapter may inject its
repository, actual checkout and exact orchestration names; an injected capture
root cannot be overridden. Reporting has no retry/state/engine activation.

The one canonical stdlib core is also exported in separated layout under
`.workflow/tools/tracker_ci_accounting.py`, with its compatible scripts forwarder.
A released accounting artifact may be mechanically copied with its own source
commit/helper SHA provenance while a project retains its current whole-workflow
pin. This does not forge a new engine manifest or require task-context adoption.

Native job IDs/attempts alone do not establish new actual execution. Match full
native interval/runner/step and supplied matrix facts; preserve multiplicity in
the same attempt. Changing duplicate IDs across GET pages is incomplete coverage,
and no version of that conflicting ID is a proven interval/size. Unknown coverage
has no global upper estimate. No matrix is guessed from display names. Skips do
not prove reuse. Created-run grouping differs from actual execution-period
grouping; capped queries are partitioned or explicitly incomplete. Report latency,
platform, actual PR/batch links and orchestration separately. Artifact/cache sizes
and retention are observations, never deletion requests. Rounded interval
estimates, including synthetic212 versus112, are not invoices. Unsourced rates,
included usage, discounts and per-job net charges remain unknown; no fixed Windows
money multiplier is used. No report daemon or automatic billing lookup exists.

## Explicit policy and protocol

Select exactly `ci_retry_policy: {"schema_version":1,"max_executions":2}` in Tracker
config, mirrored exactly by Master `workflow.ci_retry`. Values1 and2 are supported:
1 means original only,2 means original plus at most one retry. Both true integer
fields are mandatory; no missing-field default, boolean equivalence or inherited
selection is accepted. Export uses `--ci-retry-policy FILE`; absence stays absent.
Local config, the explicit retry policy file and official adoption config reject
duplicate JSON keys, including duplicate policy/schema/limit fields; last-key-wins
never hides ambiguous selection. Valid legacy config still needs no retry field.

Activate only after publication of compatible writers and actual readers/service.
Outer state6 wraps retained1/2/4/5 state; maintenance3 retains base6. Completion,
context, owners, receipts and every retry history/intent survive. An old reader
refuses both6 and3/base6. All bound readers/writers require the same repository
and exact policy. Selection is never adoption. New independent policies must be
selected and activated sequentially, in separate reviewed configs; activate
already selected completion/context/execution before adding retry. Do not weaken
a fence to migrate multiple new selections together.

For an existing Stage-off v2/v3 package project, deliver the one-policy selection
batch under its genuine operative baseline before reserving maintenance. Follow
the exact [selection package contract](DELIVERY_POLICY.md#one-policy-selection-package-before-activation);
it neither relaxes other pending deliveries nor authorizes retry dispatch. Check
actual reader/writer and complete last-good compatibility with wire6 first.
Context/completion-only activation does not require selecting retry. Existing
retry episodes, the first adoption anchor and unknown intents survive later
separate context/completion selections without a new budget.

```sh
python scripts/task_tracker.py maintenance reserve --actor Maintainer-ID --session ACTUAL_RUN --report-file safe-public-maintenance.md --apply
python scripts/task_tracker.py ci-retry adopt --actor Maintainer-ID --session ACTUAL_RUN --claim ACTUAL_CLAIM --report-file safe-public-maintenance.md
python scripts/task_tracker.py ci-retry adopt --actor Maintainer-ID --session ACTUAL_RUN --claim ACTUAL_CLAIM --report-file safe-public-maintenance.md --apply
python scripts/task_tracker.py maintenance resume --actor Maintainer-ID --session ACTUAL_RUN --claim ACTUAL_CLAIM --report-file safe-public-maintenance.md --apply
```

For a limit change, reserve maintenance with the currently matching old config,
drain every owner/unfinished native intent, commit the reviewed new exact main
config, then owned `ci-retry adopt` previews/applies the new limit without deleting
episodes/causes/history. Lowering to1 disables new retries without discarding
consumed budget. Removing selection or downgrading wire6 by hand is unsupported.
Maintenance reports must be safe in the repository; a branch is not private
storage merely because the board hides it.

The first genuine `adopted` history record fixes the permanent governance
boundary. Policy edits, restarts and re-selection cannot move it. This is an
explicit opt-in boundary, not a claim to reconstruct every historical execution.
Current/prior strict candidates in an episode, retained observations and intents
remain counted even before adoption; an old original's attempts are never reset.
Other runs can be classified as pre-governance only with terminal native state,
valid native updated_at and a complete latest-attempt job listing proving all
executed jobs completed strictly before that first boundary. Native skipped jobs
are no execution and never reuse proof. Missing/ambiguous/empty completion facts
remain held. A run created years ago but rerun after adoption still counts.

## Plan and separate operator attestation

The full author client owns `ci-retry show|plan|adopt|diagnose|retry|reconcile|resolve`.
Portable CI stays read-only and engine-free. Mutations need `--apply` and real
neutral Maintainer/session credentials. Authenticated GitHub `/user` and current
repository maintain/admin permission are checked for operator attestation and
dispatch; an alias, environment login, token role or installation token alone
does not prove a review. Built-in maintain is GitHub role_name=maintain with
legacy permission=write; unknown/custom roles are unsupported here.

Start `ci-retry plan --run RUN_ID [--pr PR] --evidence-file plan.json`. The exact
plan is schema1 with `closure_paths`, `input_paths`, `profile`, `environment` and
`unrelated_workflows`. Paths are explicit unique repository files (1..100 each).
Include the original workflow and all relevant reusable jobs/scripts/actions/
producer configuration in closure; include every relevant verification input.
Every closure path is a mandatory budget input. Explicit unrelated workflow
exclusions also belong in that complete reviewed closure, with their relevant
scripts/actions; changed closure bytes cannot retain an old exclusion.
Environment contains only platform, runner_labels, container and runtime facts,
without arbitrary variables/secrets. Profile is a stable identifier. Exact
reviewed unrelated workflow path→SHA256 exclusions are optional (use `{}`);
equivalent workflow bytes always count even after a rename.

Plan reads actual immutable files, native jobs/runner labels, original actor,
run/attempt/SHA/ref and current head/base/tree/parents/scope/composition. A PR must
retain its original native base/head snapshot, still matching the actual current
synthetic candidate. Missing original identity or changed base/head stops: a rerun
retains original SHA/ref and triggering privileges. Main must be the actual current
main SHA. A genuine fix needs normal new-candidate verification, not a rerun of an
old run relabelled as new. No retry record becomes CI proof/admission. The actual
original PR checkout needs the existing trusted native producer. Direct main
events can also use an enforced
narrow static JSON workflow convention: immutable default checkout first in
every actual native job, no custom ref/reusable/dynamic inherited context.
Ordinary YAML/custom contexts stop without trusted actual original context;
an event HEAD alone does not prove which inputs executed. The operator still
reviews all subsequent effects and complete transitive inputs; static syntax
does not certify arbitrary shell behavior.

Use the returned exact identity and specification in a separate review JSON:
`verdict:"verification-only"`, `business_effects:[]`, an explicit category, and
nonempty concrete assessments `workflow_and_reusables`, `scripts_actions_permissions`,
`rerun_conditions`, `targets_and_credentials`, `disposable_verification`,
`relevant_inputs`, `report`. Then `ci-retry diagnose --run RUN_ID [--pr PR]
--evidence-file review.json --actor Maintainer-ID --session ACTUAL_RUN [--apply]`.

This is explicitly **operator-attested** review of the actual full workflow and
transitive effects, not machine certification, an inferred human review or static
proof that arbitrary shell is safe. Rerun-sensitive conditions matter: a skipped
business release job may become active on rerun. Refuse mixed test/business
workflows in version1; do not select only their tests. Isolated disposable
installer build/install/uninstall verification with synthetic inputs is allowed.
Customer-host updates, production credentials/targets and real deploy/publish
effects are excluded. Logs/annotations remain untrusted causal text even if the
CheckRun app is github-actions. Failure/startup_failure/403 does not diagnose
transient infrastructure, billing or rate limiting.
Native outcomes use supported typed conclusions and positive integer actor IDs
or explicit unknown. Malformed objects/booleans never become failure evidence or
match a principal ID; unknown actor identity cannot attribute a dispatch.

Categories are product, transient-infrastructure, configuration, account
(billing/access/quota), unknown. Automatic eligibility uses an existing exact
transient attestation. Unknown/permanent causes stop. Persistent unresolved causes
survive unrelated heads, renamed plans/profiles/causes and different-platform
success. Reviewed strict identity changes require fresh assessment, not new
budget. Same relevant inputs retain their episode across run IDs, empty commits,
new Issues/chats and plan names. A new genuine relevant-input fix needs an actual
changed input set plus an attested `fix_relation` to the previous episode with
kind=relevant-fix, verified_change and semantic_reason. History/lineage remains.
Unknown relations stop rather than replenishing budget.

Upgrading these helpers also changes the canonical ci_evidence.py producer bytes,
even though emit output is preserved. An existing selected native profile must
refresh its actual producer copy and immutable producer_files blob hashes through
its own reviewed source PR, including workflow/policy references as applicable.
Master's ordinary upgrade preserves selected policy values structurally; it does
not establish current native producer readiness. Old producer hashes/proofs cannot
be relabelled as the new exact producer, and no compatibility allowlist is added.

State may be public. Persist only allowlisted structural native facts/digests and
neutral ownership credentials. Raw reports, instruction text, private source/chat
references and local evidence stay in restricted files outside Git. Digests bind
reviewed input; they do not prove the diagnosis correct. `show` prints a sensitive
operational ledger for the actual author and is not a public board projection.

## Dispatch, native reconciliation and uncertainty

`ci-retry retry --episode ID --diagnosis ID --actor Maintainer-ID --session ACTUAL_RUN`
previews; `--apply` is a real full rerun request. Version1 exposes no partial,
failed-job, transient, force or budget-reset flag. Existing ACL coverage adapters
do not grant partial dispatch here. Unsupported/mixed-attempt coverage stays fresh.

Actual external native executions consume the episode too. The canonical created
history query partitions the provider's1000-result cap from the actual repository
creation boundary, with at most256 listing GETs. Missing/capped/changing history
refuses automatic dispatch. Historical PR and direct events need proven original
checkout inputs, never merely event HEAD or today's PR merge. Retained exact
candidates and the existing trusted immutable native producer can establish them.
A reviewed identical static JSON default-checkout closure also supports direct
events. Unproved historical input relations consume conservative held budget
instead of freeing a slot. An applied refused retry persists newly observed
outside attempts. The explicit pre-governance boundary above excludes only
otherwise unretained, proved finished work; it never rolls forward with elapsed
time or a new episode. A verified relevant fix retains its ancestor's budget and
causes, while proved different inputs do not charge that old episode into the
new fix. Unsupported checkout contexts after adoption can conservatively exhaust
the new episode; trusted original context is required to distinguish them.
Delayed/empty job lists never free accepted/unknown intent budget. Job interval
deduplication in the report is separate from this conservative dispatch budget.

CAS first reserves a unique intent, then crosses a durable dispatch barrier. Only
the invocation whose barrier CAS has just succeeded may send one native POST.
The same owner/restart that only observes its barrier remains GET-only. Recheck
actual candidate, policy, native attempts, outside executions and state immediately
before POST. A human can still race direct Actions access; this is not a global
at-most2 lock or a technical prevention of human requests.

Use `ci-retry reconcile --episode ID [--apply]` for native GET reconciliation.
Accepted POST plus its exact attributable native actor/attempt can settle an
intent; a lost reply remains dispatch_unknown even if an equivalent external
attempt appears. Crashes after barrier, ambiguous reservation/barrier PUT and
failed outcome saves never authorize a blind repeat. Elapsed time, success text,
cause resolution or instruction cannot release an uncertain crossed barrier.

The actual stopped reservation owner may use `reconcile --episode ID --intent ID
--stopped --report-file stopped.md --actor Maintainer-ID --session ACTUAL_RUN
[--apply]` only while the durable state is still reserved, before any barrier.
Fresh native reconciliation and CAS preserve the proof of no POST authority.
Once crossed/ambiguous, it stays held. All native POST errors including403 remain
unknown in the current adapter; it has no method-specific provider no-enqueue
proof. Wire state rejected_no_effect is reserved for future documented supported
evidence, not an available status/log-based recovery path.

## Relevant resolution and explicit instruction

`ci-retry resolve --episode ID --evidence-file resolution.json --actor Maintainer-ID
--session ACTUAL_RUN [--apply]` retains prior cause/history and never refills the
unchanged budget. A resolution identifies cause, actual run/pr, exact identity,
report and relevant_resolution. Kind=native-success needs matching workflow,
inputs/environment and actually executed successful native jobs; skipped/empty
coverage or smaller/different-platform success cannot clear the incident.
Kind=verified-change also requires actual changed relevant inputs and an attested
verified_change; source edits alone cannot certify account/billing repair.

Alternatively kind=specific-instruction is a separate operator attestation of an
**actual specific direct human instruction**, never a claim of machine-certified
human origin. Include the exact verification-only review above and instruction
episode, action=one-full-verification-rerun, causes=[CAUSE_ID,...], source, time,
message, specific_decision, acknowledged_causes={CAUSE_ID:CATEGORY,...} and
basis=actual-direct-human-instruction. IDs are unique actual retained causes;
the category map must match each exactly. For one cause, acknowledged_cause=CATEGORY
also works. Repeated diagnoses keep every cause/history: explicitly acknowledge
all blockers in one record. An omitted or newly introduced cause still blocks.
The operator must have inspected that actual bounded source/message and explicit
authorized action. A general commission, "continue" or a privileged caller's flag
is insufficient. Only source/message/decision digests are retained. Retry cites
the returned durable diagnosis ID once, bound to this exact current native
attempt/strict identity with remaining budget. It neither clears the cause
globally nor settles any unknown intent, and grants no business publication.

## Fast/full checks and release boundary

Tracker's draft fast-feedback checks syntax only, in addition to full verify.
The existing verify check keeps all Python/JavaScript/browser coverage for every
source PR (including unknown/mixed drafts) and every main push. Readiness still
requires successful full coverage of the exact candidate; a draft→ready label
change alone does not start another heavy suite for unchanged covered inputs.
Metadata linkage retains its readiness event. No new complete-diff fast-only
classifier or savings claim is introduced. Same-PR superseded runs may cancel; main/source/
integration identities stay distinct. Record why another heavy run is needed and
group corrections; fast/skipped checks never satisfy full verification.

Source conformance self-exports both layouts, runs actual engine-free Python3.9
CLI/state checks and proves the old released Master1.2.0 trusted record refuses
this changed manifest. It makes no positive paired-publication claim. Final
acceptance uses Master's one maintained harness with --retry-contract plus the
portable/selected/bounded/require-pinned flags after actual Tracker release, with
genuine previous released Master1.2.0/Tracker3.4.0 upgrade origins. Publish exact
compatible Master and full canonical skill mirror next. Actual reader/service
compatibility and explicit owned consumer activation remain separate outcomes.
Synthetic races/crashes/native/accounting data are never actual receipts or paid
demonstrations. ACL's first adapter, ordinary-work before/after evidence and any
blocked consumer adoption stay explicitly tracked; a source PR alone cannot
complete all of Task05.
