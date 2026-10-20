# ACL canonical support and the product boundary

This document records the boundary for [ACL-2026](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/1026),
including its latest owner amendment. It is author evidence for independent review,
not a local completion algorithm or an approval. The canonical mechanism is
[PROCESS_SUPPORT.md](PROCESS_SUPPORT.md); actual commands, immutable proof and
whole-scope review remain authoritative.

## Accepted scope and provenance

Select published Master1.6.0 `6cbc2bbfd5df76fc2f1ad6f285d33a45dd5ff504`
and Tracker3.8.0 `9c3ad4deec6b3596d5ef48400430b126155012dd`.
The task adopts common executor activity and the global separation of process
delivery from product delivery. The owner expressly requested the latter outcome
after the original source approval. The genuine Coordinator returned the source
before the same author resumed it; old approval does not cover the revision.

Compare against actual ACL main `c61501607ce11744e45add8b0d3b314368c216c5`.
Keep every non-pin descriptor field, Tracker config and separated layout unchanged,
including the descriptor's historical explanatory exception. Preserve execution3,
5/5/5 capacity, process1, no Stage with permanent stage retained, package target,
all checks, absent optional policies, 71 aliases, +1000 IDs and existing receipts.
Updated support explanations belong in repository prose. The maintained upgrade
retains the genuine upstream pristine baseline and authenticated helper closure.

## Product and frozen bundle inputs

The immutable comparison covers 306 unchanged blobs across `src`, `config`,
`knowledge_base`, `assets`, `packaging/windows`, runtime/development requirements,
`.env.example`, `serve.py`, the runtime/smoke/update-gate helpers, release manifest,
publication/continuation/CI-reuse scripts, and release/complete-delivery workflows.
Recheck the final candidate rather than inferring isolation from those paths.

`packaging/windows/build.ps1` runs the existing clean-input guard, creates build
metadata, then invokes PyInstaller with `acl_windows.spec`. The spec's `Analysis`
starts at `packaging/windows/launcher.py`; its data inputs are explicit product
configuration, knowledge, web assets/templates, generated build/update metadata
and Playwright. Hidden imports cover the reviewed product/runtime modules plus
`scripts.acl_runtime_manager` and `scripts.acl_smoke`. Governance documents,
delivery descriptor, Tracker config and `.workflow/tools` are not bundled as data.
No packaging, dependency, launcher or imported product input changes here.

The source import scan also checks indirect paths. `src.web.acl_update` imports
`scripts.acl_update_gate`, which imports `scripts._console` and product configuration;
none imports the changed shared governance helpers. The runtime manager likewise
uses product configuration and `_console`. Smoke imports its explicit dependency
and product-module lists; its dynamic imports do not discover arbitrary process
helpers. Launcher diagnostics/setup/updater modules retain their existing imports.

This is not a claim of identical newly built installer bytes: generated build
metadata contains source/version/time. Required native checks may still build and
test an installer; those checks do not publish a product or establish product
delivery. No Windows acceptance is claimed from this source comparison. Product
changes or explicit publication still need their real checks and configured delivery.

## Release manifest's shared parser

`scripts/acl_release_manifest.py:tracker_issues` imports
`scripts/tracker_pr_check.py`, which forwards to the exact portable PR helper.
The consumed `linked_numbers` function is unchanged from actual main and from
Tracker3.7.3 to 3.8.0. Its globals are ordinary parsing dependencies; it never
references `delivery_policy` or calls completion/projection functions. Legacy
aliases and the +1000 identity mapping remain unchanged.

The portable PR helper has SHA-256
`3760865632e2e0cb8e80255320e14a6aeacf5a4010c3e8e676148b522db48a0d`.
Its canonical engine source is unchanged; the existing separated-layout transform
only adjusts the CLI default config path. Module-import statements and other
non-definition top-level AST in the transitively imported `delivery_policy` are
unchanged. Loading the parser does not execute new completion behavior. Five
offline cases of the actual manifest parser, using the unchanged candidate
function without a policy global, retain valid IDs/aliases and reject missing,
duplicate or capture linkage. No API, publication or product operation is invoked.

## Reachable release governance

Do not describe all release tooling as independent of shared policy.
The existing complete-delivery workflow invokes `tracker_delivery_receipt`;
its resolver/recorder reaches `require_completion_scope`, then
`completion_assessment` and `summary`. Those policy functions intentionally gain
handling for authenticated process-support receipts. The receipt helper's own
source bytes are unchanged from Tracker3.7.3 to 3.8.0. The new policy branches
remain process governance, outside the frozen product and manifest-parser path.

Ordinary product receipts retain their existing admission, target, request,
scope, checks and actual verification path. Existing requests are neither
cancelled nor satisfied by this update. Inspect the canonical policy diff and
supported proof plus the applicable regressions; do not claim that the functions
are unreachable, or that directory names alone establish product isolation.

## Verification and completion boundary

Run the read-only native `process-support 1027` against the final immutable source.
Its content proof is separate from a genuine independent Reviewer's assessment
of the complete amended task and product isolation. Scope/head/base changes need
fresh relevant evidence. Ordinary source approval, frozen batch, combined review,
all applicable checks and actual admitted main integration remain required.

Select process delivery only through that supported evidence, without creating
a consumer product-release request. The earlier automatically rejected Windows
publication is not resumed. Unknown/mixed content cannot receive process-only
completion; explicit product publication and unfinished product/business outcomes
keep their real acceptance. This task changes no campaign behavior, employee data,
live advertising or optional state policy and performs no migration.
