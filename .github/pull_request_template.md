Describe the concrete problem and resulting behavior.

Tracker issues: #ISSUE

Validation: commands, results, environment, and outstanding checks.

Every implementation needs a task, including immediate fixes. Before code, use `python scripts/task_tracker.py work ID` or `work --title ... --module ... --type ...` and verify In Progress. Set Review when implementation is ready. Run `python scripts/task_tracker.py check-pr PR_NUMBER` before merge. After all work is reviewed, merged and verified, use the merged dry run and apply with `--reviewed-complete`; verify the UI. Partial work never completes a parent task.
