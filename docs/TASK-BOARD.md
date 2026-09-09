# Task tracker

GitHub Issues are the source of truth. The private English UI is [tracker.team7.cloud](https://tracker.team7.cloud).

The workflow is **Idea → Backlog → Todo → In progress → Review → In master**. There is no separate Done. Priorities are **High / Medium / Low**, default Medium for accepted work. Agents automatically claim the task and mark In progress before coding; full reviewed merges are verified before In master.

- [Agent instructions](TRACKER_AGENT_WORKFLOW.md): ideas, classification, Todo batches and verified completion.
- [Project configuration](../tracker/config.json): module boundaries and examples.
- [Workspace, hosting and reusable template](../tracker/README.md).
- [Workflow specification](TRACKER_REORGANIZATION.md) and [approved Design v2](TRACKER_DESIGN_V2.md).

The top navigation is All · Idea · Backlog · To do · In progress · Review · Master · Archive, with project-wide counts. All clears filters and shows every nonarchived task. Master means the same final In master stage. The sidebar contains modules only; its counts match the selected status/release scope before extra filters. On narrow screens Navigation opens those modules.

List and Board share search and filters, with compact rows and separate task details. Clicking anywhere on the ID, including its small icon, copies its exact identifier. Light / Dark / System changes smoothly; reduced motion disables animations.

The compact MVP widget shows percentage and completed / total required leaf tasks. Clicking it opens that required scope. Required archived leaf tasks remain unresolved until explicitly removed from scope; initiative parents do not inflate readiness.
MVP uses an explicit milestone and Required flags. Empty scope displays Scope not defined. Stale sync means readiness is unknown, and the last saved tasks remain visible. Scope readiness is not the publication of a release. All UI changes remain presentation preferences: task mutations happen through GitHub and the canonical agent commands.

The previous [Obsidian export](archive/TASK-BOARD-LEGACY.md) and task-board-snapshot.md are historical and are not read by the live tracker. Local commits and checked boxes do not independently prove merge or release.
