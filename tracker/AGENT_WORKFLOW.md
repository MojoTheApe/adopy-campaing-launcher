# Task tracker workflow · schema 2

Read this document and `tracker/config.json` at the start of every task, including work resumed in an existing chat. GitHub Issues are the task source of truth; the web UI is read-only. Follow the repository's review, CI, merge and release rules as well. A tracker command does not authorize an otherwise unauthorized merge or deployment.

## Start before coding

When the owner asks you to implement a task, identify its Issue (or create one for the requested work), read the acceptance criteria and dependencies, then run:

```sh
python -m tracker start ISSUE
```

The command reserves the shared execution slot, writes `status:in_progress`, rereads the Issue and confirms it. **Do this automatically before the first implementation edit, without asking the owner to update the status.** Do not continue coding if the transition failed. A manual label check followed by a write is not a substitute for the shared reservation.

Only one agent implements tasks in this project at a time. If another task owns the slot, inspect its Issue, branch, PR and handoff. Do not steal it based on elapsed time. For an explicit continuation of that same work, use `start ISSUE --resume` after confirming the previous execution stopped. Never resume the same task concurrently. Keep the Issue's branch/PR and current checks updated so another chat can continue.

## Stages and properties

`Idea → Backlog → Todo → In progress → Review → In master` (the final display name follows config). There is no separate Done. Blocked, Design, priority and release membership are independent properties.

- **Idea**: an unaccepted suggestion. Save it when the owner says “new idea.”
- **Backlog**: accepted work that has not been selected for execution.
- **Todo**: an explicit, manageable batch selected by the owner.
- **In progress**: currently being implemented, including fixes following review.
- **Review**: implementation is ready for verification and merge.
- **In master**: all task work is reviewed and merged into the configured base branch. Partial merges do not complete a parent task.

Use exactly one `status:*` and one configured `module:*`. Priorities are **High / Medium / Low** (`priority:high`, `priority:medium`, `priority:low`). The default on accepting work into Backlog or Todo is **Medium**. Ideas may have no priority. Never use Normal. Types: Bug, Feature, Improvement, Research, Operations. No assignee field is required: the executor is an AI agent.

Classify by the intended user result using module descriptions, boundaries and examples in config. Do not classify solely by file name, “API,” or “design.” Add `facet:design` for UI/UX work within its owning module. Choose one main module; use explicit dependencies for cross-module work. Do not invent new modules or use Platform as a catch-all. If important details are missing, save the idea without `--module`; it will appear under Unclassified with `flag:needs_classification`, then ask one focused question.

Before creating an Issue, read current tasks with `python -m tracker list` and check for semantic duplicates, including completed and archived tasks. The CLI also detects exact-title repeats. After a failed/uncertain create, search for the title before retrying; do not blindly repeat the POST.

## Conversation to commands

Replace the symbolic IDs below with verified Issue and milestone numbers. Use a UTF-8 body file for multiline content; preserve existing descriptions, checklists and references. Titles and newly authored task content should be English, preserving the owner's meaning. Existing historical content is retained.

```sh
# “New idea: simplify the account card.” Choose module/type from config.
python -m tracker create --title "Simplify the account card" --body-file idea.md --module accounts --type improvement --design
# “Accept this idea into Backlog with high priority.”
python -m tracker set ISSUE --status backlog --priority high
# “Put these tasks into Todo in this order.”
python -m tracker set ISSUE --status todo
python -m tracker queue order ISSUE_A ISSUE_B ISSUE_C
# “This is essential for MVP.” Resolve the milestone number from tracker list.
python -m tracker set ISSUE --milestone MILESTONE --required
# “Blocked on a dependency.” Status is preserved; record the reason.
python -m tracker set ISSUE --blocked --reason "Waiting for the shared API; needs: #DEPENDENCY"
python -m tracker set ISSUE --no-blocked
# Implementation and tests ready for review.
python -m tracker set ISSUE --status review
```

Only use module IDs that exist in the current project; the `accounts` example is illustrative. For dependencies, write `needs: #123`, `needs: project-123`, or `needs: https://github.com/OWNER/REPO/issues/123` in the Issue body. A casual issue mention is not a dependency. Use one explicit marker per dependency. Leave the reason/history intact when unblocking; record the resolution in the Issue. Changes preserve unrelated labels.

## Explicit Todo batches

A request to work through Todo starts a batch; merely adding tasks does not launch background execution.

```sh
python -m tracker queue begin
python -m tracker queue next
```

`begin` snapshots Todo IDs. Later additions belong to a later batch. Order is the owner's explicit order, otherwise priority, Todo entry time and Issue number. Unfinished dependencies override that order: `next` skips unavailable tasks and claims only an eligible task from this batch, setting In progress before coding. It never pulls a dependency or new work from Backlog. Missing tasks and moved statuses remain visible as waiting; they are not silently completed.

Implement, test, move to Review, review and perform the authorized merge, verify In master, then call `queue next`. Fixes after review use `start ISSUE --resume`. A blocker records a reason and releases the execution slot so independent tasks can continue. `queue release ISSUE` also permits an explicit Review handoff; record its branch, PR and remaining checks first. `queue show` restores shared context after an interruption. `queue cancel` stops a batch only after its active task is handed off. Do not claim that an interrupted batch has completed.

State is stored in `.tracker-state.json` on the dedicated `tracker-state` branch of the same repository. Writes use the GitHub Contents API SHA as a compare-and-swap guard. A conflict stops the operation. An uncertain response retains the reservation; inspect state and resume explicitly. No extra task or assignee appears in the UI.

## Merge and completion

Before merge, link the real task scope in the PR: `Tracker issues: #123`. Review all task work and acceptance criteria, not just an ID mention. Follow required checks; exceptions for earlier PRs do not carry forward.

```sh
python -m tracker merged ISSUE --pr PR
python -m tracker merged ISSUE --pr PR --apply --reviewed-complete
```

The first command is a dry run. The second is the reviewer's confirmation that ALL work for the selected Issue is complete; it is not a new permission request. Repeat `--pr` for multiple PRs. The command checks that PRs were merged into the configured base, that their merge commits remain in that branch's ancestry, that the Issue is genuinely linked, and that it has no unfinished checklist items. It does not determine semantic completeness or detect every later revert: the reviewer must check the current code and scope. After applying it, the task has only `status:merged`, other labels are preserved, and the execution slot is released.

Do not close Issues or tick checkboxes just to bypass these checks. If the task is partial, keep it in Review or split genuinely independent results into linked Issues. Cancelled/duplicate Issues are archived and excluded from completed totals.

For a Research/Operations task with **no code changes**, verify the actual result and use `python -m tracker complete ISSUE --evidence-file result.md --verified-non-code` from Review. The final stage displays **Complete**, records evidence and `resolution:non_code`, and does not claim a merge. Never use this path to skip a code review or failed CI.

## Releases and verification

A release is a GitHub milestone; `release:required` identifies essential tasks within it. High priority does not imply MVP membership. Do not assign existing ideas or the entire Backlog to MVP automatically. Only the owner determines its required scope. `kind:initiative` excludes a parent from leaf-task readiness totals; its child Issues carry release properties individually.

The UI computes readiness from a nonempty required scope, verified completion, blockers, explicit dependencies and metadata warnings. An empty scope is **No scope defined**. Stale data means readiness is unknown. Reaching In master does not publish a release.

After each operation reread the Issue and verify the intended labels, preserved metadata and current tracker data after sync (normally within two minutes). Report the Issue link, module, status and requested priority/release briefly. A failed status write or sync is unfinished work; preserve the last good UI and report the remaining step accurately.

## Reading Design v2 after a command

All tasks shows every nonarchived task in all six stages; clicking it clears presentation filters. Work shows Todo, In progress and Review. Plan shows Idea and Backlog. Modules refine a view. List and Board use the same results; a card opens its details, and its copy button returns the exact canonical ID (or `#NUMBER`). The UI is read-only: clicking a stage or changing a filter does not change an Issue.

Project totals remain global. The release overview counts remaining required leaf tasks; archived required members stay unresolved until their scope is explicitly changed, and initiative parents do not count twice. Scope not defined means no required release scope exists. Never fill that scope from High priority or all Backlog without the owner's instruction. Scope ready does not mean the release was published.

After a status command, verify the Issue and the next successful UI snapshot. If a task has left the selected view, use All tasks or search its canonical ID; open details may show Outside current filters. A stale sync message means the previous snapshot is still displayed, not that the task was deleted. Follow the same automatic start, queue and merge rules regardless of List/Board, theme or the current chat.
