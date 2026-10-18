# ACL: requesting work and reading the Tracker

Use the ACL board to see tasks and their recorded progress. GitHub keeps the
original task, proposed change, review and result. Master Repo provides shared
instructions; agents follow those instructions and ACL's existing local workflow.
You do not need to operate the development tools yourself.

## Request a task

Tell the agent what should improve, what happens today, and what a successful
result should look like. Include a screenshot or example when useful. For example:
"Keep the slot counts visible as 0/5 and remove the extra '5 free' text."
Say whether you want the agent to start or only record the idea for later.

The agent checks for an existing task, records the agreed requirements and gives
you its ACL task ID. Use that ID for follow-ups. Changes to the request belong in
that same task so earlier requirements and decisions remain visible.

## Read progress

| Label | What it means for you |
| --- | --- |
| Idea | A suggestion that still needs a decision. |
| Backlog | Recorded work waiting to be selected. |
| To Do | Selected work that has not started. |
| In Progress | An implementer has taken the task. Check the latest update to confirm what they are doing now. |
| Paused | A stop has been explicitly recorded. The card appears with planned work and has no active elapsed timer; it can be resumed through the existing ownership procedure. |
| Review / Ready for Review | A change is ready for an independent check. It may still be waiting for a reviewer. |
| In Review | An independent reviewer holds the review assignment. |
| Approved | The source change passed review and is waiting to be combined and delivered. This is not yet a finished application update. |
| Integrating / Awaiting Delivery | Approved work is being combined, or the required delivery is still pending. |
| Done | The task's required completion evidence has been recorded (the underlying repository status is Merged). For application work this includes the required verified package delivery; repository instructions can finish after checked publication on main. |
| Blocked | An additional flag: a specific obstacle prevents the next step. Read its reason and required action. |

An old task or an unavailable computer does not prove that work stopped. The agent
must confirm the stop before releasing its assignment or recording Paused. If that
confirmation is unavailable, the next update should say so plainly rather than
claiming the agent is running or that the task is finished.

An explicit stop is different from a missing activity record. "Status unconfirmed"
means the Tracker cannot establish a reliable current state from the available
record. Ask the agent to check the original task and ownership. Do not restart,
close or delete the task just to remove that label. Review without a recorded
active reviewer still means Review; it does not prove a reviewer is working now.

## Read the counters and time

- **Work 2/5** means two recorded implementation assignments out of five available
  slots. It is not a live measurement of two running agents.
- **Source review 0/5** means no source-review slots are held out of five.
- **Review queue 6** means six tasks are waiting in the review queue. This is a task
  count, not six occupied reviewer slots.
- **Integration 0/1** means the single slot for combining approved changes is free.

The counters stay visible. A separate "free" suffix is unnecessary because the
ratio already shows capacity. **Elapsed** measures time since assignment, not
hours of continuous work. Paused work has no active elapsed timer.

**Details** contains supporting operational records. A reservation is recorded
ownership. "Admission not assessed" means eligibility for the next step has not
been checked; it is not itself a failure. "Batch selection required" means the
agent coordinating delivery must select approved work to combine. "Integration
paused" describes that delivery activity, not every task. "Activity not recorded"
means no activity evidence was recorded. A waiting approval and a batch size are
queue/capacity information. These messages alone are not requests for you to act;
a real blocker should name the obstacle and the specific help needed. A stale or
unavailable board should be checked against the original GitHub task.

## Find the result or help with a blocker

Open the task and follow its GitHub task and pull request links. The final update
should say what changed, which checks passed, where the result is available and
what remains unfinished. Ask for that information if it is missing.

For instructions, the result is the published document on the main branch. For an
ACL application change, a merged source change alone is insufficient: look for the
verified stable release and installation/update evidence in the task. Completion
does not claim that every employee's machine has already updated.

If work is blocked, the agent should give you one concrete request, such as
restoring access to the Windows computer, and continue any independent work.
Unavailable Windows access leaves the affected verification open; it does not
require stopping unrelated documentation or interface work.

## Main project and related resources

ACL is the main project. **Related Resources** links its **acl-lib** shared targeting/ad-text data and
**acl-releases** release repository. Those links help find supporting data and
published packages; they do not claim separate active tasks or completed tests.
The current rollout is limited to this ACL family. Other projects are deferred;
React Forge and The Source are excluded from the requested connections.

For agents implementing work, the detailed rules remain in [WORKFLOW.md](WORKFLOW.md),
the [selected Tracker guide](TRACKER.md) and [DELIVERY.md](DELIVERY.md). This guide
explains the existing process; it adds no scripts, policies or new automation.
