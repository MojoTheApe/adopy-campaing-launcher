# Prepared Task Tracker companion

This patch updates the existing ACL registration from master to main and its
fallback configuration to the preserved ACL IDs/modules plus Stage/package policy.
It was prepared against ea969826398a808925bbd907ca8719445177ab59 (same tree as
the reviewed Tracker main merge c8b5b5642bc5add250fcad3f728de6381424a41f).

It is not applied to the live Tracker. Before activation, create/link its real
Tracker task and reviewed PR, refresh the source and run registry/model tests.
Use git apply --check first; never force an outdated patch. Review historical
completion with the ACL migration, then coordinate publication with actual main
existence and the source policy cutover. Publishing early can make the board
read a missing branch or reinterpret historical completion.

Retain workspace ID acl, URL, repository, modules and every legacy ID. Verify a
fresh authenticated board after the maintained publication procedure. Do not
create a second registration or redeploy the retained tracker/ package from ACL.
