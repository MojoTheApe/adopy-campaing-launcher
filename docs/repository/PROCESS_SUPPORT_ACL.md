# ACL canonical support and the product boundary

This document records the boundary for [ACL-2026](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/1026),
including its latest owner amendment. It is author evidence for independent review,
not a local completion algorithm or an approval. The canonical mechanism is
[PROCESS_SUPPORT.md](PROCESS_SUPPORT.md); actual commands, immutable proof and
whole-scope review remain authoritative.

## Accepted scope and provenance

Select published Master1.7.2 `4f148cbbbc2c9c65a7fdebba9b37551f91566e04`
and Tracker3.9.2 `3d32586d72e4621f70d56d35f1e199a19fa13b00`.
The cumulative task adopts common activity/history, process/product separation,
Pause support and shared lifecycle/correction guidance. The owner explicitly
selected one combined update on the original Issue/PR. The genuine source Reviewer
returned the current source before its original Worker reclaimed the same branch.
Previous ca9a650 and 8ce4862 scopes, checks and handoffs remain historical; none
approves this changed cumulative source or proves live Pause activation.

Compare against actual ACL main `259ae7f1283a0a0acab40354b6b97d55162c91b5`.
Keep every non-pin descriptor field, Tracker config and separated layout unchanged,
including the descriptor's historical explanatory exception. Preserve execution3,
5/5/5 capacity, process1, no Stage with permanent stage retained, package target,
all checks, absent optional policies, 71 aliases, +1000 IDs and existing receipts.
Updated support explanations belong in repository prose. The maintained upgrade
retains the genuine upstream pristine baseline and authenticated helper closure.

## Product and frozen bundle inputs

The current-main comparison covers 307 unchanged product/build input blobs across `src`, `config`,
`knowledge_base`, `assets`, `packaging/windows`, runtime/development requirements,
`.env.example`, `serve.py`, the runtime/smoke/update-gate helpers, release manifest,
publication/continuation/CI-reuse scripts, and release/complete-delivery workflows.
The current main includes unrelated product changes (configuration unification,
goals/profile search and RPA guards/tests); this candidate includes them unchanged.
Recheck the final immutable diff rather than inferring isolation from those paths.
Existing Git attributes check out four PowerShell inputs as CRLF; their normalized
Git blobs are identical to main. No new line-ending rule is introduced.

`packaging/windows/build.ps1` runs the existing clean-input guard, creates build
metadata, then invokes PyInstaller with `acl_windows.spec`. The spec's `Analysis`
starts at `packaging/windows/launcher.py`; its data inputs are explicit product
configuration, knowledge, web assets/templates, generated build/update metadata
and Playwright. Hidden imports cover the reviewed product/runtime modules plus
`scripts.acl_runtime_manager` and `scripts.acl_smoke`. Governance documents,
delivery descriptor, Tracker config and `.workflow/tools` are not bundled as data.
No packaging, dependency, launcher or imported product input changes here.

The current-main import scan starts from 233 product Python roots and reaches
234 local modules; its only extra project helper is `scripts/_console.py`. The
new `config.config_unify` module imports product configuration and standard-library
modules, with no Tracker/governance dependency. The scan also checks indirect paths. `src.web.acl_update` imports
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
The consumed `linked_numbers` function is unchanged from the actual main pin
through Tracker3.9.2. Its globals are ordinary parsing dependencies; it never
references `delivery_policy` or calls completion/projection functions. Legacy
aliases and the +1000 identity mapping remain unchanged.

The portable PR helper has SHA-256
`3760865632e2e0cb8e80255320e14a6aeacf5a4010c3e8e676148b522db48a0d`.
Its canonical engine source is unchanged; the existing separated-layout transform
only adjusts the CLI default config path. Module-import statements and other
non-definition top-level AST in the transitively imported `delivery_policy` are
unchanged. Loading the parser does not execute new completion behavior. Five
offline cases of the actual candidate manifest parser retain valid IDs/aliases
and reject missing,
duplicate or capture linkage. No API, publication or product operation is invoked.

## Reachable release governance

Do not describe all release tooling as independent of shared policy.
The existing complete-delivery workflow invokes `tracker_delivery_receipt`;
its resolver/recorder reaches `require_completion_scope`, then
`completion_assessment` and `summary`. Those policy functions gain handling for
authenticated process-support receipts.
The latest shared policy additionally validates pause state and blocks conflicting
admission; the portable receipt resolver/recorder now decodes/encodes the outer8
state envelope. Retry/direct helpers retain the same pause-aware guard closure.
These are reachable release-governance changes, outside the frozen product and
manifest-parser path. They are exact canonical support, with applicable bridge
regressions and native checks; they must not be described as unchanged tooling.

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

## Live Pause is a separate boundary

The new pin and exact 14-helper closure supply actual `pause stop/resume` commands.
They are usable only after compatible reader/every-writer readiness, recovery and
genuinely idle owned `pause activate` preview/apply/resume are verified. Existing
foreign owners/intake and unknown writers are preserved rather than cleared to
create idleness. Source, actual process completion and live outer8 activation are
separate facts; the current source establishes no live activation. The previous
To Do-moving stop procedure remains explicit pre-activation behavior. See
[the recorded readiness boundary](EARLY_ADOPTION.md#pause-readiness-and-live-boundary).
