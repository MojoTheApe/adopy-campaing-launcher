# Executor activity and recorded history

A reservation is ownership, not proof of a working agent. Only the actual
recorded owner/session/claim reports its own activity. Report at least once a
minute while working, and report `waiting` before waiting for checks or input.
Report `running` on actual continuation and `stopped` when execution ends.
Running and waiting observations expire after five minutes; this freezes work time at
the last report and shows Connection lost. Never-confirmed activity stays Activity unconfirmed. It never frees a claim, approves
a source, releases a review lane or changes a task's native workflow status.
A recorded stop is retained while that same owner remains assigned; another
executor's heartbeat or retention cleanup cannot turn that known stop into a run.

## Current-stage time is separate

Tracker3.12.0 labels current-stage residence `In stage` in task rows/cards and
details. It includes waiting, independently of Working/Waiting/Connection lost/
Activity unconfirmed or known stop. Confirmed Work/Review/Merge totals remain in
details. A genuine new stage starts its own clock, including a return after
corrections. Backlog, completed and archived tasks have no live stage clock.
Explicit phase-preserving pause freezes residence; supported resume continues
it with the union of actual paused intervals excluded, including repeated frozen
batch pauses with a stable batch ID and genuinely new sessions.

`stage_timing.py` projects entry facts from native status events, exact owned
operations/approvals and qualified pending-publication/job records. Issue edits,
snapshot time, source commit age and another executor are not entry facts.
Unknown old starts remain `Time unknown`. A known start may outlive the visible
500-event history only when complete incremental reads prove no intervening
transition; a missing collection window cannot establish continuity. A stale
snapshot freezes residence at its saved observation, without claiming current
activity. Public fields contain only stage keys, UTC entry/as-of and safe totals.

After an actual successful owned CLI launch/resume, the compatible engine seeds
that same caller's first running observation. Optional reporting failure leaves
the real claim intact and returns `executor_activity: unconfirmed`, never replays
the committed ownership operation. This is not a persistent producer. The actual
executor must still report once a minute or run its actual foreground agent in
the wrapper below. Read-only polls, queued work and foreign/old runs do not seed.

New legacy `work/start` with an actual Worker actor/session binds its identity
only when the existing outer8 compatible-writer fence is already active. It
returns the actual claim for later owned operations. Old unfenced/unbound runs
stay unknown; do not activate a policy, invent authority or edit private state
to hide that limitation. Current pins/runs do not gain a producer merely because
the central UI or instructions changed. Master companion:
[MR-79](https://github.com/MojoTheApe/master-repo/issues/79).

## Reporting an actual run

Use the same actual role, session and claim returned by the ownership command:

```sh
python scripts/task_tracker.py activity report --kind reviewing \
  --actor Reviewer-ABCD --session ACTUAL_SESSION --claim ACTUAL_CLAIM --state running
python scripts/task_tracker.py activity report --kind reviewing \
  --actor Reviewer-ABCD --session ACTUAL_SESSION --claim ACTUAL_CLAIM --state waiting
```

Replace the command with `python -m tracker` in the engine repository and with
the project's guarded adapter where required. Kinds are `implementing`,
`reviewing` and `merging`; modes are `running`, `waiting`, `stopped`.
The command verifies current ownership on every report. Execution schemas 1–4
and owned legacy review/merge observations for real tasks are supported. Knowledge
capture subjects do not become task clocks or synthetic tasks. A legacy observation
without a claim ID uses its actual session as `--claim`. Legacy implementation
slots with no verifiable actor/session stay unconfirmed; this command never
invents authority for them.

For a real foreground executor process, the shared wrapper reports every minute
while that child exists and reports stopped when it exits. It preserves the
child's exit code and terminates the child if interrupted during its scope.
Temporary reporting outages leave the child running and let observations expire;
confirmed loss of ownership ends the wrapper's child instead:

```sh
python scripts/task_tracker.py activity run --kind implementing \
  --actor Worker-ABCD --session ACTUAL_SESSION --claim ACTUAL_CLAIM \
  --exec actual-agent-command --its-own-arguments
```

The wrapper renews the latest explicit mode of that same run, including waiting
and stopped. Its periodic heartbeat never turns those states back into running;
an explicit owner report or actual new launch resumes running. Mode is read inside
every CAS retry. A missing observation requires a new explicit owner report and
remains unknown in the meantime.

Wrap the actual agent, not a perpetual sleep or independent reporter. A coordinator
must not renew a stopped worker's heartbeat. Running tests only proves that child
process is alive, not that a separate reviewing agent is working. Interactive
agents report at their actual tool boundaries; long periods without a report
honestly become unconfirmed. A healthy process is not proof of useful progress.
Existing runs do not acquire a producer by installing a new pin; use the supported
launcher on the next actual run or report explicitly from the current owner.

Small observations use `.tracker-activity.json` on the existing `tracker-state`
branch, separately from the large authority ledger. Bounded conflict retries
reload both current authority and observations. Uncertain writes are inspected by
the next read; no owner is cleared. Seven-day run records compact into confirmed
per-task totals. Heartbeats do not append permanent history entries. Public output
contains only mode, kind, UTC confirmation/expiry and safe totals/neutral actor.
Sessions, private claims, commands and reports remain outside the public snapshot.

Only consecutive reports no more than five minutes apart confirm running time;
waiting and lost-reporting gaps are excluded. A live display may advance between
reports, but expiry returns to the last confirmed total. Older recorded elapsed
history remains in the source ledger and is explicitly incomplete as runtime
evidence; it is never relabeled as measured work. Retained observations never
resurrect a completed, archived or transferred claim.

Task history is a read-only window of native GitHub status/archive label events,
including direct and bulk changes. It records the actual GitHub actor, not an
invented individual behind a shared account. Genuine approved waiting work appears
in Delivery as Ready for integration; Review contains unfinished review. Native
history preserves the actual label changes. Approval waiting age is separate from
confirmed Work/Review/Merge time, and missing history stays unknown.
History does not claim that every label edit changes a category count. Initial
and reconciliation reads cover at most ten pages, with overlap on incremental
reads; retain at most 500 relevant native events and show 10 readable changes per
page, grouped by local day with task search and expandable original event details.
Only unambiguous opposite status-label events for one task/account within five
seconds form a transition; Archive and ambiguous records remain separate events.
Missing/truncated coverage and refresh failures are explicit, with original GitHub histories retained.
Raw webhook bodies, unrelated labels, Issue prose and credentials are not published.

Master companion: [MR-65](https://github.com/MojoTheApe/master-repo/issues/65).
ACL adopts the common engine and guidance separately; no campaign code or
execution/delivery policy migration is required.

Explicit phase-preserving pause and fresh-authority resume are separate owned
[task pause](TASK_PAUSE.md) operations; heartbeat expiry never invokes them.
