# Accepted-scope completion

`completion_policy: {"schema_version": 1}` explicitly selects scoped completion.
Master descriptors mirror it as `workflow.completion_policy`. Engine installation,
export and descriptor selection alone never activate a project. Projects without
this selection and activation retain their adopted completion semantics, including
legacy whole-body evidence. The maintained registry has not been opted in by this
source change.

Independent review freezes each Issue's requirements scope. Main completion
retains the reviewed scope, repository/Issue identity, admitted PR merge/tree,
original review revision/tree, delivery policy and required target. Frozen batch
members supply their original scope. Promotion uses the original Stage source
review. Completion never hashes today's body as an earlier review. Sources jointly
completing one task must have accepted the same scope. An identical source/policy
retry retains the original receipt, including scope, history and verification.

The same completion assessment serves task projection, dependency admission,
release readiness and delivery recording. Merged work with changed requirements
is unfinished; it does not satisfy a new dependency. Pending and failed delivery
remain separate. Unknown accepted scope is unverified after activation. A fresh
successful acceptance of new scope can complete a task again. No command here
auto-reopens an Issue or rolls back already started dependents. Archive retains
its existing behavior.

`task_context_policy` scope v2 excludes only the exact valid terminal Tracker
journal. Old unprefixed hashes retain the conservative whole-body contract even
in a v2 project. A malformed journal invalidates that Issue's assessment without
hiding unrelated tasks. Changes to title, classification or requirements still
invalidate current completion. Historical owner acceptance retains its explicit
manifest and is shown as **Done · Accepted before adoption**; it makes no current
Delivered claim.

## Publication and activation

Publish a compatible snapshot reader and all compatible writers first. Check the
project's exact engine pin, service revision and exported portable recorder. Keep
nonactivated projects listed explicitly in the rollout report. Commit the matching
config/descriptor in the project, preserving its existing delivery/execution
policy and historical allowlist. Then use an actual Maintainer's owned reservation
and drain existing owners; elapsed time never releases a claim.

For an existing package project, finish the selection source's already authorized
package **before** reserving maintenance. The bounded
[one-policy package continuation](DELIVERY_POLICY.md#one-policy-selection-package-before-activation)
derives its operative prior config from the genuine frozen batch baseline; it
does not relax ordinary admission or activation. Pending/failed target delivery
blocks completion adoption even after source ownership is released.

For an existing execution-v2/v3 project adopting both `task_context_policy` and
`completion_policy`, use **two separate reviewed config selections and two
activations**. Either activate task context first, then select and activate
completion; or select completion alone, activate it, then select and activate task
context. Never select both policies together while neither is activated: each
adoption requires the other selected policy already to match durable state, so
compatible commands refuse this combination before writing anything. Complete
and verify the first activation, resume its maintenance reservation, then make
the second reviewed config change and use a new owned reservation. Each step
keeps the genuine current-main config, receipts, queues and historical manifests.
New projects may initialize their selected execution/task-context topology
through `execution migrate` before the separate completion adoption below.

```sh
python -m tracker maintenance reserve --actor Maintainer-EX01 --session activation-example --report-file report.txt --apply
# Preserve the returned claim; use it for all following operations.
python -m tracker completion adopt --actor Maintainer-EX01 --session activation-example --claim CLAIM --report-file report.txt
python -m tracker completion adopt --actor Maintainer-EX01 --session activation-example --claim CLAIM --report-file report.txt --apply
python -m tracker maintenance resume --actor Maintainer-EX01 --session activation-example --claim CLAIM --report-file report.txt --apply
```

The dry run lists original, reconstructed and unverified scopes. Reconstruction
uses only retained matching original review/batch proof. It never consults the
current Issue body, manufactures historical acceptance or expands a manifest.
Unknown receipts and history remain intact. New projects initialize their selected
execution topology first, then perform this same explicit completion adoption.
Legacy single-slot projects use the same owned adoption without changing their
execution topology.

Activation uses outer private-state protocol 5, retaining base protocol 1, 2 or 4.
Owned maintenance remains outer 3 with retained base 5. Older engines reject this
state before they can acquire a current CAS revision; compatible readers and
writers reject config/state/repository mismatches. Public projection exposes only
policy and repository activation, never reports or private owner identities.

Manual and native/portable delivery verify the accepted scope before recording.
They preserve scope when materializing an audited policy reconciliation, before
removing its temporary chain. A CAS conflict requires a fresh read. An uncertain
successful write is resumed against durable original proof; it never reaccepts the
current Issue body. If an Issue changes after the last read, the frozen scope stays
unchanged and the next fresh projection/admission rejects it.

## Export and validation

`export-template --completion-policy FILE` selects the policy explicitly; export
never inherits it from the engine project. The export includes this document and
its existing portable `tracker_delivery_policy.py`, `tracker_task_context.py`,
`tracker_pr_check.py`, `tracker_batch_policy.py`, `tracker_process_instructions.py`,
`tracker_ci_evidence.py` and `tracker_delivery_receipt.py`. The adoption command
runs through the pinned shared engine; the application is not copied locally.

TT-142 supplies adversarial scope, journal, lifecycle, writer, CAS, recovery,
activation/export and browser visibility regressions. Source validation does not
claim consumer activation, native delivery, service publication or pilot success.
The matching Master change owns descriptor validation/export consistency. Final
rollout evidence belongs in the coordinated implementation ledger.

## Re-affirming delivery after requirements changed (TT-242)

A Merged task whose receipt is admitted becomes `requirements-changed` when its Issue
is edited later. `historical_completion` cannot help because it applies only when no
receipt exists, and dependents stay blocked. `completion reaffirm` is the explicit,
owner-authorized, audited repair. It is a dry run unless `--apply` is supplied and runs
inside an owned maintenance reservation, exactly like `completion adopt`:

```sh
python -m tracker maintenance reserve --actor Maintainer-EX01 --session reaffirm-example --report-file report.txt --apply
python -m tracker completion reaffirm --issues TT-1 TT-2 --evidence-file reaffirm.json --actor Maintainer-EX01 --session reaffirm-example --claim CLAIM
python -m tracker completion reaffirm --issues TT-1 TT-2 --evidence-file reaffirm.json --actor Maintainer-EX01 --session reaffirm-example --claim CLAIM --apply
python -m tracker maintenance resume --actor Maintainer-EX01 --session reaffirm-example --claim CLAIM --report-file report.txt --apply
```

Evidence is `kind: scope-reaffirmation`, `schema_version: 1`, the exact repository and
delivery policy fingerprint, `approved_by` (the GitHub login of the repository admin who
approves), a durable `report`, `issues` (at most 16, identical to `--issues`) and, for
each task, exactly: `old_scope` (the receipt scope), `new_scope` (the engine-computed
current task scope), `accepted_scope` (the current Issue digest), `implementer`, an
independent `reviewer`, `verdict: requirements-diff-accepted`, `requirements_diff`
(a real explanation of 40-4000 characters, not a placeholder) and a task `report`.
Unknown fields anywhere are refused.

Owner approval is a verifiable artifact, not a string. Every Issue needs a comment whose
author is `approved_by` and currently has repository `admin` permission (the same native
check as the other owner-attested commands), containing the line
`Tracker scope-reaffirmation sha256: <digest>`. The comment must never have been edited
(`updated_at` equals `created_at`): anyone with write access can edit another user's
comment while GitHub keeps the original author, so an edited comment is not evidence. If
an approval comment was edited for any reason, post the approval again as a new comment.
The digest is the canonical SHA-256 of that task's own manifest entry together with the
kind, schema, repository, policy, Issue number, `approved_by` and the top-level `report`,
so changing anything the owner approved invalidates the comment. The dry run lists the
digest each Issue still needs (`approvals`, `missing_approvals`); apply refuses while any
is missing and records the comment id, author, creation and update time in the audit entry.
Deleting a comment after apply is not detected; the audit entry retains its identity.

The command refuses, without writing anything, unless the maintenance claim belongs to the
caller, the config equals the official base-branch config, and every selected task is a
Merged, unarchived, unowned and fully checked task in `requirements-changed` state whose
receipt is a non-process receipt that the completion assessment treats as current policy
(including an audited policy reconciliation), admitted, in state `verified` or `pending`,
with `old_scope` equal to its recorded scope and a retained scope provenance. A `pending`
receipt only passes when the policy needs no verification (reviewed-merge); the recomputed
assessment must also be verified when the policy requires it. The reviewer must differ,
after trimming and case folding, from the manifest implementer, the recorded task
implementers and every source PR author. Git is re-verified: each source PR is the recorded
merge into the base branch, the merge tree is unchanged, each source and verification
revision is in the current base head, and a recorded release is still published, matches
the retained release identity or installer digest, and is unchanged again just before the
write. CI, native-run and package proofs are not re-run; the retained receipt is trusted
for those.

Apply appends `scope_reaffirmations[]` (`from_scope`, `to_scope`, manifest digest,
`approved_by`, `report`, maintenance actor/session/claim, approval comment, task digest,
reviewer, implementer, the requirements-diff text and digest, accepted scope, time) and
moves the receipt `scope` together with that record. The original `receipt_digest` is
unchanged. `delivery_state` is recomputed by the shared completion assessment and is never
supplied; the write is refused if the result would not be current and admitted. A repeat
with the same manifest is a no-op and a changed manifest is refused. A stale
`flag:blocked` keeps `complete` false; the command only reports it, and removing it is a
separate owner-approved step.

Portable consumers must take the matching `tracker_delivery_policy.py` before the
first apply, because `receipt_digest` now ignores `scope_reaffirmations`.

Always run `maintenance resume` after any outcome of `--apply`, including a refusal or a
failed run. The reservation holds the whole project (task admission and native delivery
recording) until it is resumed; nothing cleans it up automatically.
