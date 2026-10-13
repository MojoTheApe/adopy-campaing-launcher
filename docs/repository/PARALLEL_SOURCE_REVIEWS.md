# Three source reviews and one integration coordinator

Task: [ACL-1868](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/868).
Shared implementation: [TT-93](https://github.com/MojoTheApe/task-tracker/issues/93)
and [MR-22](https://github.com/MojoTheApe/master-repo/issues/22).

This is the explicit adoption of execution schema 3. Source preparation alone
does not activate its policy. The live state must show the matching policy after
supported migration before three review claims can be acquired.

## Capacity and ownership

ACL selects five implementation claims, three independent source-review claims,
and batches of at most five tasks. One separate Coordinator still owns one frozen
integration batch through combined review, stage verification and main promotion.
Source reviews have per-PR ownership and may proceed while a batch is checked.
The combined-review reservation belongs to that batch; it is not a fourth source
review or a second integration coordinator. One actual run cannot manufacture
multiple identities or approve its own implementation. Repository ceilings do
not guarantee enough sessions on one host.

## Queue and batch throughput

Check the current source head/base, complete task scope and required source CI
before assigning final review. Return tasks with failed applicable checks to their
actual authors through supported claims; preserve failures and their cause. A
green aggregate with skipped installer build is not an installation receipt.

While one batch runs CI, prepare the next compatible tasks for Approved. Prefer
three to five independent ready tasks together, but permit a genuinely urgent
single task. Freeze the intake and batch before assembly; never append new tasks
to a tested candidate. Changes to main, source scope or policy require the
supported refresh/review of affected evidence. Identical-tree reuse remains
subject to the existing verified CI contract.

## Source integration and activation

1. Integrate and independently verify the shared Tracker and Master support.
   Use exact compatible revisions for ACL helpers, descriptor and Tracker lock.
2. Complete this adoption source through its actual old-policy approval/batch,
   combined review, full CI, isolated Windows stage verification and unchanged
   main promotion. Keep an unchanged old-policy checkout for its own handoff.
3. Finish active review, implementation and integration owners through their
   ordinary lifecycle. Do not force-release foreign claims. Preserve queued
   handoffs, original authors, approvals, receipts and historical task identities.
4. Align the maintained shared-service engine and ACL registry fallback through
   their reviewed procedures. Preview the supported schema-2 to schema-3
   queue-preserving migration, inspect its before/after comparison, then apply
   exactly that migration. Never edit or overwrite shared state by hand.
5. Verify effective five/three/five limits and all preserved queue/receipt/history
   fields on fresh shared state and the board snapshot. Source approvals tied to
   an earlier policy must be revalidated as required by the supported engine.
   Record actual activation evidence separately from source preparation.

## Owner test dashboard

The owner's existing test link opens `http://127.0.0.1:9095/`. At intake its server
runs from `C:\fb_agents`, branch
`feature/campaign-builder-dashboard-and-rpa-adlevel`, commit
`dc503e86059fb0ab072dfd476b80168f0fca53e5`, with a local `CLAUDE.md` change.
It is a source checkout; browser refresh does not update its server code. Preserve
that local change, configuration, workbooks, creatives and active profile jobs.
An update follows the maintained gated source-update mechanism and verified
candidate, with an idle/restart check. Do not replace that checkout or switch its
branch blindly. Updating this test server does not publish a stable installer or
authorize advertising operations.

Keep source push, source approval, stage/main merge and the running dashboard
version separate in the visual queue. Display observation time and exact source
identity; do not label a snapshot as live streaming or a commit time as push time.
