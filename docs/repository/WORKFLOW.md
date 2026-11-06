# ACL repository workflow

## Current workflow

- **Start in Task Tracker.** Use the existing ACL task or create one for new work.
  Claim it before editing and link its PR. Use a separate branch from current
  `main`. Save working fixes from a local pilot to that branch and record the
  tested revision in the task so another session can retrieve the same code.
- **Review and integrate.** Ordinary changes receive independent source review,
  then go through a reviewed batch of up to five tasks into `main`. Stage is
  disabled. Its branch is retained, and `master` serves legacy source installs.
  Eligible repository instructions use the [short reviewed route](PROCESS_INSTRUCTIONS_ACL.md).
- **Deliver and record.** For product changes, publish the Windows package
  built from the verified revision to the [stable release channel](https://github.com/MojoTheApe/acl-releases/releases/latest).
  Done follows the [published-package verification](../runtime/DELIVERY.md) and
  its Tracker record. The release history and the version tested are separate
  from the merge status.
- **Record exceptions in the original task.** For a local build transfer, link
  both source revisions and record the selected result, checks and rollback.
  For an integration or release failure, keep the first error, the affected
  version, the run link and the actual resolution. If GitHub merged the PR
  outside Tracker, follow the [manual-merge recovery checklist](#recover-a-manual-github-merge).

### Hand off a tested local result

Before another executor takes over, push the tested source to an accessible
GitHub branch and put its branch/PR link and exact tested revision in the original
task. Say explicitly which changes remain only on the local computer; a branch
name alone does not establish that the tested content is available remotely.
Keep the existing handoff procedure and actual ownership.

After an exceptional transfer, manual merge or recovery, finish the same task's
comment or handoff with a short outcome: what happened; the accepted version and
its source; which changes from the shared version were preserved; links to the
existing verification and any release; and unfinished work with its task links.
Keep the first failure and actual resolution. This uses the existing task, not a
new incident register or a report for every commit. Inaccessible local files stay
uninspected, and a transfer must preserve other work and rollback information.

New requirements for an already accepted result belong in a linked follow-up task;
keep the original acceptance and decisions. Correcting a typo or clarifying
history alone needs no new engineering task. If an old record no longer matches
an already accepted result, inspect the requirements difference and use the
supported reconciliation procedure; do not rewrite acceptance or set Done by hand.

### Recover a manual GitHub merge

1. In the original task, link the merged PR and batch ID. Record why it was
   merged manually and link any existing release and verification evidence.
   Preserve the original review and ownership history.
2. If the original batch still holds the exact reviewed candidate, use its
   existing handoff to record the merge. For a cancelled direct-`main` batch, use the pinned
   `scripts/acl_task_tracker.py batch adopt-merged` preview with the PR and batch
   ID. Verify the exact merged contents and use the existing checks. Record
   independent review of that merge, then pass `--apply` with the preview's
   `--expected-recovery` and the review `--evidence-file`. See the
   [recovery procedure](BATCH_INTEGRATION.md#recover-a-cancelled-batch-already-merged-in-github).
3. Record any applicable acceptance, run `merged` for the actual PR, and finish
   with `batch close-sources`. If a published release contains that exact
   change and its verification covers the accepted result, use
   `record-delivery` with that evidence. Reuse the published package and its
   verification. If the evidence is missing, leave delivery pending and record
   what is needed.
4. Read the task back from Tracker. Confirm **Done** and verified delivery for
   product work, and check that the recovered batch released its integration slot.

Use the [owner guide](OWNER_GUIDE.md) for reading the board and the
[process history](CHANGELOG.md) for past changes. The
[branch](../BRANCHING.md), [board](../TASK-BOARD.md) and [release ledger](../RELEASES.md)
pages retain their historical text.

Read the exact [selected shared guide](TRACKER.md), the [ACL support/cutover plan](EARLY_ADOPTION.md) and the guarded operator mapping in [AGENTS.md](../../AGENTS.md).
Shared contract references retain canonical bodies; `_ACL` documents preserve the original local cutover history and stronger controls.

## Current executable workflow provider

Use the exact published Task Tracker revision selected by
[tracker/engine.json](../../tracker/engine.json) and
[delivery.json](../../delivery.json). ACL-2421 selects published Tracker3.27.3 at
`e94e9a4c7f5291631cf9e6046dc3a719e7e04d35`. ACL-2417's Tracker3.26.0 recovery
support remains history. The previous ACL-2351 selection of
Tracker3.24.1 on 4 November 2026 remains preserved as history.
The original ACL-2156 selection of Tracker3.14.1 remains in the
[dated provider history](STANDALONE_PROVIDER_ACL.md#historical-adoption--acl-2156-24-october-2026).
`delivery.json.workflow_provider`, the engine lock and immutable private-provider checkout name the
same executable source. The descriptor uses the source-owned private-checkout transport with a separately authorized dedicated read-only key; it validates ACL only as data after provider authentication is removed.
[AGENTS.md](../../AGENTS.md) links the current Master Repo 1.9.0 guidance.
The descriptor and template baseline retain the provider's audited Master 1.8.0 ancestry.

Use the exact official Tracker source and its isolated
`scripts/workflow_provider.py` entry point for future supported rendering. Read
[the local provider adoption](STANDALONE_PROVIDER_ACL.md) and
[the immutable source procedure](https://github.com/MojoTheApe/task-tracker/blob/e94e9a4c7f5291631cf9e6046dc3a719e7e04d35/docs/STANDALONE_WORKFLOW_PROVIDER.md).
Preserve all local rules below, true pristine ancestry, independent review and
full applicable checks. The native configuration, adopted execution3, capacities,
target, required checks, IDs, existing owners and original task scope are unchanged.
ACL-2423 separately declared the four existing process controls and three tests
in `delivery.json`; that local boundary grants no approval by itself. TT-188
activated lossless outer9 storage, retaining the unchanged inner pause8 contract,
all history and existing ownership. See [the archive guide](STATE_ARCHIVE.md).
ACL-2424 connects the existing bounded review-handoff wait and prepares delivery
continuation through the published original-request proof. The normal receipt
writer uses current-main helpers; product verification and publication keep the
original product checkout, CI, tag, target and actual acceptance.
Instruction schema1 remains selected: only the later separately reviewed
ACL-2425 selection can enable independent instructions while a product waits.
Until then the existing pending-release admission guard remains active.
ACL-2099 is a separate preparation-only selection;
its own package must be verified through the retained actual preselection config
before any separately authorized owned idle adoption.

The original ACL-2156 migration retained its explicitly requested package delivery;
that dated obligation is not a new release request for every later process update.
Current completion follows the process/product boundary below and any actual
publication request. A pin alone grants no completion waiver or live activation.

## Adoption boundary

The owner selected an installed Windows application with automatic updates, not
manual Git delivery. Target profile: `package`, with temporary no-stage batches into `main`; retain
permanent `stage` for later re-enablement. Read [POST_MAIN_DELIVERY.md](POST_MAIN_DELIVERY.md)
for the selected contract and separately verified activation boundary.
The completed source and Tracker cutover is recorded in [ADOPTION.md](ADOPTION.md).
Use the pinned guarded ACL adapter and the currently activated route below.
Initial source ACL-1924 used old-stage admission before its recorded idle transition.
Retired master clients and one-time activation pauses are historical only;
package publication and employee updates retain their separate release rules.

## Select the route by actual changed files

Read [PROCESS_INSTRUCTIONS.md](PROCESS_INSTRUCTIONS_ACL.md). Once its explicit policy
is already adopted in main, normal `AGENTS.md`, `CLAUDE.md` and repository-process
Markdown changes can use one-task Review -> independently reviewed/checks -> main
-> Merged. They hold the source review lane through merge and do not enter Approved.
No active integration/stage candidate may be displaced. Runtime/system/product,
unknown/mixed, policy/helper/CI/config changes retain the reviewed frozen-batch route below.
Initial adoption itself cannot enable its own shortcut. Source changes and any
separately authorized live activation remain distinct.

## Tasks, branches and review

Ideas start Idea; accepted tasks start Backlog. The owner selects To Do. Starting
development requires an explicit request, a real Issue, verified In Progress and
an implementation claim under the matching five-slot execution policy. Read the entire Description and Specifications plus
linked context. Classify by the existing six modules, preserve dependencies and
release membership, and never steal another Worker's claim. The completed
[batch cutover](BATCH_INTEGRATION_ACL.md) and [schema-3 activation](SOURCE_REVIEWS.md)
retain their dated evidence; current work preserves actual held ownership.

Keep the existing ACL IDs: 71 historical aliases plus Issue number + 1000 for new
IDs. Copy IDs from the Tracker. Branch: `work/acl-1234-short-name`, or
`codex/acl-1234-short-name` when the host requires that prefix. PR title:
`[ACL-1234] Summary`. Execution v1/v2/v3 requires exactly one task per source PR.
The PR body has exactly one `Tracker issues: ACL-1234` line and that Issue links back to
it. Combined v2 integration/promotion PRs instead list the frozen members and one
`Tracker batch: ID` line; see [batch integration](BATCH_INTEGRATION_ACL.md). GitHub PR numbers are separate. No automatic issue-closing keywords in commits
or PRs. Old IDs and history are not renamed during migration.

Implement in an isolated task worktree, self-check and update relevant system and
process history. For several requested independent tasks, the current agent acts
as Coordinator and delegates one real Worker per task up to both free repository
capacity and actual host capacity. Keep dependent/conflicting tasks queued. Workers
create their PRs and explicitly hand off their claims, freeing implementation slots.
Review labels alone do not release claims. Record Worker, Coordinator and Reviewer
separately with neutral opaque identities; GitHub Assignees remain real accounts.

For ordinary/application work, execution schemas 2/3 separate source approval from
integration. A separate real,
non-implementing Reviewer acquires a source-review lane and reviews the
complete current scope/head/base. Record the report, keep required source checks,
then use `batch approve` to move Review -> Approved and release that lane. Each
source keeps its genuine Worker and independent Reviewer. Labels cannot approve
work. Ordinary batch source PRs target main and remain open; their direct merge is refused.
The proven instruction-only route is the explicit exception described above.
Existing queued stage-targeted PRs become approval-only after the supported migration.

The active schema-3 policy permits five source Reviewer runs independently of
five Workers, one integration Coordinator and one combined-batch Reviewer. Use the
PR/claim to identify each source lane; returning, resuming or approving one source
preserves all other owners. Source review/approval may continue during batch review.
Instruction-only reviews consume source capacity and still block new intake/batch
creation; their main admission waits for existing intake/integration/stage ownership.
See [SOURCE_REVIEWS.md](SOURCE_REVIEWS.md) for the completed ACL-1874/1875 source,
publication, idle migration and fresh-board evidence. That runbook retains the
historical old-v2 completion recovery; it is not the current policy. Compatible
standard/engine upgrades preserve genuine current-policy ownership through their
handoff and do not migrate state or reserve maintenance merely to change pins.

For changes before approval, the Reviewer returns review and the author takes a
fresh implementation claim before editing the original branch. After approval,
use the owning Coordinator's `batch return`, followed by an author claim, checks,
handoff and fresh independent approval. Changed code, main/base, task scope or
policy requires the current exact review/admission evidence enforced by the pinned
engine. It does not automatically invalidate test results for unchanged inputs
within the adopted collector's actual coverage. A new agent/session alone needs no
blanket test rerun. A Reviewer who edits becomes an implementer; a different agent
must approve. Do not fabricate a second identity for one run.

On an authorized ship request, the Coordinator freezes the current eligible
Approved intake, then selects sequential batches of up to five. New approvals wait
for another intake. One urgent task is valid; the Approved backlog has no size cap.
One separate Coordinator claim holds the frozen batch through integration review,
checks and the configured integration route. Preserve every source author, tip, review and member ID.
Follow [BATCH_INTEGRATION.md](BATCH_INTEGRATION_ACL.md) and the exact pinned guide for
assembly, independent combined review and recovery. Execution-v1 source-adoption
commands remain in force only until the documented old-policy handoff completes.

Use `check-pr N` for early linkage feedback; run `check-merge N` once immediately before merging.
It includes linkage, so do not repeat a separate immediate `check-pr` call. Keep
the distinct required review, merge-recording and delivery actions.
Final admission checks current review, CI and any configured stage evidence. These agent commands are
not GitHub branch protection. Verify actual GitHub controls; repository visibility
alone proves neither branch protection nor ruleset enforcement. Never present
an unenforced agent rule as a server-enforced guarantee. Do not purchase a plan or
make the repository public as an implicit part of adoption.

## Current pause and retained pre-activation history

ACL-2414 completed the owned idle activation on 5 November 2026. Use the selected
[pause/resume commands](TASK_PAUSE.md) for a confirmed stopped executor. An actual
ACL-2414 implementation pause and resume preserved In Progress, its branch and
work location while releasing the old claim and acquiring fresh authority.
The following pre-activation procedure is retained as historical guidance.

The source includes compatible phase-preserving [pause support](TASK_PAUSE.md),
but a pin update does not activate it. Use `pause stop/resume` only after actual
owned idle outer8 activation with compatible reader/every-writer evidence. Until
that boundary is verified, the previously adopted procedure below remains in
force. Its stop moves to To Do; it does not preserve Review or Delivery phase.
Keep its historical instructions and evidence distinct from the new mechanism.

Accept the owner's direct confirmation that an agent has stopped; do not demand
Windows access solely to reconfirm it. Read the actual claim and use the exact
pinned `execution stop` under the current real owner. If that owner has stopped,
use supported `execution takeover --previous-claim ... --stopped --reason ...`
with the acting Worker's real identity before stopping its new claim. Preserve
the recorded branch and original owner/worktree in the handoff and transfer
history. An isolated administrative location must be described honestly; it is
not a copy of inaccessible Windows work. Keep uninspected contents unknown.

Verify the requested claim is gone and the task is recorded stopped/Paused.
The current stop command also adds `flag:blocked`. For a purely voluntary pause,
after checking actual dependencies and obstacles, use the existing
`python scripts/acl_task_tracker.py set ACL-ID --no-blocked --reason "Owner-requested pause; no remaining blocker"`.
Do not remove a real blocker, change requirements or bypass a failed ownership
guard merely to free a slot. Review queue length and reserved capacity do not
prove that agents are running.

Before resuming implementation, inspect retained work, current scope/main and
dependencies; use normal supported ownership and preserve the original branch
where appropriate. Continue individual independent review, Approved integration
and required package delivery. A pause neither accepts partial work nor removes
the existing checks or delivery condition.

## Stage, main and completion

The global [process/product boundary](DELIVERY.md#process-and-product-delivery)
applies to every profile. A published canonical shared-support upgrade retains
ordinary source approval, frozen-batch composition, combined review and required
checks. Only the exact [native proof](PROCESS_SUPPORT.md) and genuine independent
whole-scope/product-isolation review can establish process delivery without a
consumer product-release request. This is not the instruction-only shortcut.
The package publication clauses below apply to product outcomes and explicit
product requests; preserve existing requests and unfinished product acceptance.

The sequence below applies to ordinary/application work. Proven adopted
instruction-only tasks hold their independent source review through direct main
admission and complete with the normal verified process receipt, as described in
[PROCESS_INSTRUCTIONS.md](PROCESS_INSTRUCTIONS_ACL.md). They do not enter Stage or
require an application installer/delivery receipt.

Approved ordinary source branches -> reserved `codex/batch-ID` integration branch ->
main, after independent combined review and full applicable CI. Ordinary one-task
sources stay approval-only. For product outcomes, standing owner policy includes package delivery when
integration/main merge is approved; the agent automatically records the genuine request through
`batch begin --deliver` unless the owner explicitly requests source-only/skip-release.
Main CI then continues publication,
actual published Windows package verification and exact-task delivery without a
second routine approval. Failed or missing delivery remains unfinished. Follow
[POST_MAIN_DELIVERY.md](POST_MAIN_DELIVERY.md) for commands, serialization, safe
retries, initial old-policy source admission and re-enabling Stage. Keep original
source branches and the permanent stage branch/history.

Merged means all of a ticket's implemented code scope is actually included in main.
For product/application tasks, package completion additionally requires an immutable published installer and the
agreed installation/update verification. Record delivery against the exact merged
content. Do not wait for every employee to update; do not claim all have updated.
A partial task stays active even after a partial PR. A Blocked ticket cannot pass
normal PR admission. Preserve outstanding criteria and the owner's full outcome
when decomposing implementation into linked children; review the actual scope.
A change to the requested outcome needs the owner's agreement. Do not erase a blocker
or mark unfinished checklists complete to force a merge.

Source-only, research and operations outcomes keep their actual acceptance criteria;
never reclassify product work as non-code to bypass a missing installer or pilot.
For historical tasks follow the explicit reconciliation in ADOPTION.md.

## Verification and collaboration

Follow [VERIFICATION.md](VERIFICATION.md) for the pinned common verification and
coordinated-upgrade guidance. Keep ACL's existing bounded `acl_ci_reuse.py`
collector and its actual proof rules. The optional TT-107 native protocol does not
cover ACL's dynamic YAML, Windows matrices or installer acceptance; exported helper
support enables no `ci_evidence_policy`. Reuse only covered applicable jobs and
retain independent composition, exact-stage and package-delivery checks.

Use the requirements and isolation rules in [engineering context](../system/AGENT_CONTEXT.md).
Run focused affected tests locally, plus the applicable scanner/smoke checks.
The complete product suite and applicable packaging checks run in hosted CI on
the final candidate, as detailed below; do not duplicate them locally by default.
CI (Linux and Windows test jobs) remains required for code, dependencies, packaging and CI changes. Record success, skip, infrastructure failure
and unrun manual checks separately. Group related fixes into a meaningful ready
submission while preserving useful early feedback. Do not request another run for
a successful unchanged candidate without a new failure, change or unresolved concern.
Existing GitHub events still start their configured checks; these instructions
do not authorize workflow/settings changes. Inspect actual outcomes before retrying
an uncertain command. Follow [VERIFICATION.md](VERIFICATION.md) for supported reuse
and short observations on ordinary work.

### Fast feedback before full applicable checks

Use the project-owned [fast/full plan](VERIFICATION.md#fast-feedback-and-full-verification)
for the exact `Classify and check project tooling` commands and omissions.
Its tooling feedback helps group local corrections; it does not replace
`ACL verification`, `Installer verification` or actual delivery acceptance.
Metadata edits retain linkage-only feedback; new code candidates keep normal
applicable checks and only superseded same-PR runs cancel.

### Running the test suite

The full suite runs in parallel with pytest-xdist (`requirements-dev.txt`):

    python -m pytest tests/ -q -n auto --dist loadfile

CI (`.github/workflows/ci.yml`, ACL-1582) splits the suite by platform:

| Job name (exact) | Runner | Python | Files |
|---|---|---|---|
| `linux tests (<py>, <k>)` | ubuntu-latest | 3.11 and 3.13 on pushes and manual runs, 3.13 on most PRs (below); shards k = 1..3 | every `tests/test_*.py` without the `windows_only` marker: `python scripts/acl_test_shard.py --platform linux <k> 3` |
| `windows tests (<py>)` | windows-2022; actual Server 2022/build 20348 checked before tests | 3.13 | the complete `windows_only` + Windows smoke selection (`--platform windows 1 1`), after secret scan and startup smoke; checked each-once phases: general files in parallel, then ACL-1309, ACL-2063 and lock stress separately below |
| `ACL verification` | ubuntu-latest | - | aggregates `scope`, both Linux and Windows test jobs |

A test file that needs Windows says so at module level, with the reason:

    pytestmark = pytest.mark.windows_only(reason="msvcrt byte-range locks")

`tests/test_acl_test_platform.py` (also run in the fast scope job) fails when an
unmarked file uses Windows-only APIs or assumptions: `msvcrt`/`winreg` imports,
`ctypes.windll`, `os.name`/`sys.platform` Windows branches (including
`skipif(os.name != "nt")`, which would silently drop coverage on Linux),
`powershell`/`pwsh`, `.ps1`, `LOCALAPPDATA`, Windows commands (`taskkill`,
`cmd.exe`, `*.bat`...) and backslash paths inside `assert`. A line that only
mentions such a thing as data carries `# linux-ok: <why>`. When a test fails on
Linux only because of paths or CRLF, mark it; do not silently change it in a way
that hides the Windows behaviour. `python scripts/acl_test_platform.py report`
prints the split and every reason. Without `--platform`, `acl_test_shard.py`
shards all files as before; the shards together are exactly the platform's
selection (`tests/test_acl_test_shard.py`). The hosted runners of this private
repository have two CPUs, so xdist alone did not shorten CI.

The job names are fixed with `name:` because `scripts/acl_ci_reuse.py`
(`REQUIRED_JOBS`) and the release gate (`scripts/acl_publish_release.py`) look
jobs up by name; `tests/test_ci_layout.py` keeps them in step with the matrices.
A reuse proof or a release needs every Linux shard, the Windows job and
`ACL verification` of one run. Runs of the old layout (`test (<py>, <k>)`, all on
Windows, before ACL-1582) are refused, not translated: a release of a tree that
only has such a run needs a fresh `ci.yml` run on main.

Python versions: under execution schemas 2/3, ordinary one-task approval sources
into main (and retained v1 stage sources) keep the affected-check selection: 3.13,
with both Linux versions and installer when dependency/packaging inputs change.
A combined batch into configured stage or main, stage/main pushes, promotion, manual run and any
unknown route require Linux 3.11/3.13, Windows 3.13 and installer. Source docs-only
classification remains bounded; combined docs-only batches still require the full
applicable checks. Execution-v1 selection remains unchanged. The scope job reads
matching explicit descriptor/Tracker policy and fails to full checks on uncertainty.
Per-job reuse requires the exact tested Git tree; separate source trees never prove
a combined candidate. Release/manual rules stay in force.

A new push to a PR branch cancels that PR's superseded CI and installer runs;
stage/main/master pushes and manual runs retain their own run identities.
The installer build/install job stops at 20 minutes.
The Chromium install step (Linux and Windows) runs through `scripts/ci_retry.sh`: 3 attempts, each cut at 300 s (Linux) or 180 s (Windows), apt sources switched off the stalling Azure mirror, apt locks cleaned between attempts, the browser download cached by Playwright version, and a hard step limit (16 min Linux, 12 min Windows). The Linux test job stops at 32 minutes (shard 1 normally takes 20-22 min, the longest green run was 29.2), the Windows one at 30 (ACL-1710: an `apt-get update` hang used to burn 35 minutes per job).
`--dist loadfile` keeps every test file on one worker: more than 80 files start
their own Chromium in a module fixture, and spreading one file over all workers
starts that browser once per worker (on the owner's laptop the test-by-test
split took 41 min, `loadfile` 20 min; serial was 82 min on the same machine).
Where each suite runs (owner decision 2026-09-30, ACL-1397): workers and
reviewers run only the affected tests locally; the full suite runs in CI, and
green CI is the acceptance signal. If you do run the full suite locally on a
machine other sessions share, use `-n 4` or less instead of `auto` so the others
keep CPU; a single file or a few tests are faster without `-n`.

Run with `ACL_LIBRARY_REPO`, `ACL_LIBRARY_TOKEN` and `ACL_LIBRARY_BRANCH` empty;
`tests/conftest.py` also sets them to empty strings in every worker process, so
no test reaches the live team library even when the machine `.env` configures it.
Each test gets its own `tmp_path`, machine config folder (`ACL_CONFIG_DIR`) and
telemetry journal from the conftest fixtures.

Windows serial phases (ACL-2164): `test_acl1309_two_windows_people.py` and
`test_acl2063_two_windows_people.py` each use the machine's real Task Scheduler
and scoped batch-logon admission. `test_shared_file_locks.py` intentionally
saturates every CPU. Run these three files separately, in that order, after the
general parallel Windows files, using `-n 0` for each phase. The existing selector
still supplies every Windows-only/smoke file. Its runtime planner refuses missing,
extra or duplicate plan members and verifies that the phases cover the exact
selection once. Internal real users, simultaneous writers, all-core burners and
all original deadlines/assertions remain unchanged; the whole job retains its
30-minute limit. Host pinning alone did not prevent the retained COM recurrence
on Server 2022; this isolates shared test resources without claiming the unknown
system-service cause is repaired.
Several real-Chromium tests check the robot's own time budgets (a tab that does
not answer in 2-5 s counts as frozen); on a CPU-starved machine they can fail in
any mode, serial included. If a budget/timing test fails locally, rerun that
file alone before suspecting the code, and lower `-n`.

Required admission names: `descriptor`, `tracker-link`, `ACL verification`,
`Installer verification`. Aggregates require all applicable underlying jobs; a
metadata-only PR may skip the Linux and Windows test jobs only through the existing
closed classification.
Combined batch, actual stage, promotion/main and manual contexts require complete
applicable checks. A push or PR may reuse successful applicable jobs of the exact
same Git tree (ACL-1565/1649, `scripts/acl_ci_reuse.py`); the run summary links
that evidence. Combined batch PRs run their own full matrix without borrowing jobs.
Release validation fetches the reused run IDs recorded by the successful main
scope job (annotation, or its older classifier JSON log), revalidates their exact
tree, merged-PR binding and jobs, and combines them with main's successful jobs.
Complete proof returns without a run-list lookup; partial proof retains bounded
discovery and fail-closed validation. Manual runs and uncertainty run in full. Never treat separate
source checks as combined-content proof. The private pinned Master
Action's access must be verified in a real consumer run. Draft descriptor success
is structural feedback, not activation or live environment evidence.

The owner's past CI exceptions apply only to their exact recorded PRs/commits.
Unavailable required CI blocks this migration's merge unless the owner explicitly
approves a concrete new exception. A source exception never promotes a release.
Merge within the authorized task after valid checks/review; do not add a routine
extra permission round. Runtime work outside the approved package delivery retains its separate authorization.

Preserve other worktrees, machine configuration and active file ownership. Never
stash/reset/clean another session, stage all files blindly or infer abandonment.
Fetch current refs, record base/head and inspect explicit push destinations. Do not
silently copy historical multi-remote push settings. Use the Issue/PR as the durable
handoff; local documents do not notify other agents. If another participant lacks
access, report the limitation instead of claiming agreement.

Update docs/system for implemented behavior, docs/repository for process changes,
and docs/runtime for operational procedures. Preserve original specifications.
Assess Master Repo and Task Tracker impact for process changes; link the owning
companion change rather than silently modifying another repository.

## Read the task lifecycle

Review contains unfinished review. Actual approved waiting work appears in Delivery
as Ready for integration, preserving the native approved key and current admission
rules. Queue size, occupied slots and confirmed activity are different facts.
Waiting age uses the actual current approval timestamp; missing history stays
unknown and never counts as active work. Paused ready work retains Delivery.
Genuine owned author returns wait as Needs fixes in Backlog (native To Do); only
actual implementation ownership shows Fixing in In Progress. Preserve genuine
origin through supported stop/takeover/pause/resume, then require fresh independent
Review. Old missing origin stays neutral; a claim never proves running activity.
The active Coordinator continues corrections of the owner's already selected set
within actual capacity/admission. New unselected work, explicit pause resumption
and materially changed outcomes retain owner decisions. A free slot launches no
agent. Source approval alone does not authorize integration; already authorized
integration includes necessary verified delivery. Integration-only findings do not
return every frozen source; publication-only failure remains Delivery.
Integrating and required publication remain separate steps; Done follows the
project's actual whole-scope delivery, without a consumer installer for process-only work.

## Executor activity

Follow the selected pinned Tracker guide for `activity report` and the foreground
`activity run --exec` wrapper when supported. Only the current owner/session/claim
reports running, waiting and stopped. Confirm once a minute while running; five
minutes without a running/waiting report freezes work time and means Connection lost after prior confirmation; never-confirmed work stays Activity unconfirmed. A recorded stop remains while that owner is assigned. Neither
condition releases ownership. No detached endless reporter or coordinator heartbeat
for another executor. Existing runs without a producer remain unknown.

## Explicit phase-preserving pause after verified activation

The ACL-2414 activation is now verified; see the [process history](CHANGELOG.md).

Source support is installed separately from live activation; see the actual
readiness boundary in [EARLY_ADOPTION.md](EARLY_ADOPTION.md#pause-readiness-and-live-boundary).
Only after that boundary is verified, follow the compatible selected Tracker guide for owned `pause stop/resume`.
A confirmed stopped executor displays Paused inside its saved category and
releases only its own slot. Preserve native phase, saved work, history, frozen
integration/Stage and genuine blockers. Resume requires current scope/source
checks, role independence, genuine capacity and fresh authority; otherwise remain
paused with an explanation. A frozen Coordinator needs a genuinely new session
before its combined Reviewer. Silence never invokes pause or releases ownership.
Publish compatible readers/all writers before owned idle `pause activate`; source,
exports, pin and skill updates never activate live state or publish a product.
