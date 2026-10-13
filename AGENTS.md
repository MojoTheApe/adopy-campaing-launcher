# ACL project instructions

**Use the shared workflow on `main` and permanent `stage`.**
[Instruction-only tasks](docs/repository/PROCESS_INSTRUCTIONS.md) have a separately
opted-in reviewed main route; application/mixed/policy/automation changes keep stage. Read
[adoption](docs/repository/ADOPTION.md) for the verified cutover and remaining
release boundaries. `master` is a frozen compatibility channel for old source
installations. The project client loads the exact pinned shared Tracker. During
the activation PR, ordinary admissions remain paused until its stage verification,
main promotion and normal `merged` handoff have actually completed.

- [Repository workflow](docs/repository/WORKFLOW.md), [delivery](docs/repository/DELIVERY.md),
  [Tracker](docs/repository/TRACKER.md) and [file ownership](docs/repository/LAYOUT.md).
- [Current system](docs/system/PROJECT.md) and [engineering context](docs/system/AGENT_CONTEXT.md).
- [Installation/release/recovery](docs/runtime/DELIVERY.md) and [isolated staging](docs/runtime/STAGING.md).
- Machine-readable settings: `delivery.json`, `tracker/config.json`, `.workflow/adoption.json`.

Preserve employee data, existing installers and draft-only advertising controls.
Implementation requires a real Issue and a durable Worker claim. After the
[parallel cutover](docs/repository/PARALLEL_EXECUTION.md), ACL permits five
implementation tasks across all agent platforms. The prepared
[source-review pool](docs/repository/SOURCE_REVIEWS.md) selects five independent
source reviews; the live v2 single source lane remains until its guarded v3 migration.
The explicit [batch cutover](docs/repository/BATCH_INTEGRATION.md) releases review at Approved
and gives one separate Coordinator ownership of at most five tasks through stage/main.
Source child ACL-1875 finishes its current v2 source/batch/stage/main handoff before
the supported live v3 state migration. The v1 cutover evidence remains historical.
Use only available real sessions in isolated worktrees; capacity is not a promise
that a particular host can spawn five Workers. Use a separate non-implementing
agent for final review. The full shared skill is installed
once per agent environment from Master Repo; do not replace ACL's product skills.
