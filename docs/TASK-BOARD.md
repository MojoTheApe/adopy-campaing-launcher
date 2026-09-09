# Task tracker

GitHub Issues are the source of truth. The private English UI is [tracker.team7.cloud](https://tracker.team7.cloud).

The workflow is **Idea → Backlog → Todo → In progress → Review → In master**. There is no separate Done. Priorities are **High / Medium / Low**, default Medium for accepted work. Agents automatically claim the task and mark In progress before coding; full reviewed merges are verified before In master.

- [Agent instructions](TRACKER_AGENT_WORKFLOW.md): ideas, classification, Todo batches and verified completion.
- [Project configuration](../tracker/config.json): module boundaries and examples.
- [Workspace, hosting and reusable template](../tracker/README.md).
- [Workflow specification](TRACKER_REORGANIZATION.md) and [approved Design v2](TRACKER_DESIGN_V2.md).

All tasks shows every nonarchived task and clears all filters. Work contains Todo, In progress and Review; Plan contains Idea and Backlog. Modules refine the current view. List and Board share search and filters; clicking a task opens its details separately. Use the small copy icon beside the ID to send its exact identifier to an agent. On narrow screens, Navigation opens Views and Modules; Board shows one explicitly selected stage.

The overview shows Total tasks, Backlog and the selected release's remaining required leaf tasks, plus workflow counts. The totals are project-wide, independent from local filters. Archive is counted separately. Required archived leaf tasks remain unresolved release scope until explicitly removed from it; initiative parents do not inflate readiness.

MVP uses an explicit milestone and Required flags. Empty scope displays Scope not defined. Stale sync means readiness is unknown, and the last saved tasks remain visible. Scope readiness is not the publication of a release. All UI changes remain presentation preferences: task mutations happen through GitHub and the canonical agent commands.

The previous [Obsidian export](archive/TASK-BOARD-LEGACY.md) and task-board-snapshot.md are historical and are not read by the live tracker. Local commits and checked boxes do not independently prove merge or release.
