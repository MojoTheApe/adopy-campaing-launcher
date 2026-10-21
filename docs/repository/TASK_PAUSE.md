# Explicit task pause and safe resume

TT-179 / Master70: pause is an owned executor transition, independent of workflow
phase. A Review task displays Paused inside Review. An implementation remains in
In Progress; admitted Stage or frozen integration stays in Delivery. Actual
Issue status labels, scope, saved branch/worktree, PRs, reports, Stage receipts,
requested product delivery and real blockers remain intact. Completion and archive
still take precedence over stale presentation evidence.

Genuine approved waiting work appears in Delivery as Ready for integration.
Its explicit paused display therefore retains Delivery too; native approved
phase and saved source are unchanged. Older public pause observations remain
readable under that same phase-to-category mapping.

No report for five minutes means **Connection lost** only after an actual prior
confirmation. **Activity unconfirmed** means no confirmed observation exists.
Both freeze the confirmed work clock and retain ownership. Silence never pauses,
releases a slot or resumes work. An explicit activity `stopped` report alone is
also not an authority release. Confirm the actual executor stopped, then use the
owned pause transition. Never report another executor alive or invent an identity.

## Install readers and writers before activation

The maintained engine owns commands. The small pinned author client runs them
on the agent's machine against the existing GitHub CAS state; the existing shared
service renders public snapshots. Portable Python3.9 CI helpers only inspect
state. There is no new daemon, scheduler, timer-based takeover or product installer.

Publish a compatible Tracker engine/14-helper closure, Master pin and selected
instructions; install the compatible service reader. Upgrade **all actual writers**,
including adapters/CI recorders, and retain their exact revisions and service
verification in a nonempty maintenance report. Adoption is separate from source
publication. An old reader rejects outer8 instead of ignoring a pause.

Under the project's actual authorization, reserve ordinary maintenance with its
real Maintainer actor/session, retain the returned claim, and drain/stop actual
owners using their supported existing operations. Queued tasks remain queued.
Check maintenance show: implementation, review, integration, retained legacy timers,
pending capture/retry/Stage/delivery and running queue work must be idle. Then:

```sh
python scripts/task_tracker.py pause activate --actor Maintainer-ABCD \
  --session ACTUAL_RUN --claim ACTUAL_MAINTENANCE_CLAIM --report-file verified-rollout.md
```

Preview first; repeat with `--apply` only after the report's real reader/all-writer
readiness is verified. Resume the same owned maintenance with its supported command.
Activation writes outer8 with the exact retained underlying1..7 guard bundle and
records no task pause/resume. It never changes a project policy, capacity, target,
Issue or installer. Never downgrade/remove the fence after resumed history exists.
Tracker publication or a consumer pin edit alone does not activate this state.

## Pause one stopped owner

Inspect `pause show TASK_ID` privately. Use the genuine current claim and actual
owner identity, or its recorded Coordinator after verifying that run stopped.
The handoff report records the stop, saved work, remaining scope and checks:

```sh
python scripts/task_tracker.py pause stop TASK_ID --kind reviewing \
  --actor Reviewer-ABCD --session ACTUAL_RUN --claim ACTUAL_REVIEW_CLAIM \
  --stopped --report-file pause-handoff.md
```

Preview then `--apply`. One CAS checkpoints the owner, native positions/scopes,
source identity, original handoffs/review history and necessary integration/intake
state, revokes only that executor, and releases its actual slot. It does not
move the task to Todo or erase a real blocker. Old claims cannot report activity,
handoff, approve or merge. An uncertain response is reconciled by the exact same
operation; concurrent changes cause a reload/refusal rather than an overwrite.

Kinds are implementing, reviewing and merging. In bare legacy ownership, inspect
the exact private shared-slot token; a genuine Coordinator may checkpoint an
unbound legacy slot only after confirming its stop. Resumed legacy Workers acquire
real identity/session/claim and can then report their own activity.

Frozen batch composition/main and product requests are retained. A combined
Reviewer pause keeps its Coordinator reservation. A Coordinator pauses only after
its combined Reviewer is stopped/paused; resume that Coordinator before resuming
the Reviewer. A held frozen selection blocks conflicting intake/admission even
when its executor slot is free. Ordinary capture notes never invent task Issues.
Operations already committing/finalizing main must use their supported recovery
before releasing authority; pause refuses an unsafe operation without changing it.
A stable admitted Stage boundary can be paused while preserving its exact receipt.

## Resume at the saved phase

Use the returned pause ID as `--claim`, an actual role/run and a current report:

```sh
python scripts/task_tracker.py pause resume TASK_ID --actor Reviewer-ABCD \
  --session ACTUAL_REVIEW_RUN --coordinator Coordinator-ABCD \
  --claim ACTUAL_PAUSE_ID --report-file resume-handoff.md
```

Preview then `--apply`. Workers also supply the exact saved `--branch` and
`--worktree`. A genuine available slot, unchanged scope/native position, current
PR linkage/head/branch/base/repository/route, actual dependencies/blockers and
selected context/ownership rules are rechecked. Stable admitted Stage must retain
its exact source inclusion, Stage receipt/tree and main tree. Frozen integration
requires unchanged main/composition. A new Coordinator run/session is mandatory;
its batch ID remains the immutable selection identity. No capacity or failed check
removes the pause: explain the refusal and wait or reconcile actual changed work.

A fresh claim is returned, the paused display disappears, and the prior stage
reappears. Prior review history is retained privately; fresh independent review
must be recorded before any new admission. Report actual running activity with
the new claim. Resumed bound legacy runs also supply that same actor/session/claim to
`work --resume`, `set`, `queue release`, legacy timer stop and finalization;
old credential-free commands cannot take over or clear their authority. Bound legacy
review documents carry the returned actual Reviewer in `reviewer`, its current
`review_session` and new claim in `execution_lane`. Current admission rechecks
that binding even without execution policy. A fresh bound Reviewer can re-review
only the exact retained admitted Stage source after its original merge; current
source/main/Stage/scope gates remain mandatory before promotion. Paused/resumed events retain the existing phase clocks; paused time
and disconnected reporting gaps never become confirmed work.

The UI remains read-only. It exposes only phase/category, kind and pause time;
private sessions, claims, paths, reports and checkpoint state stay private. Existing
native status history remains separate and does not invent status label moves for
pause/resume. All project-local installer/deployment and operational controls remain.
