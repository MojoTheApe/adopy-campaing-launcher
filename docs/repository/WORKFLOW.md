# ACL repository workflow

## Adoption boundary

The owner selected an installed Windows application with automatic updates, not
manual Git delivery. Target profile: `package`, with permanent `stage` and `main`.
The source and Tracker cutover is recorded in [ADOPTION.md](ADOPTION.md).
Use the pinned shared client and the stage/main route below. The one-time
activation keeps ordinary admissions paused until its verified promotion and
normal Tracker `merged` handoff. Retired master clients are historical only;
package publication and employee updates retain their separate release rules.

## Tasks, branches and review

Ideas start Idea; accepted tasks start Backlog. The owner selects To Do. Starting
development requires an explicit request, a real Issue, verified In Progress and
an implementation claim under the matching five-slot execution policy. Read the entire Description and Specifications plus
linked context. Classify by the existing six modules, preserve dependencies and
release membership, and never steal another Worker's claim. Ordinary admissions
remain paused until [the explicit cutover](PARALLEL_EXECUTION.md) is complete.

Keep the existing ACL IDs: 71 historical aliases plus Issue number + 1000 for new
IDs. Copy IDs from the Tracker. Branch: `work/acl-1234-short-name`, or
`codex/acl-1234-short-name` when the host requires that prefix. PR title:
`[ACL-1234] Summary`. Execution v1 requires exactly one task per source PR.
The PR body has exactly one `Tracker issues: ACL-1234` line and that Issue links back to
it. GitHub PR numbers are separate. No automatic issue-closing keywords in commits
or PRs. Old IDs and history are not renamed during migration.

Implement in an isolated task worktree, self-check and update relevant system and
process history. For several requested independent tasks, the current agent acts
as Coordinator and delegates one real Worker per task up to both free repository
capacity and actual host capacity. Keep dependent/conflicting tasks queued. Workers
create their PRs and explicitly hand off their claims, freeing implementation slots.
Review labels alone do not release claims. Record Worker, Coordinator and Reviewer
separately with neutral opaque identities; GitHub Assignees remain real accounts.

The Coordinator acquires the single review/integration lane and starts a separate
non-implementing subagent to review the full scope and actual head/base. Other
Workers may continue independent implementation. Fixes stay on the same branch:
first use `execution review-return`, then a fresh author implementation claim
before any edit, base update or conflict resolution; self-check and hand off again.
Retain the lane with `--keep-lane` when needed, mandatorily after entering stage.
Reacquire/resume the lane and independently review the current candidate. A reviewer
who edits code becomes an implementer; a different agent must give final approval.
Changed code, base, task scope or policy invalidates earlier approval. Record the
actual report with the pinned Tracker; do not fabricate reviewer identities.

Run `check-pr N` and `check-merge N` immediately before merging.
The latter checks current review, CI and stage evidence. These agent commands are
not GitHub branch protection. Verify actual GitHub controls; the private repository
currently reports a plan restriction for branch protection/rulesets. Never present
an unenforced agent rule as a server-enforced guarantee. Do not purchase a plan or
make the repository public as an implicit part of adoption.

## Stage, main and completion

Working branch -> stage after review and checks. Record Stage for the actual
integration and verify the isolated Windows target in [STAGING.md](../runtime/STAGING.md).
Then stage -> main with the same single task and unchanged approved content. Hold
the sole review/integration lane through stage and main; preserve the working
branch for fixes. If main changes, return review and claim implementation before
reconciling in that branch, reviewing and staging again. Promotion contains no extra fixes.
Use merge or squash, not rebase merge. Do not automatically delete permanent branches
or remove a working branch before its promotion and corrections are finished.

Merged means all of a ticket's implemented code scope is actually included in main.
Package completion additionally requires an immutable published installer and the
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

Use the requirements and isolation rules in [engineering context](../system/AGENT_CONTEXT.md).
Run focused behavior tests first, then scanner, smoke, the complete required tests
and packaging checks on the final candidate. CI (Linux and Windows test jobs)
remains required for code, dependencies, packaging and CI changes. Record success, skip, infrastructure failure
and unrun manual checks separately. Do not rerun a successful unchanged candidate
without a new failure, change or unresolved concern.

### Running the test suite

The full suite runs in parallel with pytest-xdist (`requirements-dev.txt`):

    python -m pytest tests/ -q -n auto --dist loadfile

CI (`.github/workflows/ci.yml`, ACL-1582) splits the suite by platform:

| Job name (exact) | Runner | Python | Files |
|---|---|---|---|
| `linux tests (<py>, <k>)` | ubuntu-latest | 3.11 and 3.13, shards k = 1..3 | every `tests/test_*.py` without the `windows_only` marker: `python scripts/acl_test_shard.py --platform linux <k> 3` |
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
Stage/main pushes and manual runs execute the full checks, except that a push or PR
whose exact git tree already passed a full push or workflow_dispatch run of the same
workflow reuses it
(ACL-1565, `scripts/acl_ci_reuse.py`; the run summary links the reused run). Manual
runs and any doubt about that proof run in full. The private pinned Master
Action's access must be verified in a real consumer run. Draft descriptor success
is structural feedback, not activation or live environment evidence.

The owner's past CI exceptions apply only to their exact recorded PRs/commits.
Unavailable required CI blocks this migration's merge unless the owner explicitly
approves a concrete new exception. A source exception never promotes a release.
Merge within the authorized task after valid checks/review; do not add a routine
extra permission round. Runtime work retains its separate authorization.

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
