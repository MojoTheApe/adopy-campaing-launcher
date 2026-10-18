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
| Needs clarification | The recorded work does not establish a ready review handoff or an active reviewer. Ask the agent to reconcile the saved work and task; it is not proof that review is running. |
| Ready for Review | A ready handoff is recorded in the review queue, waiting for an independent reviewer. |
| In Review | An independent reviewer holds the review assignment. |
| Approved | The source change passed review and is waiting to be combined and delivered. This is not yet a finished application update. |
| Integrating | Approved work is being combined and checked for main. |
| Awaiting Release | Required delivery is unfinished and no current release job is confirmed, or its result still needs the delivery confirmation. |
| Releasing | A current release verification job is confirmed for the accepted source. The task remains unfinished until delivery is verified. |
| Done | The task's required completion evidence has been recorded (the underlying repository status is Merged). For application work this includes the required verified package delivery; repository instructions can finish after checked publication on main. |
| Blocked | An additional flag: a specific obstacle prevents the next step. Read its reason and required action. |

Historical tasks marked **Accepted before adoption** retain their earlier
acceptance; they do not prove a verified installer.

An old task or an unavailable computer does not prove that work stopped. Your
direct confirmation that an agent has stopped is enough to establish that fact;
the agent does not need to bring the old computer online to ask again. It records
the pause through the existing procedure and checks that the requested assignment
was released. Without confirmation, it reports the uncertainty.

Pausing preserves the original task, branch, work location and history. Files on
an unavailable Windows computer remain uninspected, not lost or declared complete.
A voluntary pause alone is not Blocked; a real dependency or access obstacle stays
visible with its reason. To resume, ask the agent to continue the same task: it
first checks the saved work, current requirements and dependencies, then records
normal ownership. You do not need a new task or a manual Tracker status change.

An explicit stop is different from a missing activity record. "Status unconfirmed"
means the Tracker cannot establish a reliable current state from the available
record. Ask the agent to check the original task and ownership. Do not restart,
close or delete the task just to remove that label. Review is the group containing
Needs clarification, Ready for Review, In Review and Approved; it does not by itself
prove that a reviewer is working. The agent records changes through the existing
tools; the board does not offer direct status-edit buttons.

## Read the counters and time

- **Work 2/5** means two recorded implementation assignments out of five available
  slots. It is not a live measurement of two running agents.
- **Source review 0/5** means no source-review slots are held out of five.
- **Review queue 6** means six tasks are waiting in the review queue. This is a task
  count, not six occupied reviewer slots.
- **Integration 0/1** means the single slot for combining approved changes is free.

The counters stay visible. A separate "free" suffix is unnecessary because the
ratio already shows capacity. **Elapsed** adds up recorded intervals in that
workflow activity, including the current assignment. It does not measure
continuous agent runtime. Paused work has no active elapsed timer.

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
