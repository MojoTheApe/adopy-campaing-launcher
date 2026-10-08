Tracker issues: ACL-1234

## Result

Describe the problem and resulting behavior. Replace the example ID with the
actual task. Title: `[ACL-1234] Summary`. Exactly one task per source PR; link this
PR back from that Issue. A new source targets main and remains approval-only. For a combined batch/promotion,
list the exact frozen tasks in the title and one Tracker issues line, add one
Tracker batch line, and link the source PRs/approvals; see docs/repository/BATCH_INTEGRATION.md.

## Verification

Self-checks and results. Implementation claim, handoff, independent Reviewer/report
and exact reviewed version. For batch stage/promotion: frozen members/source heads,
independent combined review, complete applicable CI, same tree and exact environment
proof. ACL-1747 uses only its documented bounded old-policy source cutover route.
Robot (RPA) fix: the live capture that motivated it is in the bench — fixture
cut with `scripts/capture_to_fixture.py`, a test running the step on it, a row
in `tests/bench/catalogue.json` — or the reason there is no capture.

## Documentation and related processes

Current system/history updated, or no impact with reason.
Master Repo / Task Tracker impact: linked matching change, or no impact with reason.
Local exceptions and deployment controls preserved. Completion and delivery still
pending, where applicable. No automatic closing keywords.
