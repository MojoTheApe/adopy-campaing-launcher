# Authorized delivery continuation

Follow the [shared continuation contract](https://github.com/MojoTheApe/master-repo/blob/864ca7a5814e703dd6e61207362d24cbc26dc9ad/docs/delivery-continuation.md)
at this project's exact standard pin and [runtime procedure](../runtime/DELIVERY.md).
Under this adopted flow, owner approval to integrate/merge selected tasks into
main covers review/checks,
optional stage, admitted main merge, maintained delivery, verification and final
Tracker receipt. The owner need not also say “and deliver”. Starting development
or source approval alone does not authorize integration/deployment.
Do not request another routine owner approval after main; report a concrete missing
permission, access or failed check instead.

The Coordinator must continue or hand off durably to the configured project CI.
Shared templates do not create a release/deploy mechanism or a background agent.
For compatible runtime/package batches, the Coordinator persists this standing
authorization by default with `batch begin --deliver --delivery-workflow PATH --delivery-job
EXACT_NAME`, selecting the real project native verification producer. This is an
agent implementation detail, not another approval. Honor explicit source-only or
merge-only/skip-release instructions; deferred runtime delivery stays unfinished.
Use the maintained separate release path when later authorized rather than adding
permission by resuming an old batch. Arbitrary main pushes cannot authorize release. A trusted
workflow can use the exported `tracker_delivery_receipt.py resolve`/`record` with
a same-repository token, successful main CI, actual Merged handoff and exact native
verification job/log proof. Follow the pinned guide; the portable recorder creates
no authorization or deployment. No private engine access token is required.

Before claiming it works automatically, implement and test the project's actual
adapter, including a failed run, resumable retry and duplicate request.

Persist exact merged revision/tree, task set, configured target and run/artifact
identity; serialize changes per target and prevent stale runs replacing newer work.
Verify actual profile-specific acceptance, then record delivery through the exact
pinned Tracker and read back completion. Preserve failed/pending tasks and evidence.
A receipt retry must not repeat a completed install/deploy or create a new version.

Tooling/local and explicitly n8n-source work retains admitted reviewed-merge
completion. Package requires stable publication and agreed installation verification;
VPS/ordinary n8n requires actual configured production behavior. Preserve local
release, credential, backup, rollback and separate authorization controls.
