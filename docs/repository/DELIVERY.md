# ACL delivery contract

Read [ADOPTION.md](ADOPTION.md) for actual cutover evidence and the one-time
activation gate before any live operation. Main/stage and the shared board exist;
the descriptor remains draft because the pinned standard has no published release.

`delivery.json` is the project-specific delivery description. `tracker/config.json`
is the matching Tracker configuration. The standard and engine use exact commit
pins; neither automatically follows main. Populate actual procedures and verify
registration before changing adoption from draft to ready.

The selected temporary no-stage route keeps Approved and 5/5/5 source/batch
capacity. A reserved independently reviewed batch targets main, while ordinary
source PRs stay approval-only. Read [POST_MAIN_DELIVERY.md](POST_MAIN_DELIVERY.md)
for the one-request release continuation, exact native proof, initial old-policy
source admission, separately verified idle activation and restoring Stage.

The explicitly opted-in [instruction-only route](PROCESS_INSTRUCTIONS.md) admits
normal repository-process Markdown directly from Review into main after independent
review and its applicable checks. Its verified reviewed-merge receipt completes that
bounded task without package delivery. Application/mixed/config/automation changes,
including this route's adoption, retain the normal stage and runtime contract.

## Path from request to completion

Idea -> Backlog -> To Do are planning steps under the owner's direction. An
explicit development request starts In Progress and a ticket branch. The author
implements, self-checks and creates the linked PR, then sets Review. A separate
subagent reviews; fixes stay on the same branch and require fresh approval.
For a proven adopted instruction-only task, record current independent review,
run its explicit checks and `check-merge`, merge the one-task source into main and
record the normal reviewed `merged` transition. Hold its review lane through that
merge. Its verified process receipt completes the instruction task; do not create
Stage, installer or package-delivery evidence for it.

For ordinary/application work, execution schemas 2/3 keep one task per source PR,
independently reviewed into Approved. Approval releases source review ownership; it does not merge or deliver.
A separate Coordinator freezes intake and one batch of up to five. Assemble exact
approved sources, review their combined candidate independently and run full
applicable checks before the main merge. Record actual Merged scope. Under the
standing owner policy, integration approval includes delivery unless
explicitly source-only/skip-release; the agent adds `batch begin --deliver`, which
retains its genuine exact request. The durable continuation publishes and verifies the package before recording
Delivered. Failure or missing acceptance retains pending delivery. Do not tick
unfinished business criteria to force completion. Initial source ACL-1924 still
uses the old stage/main policy; no source change activates its own shortcut.

Run the pinned client's guide, `check-pr N`, `record-review`, `check-merge N`,
`stage`, `verify-stage` and `merged ... --reviewed-complete --apply` at the matching
lifecycle steps. Commands reserve/record evidence; they do not run review or deploy.
Exact-content CI reuse is allowed only for applicable proven jobs. Actual server
branch protection must be verified separately from agent admission.

Merged records inclusion in main. A local manual-pull project completes at that
verified merge. For ordinary/application tasks, VPS/n8n/package profiles require
their actual delivery and checks,
recorded with `record-delivery`. Failed or pending delivery remains unfinished.
The explicitly selected n8n-source profile completes the Git source task after
the admitted main merge. n8n transfer/publication is a separate process and must
never be claimed from Git evidence. Existing runtime acceptance criteria survive
adoption. For repository-only work, label stage evidence as Git checks; consult the project runtime guide when runtime verification is required;
branch-to-environment connections require their own explicit setup.
A partially implemented ticket stays active. Never substitute a closed Issue or
non-code completion for missing code/delivery evidence.

## Procedure references

Follow [VERIFICATION.md](VERIFICATION.md) for common check selection and coordinated
upgrades, while preserving ACL's affected-source selection, actual required names,
full applicable integration/platform/installer gates and bounded local collector.
TT-107's optional native protocol cannot replace those Windows/matrix/installer
proofs. No optional CI evidence policy or maintenance interval is enabled here.

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
