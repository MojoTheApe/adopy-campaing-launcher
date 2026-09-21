# Prepared Task Tracker companion

This patch updates the existing ACL registration from master to main and its
fallback configuration to the preserved ACL IDs/modules plus Stage/package policy.
It was prepared against reviewed TT-61 / PR #62 at
5f211d3095a392a16c2c01a65eb02ff8719a7243. It also carries the identical, owner-approved
74-task historical-completion manifest from ACL's candidate config. No delivery
receipts are invented; see docs/repository/HISTORICAL_COMPLETION.md.

It is not applied to the live Tracker. Before activation, create/link its real
Tracker task and reviewed PR, refresh the source and run registry/model tests.
Use git apply --check first; never force an outdated patch. Review historical
completion with the ACL migration, then coordinate publication with actual main
existence and the source policy cutover. Publishing early can make the board
read a missing branch or reinterpret historical completion.

Retain workspace ID acl, URL, repository, modules and every legacy ID. Verify a
fresh authenticated board after the maintained publication procedure. Do not
create a second registration or redeploy the retained tracker/ package from ACL.
