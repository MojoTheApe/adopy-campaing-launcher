# Project task tracker

Development Issues for this project remain in `MojoTheApe/adopy-campaing-launcher`. The shared UI is https://tracker.team7.cloud/projects/acl/.
Base branch: `master`.

Read `tracker/config.json` for the canonical module descriptions, examples, statuses and release configuration. Read the pinned common workflow with:

```sh
python scripts/task_tracker.py guide
```

The small client loads the exact shared engine revision in `tracker/engine.json`, caches it in ignored `.tracker-runtime/`, and always selects this project's config. Python 3.11+, Git and an authenticated GitHub CLI are required. The first call downloads the shared private repository; subsequent calls reuse the verified checkout. Do not install independent application copies into this project. `TASK_TRACKER_HOME` may reference a clean checkout of that exact engine SHA.

Before code edits, identify/create the Issue and run `python scripts/task_tracker.py start ISSUE`. Verify In Progress. Use the shared queue; never steal another task's slot. A new idea enters Idea; accepted work enters Backlog; To Do is the explicitly selected execution batch. Choose the module and Type from the user's intended result. Default priority for accepted tasks is Medium.

When no module fits a clear new functional area, autonomously add a stable module with name, boundaries, examples and reason using `module add`; update/commit configuration through normal project rules, verify its GitHub label and report the decision. These instructions read the updated catalogue directly. If meaning is ambiguous, preserve the idea with Needs classification and ask one focused question; do not use Operations as a catch-all.

Archive is independent of workflow status and preserves history. Use `archive ISSUE --reason REASON` and `restore ISSUE`. The generic Release widget displays this project's explicitly scoped milestones; MVP may remain a milestone name. Do not infer Required or release membership from priority.

After all task work is reviewed and merged into the configured base, run the `merged ISSUE --pr PR` dry run, then `--apply --reviewed-complete`; reread labels and verify the UI snapshot. Partial merges do not finish a task. All new UI/task text is English. Type Operations does not imply there were no code changes.

Existing repository review, testing and release rules still apply. Read AGENT_CONTEXT.md where present. n8n projects retain LIVE_FIRST: Git representation, publication, execution and successful business results are different facts. Installing or using the tracker does not authorize n8n runs, production workflow edits, Telegram messages or operational Jira writes.

The old `tracker/` Python/UI implementation remains only as an ACL compatibility fixture for existing imports/tests. All new task operations use `python scripts/task_tracker.py`, and the production UI is deployed from `MojoTheApe/task-tracker`. Do not extend or deploy the retained package.
