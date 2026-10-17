"""On-demand Actions execution accounting, not an invoice (ACL-1968).

--snapshot replays private saved GET observations without network. Creation
cohorts include all observed attempts; execution periods clip observed intervals.
Bounded creation lookback cannot prove absence of older reruns. No dispatch,
deletion, retention mutation or automatic collection is performed.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time

DEFAULT_REPO = "MojoTheApe/adopy-campaing-launcher"
UTC = dt.timezone.utc
ROOT = Path(__file__).resolve().parents[1]
CAP = 1000
OVERHEAD = {"Classify and check project tooling", "ACL verification", "Process instructions",
            "Verify build inputs", "descriptor", "tracker-link", "Installer verification"}


class APIError(RuntimeError):
    def __init__(self, status=None):
        super().__init__("GitHub GET unavailable")
        self.status = status


def gh_api(path, attempts=4):
    """Read-only transport retries are not native execution attempts."""
    for attempt in range(attempts):
        try:
            return json.loads(subprocess.check_output(
                ["gh", "api", "--method", "GET", path], text=True, encoding="utf-8",
                stderr=subprocess.PIPE, timeout=60))
        except (subprocess.SubprocessError, OSError, ValueError) as error:
            match = re.search(r"HTTP (\d{3})", str(getattr(error, "stderr", "")))
            status = int(match[1]) if match else None
            if attempt == attempts - 1 or status and status < 500 and status != 429:
                raise APIError(status) from None
            time.sleep(2 * (attempt + 1))


class SnapshotAPI:
    """One observation per GET endpoint. Missing replay coverage never fetches latest."""
    def __init__(self, transport=None, snapshot=None):
        self.transport = transport
        self.snapshot = snapshot if snapshot is not None else {"schema_version": 1, "responses": {},
                                     "captured_at": dt.datetime.now(UTC).isoformat(timespec="seconds"),
                                     "consistency": "Sequential GET observations, not an atomic historical snapshot"}
        if not isinstance(self.snapshot, dict) or self.snapshot.get("schema_version") != 1 or not isinstance(self.snapshot.get("responses"), dict):
            raise ValueError("Expected raw GET snapshot schema 1")

    def __call__(self, path):
        responses = self.snapshot["responses"]
        if path not in responses:
            if self.transport is None:
                raise APIError()
            try:
                responses[path] = {"value": self.transport(path)}
            except APIError as error:
                responses[path] = {"error": {"http_status": error.status}}
        row = responses[path]
        if "error" in row:
            raise APIError(row["error"].get("http_status"))
        return row["value"]

    def save(self, path, parameters):
        path = Path(path).resolve()
        if path.is_relative_to(ROOT):
            raise ValueError("Raw private snapshots belong outside this repository")
        self.snapshot["parameters"] = parameters
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(self.snapshot, output, indent=2, sort_keys=True)
            output.write("\n")


def _time(value):
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(UTC) if parsed.utcoffset() is not None else None
    except (AttributeError, TypeError, ValueError):
        return None


def _iso(value):
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _bounds(first, last, until=None):
    try:
        start = dt.datetime.combine(dt.date.fromisoformat(first), dt.time(), UTC)
    except ValueError:
        start = _time(first)
        if not start or start.microsecond:
            raise ValueError("--from needs a date or whole-second ISO timestamp with timezone")
    end = dt.datetime.combine(dt.date.fromisoformat(last) + dt.timedelta(days=1), dt.time(), UTC)
    if until:
        cutoff = _time(until)
        if not cutoff or cutoff.microsecond:
            raise ValueError("--until needs an ISO timestamp with timezone")
        end = min(end, cutoff)
    if end <= start:
        raise ValueError("Report period must be nonempty and ordered")
    return start, end


def job_seconds(job):
    start, end = _time(job.get("started_at")), _time(job.get("completed_at"))
    return (end - start).total_seconds() if start and end and end >= start else None


def job_minutes(job):
    seconds = job_seconds(job)
    return math.ceil(seconds / 60) if seconds is not None else None


def platform(job):
    text = " ".join(str(x).lower() for x in [*(job.get("labels") or []), job.get("runner_name") or ""])
    if "windows" in text:
        return "windows"
    if "macos" in text or "mac os" in text:
        return "macos"
    return "linux" if "linux" in text or "ubuntu" in text else "unknown"


def is_windows(job):
    return platform(job) == "windows"


def list_runs(api, repo, first, last, until=None):
    """Partition the documented 1000-result created search cap to UTC seconds."""
    start, end = _bounds(first, last, until)
    found, gaps, windows = {}, [], []
    duplicates = 0

    def window(left, right):
        nonlocal duplicates
        query = f"repos/{repo}/actions/runs?created={_iso(left)}..{_iso(right - dt.timedelta(seconds=1))}&per_page=100"
        try:
            response = api(query + "&page=1")
            rows, total = response["workflow_runs"], response.get("total_count")
            if not isinstance(rows, list):
                raise ValueError()
        except (APIError, KeyError, TypeError, ValueError):
            gaps.append({"from": _iso(left), "to_exclusive": _iso(right), "reason": "run_query_unavailable"})
            return
        if type(total) is int and total >= CAP and (right - left).total_seconds() > 1:
            middle = left + dt.timedelta(seconds=int((right - left).total_seconds()) // 2)
            window(left, middle)
            window(middle, right)
            return
        all_rows, page = list(rows), 1
        while len(rows) == 100 and len(all_rows) < CAP:
            page += 1
            try:
                rows = api(query + f"&page={page}")["workflow_runs"]
                if not isinstance(rows, list):
                    raise ValueError()
            except (APIError, KeyError, TypeError, ValueError):
                gaps.append({"reason": "run_page_unavailable", "from": _iso(left)})
                break
            all_rows.extend(rows)
        if any(not isinstance(row, dict) for row in all_rows):
            gaps.append({"reason": "invalid_run_row", "from": _iso(left)})
            all_rows = [row for row in all_rows if isinstance(row, dict)]
        windows.append({"from": _iso(left), "to_exclusive": _iso(right), "reported_count": total, "observed_rows": len(all_rows)})
        if len(all_rows) >= CAP or type(total) is int and total >= CAP:
            gaps.append({"reason": "irreducible_search_cap", "from": _iso(left), "to_exclusive": _iso(right)})
        elif type(total) is not int or total != len({r.get("id") for r in all_rows}):
            gaps.append({"reason": "count_missing_or_changed", "from": _iso(left)})
        for row in all_rows:
            identifier, created = row.get("id"), _time(row.get("created_at"))
            if type(identifier) is not int or not created:
                gaps.append({"reason": "invalid_run_identity"})
                continue
            if not left <= created < right:
                continue
            if identifier in found:
                duplicates += 1
                if found[identifier] != row:
                    gaps.append({"run_id": identifier, "reason": "run_changed_during_collection"})
            found[identifier] = row

    window(start, end)
    return sorted(found.values(), key=lambda r: r["id"]), {"complete": not gaps, "gaps": gaps,
            "windows": windows, "duplicate_run_rows": duplicates}


def pages(api, endpoint, key):
    rows, seen, gaps = [], set(), []
    for page in range(1, 1001):
        try:
            response = api(endpoint + f"&page={page}")
            batch = response[key]
            if not isinstance(batch, list):
                raise ValueError()
        except (APIError, KeyError, TypeError, ValueError):
            gaps.append("unavailable_page")
            break
        for row in batch:
            if not isinstance(row, dict):
                gaps.append("invalid_native_row")
                continue
            identifier = row.get("id")
            if identifier is None:
                gaps.append("missing_native_id")
            elif identifier in seen:
                continue
            seen.add(identifier)
            rows.append(row)
        if len(batch) < 100:
            if type(response.get("total_count")) is not int or response["total_count"] != len(rows):
                gaps.append("count_missing_or_changed")
            break
    else:
        gaps.append("page_limit")
    return rows, {"complete": not gaps, "gaps": gaps}


def _step_digest(job):
    steps = job.get("steps")
    return hashlib.sha256(json.dumps(steps, sort_keys=True).encode()).hexdigest() if isinstance(steps, list) and steps else None


def _identity(job):
    # Native carried rows encode an absent runner group as null instead of 0.
    # Both mean no group identity; positive/different real identities stay distinct.
    runner = [job.get(k) or None for k in ("runner_id", "runner_name", "runner_group_id", "runner_group_name")]
    runner.append(sorted(job.get("labels") or []))
    if job_seconds(job) is None or not _step_digest(job) or not any(runner[:2]):
        return None
    return json.dumps([job.get("name"), job.get("started_at"), job.get("completed_at"),
                       runner, job.get("conclusion"), _step_digest(job)], sort_keys=True)


def execution_rows(jobs):
    """Match carried observations across attempts; preserve same-attempt multiplicity."""
    rows, prior = [], collections.defaultdict(list)
    for job in sorted(jobs, key=lambda j: (j.get("run_attempt") if type(j.get("run_attempt")) is int else 0,
                                         j.get("id") if type(j.get("id")) is int else 0)):
        attempt, identity = job.get("run_attempt"), _identity(job)
        if type(attempt) is not int or attempt < 1:
            attempt = None
        carried = next((r for r in prior[identity] if r["first_attempt"] is not None and r["first_attempt"] < attempt and
                        not any(o["run_attempt"] == attempt for o in r["observations"])), None) if identity and attempt else None
        observation = {k: job.get(k) for k in ("id", "run_attempt", "created_at", "check_run_url", "url", "html_url", "status", "conclusion")}
        if carried:
            carried["observations"].append(observation)
            continue
        same_interval = any(r["job_name"] == job.get("name") and r["started_at"] == job.get("started_at") and
                            r["completed_at"] == job.get("completed_at") and r["first_attempt"] != attempt for r in rows)
        row = {"job_name": job.get("name"), "started_at": job.get("started_at"), "completed_at": job.get("completed_at"),
               "first_attempt": attempt, "platform": platform(job), "runner": {k: job.get(k) for k in (
                   "runner_id", "runner_name", "runner_group_id", "runner_group_name", "labels")},
               "matrix": job.get("matrix"), "matrix_source": "native" if job.get("matrix") else "unknown; native job name retained",
               "steps_sha256": _step_digest(job), "interval_seconds": job_seconds(job),
               "rounded_interval_minutes_estimate": job_minutes(job), "observations": [observation],
               "identity": "unresolved_unknown_attempt" if attempt is None else "unresolved_metadata_change" if same_interval else "observed_interval",
               "retry_cause": "unknown; no diagnosis source", "retry_execution": None if same_interval or attempt is None else attempt > 1,
               "overhead_role": "known_orchestration" if job.get("name") in OVERHEAD else "unclassified"}
        if job.get("conclusion") == "skipped":
            row.update(identity="skipped_not_proven_reuse", interval_seconds=None, rounded_interval_minutes_estimate=None)
        rows.append(row)
        if identity:
            prior[identity].append(row)
    return rows


def storage_snapshot(api, repo):
    result = {"meaning": "Current API listing; not historical storage usage/charges"}
    for kind, key in (("artifacts", "artifacts"), ("caches", "actions_caches")):
        rows, coverage = pages(api, f"repos/{repo}/actions/{kind}?per_page=100", key)
        fields = ("id", "name", "size_in_bytes", "created_at", "expires_at", "expired", "last_accessed_at")
        result[kind] = {"coverage": coverage, "reported_bytes": sum(r["size_in_bytes"] for r in rows)
                       if coverage["complete"] and all(type(r.get("size_in_bytes")) is int and r["size_in_bytes"] >= 0 for r in rows) else None,
                       "entries": [{k: r[k] for k in fields if k in r} for r in rows]}
    try:
        limit = api(f"repos/{repo}/actions/cache/retention-limit").get("max_cache_retention_days")
        result["cache_retention_limit_days"] = limit if type(limit) is int else None
    except (APIError, AttributeError, TypeError):
        result["cache_retention_limit_days"] = None
    return result


def collect(api, repo, first, last, until=None, *, grouping="created", lookback_days=30, include_storage=True):
    start, end = _bounds(first, last, until)
    if grouping not in ("created", "execution") or lookback_days < 0:
        raise ValueError("Expected created/execution grouping and nonnegative lookback")
    query_first = (start - dt.timedelta(days=lookback_days)).date().isoformat() if grouping == "execution" else first
    runs, coverage = list_runs(api, repo, query_first, last, until)
    if grouping == "execution":
        coverage["complete"] = False
        coverage["gaps"].append({"reason": "older_run_reruns_unknown", "created_lookback_start": query_first})
    total = dict(native_job_rows=0, carried_forward_rows=0, counted_intervals=0, counted_seconds=0,
                 rounded_interval_minutes_estimate=0, unresolved_identity_intervals=0, unknown_duration_rows=0,
                 skipped_rows_not_proven_reuse=0, known_orchestration_seconds=0,
                 unresolved_observed_interval_seconds=0, unresolved_rounded_interval_minutes_estimate=0)
    result = {"schema_version": 2, "repository": repo, "period": {"from": _iso(start), "to_exclusive": _iso(end),
              "grouping": grouping, "created_lookback_start": query_first,
              "meaning": "All observed attempts of created-run cohort" if grouping == "created" else "Execution overlap clipped to period; older reruns may be absent"},
              "coverage": coverage, "runs": [], "days": {}, "totals": total,
              "required_coverage": {"status": "unknown", "expected_cells": None,
                                    "reason": "Consumption observations do not establish an admission contract or prove required reuse"},
              "billing": {"status": "unknown", "net_charge": None, "rates": None,
                          "reason": "No verified historical SKU/rate, invoice, included-usage or discount source; estimates are not charges"}}
    days = collections.defaultdict(lambda: collections.defaultdict(lambda: {"seconds": 0, "rounded_minutes_estimate": 0}))
    platforms = collections.defaultdict(lambda: {"seconds": 0, "rounded_minutes_estimate": 0})
    workflow_events = collections.defaultdict(lambda: {"seconds": 0, "rounded_minutes_estimate": 0})
    for run in runs:
        jobs, job_coverage = pages(api, f"repos/{repo}/actions/runs/{run['id']}/jobs?filter=all&per_page=100", "jobs")
        row = {k: run.get(k) for k in ("id", "name", "path", "workflow_id", "event", "created_at", "updated_at", "run_started_at",
               "run_attempt", "head_sha", "head_branch", "html_url", "pull_requests")}
        row.update(job_coverage=job_coverage, intervals=execution_rows(jobs), batch_ids=[], batch_source="unknown",
                   reuse={"proven": None, "reason": "Skipped alone proves neither reuse nor required coverage"})
        for pr in run.get("pull_requests") or []:
            number = pr.get("number")
            if type(number) is int:
                try:
                    body = api(f"repos/{repo}/pulls/{number}").get("body") or ""
                    row["batch_ids"].extend(re.findall(r"^Tracker batch:\s*([a-f0-9]{32})\s*$", body, re.M))
                    row["batch_source"] = "linked native PR body; not authorization proof"
                except APIError:
                    pass
        if not job_coverage["complete"]:
            coverage["complete"] = False
            coverage["gaps"].append({"run_id": run["id"], "reason": "jobs_incomplete"})
        starts = [_time(j.get("started_at")) for j in jobs if _time(j.get("started_at"))]
        created = _time(run.get("created_at"))
        row["first_job_latency_seconds"] = (min(starts) - created).total_seconds() if starts and created and min(starts) >= created else None
        ends = [_time(j.get("completed_at")) for j in jobs if _time(j.get("completed_at"))]
        row["first_finished_job_latency_seconds"] = (min(ends) - created).total_seconds() if ends and created and min(ends) >= created else None
        row["observed_cycle_seconds"] = (max(ends) - min(starts)).total_seconds() if ends and starts and max(ends) >= min(starts) else None
        total["native_job_rows"] += len(jobs)
        for item in row["intervals"]:
            total["carried_forward_rows"] += len(item["observations"]) - 1
            seconds = item["interval_seconds"]
            if item["identity"] == "skipped_not_proven_reuse":
                total["skipped_rows_not_proven_reuse"] += len(item["observations"])
            elif item["identity"].startswith("unresolved") or seconds is None:
                total["unresolved_identity_intervals" if item["identity"].startswith("unresolved") else "unknown_duration_rows"] += 1
                if item["identity"].startswith("unresolved") and seconds is not None:
                    left, right = _time(item["started_at"]), _time(item["completed_at"])
                    possible = max(0, (min(right, end) - max(left, start)).total_seconds()) if grouping == "execution" else seconds
                    total["unresolved_observed_interval_seconds"] += possible
                    total["unresolved_rounded_interval_minutes_estimate"] += math.ceil(possible / 60)
                coverage["complete"] = False
                coverage["gaps"].append({"run_id": run["id"], "job_id": item["observations"][0]["id"], "reason": item["identity"] if seconds is not None else "unknown_duration"})
            else:
                left, right = _time(item["started_at"]), _time(item["completed_at"])
                selected = max(0, (min(right, end) - max(left, start)).total_seconds()) if grouping == "execution" else seconds
                item.update(selected_seconds=selected, selected_rounded_minutes_estimate=math.ceil(selected / 60))
                if grouping == "execution" and not selected:
                    continue
                total["counted_intervals"] += 1
                total["counted_seconds"] += selected
                total["rounded_interval_minutes_estimate"] += math.ceil(selected / 60)
                for bucket in (platforms[item["platform"]], workflow_events[str(run.get("name")) + " / " + str(run.get("event"))]):
                    bucket["seconds"] += selected
                    bucket["rounded_minutes_estimate"] += math.ceil(selected / 60)
                if item["overhead_role"] == "known_orchestration":
                    total["known_orchestration_seconds"] += selected
                cursor = max(left, start) if grouping == "execution" else created
                while selected > 0:
                    boundary = dt.datetime.combine(cursor.date() + dt.timedelta(days=1), dt.time(), UTC)
                    portion = min(selected, (boundary - cursor).total_seconds()) if grouping == "execution" else selected
                    bucket = days[cursor.date().isoformat()][item["platform"]]
                    bucket["seconds"] += portion
                    bucket["rounded_minutes_estimate"] += math.ceil(portion / 60)
                    selected -= portion
                    cursor = boundary
        result["runs"].append(row)
    result["days"] = {day: dict(sorted(values.items())) for day, values in sorted(days.items())}
    result["estimate_coverage"] = {"complete": coverage["complete"],
        "observed_unresolved_additional_seconds_min": 0,
        "observed_unresolved_additional_seconds_max": total["unresolved_observed_interval_seconds"] if not total["unknown_duration_rows"] else None,
        "global_upper_seconds": total["counted_seconds"] if coverage["complete"] else None,
        "meaning": "Unresolved bounds apply only to observed identity rows; absent runs/jobs/older reruns remain unbounded"}
    total["by_platform"] = dict(sorted(platforms.items()))
    result["by_workflow_event"] = dict(sorted(workflow_events.items()))
    result["storage"] = storage_snapshot(api, repo) if include_storage else {"status": "not_requested", "reported_bytes": None}
    return result


def totals(result):
    return result["totals"]


def render(result):
    period, total = result["period"], result["totals"]
    lines = [f"Actions execution observations: {result['repository']}",
             f"UTC {period['from']} .. {period['to_exclusive']} (exclusive); grouping={period['grouping']}", period["meaning"],
             f"Coverage: {'complete within created query' if result['coverage']['complete'] else 'INCOMPLETE / unknown'}",
             f"Native rows {total['native_job_rows']}; carried forward {total['carried_forward_rows']}; counted intervals {total['counted_intervals']}",
             f"Seconds {total['counted_seconds']}; rounded interval estimate {total['rounded_interval_minutes_estimate']} minutes (not an invoice)",
             f"Unresolved identities {total['unresolved_identity_intervals']}; unknown durations {total['unknown_duration_rows']}; skipped, not proven reuse {total['skipped_rows_not_proven_reuse']}",
             "Estimate bounds: " + json.dumps(result["estimate_coverage"], sort_keys=True)]
    lines.extend("Coverage gap: " + json.dumps(g, sort_keys=True) for g in result["coverage"]["gaps"])
    lines.extend(day + ": " + json.dumps(v, sort_keys=True) for day, v in result["days"].items())
    lines.extend(name + ": " + json.dumps(v, sort_keys=True) for name, v in result["by_workflow_event"].items())
    for run in result["runs"]:
        lines.append(f"Run {run['id']} {run.get('name')} / {run.get('event')}; candidate {run.get('head_sha')}; first-job latency {run['first_job_latency_seconds']}")
        for row in run["intervals"]:
            lines.append(f"  {row['job_name']} / {row['platform']} attempt {row['first_attempt']}: {row['interval_seconds']} s, {row['rounded_interval_minutes_estimate']} rounded min; {row['identity']}; observations {len(row['observations'])}")
    lines.extend(["Billing: unknown; no Windows money multiplier or per-job net charge.",
                  "Reuse: unknown without actual producer proof; skipped is not reuse.",
                  "Storage is a current snapshot, not historical charges:"])
    for kind in ("artifacts", "caches"):
        if kind in result["storage"]:
            item = result["storage"][kind]
            lines.append(f"  {kind}: reported bytes {item['reported_bytes']}; complete listing {item['coverage']['complete']}")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="first")
    parser.add_argument("--to", dest="last")
    parser.add_argument("--until")
    parser.add_argument("--repo")
    parser.add_argument("--group-by", dest="grouping", choices=("created", "execution"))
    parser.add_argument("--lookback-days", type=int)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--snapshot", type=Path, help="Replay raw GET schema1; no network fallback")
    parser.add_argument("--save-snapshot", type=Path, help="Explicit private capture outside repository, mode0600; never overwrite")
    parser.add_argument("--no-storage", action="store_true")
    args = parser.parse_args(argv)
    try:
        saved = json.loads(args.snapshot.read_text(encoding="utf-8")) if args.snapshot else None
        if args.snapshot is not None and not isinstance(saved, dict):
            raise ValueError("Expected raw GET snapshot object")
        defaults = saved.get("parameters", {}) if saved else {}
        if not isinstance(defaults, dict):
            raise ValueError("Expected saved parameter object")
        today = dt.datetime.now(UTC).date().isoformat()
        parameters = dict(repo=args.repo or defaults.get("repo", DEFAULT_REPO), first=args.first or defaults.get("first", today),
                          last=args.last or defaults.get("last") or (args.first or defaults.get("first", today))[:10],
                          until=args.until or defaults.get("until"), grouping=args.grouping or defaults.get("grouping", "created"),
                          lookback_days=args.lookback_days if args.lookback_days is not None else defaults.get("lookback_days", 30),
                          include_storage=False if args.no_storage else defaults.get("include_storage", True))
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", parameters["repo"]):
            raise ValueError("Expected owner/repository")
        api = SnapshotAPI(None if args.snapshot is not None else gh_api, saved)
        result = collect(api, **parameters)
        if args.save_snapshot:
            api.save(args.save_snapshot, parameters)
        print(json.dumps(result, indent=2, sort_keys=True) if args.json else render(result))
        return 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        print("Report could not be produced: " + type(error).__name__, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
