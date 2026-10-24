# ACL standalone workflow provider — ACL-2156

The owner selected Task Tracker as ACL's executable workflow provider on
24 October 2026. Master Repo stays the immutable documentation reference.
This is an explicit additive route for ACL, not an upgrade of other consumers,
a global skill change or a live Tracker service deployment.

## Exact source and unchanged controls

Use published [Task Tracker v3.14.1](https://github.com/MojoTheApe/task-tracker/releases/tag/v3.14.1),
actual merged commit `771d55f3317397e7d56471aa966f0a9de37d1a2e`.
`delivery.json.workflow_provider` schema1, the descriptor Tracker pin,
`tracker/engine.json` and `.github/workflows/delivery-check.yml` identify that
same source. The explicit `workflow_provider_transport: private-checkout` is
retained in the descriptor and genuine source-rendered baseline inputs. The
source-owned workflow fetches the immutable official provider first, then ACL in
a separate sibling directory. Its fixed isolated Python entry validates ACL only
as data and authenticates the complete source-owned export instead of trusting a
consumer-rehashed manifest. Local work uses a clean official exact-source
checkout and `scripts/workflow_provider.py`; see the immutable
[provider procedure](https://github.com/MojoTheApe/task-tracker/blob/771d55f3317397e7d56471aa966f0a9de37d1a2e/docs/STANDALONE_WORKFLOW_PROVIDER.md).

Master1.8.0 at `fddcdd0d60db2e14cce0a53e6af23e12356c3bc7` remains the identical
`standard` reference. The descriptor's verification procedure now names the
equivalent trusted Task Tracker provider validation and retains ACL's adoption
audit. Use the clean immutable official source with ACL as `--root`; the private checkout
uses the official source verification for clean origin, full HEAD and every
tracked source blob. Its fixed local CLI deliberately omits `--expected-revision`,
which belongs to the separate remote Action path. Actual required GitHub
check names are unchanged. Do not execute a candidate validator or require a
Master publication for this selected route.

`tracker/config.json` is byte-identical to the real prior Main. Current adopted
execution3 capacity5/5/5, disabled Stage, package target, completion requirement,
all exact required checks, legacy aliases/+1000 IDs and existing ownership/history
remain. The guarded ACL adapter, native state, installer, product behavior,
employee data and complete local runtime/engineering knowledge are preserved.
Source pins do not activate task context, completion, retry, CI evidence, pause
or another optional policy. Existing draft adoption and readiness guards remain.

## Public ACL and private Task Tracker

ACL is public; Task Tracker remains private. The ordinary private remote Action
sharing setting cannot serve this public consumer and remains at its original
`none`. Use the provider-owned private-checkout workflow without local extra
steps. Both official checkout invocations retain their exact pinned commit and
`persist-credentials: false`; provider SSH uses strict host verification. The
provider and consumer stay in `trusted/provider` and `trusted/consumer`. Only the
verified provider entry executes, after provider authentication has been removed;
the job never imports, installs or executes consumer code or uploads private
source artifacts.

The repository operator must separately authorize and provision the dedicated
read-only Task Tracker deploy key through ACL Actions secret
`TRACKER_PROVIDER_READ_KEY`. It grants trusted ACL workflow writers effective read
access to the private provider's complete code/history; do not reuse developer,
release or update credentials. Source selection alone does not prove that grant
was authorized or provisioned. Public fork and Dependabot PRs without the secret
fail closed; no privileged PR event or skipped validation substitutes for a
successful actual trusted-branch descriptor run. Verify that actual run before
source admission. Normal independent review, every required check and full
published-package acceptance remain mandatory. No private key or internal report
belongs in this repository.

## Honest template ancestry and local adaptations

The actual prior four-field baseline contains92 pristine files. Its SHA256 was
`9850c00d747ba1e932b3b3dfad5f667dde6463b06ff80d36ff2d7752eab76bb4`.
The exact official historical Master `fddcdd0d60db2e14cce0a53e6af23e12356c3bc7`
and Tracker `e71c9829247e60d79031dbb1bb038896fcedf4cc` reproduced every file
byte-for-byte. They were read for the one-time ancestry audit; no Master source
or remote was changed. The new92 pristine bodies and baseline come from the
published provider's deterministic renderer, never hand-written metadata.
The baseline keeps its original four-field schema and documentation reference;
its inputs additionally identify the executable provider.

The ordinary three-way migration found exactly two genuine local overlaps:

- The descriptor workflow selects only the complete source-owned immutable
  private-checkout transport. Preserve its
  master/main/stage push events, PR behavior, concurrency, credential settings
  and draft validation.
- `WORKFLOW.md` keeps the full ACL-specific rules and adds this provider route.

These resolutions require independent review of the complete actual diff.
All other files use source-owned rendering/three-way handling and preserve
project-owned areas. Unsupported input versions, altered official sources,
unsafe paths/modes, incomplete/tampered exports, inconsistent capabilities or
check/target/policy changes must fail, not silently fall back.

## Review, delivery and later selections

This support task changes no native policy and grants no new process-completion
waiver. Preserve the normal claimed source, independent whole-scope source
review, frozen intake, combined review, all applicable Linux/Windows/installer
checks, actual Main acceptance and authorized stable package delivery. Main
merge alone is not Done. Existing Master-authenticated process-support proofs
keep their own semantics; directory names do not establish eligibility.

After this foundation is actually delivered, ACL-2046 updates its original
branch for authenticated deterministic ordinary batch folding before combined
review. ACL-2099 follows as its own genuine singleton: exactly the five generated
governance consequences plus its authentic same-task changelog fragment, with
all other pristine files unchanged. Its newer narrow selection contract retains
that fragment; it is not an ordinary terminal-fold batch. A malformed or mixed
selection stops rather than receiving a broad fold exemption.

The selected task-context config remains fenced before adoption. Its authorized
selection package uses the retained **actual official preselection** config and
the existing strict frozen-identity/source/tree/receipt proof. Do not drop a key
or invent a config to bypass the fence. Deliver ACL-2099 last in this sequence;
unrelated new work waits for any separately authorized readiness/idle adoption.
Keep original PR995 history and current PR1100. Neither this migration nor either
task silently performs that separate activation.
