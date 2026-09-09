# Task tracker

GitHub Issues are the source of truth. The private English UI is [tracker.team7.cloud](https://tracker.team7.cloud).

The workflow is **Idea → Backlog → Todo → In progress → Review → In master**. In master is the final completed stage; there is no separate Done. Priorities are **High / Medium / Low**, with Medium as the default for accepted work.

- [Agent instructions](TRACKER_AGENT_WORKFLOW.md): create and classify ideas, select Todo batches, automatically set In progress before coding, and verify completion after merge.
- [Project configuration](../tracker/config.json): six module definitions and examples.
- [Portable package and hosting](../tracker/README.md): install, export to another repository, sync, migration and rollback.
- [Approved specification](TRACKER_REORGANIZATION.md): workflow and UI decisions.

Work shows current execution, Review and Todo. Plan contains Idea and Backlog. MVP uses explicit milestone membership and Required flags. All clears filters and expands all active modules; Archive retains cancelled or duplicate tasks.

The previous [Obsidian export](archive/TASK-BOARD-LEGACY.md) and `task-board-snapshot.md` are historical. They are not read by the live tracker. A local commit or checked checkbox does not automatically prove merge or release.
