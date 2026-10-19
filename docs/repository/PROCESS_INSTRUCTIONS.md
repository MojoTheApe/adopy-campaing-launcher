# Reviewed repository instructions

This optional route lets a complete instruction-only source PR merge to main
without a stage or package cycle. The task, independent review and successful
configured checks remain required. The repository owner opts in explicitly with
matching `process_instructions` in `tracker/config.json` and
`workflow.process_instructions` in `delivery.json`:

```json
{"schema_version": 1, "required_checks": ["descriptor", "tracker-link", "Process instructions"]}
```

Names identify actual successful GitHub Actions checks on the exact source head.
The list must be unique, nonblank and include `descriptor` and `tracker-link`.
The opt-in requires an existing independent-review delivery policy; a malformed
manual config without one fails validation. Its delivery fingerprint does not change. An absent setting
preserves the existing legacy, execution-v1 or execution-v2/v3 lifecycle. Adoption,
service installation and deployment require their existing explicit authorization.
Export uses `export-template --process-instructions POLICY.json`; the exporter
never inherits this setting from the engine configuration. Master initializer and
validator support are tracked in [MR-16](https://github.com/MojoTheApe/master-repo/issues/16).

## Fixed eligibility

Only root `AGENTS.md`, root `CLAUDE.md`, and Markdown files beneath
`docs/repository/` qualify. A nonempty complete diff is reconstructed from both
immutable Git trees and verified Git blobs. Every changed file must be a normal
non-executable regular UTF-8 file, bounded to one MiB and without binary NULs.
Additions and deletions qualify; a rename qualifies only when both paths qualify.
Symlinks, executable modes, unsafe paths, missing content, malformed or truncated
trees cannot establish this route. Mixed changes, configuration, scripts, CI,
runtime, product and other documentation retain the ordinary delivery route.
Titles, labels, author claims and opt-out text cannot select the exception.

The exact opt-in must already exist in both authoritative base configuration and
base descriptor; a caller's temporary configuration cannot activate it. The
working source includes the exact main baseline. The exported portable helper
`tracker_process_instructions.py` exposes `policy(config)`,
`classify(request, config, pr)` and
`classify_local(config, repository, base_sha, head_sha)`. Classification returns
`None` for empty or mixed diffs, a complete immutable proof for eligible changes,
or raises `ValueError` when proof is unsafe or incomplete. The local classifier
uses the same predicate and complete Git objects. CI separately establishes the
actual repository, event, main/source refs and current revisions.

## Review, merge and recovery

Create and claim the actual task before work. A process source links exactly one
task and targets main using the normal ticket branch/title convention. Legacy
projects retain their existing task slot and distinct independent reviewer and
implementer attestation. Execution-v1/v2/v3 projects use the actual Worker handoff,
Reviewer claim/session and source review lane. In v2/v3 this route goes Review to
Merged: it cannot become Approved, enter an intake/batch or acquire Coordinator
integration ownership. Other tasks continue to use their ordinary flow.

`record-review` binds the independent report to the exact source head, main base,
resulting tree, complete instruction diff, task scope and process policy. Run
`check-merge` immediately before the real merge; it verifies current main, current
scope/dependencies, actual successful checks and current independent ownership.
Held stage, integration or running intake work blocks process review acquisition
and merge until that work finishes. A held process source review lane prevents
new intake or batch claims through the same state CAS, including retained fix
ownership. This prevents silently invalidating frozen work.

After the actual merge, use the normal reviewed `merged` dry run and apply. Its
first parent must equal the reviewed main base, and its tree must equal the
reviewed source tree; merge resolution or extra content invalidates the proof.
The retained process receipt is verified review/merge evidence. It makes the task
complete without pretending that a package was published or a deployment ran.
Public snapshots expose only a safe completion flag; reports, manifests, sessions,
claims and private history stay in durable private state. Dependencies and release
readiness use the same genuine completion receipt.

Concurrent writes fail the normal state CAS. Partial label/completion operations
can resume without losing review or ownership. A stopped execution Reviewer may
be replaced through the existing explicit takeover, with a fresh independent
review of the actual merged source and first parent before completion. Old
completed PRs cannot complete reopened execution tasks or changed task scopes.
After a completed process merge, the next ordinary stage candidate may bridge an
idle stage-to-main instruction gap only when the pinned helper proves the complete
eligible diff and every intervening first-parent merge has its retained current
verified process receipt, independent review, policy and unchanged task scope.
Unknown commits, missing receipts or unsafe changes fail. This read-only allowance
never resets a branch; current main remains the exact frozen candidate baseline.
Ordinary source checks, combined review, full CI, actual stage verification and
unchanged main promotion remain mandatory.

Implementation: [TT-79](https://github.com/MojoTheApe/task-tracker/issues/79).
