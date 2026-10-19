Tracker issues: ACL-1234

## Result

Describe the problem and resulting behavior. Replace the example ID with the
actual task. Title: `[ACL-1234] Summary`. Exactly one task per source PR; link this
PR back from that Issue. An ordinary batch source targets main and remains approval-only.
A proven instruction-only source uses independent Review and its explicit checks
for direct main admission; see docs/repository/PROCESS_INSTRUCTIONS.md.
For a combined batch/promotion,
list the exact frozen tasks in the title and one Tracker issues line, add one
Tracker batch line, and link the source PRs/approvals; see docs/repository/BATCH_INTEGRATION.md.

## Verification

Self-checks and results. Use the guarded ACL operator adapter described in AGENTS.md. Implementation claim, handoff, independent Reviewer/report
and exact reviewed version. For instruction-only sources: complete fixed-path/blob
proof, current main/scope, matching base opt-in and idle stage/integration ownership.
For batch stage/promotion: frozen members/source heads,
independent combined review, complete applicable CI, same tree and exact environment
proof. ACL-1747 and ACL-1750 use their documented distinct ordinary old-policy
source cutover routes; neither adoption can classify itself as instruction-only.
Robot (RPA) fix: the live capture that motivated it is in the bench — fixture
cut with `scripts/capture_to_fixture.py`, a test running the step on it, a row
in `tests/bench/catalogue.json` — or the reason there is no capture.

## Documentation and related processes

Current system/history updated, or no impact with reason.
Master Repo / Task Tracker impact: linked matching change, or no impact with reason.
Local exceptions and deployment controls preserved. Completion and delivery still
pending, where applicable. No automatic closing keywords.
