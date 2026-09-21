# Retained Task Tracker companion patch

This patch updates the existing ACL registration from master to main and its
fallback configuration to the preserved ACL IDs/modules plus Stage/package policy.
It was prepared against reviewed TT-61 / PR #62 at
5f211d3095a392a16c2c01a65eb02ff8719a7243. It also carries the identical, owner-approved
74-task historical-completion manifest from ACL's candidate config. No delivery
receipts are invented; see docs/repository/HISTORICAL_COMPLETION.md.

This retained preparation artifact was applied and reviewed through Task Tracker
TT-63 / PR #64, then published as `0dadc889e18b027c8ca98b888407d492848f2f19` after
ACL main/stage existed. Do not apply it again or create a duplicate task/registration.
See [the adoption record](../docs/repository/ADOPTION.md) and TT-63 for actual
publication, backup and verification evidence. A future change needs fresh source
comparison and review; never force an outdated patch.

Workspace ID acl, URL, repository, modules and every legacy ID were retained.
Fresh server-side snapshots and existing authentication/HTTP restrictions were
verified; the owner explicitly excluded browser/UI verification. Do not redeploy
the retained tracker/ package from ACL. Stable package delivery remains separate.
