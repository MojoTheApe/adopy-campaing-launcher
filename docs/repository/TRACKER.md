# ACL Tracker

Issues remain in MojoTheApe/adopy-campaing-launcher; the existing board identity is acl
at https://tracker.team7.cloud/projects/acl/. Keep the six established modules,
71 historical aliases and +1000 numbering. Never renumber Issues or start a second board.

The candidate pins Task Tracker c89dd82289d2c6b3e27a85736729bbeaf7461a77. Run
`python scripts/task_tracker.py guide` for that guide; helpers live in .workflow/tools
and existing scripts paths remain wrappers. The active client uses that exact
clean shared engine. `.workflow/legacy` retains the original configs for audit and
bootstrap PR history; it is not another live tracker or a second Issue store.
Do not return to the retired master client. ACL-1248's original adoption is recorded
in ADOPTION.md. The bounded ACL-1383 old-policy source handoff and ACL-1382 live
execution migration are described in PARALLEL_EXECUTION.md.

Follow WORKFLOW.md and the pinned command guide: claimed work -> handoff ->
review-acquire -> independent review -> record-review -> check-merge -> stage ->
verify-stage -> promotion -> merged -> verified package delivery. Keep five
implementation slots and one review/integration lane, explicit Release membership and Archive
semantics. Description explains the human outcome; Specifications retains full verified
context, criteria, references, checks, decisions and current handoff. Read both.

The board's Worker is the actual implementation owner, with Coordinator and
Reviewer recorded separately. Use platform-neutral opaque actor/session identities,
not synthetic GitHub accounts. Shared claims arbitrate across conversations;
labels and per-chat counters cannot reserve capacity. A stopped session requires
an explicit inspected handoff/transfer; elapsed time alone never authorizes takeover.
See the pinned engine's `docs/PARALLEL_EXECUTION.md` for command arguments.

For new work, a Merged label means source integration; the delivery receipt decides
package completion. Partial scope, failed delivery and missing evidence remain unfinished.
The owner-approved 74-task manifest preserves prior completion separately as Completed
before adoption; see HISTORICAL_COMPLETION.md. It supplies no current delivery evidence.
Never use non-code completion or issue-closing keywords to bypass missing code or delivery.
The retained tracker/ Python package reads a frozen historical config for regression
tests. Its CLI, merge recorder and old board publisher refuse live operations once
the adoption marker is present; imports remain available for historical tests. Do
not extend or deploy that package. New work uses the pinned shared client.
