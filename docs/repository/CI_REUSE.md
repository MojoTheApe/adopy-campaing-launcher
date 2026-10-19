# Shared native CI planning and gate

TT-140 / Master MR-32 implement WFO-05–08. Optional policy 1 retains its supported
source/integration behavior. Explicit policy 2 additionally supports conditional
fixed jobs and retained exact-main coverage. Neither policy changes review,
required GitHub checks, owned composition, Stage or actual delivery acceptance.

## Configure actual supported jobs

Use the fixed profile described in MAINTENANCE_AND_CI_EVIDENCE.md. Mirror it as
Master `workflow.ci_evidence`. Policy 2 uses the same profile fields, with
`schema_version: 2`. Static JSON workflows may give fixed jobs `if`/`needs`:
collection still requires the actual native job and every named verification
step to succeed. All other immutable-container, producer, toolchain, input,
metadata and lifetime rules remain. Dynamic/matrix/host-dependent or installer
jobs stay fresh. Gate/planner jobs are not product jobs in the profile.

Configure an exact required aggregate (for example `Verification`) that runs the
maintained gate after every product job, with `if: always()`. Jobs that can be
skipped cannot also remain required native names expecting success. Preserve
existing protection until a reviewed config/CI upgrade explicitly selects the
aggregate. Never globally reinterpret skipped/cancelled checks as success.
The gate must itself succeed; coverage output alone cannot satisfy admission.

Publish reviewed policy on official main under the old maintained checks before
activating its conditional workflow/protection. A source-local policy cannot grant
trust. Compatible writers/projection must support the policy before live adoption.
Agent onboarding fills actual commands/inputs; owners need not invent metadata.

## Plan, run missing jobs, gate

```sh
python scripts/task_tracker.py ci-evidence plan --profile checks --pr ACTUAL_PR --head SOURCE_HEAD --context auto --github-output "$GITHUB_OUTPUT"
python scripts/task_tracker.py ci-evidence gate --profile checks --pr ACTUAL_PR --head SOURCE_HEAD --context auto --run "$GITHUB_RUN_ID"
```

Use each product job's condition to run when its exact name is in the plan's JSON
`missing` output. Run on a failed planner as well, then let final gating report the
real proof/infra error. `--context auto` selects the actual frozen integration
route where applicable, ordinary source otherwise. Promotion still runs fresh.
For main push, omit `--pr` (or use zero), pass the actual merged SHA as `--head`;
plan/gate resolves its unique exact merged PR. An unknown/raw push runs all jobs
fresh. Every native main push keeps its own concurrency group/run.

The gate revalidates retained coverage and the actual current native run, attempt,
job/check linkage and fresh fixed-job context. Every missing job must succeed.
Failed/cancelled new work cannot be masked by old success. A skipped job needs
current genuine coverage; absent/ambiguous/pending jobs fail. Infrastructure errors
cannot become a green aggregate. No arbitrary uploaded coverage JSON is accepted.

After successful source/integration/promotion CI, retain the actual run before
merge with normal `ci-evidence collect --profile checks --run RUN --source-pr PR
--apply`. Collection remains native and CAS-safe. Gate does not collect its own
unfinished run or manufacture durable evidence. Private logs/claims stay private.

## Retained main proof

Main reuse requires stored native proof collected before merge. Historical
revalidation verifies its actual synthetic checkout and ordered original base/head,
the official unchanged source and actual merge's first parent/tree, current scope,
policy, workflow/helper, environment/inputs, native attempt/steps and lifetime.
Protocol 2's direct producer also binds the actual native Git ref in its timestamped
step log. GitHub may empty the run's PR list after merge; only this historical
empty-list case uses that exact original PR ref with all original parent/tree/head
checks. A conflicting list, open source without native membership, or missing/wrong
producer ref cannot establish coverage. It preserves the original record. Ordinary `collect` still rejects closed sources.

The actual main merge must have identical tested content. Stage promotion can use
the original stage PR only when its exact merge is the promotion head and Issue
membership matches; otherwise missing checks run fresh. Commit-sensitive profiles
require the identical tested commit, usually making main fresh. Different source
trees never prove a combined candidate. Keep independent combined review and
the admitted main/Stage provenance even when tests can be reused.

## Final admission and delivery

Run supported `check-merge` once immediately before integration; it includes
linkage, current review, required checks and configured Stage. Keep `check-pr` for
early feedback. Reviewers inspect author/CI evidence and add tests for findings,
missing coverage or uncertainty rather than blindly repeating passing suites.

Runtime, installer, package publication, manual/release acceptance and delivery
receipts remain independent and fresh. Plan/gate are read-only and cannot complete
a task, authorize deployment, mutate ownership or waive unresolved delivery.
Policy 1, absent-policy consumers and old engine pins retain their semantics.
