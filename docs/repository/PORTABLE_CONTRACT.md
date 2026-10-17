# Portable export contract 1

TT-143 makes Tracker the technical owner of the portable CI frontend, helper
bytes and structural CI policy. Source version 3.4.0 is a candidate until its
immutable release is published. Export, installation and publication adopt no
project policy, capacity, state or product delivery.

## Exact output and runtimes

`export-template --helper-layout raw|separated` defaults to the existing raw
standalone layout. Raw emits nine implementations in `scripts/`; separated emits
nine implementations in `.workflow/tools/` and nine compatible scripts forwards.
Master requests separated output and consumes these helper bytes unchanged.
Only the author client and PR checker have their known root-depth expression
adapted before emission. Both old and current CI command paths keep working.

`tracker/export-contract.json` has format version 1, the exact official engine
pin, supported policy/producer versions, Python floors and SHA-256 maps for both
actual layouts. Its bytes do not vary by selected project policy or output
layout. It includes no timestamps, selected config, receipt or self-hash. Source
exports verify the actual clean official checkout/revision and canonical helper
Git blob bytes; a declared SHA alone does not establish provenance. Low-level
renderers are also used with explicitly synthetic fixtures.

Python 3.11+ is required for the engine and full author client. Portable CI and
structural validation retain Python 3.9+. Public CI uses shipped helpers and its
own repository read token, without cloning the private engine or a warm cache.
Narrow LF attributes preserve actual helper/manifest bytes through Windows Git
checkout. Existing attributes conflicts are normal reviewed upgrade conflicts;
never overwrite project-owned files to force an upgrade.

## CI frontend and preserved proof

The initial portable frontend moves byte-for-byte from Master 1.1.1 commit
`c396e64222cb334780096a2f9e283a78c92b3829`, `templates/workflow/ci_gate.py`.
Its SHA-256 is `8496073ab5c4deabacb26255420963d7a531cd4b6cc0541d6b29a6bb1462f307`.
Tracker owns subsequent compatible changes.

`python scripts/tracker_ci_gate.py plan|gate` is the portable read-only frontend.
In separated output its implementation is `.workflow/tools/tracker_ci_gate.py`;
the scripts path remains a compatible entry point. Its sibling imports use normal
Python script execution; the isolated import test explicitly supplies the local
helper directory. `python -I ...tracker_ci_evidence.py emit-context` remains the
self-contained direct producer. Never use a scripts forwarder as producer proof.

The frontend resolves only the exact official tracker-state ref, validates its
commit target, then reads the state at that immutable commit. Its transport is
restricted to GETs in the actual Actions/config repository. Source, integration,
main and promotion identities and actual native job/attempt/workflow/environment
binding remain in the exported core. Promotion or unsupported reuse stays fresh.
It grants no review, admission, state mutation, package installation or delivery.
Read [native proof](CI_REUSE.md), [delivery](DELIVERY_POLICY.md) and
[accepted completion](COMPLETION_POLICY.md) for those separate contracts.

## Trusted validation and release order

Master's compatible source keeps the exact canonical `ci_evidence.py` snapshot
and generated expected contract record under fixed `scripts/vendor/` paths. Its
structural validator delegates only to that trusted snapshot. Consumer helpers,
plugins, environment overrides or latest upstream are never imported by Master
validation. Master pins the official engine and expected manifest digest outside
the manifest, avoiding a hash cycle. A consumer manifest must equal that trusted
record before actual installed bytes are checked. Rehashing a tampered helper
and consumer manifest cannot create trust. Missing/newer/mismatched versions or
helper bytes fail clearly; no fallback clones the engine or disables a gate.

The complete portable gate closure includes CI evidence, delivery policy,
delivery receipt, batch policy, PR checker, task context and process helpers.
The full author client is a separate runtime. The producer remains byte-for-byte
self-contained; no validator module is split out, and historical proof retains
its original meaning. Completion/task-context fences and unknown old receipts
remain strict. No current Issue body is reconstructed as old reviewed proof.

A maintained source gate exercises actual raw/separated exports and engine-free
Python 3.9 clients. An immutable public Master peer is only a test input; candidate
engine overrides are explicitly provisional. Final compatible-pair acceptance
uses clean actual Master/Tracker pins with no override, real export/init/upgrade,
local-control preservation and adversarial contract drift. Tracker support is
reviewed/released first, then the exact compatible Master and canonical skill
mirror. Native gate/protection evidence, source release, installed readers,
service migration and consumer activation are separate facts.

## Maintained checks and saved fixtures

`Portable source conformance` runs for relevant helper/export/test changes. It
uses the actual engine checkout plus immutable public Master 1.1.1
`c396e64222cb334780096a2f9e283a78c92b3829` as a **provisional test peer**. The
candidate engine override is recorded, never labelled final published-pair proof.
The source command is:

```sh
python tests/verify_portable_contract.py --master /clean/public/master-peer \
  --old-master /clean/public/master-peer --old-tracker /clean/tracker-3.3.1 \
  --python39 /actual/python3.9 --output /evidence/portable-source.json
```

It verifies both layouts, six actual selected Master initializations, genuine
Task03 old-base upgrades, unchanged local controls/exceptions, attributes conflicts
without writes and engine-free Python 3.9 plan/gate clients. Structural policy
boundary comparisons detect a changed authoritative rule against the peer. This
source-only test peer creates no circular installed/runtime Master dependency.

`Final immutable portable pair` is a separate maintained input workflow for full
reviewed public Master and actually published Tracker commit IDs. Its read-only
release guard resolves the stable release's actual tag commit (including annotated
tags), rather than trusting `target_commitish`. It refuses an unpublished/moved
engine or a mismatched Master pin. It then invokes Master's single maintained
`tests/verify_completion_pair.py` with `--portable-contract --bounded
--require-pinned --selected-instructions`, real Python 3.9 and the genuine older
`0249474c265c22d386f0a1aaa915b0a345321bbd` Master /
`2c01481a86ee8e8af31736378574a91505c7f94f` Tracker source pair. No candidate
override can supply final acceptance. Native runs retain their JSON artifact and
run/attempt identity. Required GitHub protection versus maintained admission is
still the project's actual configuration; adding a workflow does not claim that
its job was made a required branch rule. Existing required full checks remain.

`tests/fixtures/portable_native.json` contains only generic synthetic saved source,
integration, main and promotion GET responses in each layout. It contains no live
consumer capture. `tests/portable_fixture.py:exercise` executes both real entry
paths under an isolated Python bootstrap, freezes only the fixture clock, rejects
foreign Actions identity/malformed completion fences and compares every consumer
file before/after. The clock is necessary so saved native test data does not expire
with tomorrow's wall time. Engine imports exist only inside the author-side data
generator; public consumers use the saved JSON and stdlib runner, never that
generator or private source. No fixture response is an actual runtime receipt.
