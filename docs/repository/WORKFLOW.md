# ACL repository workflow

For a plain-language owner overview, read [the owner guide](OWNER_GUIDE.md).

Read the exact [selected shared guide](TRACKER.md), the [ACL support/cutover plan](EARLY_ADOPTION.md) and the guarded operator mapping in [AGENTS.md](../../AGENTS.md).
Shared contract references retain canonical bodies; `_ACL` documents preserve the original local cutover history and stronger controls.

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
policy invalidates earlier evidence. A Reviewer who edits becomes an implementer;
a different agent must approve. Do not fabricate a second identity for one run.

On an authorized ship request, the Coordinator freezes the current eligible
Approved intake, then selects sequential batches of up to five. New approvals wait
for another intake. One urgent task is valid; the Approved backlog has no size cap.
One separate Coordinator claim holds the frozen batch through integration review,
checks and the configured integration route. Preserve every source author, tip, review and member ID.
Follow [BATCH_INTEGRATION.md](BATCH_INTEGRATION_ACL.md) and the exact pinned guide for
assembly, independent combined review and recovery. Execution-v1 source-adoption
commands remain in force only until the documented old-policy handoff completes.

Use `check-pr N` for early linkage feedback; run `check-merge N` once immediately before merging.
Final admission checks current review, CI and any configured stage evidence. These agent commands are
not GitHub branch protection. Verify actual GitHub controls; the private repository
currently reports a plan restriction for branch protection/rulesets. Never present
an unenforced agent rule as a server-enforced guarantee. Do not purchase a plan or
make the repository public as an implicit part of adoption.

## Stage, main and completion

The sequence below applies to ordinary/application work. Proven adopted
instruction-only tasks hold their independent source review through direct main
admission and complete with the normal verified process receipt, as described in
[PROCESS_INSTRUCTIONS.md](PROCESS_INSTRUCTIONS_ACL.md). They do not enter Stage or
require an application installer/delivery receipt.

Approved ordinary source branches -> reserved `codex/batch-ID` integration branch ->
main, after independent combined review and full applicable CI. Ordinary one-task
sources stay approval-only. Standing owner policy includes package delivery when
integration/main merge is approved; the agent automatically records the genuine request through
`batch begin --deliver` unless the owner explicitly requests source-only/skip-release.
Main CI then continues publication,
actual published Windows package verification and exact-task delivery without a
second routine approval. Failed or missing delivery remains unfinished. Follow
[POST_MAIN_DELIVERY.md](POST_MAIN_DELIVERY.md) for commands, serialization, safe
retries, initial old-policy source admission and re-enabling Stage. Keep original
source branches and the permanent stage branch/history.

Merged means all of a ticket's implemented code scope is actually included in main.
For ordinary/application tasks, package completion additionally requires an immutable published installer and the
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
Run focused behavior tests first, then scanner, smoke, the complete required tests
and packaging checks on the final candidate. CI (Linux and Windows test jobs)
remains required for code, dependencies, packaging and CI changes. Record success, skip, infrastructure failure
and unrun manual checks separately. Do not rerun a successful unchanged candidate
without a new failure, change or unresolved concern.

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
| `windows tests (<py>)` | windows-latest | 3.13 | the `windows_only` files plus the Windows smoke set (`scripts/acl_test_platform.py` `WINDOWS_SMOKE`), after the secret scan and `scripts/acl_smoke.py`: `--platform windows 1 1` |
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

Serial-only tests: none. A test that genuinely cannot run next to others must be
listed here with the reason and run in its own step; none needed it so far.
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
that evidence. Manual runs and uncertainty run in full. Never treat separate
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
