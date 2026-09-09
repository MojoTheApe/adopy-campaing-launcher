# Shared tracker instructions

Read [the canonical tracker agent workflow](../tracker/AGENT_WORKFLOW.md) and
[the project configuration](../tracker/config.json) before creating or working on tasks.
The same instructions apply to Codex, Claude and other agents. The portable package
contains the single maintained copy so future projects receive identical rules.

ACL production is `master`; the UI is [tracker.team7.cloud](https://tracker.team7.cloud).
Follow [DEVELOPMENT_WORKFLOW.md](DEVELOPMENT_WORKFLOW.md) for review, CI and releases.

**Before the first implementation edit, automatically run `python -m tracker start ISSUE`
and verify In progress.** After complete review and merge, use `python -m tracker merged`.
Priorities are **High / Medium / Low**; default **Medium**. In master is the final task stage.
