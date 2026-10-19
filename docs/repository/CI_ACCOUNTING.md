# On-demand Actions execution accounting (ACL-1968)

`scripts/acl_ci_minutes.py` reports observed execution intervals, separately from
actual billing. It uses authenticated GitHub REST **GET** requests only. It does
not dispatch/rerun workflows, change checks, delete caches/artifacts, change
retention or run a background collector. Keep the original ACL-1649 2–3x target
and historical measurements; this correction establishes no numerical saving.

ACL supplies only its default repository, private capture boundary and known
orchestration job names. The executable report is the exact standalone
`.workflow/tools/tracker_ci_accounting.py` artifact from published Tracker3.5.0,
commit `df0154b96410296b50bc9d7a00af7847d9a97700`. Its source blob, SHA256 and
portable floor are recorded in `tracker/ci-accounting-artifact.json`; the adapter
checks its fixed digest before loading it. Changed bytes require a reviewed
artifact refresh. There is one accounting implementation, with no private engine
import or checkout at report time.

The artifact was introduced while the project used workflow engine e775; its
provenance records that historical context separately from the artifact's source.
The artifact does not select or constrain a later independently reviewed workflow
engine. It changes no descriptor/config policy, ownership/state or generated
integration file and creates no replacement full export manifest. A full workflow
upgrade must inspect this local addition and its fixed digest through the normal
three-way conflict/review procedure. The adapter owns its capture root; callers
cannot override it with `--project-root` to write raw observations into ACL.

```sh
python scripts/acl_ci_minutes.py --from 2026-10-15 --to 2026-10-15
python scripts/acl_ci_minutes.py --from 2026-10-15 --to 2026-10-15 --json
python scripts/acl_ci_minutes.py --from 2026-10-15 --to 2026-10-15 --group-by execution --lookback-days 30
```

Dates are UTC. `--from` also accepts a whole-second ISO timestamp for a bounded
ordinary-work sample; `--until` is an exclusive whole-second ISO cutoff. `--to`
includes its entire UTC date. Creation grouping includes **all observed attempts**
of runs created in the selected window, even when a rerun executed later. Execution
grouping uses overlapping intervals clipped to the period, discovering runs by
creation date within the explicit lookback. Older runs can rerun later, so this
mode always reports incomplete/unknown older-run coverage. It does not claim a
complete execution-period census from a bounded creation search.

## Raw observations, replay and privacy

Capture only when needed for an authorized report. Choose a private path outside
the source repository; the command refuses overwrite and requests mode0600
(POSIX permissions; on other systems protect the containing directory).

```sh
python scripts/acl_ci_minutes.py --from 2026-10-15T08:44:37Z --to 2026-10-15 --until 2026-10-15T08:44:38Z --save-snapshot /private/operator/ci-before.json --json
python scripts/acl_ci_minutes.py --snapshot /private/operator/ci-before.json
python scripts/acl_ci_minutes.py --snapshot /private/operator/ci-before.json --json
```

Raw GET snapshot schema1 records original successful responses or sanitized HTTP
failure status and the selected parameters. It can contain private repository,
actor, PR and artifact metadata: never commit or publish it. GET observations are
sequential, not an atomic historical snapshot. Replay uses only recorded endpoints;
missing responses remain visibly unknown and never fetch current data. The same
saved inputs/parameters reproduce the human and JSON output. JSON report schema2
replaces the earlier misleading billing/reuse fields; this is a report, not an
admission/coverage gate. Exit0 means a report was produced, including an explicitly
incomplete report; inspect its coverage and unknown fields.

The reviewed minimal native regression projection is
`tests/fixtures/acl_ci_minutes/carried_forward.json`. It retains one actual run's
execution identity/times/steps and raw native job IDs, without actors, credentials
or job logs. It is **one selected run**, not a repository-period population or a
before/after savings sample. Run `python -m pytest tests/test_acl_ci_minutes.py -q`
to reproduce its 20 rows →12 intervals, eight carried rows, and 212 naive versus
112 distinct rounded interval estimates. Both numbers are estimates, not invoices.

## What is counted and what remains unknown

Within one run, carried rows need the same job name, actually supplied matrix,
interval, actual runner, conclusion and digest of native steps across different
attempts. New job IDs,
check-run URLs or attempt numbers alone are not execution identity. Native absent
runner-group sentinels 0/null are equivalent; actual runner changes remain distinct
and unresolved. Same-attempt multiplicity is retained. Identical intervals in
different runs count separately. Changed/missing identity at the same interval
stays unresolved and is excluded from counted consumption, with visible gaps and
an explicit possible additional contribution. Observed unresolved rows have
lower/upper bounds; unknown duration or absent history leaves the overall upper
bound unknown. An incomplete estimate is never presented as a complete total.
Skipped rows are reported separately and never called proven reuse. Unknown
duration, platform, matrix axes and retry cause remain unknown; job names/native
references are retained, not interpreted as a review/coverage contract. There is no
new retry policy or execution budget in this command. Read-only GET retries do
not consume native execution attempts.

Identical repeated native IDs across pages are deduplicated. If the same ID
changes, every version of that ID is excluded from known interval/storage totals,
and an explicit collection gap remains even if a later page repeats its original
value. Conflicting jobs cannot establish a complete global upper bound; conflicting
artifact/cache observations cannot establish a byte total. Raw snapshot pages
retain the original observations for inspection.

Every interval retains seconds, its rounded-up minute estimate and native
observations. Selected execution-period seconds are clipped; daily estimates
round each daily portion and need not sum to an interval's rounded estimate.
Run/candidate/workflow/event/PR identity comes from native data. A batch ID is read
only from an actually linked PR body and is not authorization or frozen-scope
proof. First-job and first-finished-job latency and observed workflow span are
not a claimed full task cycle or proof of useful product feedback. Known exact
orchestration job names are separated; other overhead is unclassified.

The created-run endpoint's documented 1000-result cap is partitioned to UTC-second
windows; IDs are deduplicated and missing pages/counts or an irreducibly capped
second remain explicit gaps. Mutable native API pagination can change during a
read; conflicting observations are not silently called complete. Jobs are read
with `filter=all`. Relevant API contracts:
[workflow runs](https://docs.github.com/en/rest/actions/workflow-runs),
[workflow jobs](https://docs.github.com/en/rest/actions/workflow-jobs).

Platform seconds/minutes are not money. There is no fixed Windows monetary
multiplier or invented per-job net charge. Billing, historical SKU/rates, included
usage and discounts remain unknown without verified account evidence; a missing
or denied billing endpoint is not zero. Current
[artifact](https://docs.github.com/en/rest/actions/artifacts) and
[cache](https://docs.github.com/en/rest/actions/cache) listings report bytes and
their actual creation/expiry/access/retention-limit fields separately. Missing
storage access is unknown, not zero. These current listings do not reconstruct
historical storage consumption or charges, and the command changes no lifetime.

## Preserved workflow

This project-owned report changes no shared/exported policy or CLI pin. Master
Task05 (MR-38) owns the coordinated baseline; durable retry and verification-plan
changes remain separate unfinished work and exported changes require their actual
Tracker companion. Existing source/combined review, full applicable Linux/Windows/
installer CI, metadata separation, same-PR cancellation and package acceptance
remain in force. A report source PR is an ordinary singleton approval source;
five occupied Reviewer lanes do not authorize bypass or release of another owner.
