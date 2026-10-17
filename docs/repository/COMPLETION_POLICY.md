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
