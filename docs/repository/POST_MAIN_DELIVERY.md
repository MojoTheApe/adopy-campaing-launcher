# Temporary direct-main batches and authorized delivery

Owner decision: 2026-10-15. Parent [ACL-1923](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/923)
retains live activation and actual final delivery. Source child
[ACL-1924](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/924)
implements this selection. Shared contracts belong to MR-28 and TT-127. Selected actual merged pins are
Master `a3f56106f52d0ae19986920519b2fa46a380798b` and Tracker
`e775c624e0323e69ed891926691c3dee6587081a`.
**Source preparation is not activation.** Until the separately recorded supported
idle transition, all existing work uses its original stage/main admission rules.

## Selected route

The selected descriptor disables staging; the matching Tracker board omits Stage.
Keep permanent `stage`, all its history and the Windows isolation/recovery tools.
This temporary choice changes the destination of a frozen independently reviewed
batch. It preserves five Workers, five source reviews, batches of at most five,
source approval, independent combined review and every applicable required check.

One-task ordinary sources remain approval-only PRs into main. Only the actual
reserved `codex/batch-ID` branch with its matching `Tracker batch: ID`, retained
members, independent review and main checks can merge. Do not merge a source PR
directly or claim that merely changing a label authorizes delivery.

`In Progress -> Review -> Approved -> reviewed frozen batch -> main -> Merged ->
verified delivery`. Under the standing owner-approved policy, an ordinary approval
to integrate or merge the selected ACL work includes package delivery. The agent
automatically records the genuine Coordinator request through `batch begin --deliver`,
selecting `--delivery-workflow .github/workflows/complete-delivery.yml --delivery-job
'Verify published Windows package'`. Do not ask the owner to add “and deliver”.
An explicit owner source-only/skip-release instruction overrides this default.
A development request, bare push or unrequested machine batch still grants no
release authority; the durable exact-batch request remains mandatory.

The agent still runs current `check-pr` / `check-merge`, merges after gates, records
the actual `merged ... --apply --reviewed-complete` transition and cleans up the
superseded source PRs. This reviewed main journal covers complete implemented
scope. Unfinished business criteria cannot be checked off for the publisher.
Release publication and package acceptance are separate delivery evidence.

## Durable continuation

`complete-delivery.yml` wakes on successful main CI. The portable pinned Tracker
resolver reads only the official repository, actual archived authorized batch and
exact admitted per-task main receipts. An unrelated push, process-only merge,
untrusted PR event or missing request cannot create a release. A bounded retry
covers the main-CI / Coordinator handoff race; a retained pending request can be
resumed with this workflow's `workflow_dispatch` and the same successful CI run.
This is recovery under the original request, not another routine owner approval.

The resolver binds repository, main PR, SHA/tree, ordered issues, batch, policy,
target and original authorization. The release tag is deterministic
`acl-release-YYYY-MM-DD.MAIN_PR`, using the original request date; retry keeps the
same version and tag. Existing tags cannot be repointed. Native main CI must pass
the maintained full matrix proof. The current main guard prevents an old event
from publishing over newer content. A pending authorized delivery blocks the next
batch for its target; finish or explicitly recover the original request first.

The reusable existing `release.yml` builds/tests/signs the installer and publishes
the complete immutable release. A direct reusable call works when a tag is pushed
with `GITHUB_TOKEN`, whose tag push does not start another Actions workflow. On
retry a verified already-published exact release is retained and acceptance runs
again; an incomplete draft requires inspection, never clobbering.

The independent native Windows acceptance job downloads the actual previous and
current published packages through the configured update channel. It applies the
same manifest signature, size/hash and version checks as the updater, then runs
real isolated installation, update, restart, recovery and preservation checks.
The signed release manifest records the actual previous stable channel publication,
its source commit and release identity; failed/unpublished source tags and drafts
never become the update baseline. It is distinct from the prepublication
same-candidate two-build mechanism test.
Missing prior publication, unreadable channel, failed trust, missing batch tasks,
wrong version/tree or any failed acceptance leaves delivery unfinished.

Only after that exact successful native job emits its receipt can the downstream
portable Tracker recorder CAS-write delivery for its frozen merged tasks. It
validates the real official workflow/job and report in native logs, the current
request and target, and exact merged receipts. Artifacts alone do not attest
success. If acceptance passed and only the receipt write failed, dispatch with
`receipt_run` set to the original run ID. That recovery validates the original
native job and its native log-bound producer artifacts, including cloned successful jobs after
partial retries and writes only the missing receipt;
it does not rebuild or reinstall. Missing/expired evidence stays pending. No private Tracker checkout or additional broad secret is needed.
Do not claim every employee updated or that isolated tests prove live business use.

## Guarded activation and return to Stage

1. Finish source ACL-1924 under the OLD reviewed stage/main policy, preserving its
   genuine original Worker/review/batch evidence. The new head cannot adopt its own
   no-stage shortcut. Use the actual base configuration for admission/Tracker
   linkage during this policy change; full integration CI still runs.
2. Publish compatible reviewed Tracker/service and Master/consumer pins. Inspect
   all current owners, queue and stage. Do not stop or transfer unrelated runs.
3. Reserve maintained owned maintenance if required, drain actual owners and use
   the supported `delivery-policy migrate --previous-config FILE --previous-revision FULL_SHA
   --report-file FILE`
   preview/apply while idle. Preserve completed history and every queued handoff.
   Older approvals lose current-policy validity while their original proof stays
   retained. Re-review them rather than rewriting evidence.
4. Confirm the next fresh board/config uses no Stage, then resume intake. Record
   source merge, service version, policy transition and board evidence separately
   in parent ACL-1923. A descriptor or a merged source is not that proof.
5. Run/observe the authorized real continuation, native installer result and exact
   recorded task delivery. Parent ACL-1923 stays unfinished until its full outcome.

To restore Stage, make a new reviewed policy task: enable staging in the descriptor
and matching Tracker policy, restore Stage before Merged in the workflow, preserve
5/5/5 capacity, and finish pending delivery/owners first. Reconcile the retained
permanent stage with current main through reviewed merges (never reset it). Publish
compatible source, perform another supported idle preview/apply, revalidate queued
approvals and verify the fresh board. The post-main continuation remains available;
only the pre-main batch route regains exact-stage verification and promotion.
