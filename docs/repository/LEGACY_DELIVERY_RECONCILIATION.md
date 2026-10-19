# Explicit historical package delivery reconciliation

`legacy-delivery` is a bounded recovery operator for original minimal pre-batch
pending receipts retained by a genuine inspected staging-only policy migration.
It is not ordinary admission, a batch producer, a release command or an extension
of `historical_completion`. Missing receipts, modern batches, failed/verified
records and incomplete business acceptance cannot use it.

Use a clean independently reviewed **merged** engine with the consumer's exact
current `tracker/config.json`. The existing compatible consumer pin and snapshot
writer need no change; never patch a runtime cache, deploy the service or upgrade
another project as a test. First prepare and independently review the complete
allowlist and factual audit, then inspect a zero-write preview:

```sh
python -m tracker --config /absolute/project/tracker/config.json legacy-delivery \
  --evidence-file /private/audited-legacy-delivery.json
# After inspecting the unchanged exact manifest and preview:
python -m tracker --config /absolute/project/tracker/config.json legacy-delivery \
  --evidence-file /private/audited-legacy-delivery.json --apply
```

The manifest has these fields. Hashes are lower-case SHA-256; `digest` below is
`tracker.delivery_receipt.digest` (canonical compact sorted JSON). Issue numbers
are GitHub Issue numbers in this repository, not guessed public task IDs.

| Field | Required evidence |
| --- | --- |
| `kind`, `schema_version` | `legacy-delivery-reconciliation`, integer `1` |
| `repository`, `policy`, `config_sha256` | Exact current official repository, `delivery_policy.fingerprint(config)`, `digest(config)` of the entire loaded current config |
| `issues` | Explicit unique ordered selected Issue numbers, 1–128 |
| `authorization` | New actual owner decision: `approved_by`, concrete `report`, exact same ordered `issues`; previous migration retention is never permission |
| `implementer`, `report` | Actual implementation identity and durable complete-scope audit reference |
| `tasks` | Exactly the selected decimal Issue keys, each with the per-task attestation below |
| `migration_sha256` | `digest` of the genuine unchanged `delivery_workflow.policy_migrations` audit that retained the selected original records |
| `native` | Original unchanged `request` and exact `receipt`, actual integer `run`/`job`, full actual installed-package `report` object |
| `release` | Exact published release `id`, and exactly three `assets`: installer, `release.json`, `release.json.sig`; each includes `id`, `name`, `size`, `digest` from official GitHub metadata |

Each `tasks["NUMBER"]` contains `id` (actual public task ID), `scope`
(`delivery_policy.task_scope(issue)`), `accepted_scope`
(`delivery_policy.accepted_scope_digest(issue)`), `receipt_sha256`
(`delivery_policy.receipt_digest(original_pending_receipt)`), actual independent
`reviewer` distinct from `implementer`, `verdict: complete`, concrete durable
`report` and nonempty `checks` listing passed full-scope evidence. Review the full
original task, branches, source PRs, follow-ups, acceptance and live-business
requirements. A package smoke test alone does not establish business completion.
Attestations are reviewed records, not cryptographic proof of identity. Keep
private evidence and any sensitive details out of public PRs/logs.

The operator requires unchanged Merged scope/identity, no unfinished checklist
(outside fenced examples), no blocker/archive and no selected active ownership.
A successful minimal `{title, state: created, issue}` Issue-creation journal from
`work` remains unchanged as creation history; it is not active ownership. Pending,
uncertain or claimed selected intake still blocks.
Root `todo_order` retains manual sort positions: `queue.ordered_todo` first
filters actual Todo labels. A selected exact Merged task's leftover position is
not ownership; preserve the complete list without editing or dropping entries.
Actual live Todo/multiple statuses, claims, review/approval queues and running
batch/intake/run ownership remain blocked. Malformed sort metadata fails closed.

Normal `catalog.restore` removes the Issue archive flag but retains the completed
execution archive journal. Only the exact empty `implementers`/`state: archived`
stub can be treated as non-owning history here, and only with its genuine archive
history and complete official archive-label then later removal events bracketing
the recorded archive time. Current accepted Merged scope must remain unarchived;
extra author/claim/batch fields, `archiving`, missing/stale restoration proof or a
new current archive cannot use this exception. The stub/history are never edited.
The concrete ACL-1313 case is substantiated by official events
[33307225956](https://api.github.com/repos/MojoTheApe/adopy-campaing-launcher/issues/events/33307225956)
and
[33307524304](https://api.github.com/repos/MojoTheApe/adopy-campaing-launcher/issues/events/33307524304).

It reconstructs each original official historical stage-to-main source linkage,
merge SHA/tree and ancestry to both current main and the actual installed source.
The immutable previous official config must substantiate the original fingerprint
and inspected staging-only audit. It never invents modern source reviews or
admits historical PRs as new frozen batches.

Package evidence is independently corroborated, not accepted from uploaded JSON
or a green generic CI. Native log transport privately captures the bounded output
and retries with gh’s escape opt-in only on its explicit compatibility rejection.
The original archived delivery request, exact task set,
authorization digest, official main batch/merge/tree and successful main CI stay
unchanged. Its trusted native workflow/job and latest actual verifier must match
the exact successful native receipt in the real job log. The complete installed
report must also occur exactly once as actual native stdout. The report binds
passed Windows install/update/restart/recovery/data checks, valid publisher
signature/key identity, source/tree and signed manifest/installer hashes. Official
stable release and asset IDs, sizes and hashes must still match. GitHub releases
need not enable their optional immutable flag: exact native content hashes and
asset identities prevent a mutable tag/asset replacement from borrowing proof.
A download counter change is not a source/content change.

The original producer's task scope is preserved. For example, an original request
for one task remains that one task even when its real package acceptance supports
an explicitly authorized **new** retrospective record for audited older tasks.
The operator never edits that request or claims those older tasks ran in its batch.

Preview performs no writes. Apply makes a single supported compare-and-swap write
changing only selected normal `delivery_workflow.tasks` rows to current-policy
verified delivery, preserving each exact minimal original in `history`. A private
additive audit retains the new owner decision, scope/reviewer, manifest digest,
original producer reference/content digests and release identities. Existing
compatible e775 readers project these ordinary verified receipts unchanged;
portable exports, admission, state protocol and historical exemptions do not change.
Unselected receipts, claims, owners, queues and original native requests stay exact.

Unrelated Workers, Reviewers and maintenance can remain active; this receipt-only
operation does not reserve global maintenance or require global idleness. A newer
state write causes CAS rejection; inspect a fresh preview and retry instead of
replaying a stale state body. Current Issues, main/config and release identities
are re-read before the write. The actual native run/current attempts/latest
verifier and exact receipt/report stdout are also re-read through uncached
transport immediately before CAS: a superseding failed, running or different
verifier cannot borrow the original success even when Tracker state is unchanged.
Inspected same-manifest retries revalidate all proof
and preserve an already-recorded result; different or stale evidence cannot
replace verified delivery. After apply, re-read the stored rows and fresh Tracker
snapshot. A synchronization failure remains unfinished verification.

Master Repo impact: no matching source change. This explicitly invoked operator
uses existing policy, ordinary receipt history and CAS storage without changing
any descriptor/config schema, policy fingerprint, exported helper, admission
rule or public projection. Permanent pin adoption and service publication remain
separate reviewed work.
