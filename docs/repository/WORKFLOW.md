# ACL repository workflow

## Adoption boundary

The owner selected an installed Windows application with automatic updates, not
manual Git delivery. Target profile: `package`, with permanent `stage` and `main`.
The source and Tracker cutover is recorded in [ADOPTION.md](ADOPTION.md).
Use the pinned shared client and the stage/main route below. The one-time
activation keeps ordinary admissions paused until its verified promotion and
normal Tracker `merged` handoff. Retired master clients are historical only;
package publication and employee updates retain their separate release rules.

## Tasks, branches and review

Ideas start Idea; accepted tasks start Backlog. The owner selects To Do. Starting
development requires an explicit request, a real Issue, verified In Progress and
the shared execution slot. Read the entire Description and Specifications plus
linked context. Classify by the existing six modules, preserve dependencies and
release membership, and never steal an occupied slot.

Keep the existing ACL IDs: 71 historical aliases plus Issue number + 1000 for new
IDs. Copy IDs from the Tracker. Branch: `codex/acl-1248-master-adoption`; PR title:
`[ACL-1248] Adopt the shared workflow`. For related tasks list every ID in brackets.
The PR body has exactly one `Tracker issues: ACL-1248` line and Issues link back to
it. GitHub PR numbers are separate. No automatic issue-closing keywords in commits
or PRs. Old IDs and history are not renamed during migration.

Implement in an isolated task worktree, self-check and update relevant system and
process history. Create the PR and set Review. The active agent starts a separate
non-implementing subagent to review the full scope and actual head/base. Fixes stay
on the same branch: In Progress while fixing, Review for fresh review. A reviewer
who edits code becomes an implementer; a different agent must give final approval.
Changed code, base, task scope or policy invalidates earlier approval. Record the
actual report with the pinned Tracker; do not fabricate reviewer identities.

Run `check-pr N` and `check-merge N` immediately before merging.
The latter checks current review, CI and stage evidence. These agent commands are
not GitHub branch protection. Verify actual GitHub controls; the private repository
currently reports a plan restriction for branch protection/rulesets. Never present
an unenforced agent rule as a server-enforced guarantee. Do not purchase a plan or
make the repository public as an implicit part of adoption.

## Stage, main and completion

Working branch -> stage after review and checks. Record Stage for the actual
integration and verify the isolated Windows target in [STAGING.md](../runtime/STAGING.md).
Then stage -> main with the same task set and unchanged approved content. Keep one
active task set on stage and preserve the working branch for fixes. If main changes,
reconcile in that branch, review and stage again. Promotion contains no extra fixes.
Use merge or squash, not rebase merge. Do not automatically delete permanent branches
or remove a working branch before its promotion and corrections are finished.

Merged means all of a ticket's implemented code scope is actually included in main.
Package completion additionally requires an immutable published installer and the
agreed installation/update verification. Record delivery against the exact merged
content. Do not wait for every employee to update; do not claim all have updated.
A partial task stays active even after a partial PR. A Blocked ticket cannot pass
normal PR admission. Preserve outstanding criteria; agree any split with the owner,
link both scopes and obtain review for the changed task set. Do not erase a blocker
or mark unfinished checklists complete to force a merge.

Source-only, research and operations outcomes keep their actual acceptance criteria;
never reclassify product work as non-code to bypass a missing installer or pilot.
For historical tasks follow the explicit reconciliation in ADOPTION.md.

## Verification and collaboration

Use the requirements and isolation rules in [engineering context](../system/AGENT_CONTEXT.md).
Run focused behavior tests first, then scanner, smoke, the complete required tests
and packaging checks on the final candidate. Windows CI remains required for code,
dependencies, packaging and CI changes. Record success, skip, infrastructure failure
and unrun manual checks separately. Do not rerun a successful unchanged candidate
without a new failure, change or unresolved concern.

Required admission names: `descriptor`, `tracker-link`, `ACL verification`,
`Installer verification`. Aggregates require all applicable underlying jobs; a
metadata-only PR may skip Windows only through the existing closed classification.
Stage/main pushes and manual runs execute the full checks. The private pinned Master
Action's access must be verified in a real consumer run. Draft descriptor success
is structural feedback, not activation or live environment evidence.

The owner's past CI exceptions apply only to their exact recorded PRs/commits.
Unavailable required CI blocks this migration's merge unless the owner explicitly
approves a concrete new exception. A source exception never promotes a release.
Merge within the authorized task after valid checks/review; do not add a routine
extra permission round. Runtime work retains its separate authorization.

Preserve other worktrees, machine configuration and active file ownership. Never
stash/reset/clean another session, stage all files blindly or infer abandonment.
Fetch current refs, record base/head and inspect explicit push destinations. Do not
silently copy historical multi-remote push settings. Use the Issue/PR as the durable
handoff; local documents do not notify other agents. If another participant lacks
access, report the limitation instead of claiming agreement.

Update docs/system for implemented behavior, docs/repository for process changes,
and docs/runtime for operational procedures. Preserve original specifications.
Assess Master Repo and Task Tracker impact for process changes; link the owning
companion change rather than silently modifying another repository.
