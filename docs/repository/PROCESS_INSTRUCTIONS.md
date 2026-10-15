# Repository instructions and application changes use separate routes

[ACL-1750](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/750)
prepares the owner's 2026-10-08 decision. The matching process policy must already
be present in main before a source can use the new route. This adoption changes
policy, helpers and CI, so it follows the existing stage route. Source preparation
is not activation; [ACL-1747](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/747)
and its draft main promotion retain their genuine old-policy handoff and the
[BATCH_INTEGRATION.md](BATCH_INTEGRATION.md) service/state cutover gates.

## Instruction-only route

A task that changes only normal Markdown files at `AGENTS.md`, `CLAUDE.md` or
`docs/repository/**/*.md` follows:

**Claimed work -> Review -> independent review and instruction checks -> main -> Merged.**

Keep the real one-task source PR, Worker and independent Reviewer. The matching
`workflow.process_instructions` and Tracker `process_instructions` objects must
explicitly select schema1 and the actual checks `descriptor`, `tracker-link` and
`Process instructions`. The pinned engine records current review and admits the
source directly into main; this route does not enter Approved or a batch. Its
normal reviewed-Merged receipt completes the instruction-only task without an
application installer or runtime delivery claim. Review-return, author reclaim,
fresh head/base/scope approval and normal Merged recording still apply.

The task's source review lane is held through the direct main merge. Under the
prepared schema-3 policy it consumes one of five source slots; each held process
lane blocks new intake/batch creation. Live activation follows
[SOURCE_REVIEWS.md](SOURCE_REVIEWS.md). A staged or active
integration candidate blocks the shortcut: changing main underneath its frozen
baseline is not a permitted convenience. After a completed process merge, stage
may lag only by those already-admitted instruction changes. The pinned engine's
read-only `stage_base_compatible` gate verifies the bounded first-parent chain,
actual process merge receipts, current task scope/policy and exact tree before
allowing the next ordinary stage candidate or batch. It never updates or rewrites
stage. Missing receipts, mixed content, stale scope or an unexplained divergence
block admission; preserve the refs and inspect the failing evidence.

The CI selector calls the pinned portable classifier on complete immutable Git
commits and trees. Both base and head must have the unchanged matching process
opt-in. A known one-task source targets current main. A main push must match the
actual official repository, event head and fetched main ref, with the same opt-in
already in its exact before commit. Unknown/missing history or stale refs require
ordinary full checks. Successful classification skips Linux/Windows application
tests and installer jobs, while the secret scan, project tooling, descriptor,
Tracker linkage and local instruction-link checks remain applicable. These checks
are not an independent-review or merge-admission receipt.

## Application and other changes

Runtime/system/product knowledge, README, packaging, scripts, workflows, config,
policy, dependencies and mixed changes keep the normal route:

**Review -> Approved -> a Coordinator's batch of at most five -> stage -> main.**

An Approved queue may contain more than five tasks. Each batch preserves actual
source authors and their independent reviews; combined review and complete
applicable checks verify the assembled content. Installation, stable release,
update/restart/recovery and employee rollout keep the existing runtime controls.

Eligibility is determined from actual paths and Git blob modes, not a title,
label or an author's claim that work is process-related. Added executable or
symbolic-link files, moves from outside the fixed instruction paths, truncated
API data, unknown paths and changed policy cannot use the shortcut. Deleting or
renaming an allowed normal instruction file still needs complete proof of both
sides. CI/policy/helper changes adopt through the ordinary route, even when their
purpose is process improvement. Manual/release runs and stage/batch/promotion
contexts retain complete applicable verification. Never relabel a product task
or trim its outstanding acceptance criteria to obtain instruction completion.
