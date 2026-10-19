# Owned maintenance and trusted CI evidence — TT-107

This engine supplies optional support for the reviewed Master requirements in
[MR-26](https://github.com/MojoTheApe/master-repo/issues/26). Installing it changes
no consumer policy, task ownership, state or service. ACL and React Forge retain
their existing pins/configuration and product collectors. Publish compatible
admission writers and the single snapshot writer before reserving a maintenance
interval. Ordinary legacy and execution schemas 1/2/3 keep their adopted behavior.

## Maintenance intervals

`maintenance show` reads private ownership, history, actual readiness and current
state SHA. `maintenance reserve --actor Maintainer-AB12 --session ACTUAL_RUN
--report-file PRIVATE_REPORT [--apply]` previews or reserves one interval through
the same file/SHA compare-and-swap as task admission. The report records actual
authorization and compatible-writer/publication evidence; it contains no secrets.
Persist the returned private claim and reuse it with `--claim` on retries. If the
response is lost after commit, inspect `show` and recover the actual matching
owner. Never manufacture a replacement identity or infer that an owner stopped.

Reservation changes the outer state protocol to 3 and retains the original format
as `maintenance.base_schema_version` (1 or 2). Older engines reject this outer
protocol. A writer that read before reservation loses its stale CAS; after resume,
private history still changes the file SHA, preventing an ABA stale write. New
Workers, source Reviewers, intake/batch owners and integration Reviewers wait.
Existing actual owners retain resume, stopped-owner transfer, handoff, approval,
return, cancellation, stage/main completion and other supported drain operations.
A new repair claim is still new admission; resume the gate explicitly if required.

Reservation is separate from readiness. Active claims/reviews/intakes/batches,
creating-Issue journals, pending author-return/archive journals, release rollover
and unpromoted stage make the interval draining. Queued/Approved sources remain
unfinished but do not themselves own an active lane. The normal migration guards
still validate preserved queues, approvals, exact scope/base/policy and history.
`execution migrate --apply` under a held gate additionally requires its actual
Maintainer `--actor`, `--session`, `--claim`; migration retains the gate. It does
not discard owners, fix approvals, reset stage or complete tasks.

`maintenance resume` with the actual owner/claim/report restores only admission
and the retained underlying state format, adding audit history. Committed resume
and transfer retries are idempotent; an old claim cannot release a later interval.
`maintenance transfer --previous-claim ACTUAL_CLAIM --stopped --actor
Maintainer-CD34 --session ACTUAL_REPLACEMENT --report-file PRIVATE_REPORT --apply`
requires verified stopped execution and a distinct actual replacement. It leaves
all task/reviewer/Coordinator ownership untouched. Preview never persists a claim.

Only a held interval adds public `maintenance`: `admission_paused`, static
`state` (`draining` or `idle`), and a validated UTC `reserved_at`. Claims, actors,
sessions, reports and audit history stay private. Never-held and resumed projects
retain their previous public payloads and digests, including the ACL root bridge.

## Explicit CI evidence policy

`ci_evidence_policy` is optional; export does not inherit it. Schema 1 has exactly
`schema_version: 1` and a nonempty `profiles` map. Adoption must be committed to
actual current-main `tracker/config.json`; source-local or temporary configuration
cannot establish trust. Master owns any descriptor mirror/consumer adoption.
Existing configured required checks, independent review, actual stage/delivery
freshness and manual/release policy remain authoritative. Coverage is advisory;
consumer-owned CI runs every reported missing job and keeps its required aggregate
successful only after covered plus fresh jobs actually pass.

Each profile has `workflow_path`, immutable Git `workflow_blob_sha`,
`producer_files` (repository paths to immutable Git blob SHAs), `jobs`,
`max_age_seconds` (explicit 1..2592000 lifetime from actual job completion), and
`metadata_mode` (`commit` by default when authoring; `tree` requires explicit
review). Protocol validation requires the field to be explicit. Commit mode
requires the actual tested commit, including Git metadata. Tree mode requires the
entire tested tree to equal the actual candidate; it must only be adopted when
commit/branch/history-sensitive behavior is excluded by review.

A full native job name maps either to `{ "mode": "fresh" }`, for contexts that
cannot be proven, or to a fixed-job definition with exactly:

- `mode: "fixed"`, unique static `workflow_job`, stable exact `runner_labels`;
- `producer_step`, `producer_path`, and ordered nonempty `verification_steps`;
- `context`: digest-pinned `container`, explicit `host_independent: true`, exact
  observed `toolchains` (`python3`, `node`, `npm`, full versions), nonempty
  `dependencies`, and `build_inputs`/`fixtures`, each a path-to-Git-blob map.

The container must contain the reviewed resolved dependencies; lock/build/fixture
blobs bind those inputs independently on the target. Review its immutable build
and verification commands before adoption. Commands that install mutable inputs,
external fixtures, host-dependent behavior, Windows/installer acceptance or an
unproven environment use `fresh`. This is a bounded container/userland protocol,
not a claim to resolve arbitrary environments, runner images or external systems.

V1 deliberately accepts static **JSON-formatted workflows** (an unambiguous YAML
subset). Each fixed native job has the exact profile name, stable runner labels,
digest-pinned container, no matrix/reusable job/inherited env/defaults/conditions,
no self-hosted runner/mutable services/environment, and begins with `actions/checkout@FULL_IMMUTABLE_SHA` with default checkout. The
next step is exactly `{ "name": PRODUCER_STEP, "run": COMMAND }`, where COMMAND
is the shell-quoted sequence:

```sh
python3 -I scripts/tracker_ci_evidence.py emit-context --profile PROFILE --job 'FULL NATIVE JOB NAME'
```

Use the actual adopted producer path. Pin the exported helper's Git blob in
`producer_files`. V1 additionally verifies its bytes are exactly the maintained
self-contained producer at this pinned engine; wrappers/custom import closures
are unsupported and require fresh jobs. Isolated Python (`-I`) excludes repository
sidecars, `PYTHONPATH` and user-site hooks from producer imports. Every
following step is an explicitly named unconditional `run` verification step,
listed in order. Arbitrary annotations, uploaded JSON, cache hits, green aggregate
names and run-level artifact digests never establish producer/context trust.
Unsupported YAML/matrices/reusable workflows require fresh jobs until a separately
reviewed producer protocol supports them; existing product collectors continue.

The trusted producer observes actual Git HEAD/tree and input blobs, actual exact
toolchain versions and native run/attempt/workflow identity before any repository
test or dependency hook runs. It emits one versioned marker. The shipped producer
retains the step for two seconds after flushing the marker because native step
boundaries have second precision; a marker from a later test cannot enter its
proof window. Logs are read privately, never stored or published wholesale.

## Collect and plan coverage

```sh
python -m tracker ci-evidence collect --profile PROFILE --run ACTUAL_RUN --source-pr PR --apply
python -m tracker ci-evidence coverage --profile PROFILE --pr PR --head EXPECTED_HEAD --context source
```

Collection independently verifies the official repository, actual successful
native run and latest attempt, immutable workflow/producer blobs, each actual
native job/check linkage, producer step window and every actual successful test
step. For a PR it verifies the **actual default synthetic checkout** and its
ordered current base/head parents; `run.head_sha` alone is not tested content.
An open ready unchanged official PR is required. Historical/closed PR synthetic
checkout guesses are unsupported. Push/dispatch collection binds their actual
head checkout, but v1 reuse remains tied to the explicit supported PR contexts.
The recorded source binding retains current task scope, branch/base/head and
adopted delivery/execution policy. No file-import command is provided.

The versioned private record retains tested commit/tree, profile/context digest,
actual run/job/attempt/URLs, native job-log provenance and an empty artifact list.
V1 does **not** infer a producing job or attempt from native run-level artifact
metadata, does not reuse packages, and supplies no installer/runtime attestation.
A later package adapter requires actual immutable artifact-to-job proof. CAS
collection conserves all unrelated state, can occur while admission is held, and
recovers an uncertain committed write by its stable proof identity.

Coverage revalidates every retained producer against native GitHub metadata/logs;
a hash-correct uploaded/fabricated stored record cannot become trust. It resolves
target context independently from current-main adoption and actual target Git
blobs. Changed content, workflow/helper, locks/toolchains/inputs or actual attempt
requires fresh proof. Missing/failed/skipped/cancelled/ambiguous jobs are missing,
never inferred from a successful aggregate. Covered output names actual run/job
IDs, attempt and producer URL; union retains those real producer references and
still reports every missing platform/job.

For `--context integration`, the actual frozen integration batch, unchanged open
Approved source PRs, current retained independent approvals/scope/base/policy,
ordered membership and native merge composition are revalidated. Source evidence
can cover only the **entire identical combined candidate tree**; different source
trees cannot be unioned to prove a changed batch. One member still requires normal
independent combined review. Manual, release, promotion and runtime contexts
report all jobs missing and preserve their existing full/fresh product gates.
No coverage result replaces `check-pr`, `check-merge`, actual review or delivery.
