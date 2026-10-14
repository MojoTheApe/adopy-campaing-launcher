---
name: meta-builder-sync
description: Compare the latest Meta docs watcher digest with the ACL builder's Meta rules registry and open a Backlog tracker Issue for every mismatch. Use after scripts/meta_docs_watch.py has produced a digest, or when the user says "sync builder with Meta changes", "check Meta changes against the builder", "meta-builder-sync". Never edits code or the registry.
---

# meta-builder-sync

Turns "Meta changed something" into tracker Issues. It only reads and files
Issues; an executor fixes the code/registry later (owner decision: Issue first).
No ad accounts, tokens or live Meta calls are involved.

## Inputs (read all before judging)

1. Latest digest: `%LOCALAPPDATA%\ACL\meta_watch\digests\latest.json`
   (or the path/`ACL_META_WATCH_DIR` the user gives; a sample digest is fine for dry runs).
   Entry shape: `{source_url, title, category, date_seen, summary, status_label, affected_fields}`;
   plus `errors[]` for sources that failed to fetch.
2. Registry: `config/meta_rules/meta.json` (api versions) and `config/meta_rules/rules.json`
   (rules with `status`, `source_url`, `checked_date`) from ACL-1781.
3. Loader/checker: `src/campaign_bridge/meta_rules.py`.
4. Builder spec: `C:\acl-builder-design\builder-spec.md` (section 3, capabilities).
   If a path is missing, say so and continue with the rest.

If the registry files do not exist yet (ACL-1781 not merged), stop and report that;
do not guess rules.

## Modes

- **dry-run (default when the user says "dry run", "list", or gives a sample digest)**:
  print the Issues that would be created (title, module, source link, rollout status,
  duplicate verdict). Create nothing.
- **real run**: create the Issues listed. Only when the user asked for a real run.

## Procedure

1. Skip entries with `status_label: unknown` and no `affected_fields` unless the summary
   clearly changes an objective, optimization goal, billing event, CTA, placement, text
   limit, required field or API version. Mention skipped entries in one line.
2. For each remaining entry, compare against the registry and `meta_rules.py`. A mismatch is one of:
   - **new value** Meta added that the registry lacks (needs a rule);
   - **removed/deprecated value** that the registry still allows (rule must change);
   - **new API version / end-of-support date** not in `meta.json`;
   - **limit or required-field change**;
   - **fetch error** on the same source for 3+ consecutive digests (source URL may have moved).
   No mismatch -> no Issue.
3. Rollout status is kept distinct: `ga`, `limited`, `beta`, `test`, `announced`, `deprecated`.
   Copy the digest's `status_label` as a hint but confirm it from the source text.
   **An `announced`, `test` or `beta` feature never becomes an allowed rule**: the Issue must say
   "record as <status>, not allowed until GA" and must not propose a `ga` rule.
4. Dedupe: run `python scripts/task_tracker.py list` (includes completed/archived) and search for
   the source URL, the field/value name and the topic. If a matching open or recently closed
   Issue exists, do not create; report "duplicate of ACL-N" (or suggest a comment if it adds
   a new fact, e.g. the status moved from announced to ga).
5. Create (real run only), one Issue per distinct outcome:
   `python scripts/task_tracker.py create --title "<English title>" --body-file <tmp.md> --module campaigns --status backlog --type improvement --priority medium`
   Check `python scripts/task_tracker.py create --help` for current flags first.
   Body (English), with filled Description and Specifications:
   - Description: what Meta changed, in plain words, and why the builder is affected.
   - Specifications: source link(s), `date_seen`, rollout status, affected registry rule ids or
     the missing rule, expected change (e.g. "add rule X with status beta" or "mark Y deprecated"),
     acceptance criteria (a test naming the value), and "Do not allow until GA" where relevant.
   Priority high only for `deprecated`/end-of-support dates that break current production combos.
6. Do not edit code, `rules.json`, `meta.json`, or the digest. Do not start the work.

## Output

A short table: entry -> verdict (mismatch / no mismatch / duplicate / skipped) -> Issue
(created ACL-N, or "would create" in dry-run) with source link and rollout status.
End with counts. Optionally append the same summary to the Obsidian note
`дешборд/ACL — Meta изменения` when the user asks.

## Scheduling note

The watcher (`scripts/meta_docs_watch.py`) runs daily and locally; this skill runs after it,
on demand or as a second step of the same scheduled task. The scheduled task is registered
by the Coordinator with the owner, not by this skill.
