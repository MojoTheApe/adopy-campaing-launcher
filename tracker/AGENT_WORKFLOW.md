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

The owner can describe a task in ordinary language without listing fields. **Select the module, exactly one Type and any supported Focus properties yourself before creating it.** Always pass `--type` for new tasks; do not ask the owner to fill in a form when the intent is clear. Use these meanings:

| Type | Use when the intended result is |
|---|---|
| Bug | Restoring expected behavior that is broken |
| Feature | Adding a capability the product does not have |
| Improvement | Improving an existing capability, UX, performance or maintainability |
| Research | Answering a question or evaluating options before implementation |
| Operations | Operating infrastructure, deployments, maintenance or project processes |

Type describes the work; Module describes the part of the product. Operations is a valid type in any appropriate module, not another name for Platform or a guarantee that no code is involved. Design is a Focus property, not a Type: an account-card redesign is normally Accounts & Access + Improvement + Design; a broken campaign preview is Campaigns + Bug and may also involve Design.

**Focus can contain several independent properties:** Design follows UI/UX scope; Blocked requires a recorded obstacle or an explicit unfinished dependency; Required follows the owner's approved essential release scope. Do not mark a task Blocked because it is an idea, low priority or waiting in Backlog. Do not infer Required/MVP from importance, Design, or a new feature. If no Focus property applies, leave it empty. The UI derives Blocked from unresolved `needs:` dependencies as well as `flag:blocked`; record and resolve real dependencies rather than inventing a label to match a view.

For “new idea,” create Idea with this classification; for an accepted suggestion use Backlog and default Medium. Todo and execution still follow the owner's selection and the automatic start rule above. If an actual ambiguity prevents classification, ask only about the intended outcome; do not make the owner choose every field. Historical missing Type displays **Not classified** and is not an assignee. Do not bulk classify historical tasks without reading their scope. After creation, briefly report the ID and chosen properties so the owner can correct an interpretation.

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

The top strip provides All, each workflow stage and Archive; the sidebar contains only modules. All shows every nonarchived task and clears presentation filters. Master is the compact navigation label for In master, not a new GitHub status. Module counters reflect the selected status/release scope before extra filters. List and Board use the same results. List has Task, Status, Module, Priority, Release, Type and Focus columns; a narrower workspace shows the same properties with labels below the title. The filter icon expands aligned Priority, Type and Focus rows on the page background. Status is selected only in the top strip, and MVP opens the configured default release; retired stored Status/Release conditions are discarded. Release remains task metadata. Collapsing filters keeps their conditions. Changing filters keeps the viewport at the controls; background updates preserve the task being read. A row/card background opens resizable details; outside click or Escape closes them. The whole ID including its small icon copies the exact canonical ID (or `#NUMBER`). Selected controls retain a static gradient; hover or keyboard focus animates it. Modules use a left marker, and theme/layout selectors use the same border behavior. These are presentation states only. The UI is read-only: clicking a stage does not change an Issue.

Status counts remain project-wide. The compact release widget shows completion percentage and completed / total required leaf tasks; clicking it opens that required scope. Archived required members stay unresolved until their scope is explicitly changed; initiative parents do not count twice. Scope not defined means no required release scope exists. Never fill that scope from High priority or all Backlog without the owner's instruction. A saved-snapshot percentage does not prove current readiness, and Scope ready does not mean a release was published.
After a status command, verify the Issue and the next successful UI snapshot. If a task has left the selected view, use All, then search its canonical ID; open details may show Outside current filters. A stale sync message means the previous snapshot is still displayed, not that the task was deleted. Follow the same automatic start, queue and merge rules regardless of List/Board, theme or the current chat.
