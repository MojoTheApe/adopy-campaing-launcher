# ACL Tracker

Issues remain in MojoTheApe/adopy-campaing-launcher; the existing board identity is acl
at https://tracker.team7.cloud/projects/acl/. Keep the six established modules,
71 historical aliases and +1000 numbering. Never renumber Issues or start a second board.

ACL-1892 selects reviewed, merged Task Tracker
`0d1fad44f5fdbef64b061324e9a1292fb77fb725` (TT-107 / PR108) and Master
`eca8a857d9f7f81160bf248f9f9e3109a21ade49` (MR-26 / PR27), retaining ACL's
guarded client and unchanged project configuration. The compatible shared service
was already published at `bcb59dfb0e4c5066d891207b49f690f29d9f9ef4`.
No migration, maintenance reservation or optional native CI policy is selected.
Follow [verification](VERIFICATION.md); ACL's existing bounded collector and
Windows/matrix/installer gates remain authoritative.

The historical ACL-1750 source selected reviewed, merged Task Tracker
`d6ce39bede04851c37bb4d13cc3bed0f2dcd2f76` (TT-79 / PR80) with Master
`d24db185ebdff2cadcecf89f5b28061d6cd19564` (MR-16 / PR17).
Run `python scripts/task_tracker.py guide` for the exact currently configured
engine; helpers live in .workflow/tools and existing scripts paths remain wrappers.
The guarded client loads the clean engine from `tracker/engine.json`. The completed
ACL-1874/1875 cutover and preserved old-policy procedure are described in
[SOURCE_REVIEWS.md](SOURCE_REVIEWS.md). `.workflow/legacy` retains the original configs for audit and
bootstrap PR history; it is not another live tracker or a second Issue store.
Do not return to the retired master client. ACL-1248's original adoption is recorded
in ADOPTION.md. The bounded ACL-1383 old-policy source handoff and ACL-1382 live
execution migration are described in PARALLEL_EXECUTION.md.

Follow [WORKFLOW.md](WORKFLOW.md), [BATCH_INTEGRATION.md](BATCH_INTEGRATION.md),
[SOURCE_REVIEWS.md](SOURCE_REVIEWS.md) and
the exact pinned command guide. Ordinary application/batch work follows:
claimed one-task source -> handoff -> independent review -> Approved -> frozen Coordinator
batch -> additional combined review/checks -> Stage -> exact target verification
-> unchanged main promotion -> per-task Merged -> verified package delivery.
The explicit [instruction-only route](PROCESS_INSTRUCTIONS.md) uses its current
independent source review and direct main receipt instead of Approved/batch/stage.
It cannot displace an active batch or staged candidate. Policy/automation adoption
itself remains on the old source route until activated.

The active schema-3 policy keeps implementation_limit 5, review_limit 5 and
batch_limit 5; the supported guarded migration and fresh-board proof are complete.
The Approved backlog has no size cap. Ordinary batch-source
approval frees only its source review lane; only one
batch owns stage. Ordinary source PRs target main and remain open until verified
batch inclusion. Retained
queued stage PRs are approval-only, preserving actual authors/history.
Five source-review claims are independent of the single integration Coordinator
and combined review. Every simultaneous Reviewer has a distinct actual run;
capacity does not prove that a host can launch five agents. Resuming/returning a
source uses that PR/claim and preserves the other source and integration owners.

The source children ACL-1747 and ACL-1750 completed their distinct old-policy
stage/main handoffs before the historical coordinated v2 migration.
The new pin contains TT-73's large-state read fix; the bounded old-v1 operator use
is documented in the adoption runbook. New pin/config/skill alone is not activation.
Preserve explicit Release membership and Archive semantics. Description explains
the human outcome; Specifications retains full context, criteria, decisions and
handoff. Read both in full.

The board's Worker is the actual implementation owner, with Coordinator and
Reviewer recorded separately. Use platform-neutral opaque actor/session identities,
not synthetic GitHub accounts. Shared claims arbitrate across conversations;
labels and per-chat counters cannot reserve capacity. A stopped session requires
an explicit inspected handoff/transfer; elapsed time alone never authorizes takeover.
See the pinned engine's `docs/PARALLEL_EXECUTION.md` for command arguments.

For ordinary/application work, a Merged label means source integration; the
delivery receipt decides package completion. A proven instruction-only task instead
completes through its admitted verified process receipt after the reviewed main merge. Partial scope, failed delivery and missing evidence remain unfinished.
The owner-approved 74-task manifest preserves prior completion separately as Completed
before adoption; see HISTORICAL_COMPLETION.md. It supplies no current delivery evidence.
Never use non-code completion or issue-closing keywords to bypass missing code or delivery.
The retained tracker/ Python package reads a frozen historical config for regression
tests. Its CLI, merge recorder and old board publisher refuse live operations once
the adoption marker is present; imports remain available for historical tests. Do
not extend or deploy that package. New work uses the pinned shared client.
