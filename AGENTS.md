# ACL project instructions

Owner overview: [requesting work and reading the Tracker](docs/repository/OWNER_GUIDE.md).

**Use the selected shared workflow on `main`; permanent `stage` is retained.**
The temporary [direct-main and post-main delivery selection](docs/repository/POST_MAIN_DELIVERY.md)
is active through its recorded idle cutover. Its initial source used the old
stage/main route; that dated admission/recovery history remains preserved.
[Instruction-only tasks](docs/repository/PROCESS_INSTRUCTIONS_ACL.md) have a separately
opted-in reviewed main route; application/mixed/policy/automation changes use frozen approved batches. Read
[adoption](docs/repository/ADOPTION.md) for the verified cutover and remaining
release boundaries. `master` is a frozen compatibility channel for old source
installations. The guarded ACL operator adapter loads the exact pinned shared Tracker.
For ACL operations use `python scripts/acl_task_tracker.py`; when the exact shared
guide says `python scripts/task_tracker.py`, substitute the ACL adapter. The
canonical scripts and `.workflow/tools` entry points retain exact upstream bytes
and do not run ACL's historical prepared-bootstrap preflight. Read
[early adoption](docs/repository/EARLY_ADOPTION.md) before any future policy cutover.
The completed cutovers and their original recovery procedures remain historical
evidence; current work follows the active policies below.

- [Selected Master 1.7.2 standard](https://github.com/MojoTheApe/master-repo/blob/4f148cbbbc2c9c65a7fdebba9b37551f91566e04/standard.md).
- [Repository workflow](docs/repository/WORKFLOW.md), [delivery](docs/repository/DELIVERY.md),
  [selected Tracker guide](docs/repository/TRACKER.md), [verification](docs/repository/VERIFICATION.md)
  and [file ownership](docs/repository/LAYOUT.md).
- [Current system](docs/system/PROJECT.md) and [engineering context](docs/system/AGENT_CONTEXT.md).
- [Installation/release/recovery](docs/runtime/DELIVERY.md) and [isolated staging](docs/runtime/STAGING.md).
- Machine-readable settings: `delivery.json`, `tracker/config.json`, `.workflow/adoption.json`.

Confirm your own actual running, waiting and stopped state through the shared
[executor activity commands](docs/repository/EXECUTOR_ACTIVITY.md), using the
guarded ACL adapter above. Report at real tool boundaries and once a minute while
running. After five minutes without confirmation, previously confirmed activity shows
Connection lost; never-confirmed activity stays Activity unconfirmed. Neither frees
ownership. Existing agents without a producer stay unknown until their actual
owners report. Do not run a detached reporter or confirm another agent's activity.

Deliver process updates separately from the product for every profile. Canonical
shared-support upgrades require the exact [process-support proof](docs/repository/PROCESS_SUPPORT.md),
whole-scope independent review and [ACL product-isolation evidence](docs/repository/PROCESS_SUPPORT_ACL.md).
Keep ordinary source/frozen-batch admission and all applicable checks. Proven
isolated process completion does not publish an ACL installer; actual product
changes and explicit publication requests retain their existing delivery controls.

Read the selected [task pause support](docs/repository/TASK_PAUSE.md). Phase-preserving
`pause stop/resume` is usable only after separately verified reader/every-writer
readiness and an owned idle outer8 activation. Source pins do not activate it.
Until then retain the adopted pre-activation stop/reclaim procedure and its
recorded phase-changing semantics; never call that a phase-preserving Pause.

Preserve employee data, existing installers and draft-only advertising controls.
Implementation requires a real Issue and a durable Worker claim. After the
[parallel cutover](docs/repository/PARALLEL_EXECUTION_ACL.md), ACL permits five
implementation tasks across all agent platforms. The active
[source-review pool](docs/repository/SOURCE_REVIEWS.md) selects five independent
source reviews under schema 3, with one separate integration review.
The explicit [batch cutover](docs/repository/BATCH_INTEGRATION_ACL.md) releases review at Approved
and gives one separate Coordinator ownership of at most five tasks through the configured route.
ACL-1874/1875 completed the source and guarded live-v3 cutover; upgrading compatible
pins does not repeat migration or activate an optional maintenance/CI evidence policy.
Use only available real sessions in isolated worktrees; capacity is not a promise
that a particular host can spawn five Workers. Use a separate non-implementing
agent for final review. The full shared skill is installed
once per agent environment from Master Repo; do not replace ACL's product skills.
