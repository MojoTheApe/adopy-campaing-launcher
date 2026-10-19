# Task context and retained source approvals

This shared protocol implements WFO-01 through WFO-04 (TT-136 and Master MR-30).
It applies to every delivery profile through an explicit matching descriptor
`workflow.task_context` and Tracker `task_context_policy`, both exactly
`{"schema_version": 1}`. V1 requires execution schema 2 or 3. Absence preserves
the exact existing lifecycle, whole-body scope hashes and main-bound approval.
No CI reuse, new board status, service deployment or consumer activation follows
from engine support. The optional Stage and actual delivery profiles are unchanged.

## Assessment before development

Read the complete Issue, relevant code/system decisions, related tasks (including
completed work) and dependencies. Determine whether the original result is still
needed and consistent. Do not infer relevance from age, labels or a matching title.
Unchanged authorized work proceeds; a material change to the owner's outcome or
an unresolved authorization boundary needs their decision. The machine validates
binding/completeness, not the truth of a semantic assessment.

Create a planning Issue first when its ID is not known. `work --title` cannot
invent an assessment of a speculative ID: it may leave the created task in
Backlog until assessment is recorded. Before `work ID`/`start ID`, obtain current
identity with:

```sh
python -m tracker task-context assess EX-12 --actor Worker-W001 --session actual_run
```

Copy the returned identity into a JSON document. Add `verdict` (`ready`,
`needs-clarification` or `superseded`) and concrete nonempty `code`, `decisions`,
`related_work`, `dependencies` and `report` strings. Record with the same command
plus `--evidence-file readiness.json --apply`. Negative conclusions remain durable
assessments and grant no start. Start with that same genuine Worker/session.
Changed requirements or main and a new owner require a current assessment; an
unchanged same-owner resume reuses it. Claims/status writes still enforce all
existing classification, dependency, capacity and ownership gates.

## Requirements and the terminal working journal

All Issue text remains authoritative except one explicitly paired terminal block:

```text
<!-- tracker-journal:start -->
### Handoff
Current branch, PR, observations and dated verification history.
<!-- tracker-journal:end -->
```

Markers are exact standalone lines, appear once in that order and close the Issue
apart from whitespace. No checklist items, `needs:` dependencies or `Blocked:`
records belong inside. Ambiguous or unsafe placement fails closed. Ordinary
History/Verification headings have no special meaning. Moving or deleting an
outside requirement changes scope. Checkbox state is not normalized away.
New normalized scope has the opaque `v2:` prefix; old hashes always retain their
whole-body meaning. This avoids rehashing historical receipts or allowing a newly
installed engine to reinterpret old proof. Adding/changing only the permitted
journal does not invalidate new-policy source approval or acceptance criteria.

## Compatibility after a forward main change

An Approved source keeps its original author, head/tree, requirements, source
checks and independent review. A changed main still needs an independent semantic
compatibility assessment. Disjoint file paths alone are insufficient. If code or
conflict resolution is necessary, release compatibility review and use the normal
Coordinator return to the author, checks, handoff and independent source approval.

```sh
python -m tracker task-context review-acquire EX-12 --actor Reviewer-R001 --session actual_review --coordinator Coordinator-C001
python -m tracker task-context review-record EX-12 --actor Reviewer-R001 --session actual_review --claim ACTUAL_CLAIM
```

The first reserves an existing bounded source lane; the second previews the exact
evidence identity. Add `verdict: compatible` and nonempty `main_changes`,
`semantic_dependencies`, `checks` and `report`, then record with
`--evidence-file compatibility.json --apply`. The Reviewer must be a genuine
non-implementing run independent of every task implementer. The original approval
and source report are retained; a separate chained receipt binds their digest,
old/current main, exact scope/head/tree, policy and real review owner. No source
test result is relabeled as a test of new main. Status remains Approved.

A held compatibility lane blocks selection of that source into a batch. Return
or interruption cannot manufacture compatibility. The same owner resumes with
the real claim. `execution review-takeover --previous-claim ... --stopped --reason
...` supports a distinct independent replacement after verifying the old run has
stopped; it grants no verdict. `task-context review-release ID` with the real
Reviewer/session/claim and `--report-file` releases the lane without approval.
After an uncertain committed `review-record`, retry the identical evidence and
owner/claim; inspect changed main/scope before proceeding.

Each frozen batch still has independent combined review, composition verification,
all applicable candidate checks, current main admission and any Stage/production/
package checks. Already-frozen members and divergent main use normal held-batch
or author recovery. Multiple sequential batches can revalidate remaining unchanged
sources without inventing author edits or repeating original source review.

## Activation and compatibility

New project state with this explicit policy is initialized by `execution migrate`.
It uses outer protocol 4, retaining execution state 2. Old writers reject it.
Maintenance can wrap protocol 4 and resume it without dropping the fence.

For an existing project, first integrate matching reviewed descriptor/config and
publish compatible actual writers/projection. A selected package source must
complete its authorized target delivery before maintenance; use the bounded
[one-policy package continuation](DELIVERY_POLICY.md#one-policy-selection-package-before-activation)
only where its exact prior-baseline/main contract qualifies. Under genuine owned maintenance,
drain active owners/intake/stage and pending delivery, then preview/apply `task-context adopt` using the
actual Maintainer actor/session/claim/report. It verifies official current-main
config and preserves queues, old approvals, delivery receipts and history without
rehashing them. Old full-body evidence stays conservative; later newly reviewed
scope uses the explicit journal protocol. Resume the same maintenance owner and
verify the fresh board. No live adoption is part of this source implementation.
