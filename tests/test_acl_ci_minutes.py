"""Execution accounting and bounded observation coverage; no test uses the network."""
import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from urllib.parse import parse_qs, urlsplit

import pytest

from scripts import acl_ci_minutes as minutes

REPO = "owner/repo"
FIXTURE = Path(__file__).parent / "fixtures/acl_ci_minutes/carried_forward.json"


def run(identifier=1, created="2026-10-15T08:00:00Z"):
    return dict(id=identifier, name="CI", created_at=created, event="pull_request", path=".github/workflows/ci.yml",
                run_attempt=1, head_sha="a" * 40, pull_requests=[])


def job(identifier=1, attempt=1, start="2026-10-15T08:00:01Z", end="2026-10-15T08:01:02Z"):
    return dict(id=identifier, name="native cell (3.13, 1)", run_attempt=attempt, started_at=start, completed_at=end,
                runner_id=10, runner_name="hosted", runner_group_id=1, runner_group_name="Default",
                labels=["ubuntu-latest"], status="completed", conclusion="success",
                steps=[dict(name="actual test", number=1, started_at=start, completed_at=end, conclusion="success")])


class Api:
    def __init__(self, runs=None, jobs=None):
        self.runs, self.jobs, self.calls = runs or [], jobs or {}, []

    def __call__(self, path):
        self.calls.append(path)
        query = parse_qs(urlsplit(path).query)
        page = int(query.get("page", [1])[0])
        if "/actions/runs?" in path:
            left, right = [minutes._time(x) for x in query["created"][0].split("..")]
            rows = [r for r in self.runs if left <= minutes._time(r["created_at"]) <= right]
            return dict(total_count=len(rows), workflow_runs=rows[(page-1)*100:min(page*100, 1000)])
        if "/jobs?" in path:
            rows = self.jobs[int(path.split("/runs/")[1].split("/")[0])]
            return dict(total_count=len(rows), jobs=rows[(page-1)*100:page*100])
        if "/pulls/" in path:
            return {"body": "Tracker batch: " + "b" * 32}
        if "/actions/artifacts?" in path:
            return dict(total_count=1, artifacts=[dict(id=101, size_in_bytes=500, created_at="2026-10-15T00:00:00Z", expires_at="2026-11-15T00:00:00Z")])
        if "/actions/caches?" in path:
            return dict(total_count=1, actions_caches=[dict(id=102, size_in_bytes=200, last_accessed_at="2026-10-15T00:00:00Z")])
        if path.endswith("/actions/cache/retention-limit"):
            return {"max_cache_retention_days": 7}
        raise AssertionError("Unplanned GET")


def report(api, **kwargs):
    return minutes.collect(api, REPO, "2026-10-15", "2026-10-15", **kwargs)


@pytest.mark.parametrize("start,end,expected", [
    ("2026-10-15T00:00:00Z", "2026-10-15T00:00:01Z", 1),
    ("2026-10-15T00:00:00Z", "2026-10-15T00:01:00Z", 1),
    ("2026-10-15T00:00:00Z", "2026-10-15T00:01:01Z", 2),
    ("2026-10-15T00:00:00Z", "2026-10-15T00:00:00Z", 0),
    (None, "2026-10-15T00:00:00Z", None),
    ("2026-10-15T00:00:00Z", None, None),
    ("invalid", "2026-10-15T00:00:00Z", None),
])
def test_observed_rounding_and_unknown_duration_are_distinct(start, end, expected):
    assert minutes.job_minutes(dict(started_at=start, completed_at=end)) == expected


def test_native_twenty_rows_are_twelve_intervals_not_an_invoice():
    fixture = json.loads(FIXTURE.read_text())
    source = fixture["run"]
    api = Api([source], {source["id"]: fixture["jobs"]})
    result = report(api)
    total = result["totals"]
    assert total["native_job_rows"] == 20 and total["carried_forward_rows"] == 8
    assert total["counted_intervals"] == 12 and total["rounded_interval_minutes_estimate"] == 112
    assert total["by_platform"]["linux"]["rounded_minutes_estimate"] == 102
    assert total["by_platform"]["windows"]["rounded_minutes_estimate"] == 10  # linux-ok: native runner platform data
    assert sum(minutes.job_minutes(j) for j in fixture["jobs"]) == 212
    assert result["coverage"]["complete"]
    rows = result["runs"][0]["intervals"]
    assert sum(len(row["observations"]) for row in rows) == 20
    assert all(row["steps_sha256"] and row["observations"][0]["check_run_url"] for row in rows)
    assert all(row["retry_cause"].startswith("unknown") for row in rows)
    assert result["billing"]["net_charge"] is None and result["billing"]["rates"] is None
    assert "not an invoice" in minutes.render(result) and "Linux-equivalent" not in minutes.render(result)


def test_same_attempt_multiplicity_is_preserved_across_carried_attempt():
    rows = [job(1), job(2), job(3, 2), job(4, 2)]
    grouped = minutes.execution_rows(rows)
    assert len(grouped) == 2
    assert all(len(row["observations"]) == 2 for row in grouped)


def test_identical_intervals_in_different_runs_remain_real_executions():
    result = report(Api([run(1), run(2)], {1: [job()], 2: [job()]}))
    assert result["totals"]["counted_intervals"] == 2
    assert result["totals"]["carried_forward_rows"] == 0


def test_missing_native_steps_cannot_prove_carried_execution():
    second = job(2, 2); second.pop("steps")
    result = report(Api([run()], {1: [job(), second]}))
    assert result["totals"]["carried_forward_rows"] == 0
    assert result["totals"]["unresolved_identity_intervals"] == 1


@pytest.mark.parametrize("change", ["steps", "runner_id", "conclusion"])
def test_changed_identity_same_interval_is_unknown_not_a_new_known_execution(change):
    second = job(2, 2)
    second[change] = [{"name": "changed"}] if change == "steps" else 999 if change == "runner_id" else "failure"
    result = report(Api([run()], {1: [job(), second]}))
    assert result["totals"]["carried_forward_rows"] == 0
    assert result["totals"]["counted_intervals"] == 1
    assert result["totals"]["unresolved_identity_intervals"] == 1
    assert not result["coverage"]["complete"]
    assert result["estimate_coverage"]["observed_unresolved_additional_seconds_max"] == 61
    assert result["estimate_coverage"]["global_upper_seconds"] is None


def test_missing_attempt_is_not_fabricated_as_original_execution():
    unknown = job(); unknown.pop("run_attempt")
    result = report(Api([run()], {1: [unknown]}))
    assert result["runs"][0]["intervals"][0]["first_attempt"] is None
    assert result["runs"][0]["intervals"][0]["retry_execution"] is None
    assert result["totals"]["unresolved_identity_intervals"] == 1
    assert not result["estimate_coverage"]["complete"]


def test_real_later_retry_counts_once_and_never_infers_failure_cause():
    second = job(2, 2, "2026-10-15T08:10:00Z", "2026-10-15T08:11:00Z")
    result = report(Api([run()], {1: [job(), second]}))
    assert result["totals"]["counted_intervals"] == 2
    assert result["totals"]["rounded_interval_minutes_estimate"] == 3
    assert result["runs"][0]["intervals"][1]["retry_execution"]
    assert result["runs"][0]["intervals"][1]["retry_cause"].startswith("unknown")


def test_skipped_is_not_reuse_unknown_platform_and_duration_stay_unknown():
    skipped = job(2); skipped.update(conclusion="skipped", started_at=None, completed_at=None)
    unfinished = job(3); unfinished.update(completed_at=None, labels=[], runner_name="unclassified")
    result = report(Api([run()], {1: [job(), skipped, unfinished]}))
    assert result["totals"]["skipped_rows_not_proven_reuse"] == 1
    assert result["totals"]["unknown_duration_rows"] == 1
    assert result["runs"][0]["reuse"]["proven"] is None
    assert result["runs"][0]["intervals"][-1]["platform"] == "unknown"
    assert not result["coverage"]["complete"]


def test_cap_is_partitioned_and_all_runs_are_returned():
    start = minutes._time("2026-10-15T00:00:00Z")
    runs = [run(i, minutes._iso(start + dt.timedelta(seconds=i))) for i in range(1201)]
    api = Api(runs)
    found, coverage = minutes.list_runs(api, REPO, "2026-10-15", "2026-10-15")
    assert len(found) == 1201 and coverage["complete"]
    assert len(coverage["windows"]) > 1
    assert all(w["reported_count"] < 1000 for w in coverage["windows"])


def test_irreducible_second_cap_is_explicitly_incomplete():
    api = Api([run(i, "2026-10-15T00:00:00Z") for i in range(1001)])
    found, coverage = minutes.list_runs(api, REPO, "2026-10-15", "2026-10-15")
    assert len(found) == 1000 and not coverage["complete"]
    assert "irreducible_search_cap" in {g["reason"] for g in coverage["gaps"]}


def test_small_creation_window_is_exact_and_fractional_cutoff_is_refused():
    api = Api([run(1, "2026-10-15T08:00:00Z"), run(2, "2026-10-15T08:00:01Z")])
    found, coverage = minutes.list_runs(api, REPO, "2026-10-15T08:00:00Z", "2026-10-15", "2026-10-15T08:00:01Z")
    assert [r["id"] for r in found] == [1] and coverage["complete"]
    with pytest.raises(ValueError):
        minutes.list_runs(api, REPO, "2026-10-15", "2026-10-15", "2026-10-15T08:00:00.5Z")


def test_duplicate_ids_do_not_double_count_and_missing_count_is_unknown():
    class Duplicate(Api):
        def __call__(self, path):
            response = super().__call__(path)
            if "workflow_runs" in response:
                response["workflow_runs"] *= 2
            return response
    found, coverage = minutes.list_runs(Duplicate([run()]), REPO, "2026-10-15", "2026-10-15")
    assert len(found) == 1 and coverage["duplicate_run_rows"] == 1
    found, coverage = minutes.list_runs(lambda _: {"workflow_runs": [run()]}, REPO, "2026-10-15", "2026-10-15")
    assert len(found) == 1 and not coverage["complete"]


def test_execution_period_finds_old_rerun_clips_midnight_but_never_claims_complete():
    old = run(1, "2026-10-14T08:00:00Z")
    interval = job(1, 2, "2026-10-14T23:59:30Z", "2026-10-15T00:01:00Z")
    api = Api([old], {1: [interval]})
    assert report(api)["totals"]["counted_intervals"] == 0
    result = report(api, grouping="execution", lookback_days=2)
    assert result["totals"]["counted_seconds"] == 60
    assert result["totals"]["rounded_interval_minutes_estimate"] == 1
    assert result["days"]["2026-10-15"]["linux"]["seconds"] == 60
    assert not result["coverage"]["complete"]
    assert "older_run_reruns_unknown" in {g["reason"] for g in result["coverage"]["gaps"]}


def test_candidate_pr_batch_are_native_links_only_and_storage_is_separate():
    source = run(); source["pull_requests"] = [{"number": 15}]
    result = report(Api([source], {1: [job()]}))
    assert result["runs"][0]["head_sha"] == "a" * 40
    assert result["runs"][0]["batch_ids"] == ["b" * 32]
    assert result["runs"][0]["intervals"][0]["matrix"] is None
    assert result["storage"]["artifacts"]["reported_bytes"] == 500
    assert result["storage"]["caches"]["reported_bytes"] == 200
    assert result["storage"]["cache_retention_limit_days"] == 7
    assert result["billing"]["status"] == "unknown"


def test_missing_snapshot_coverage_is_unknown_not_zero_or_live_fetch():
    api = minutes.SnapshotAPI(snapshot={"schema_version": 1, "responses": {}})
    result = report(api)
    assert not result["coverage"]["complete"]
    assert result["storage"]["artifacts"]["reported_bytes"] is None
    assert result["storage"]["caches"]["reported_bytes"] is None


@pytest.mark.parametrize("invalid", ["{}", "[]", "null"])
def test_invalid_replay_never_falls_back_to_live_get(tmp_path, monkeypatch, capsys, invalid):
    source = tmp_path / "invalid.json"; source.write_text(invalid)
    monkeypatch.setattr(minutes, "gh_api", lambda _: pytest.fail("Explicit replay must never fetch live data"))
    assert minutes.main(["--snapshot", str(source)]) == 1
    assert "could not be produced" in capsys.readouterr().err


def test_unavailable_storage_is_unknown_even_when_execution_history_is_complete():
    class Unavailable(Api):
        def __call__(self, path):
            if "/actions/artifacts" in path or "/actions/cache" in path:
                raise minutes.APIError(404)
            return super().__call__(path)
    result = report(Unavailable([run()], {1: [job()]}))
    assert result["coverage"]["complete"]
    assert result["storage"]["artifacts"]["reported_bytes"] is None
    assert result["storage"]["caches"]["reported_bytes"] is None
    assert result["storage"]["cache_retention_limit_days"] is None


def test_snapshot_replay_is_deterministic_and_capture_is_private_no_overwrite(tmp_path, capsys, monkeypatch):
    parameters = dict(repo=REPO, first="2026-10-15", last="2026-10-15", include_storage=True)
    api = minutes.SnapshotAPI(Api([run()], {1: [job()]}))
    first = minutes.collect(api, **parameters)
    path = tmp_path / "private.json"
    original_open, modes = minutes.os.open, []
    def private_open(path, flags, mode):
        modes.append(mode)
        return original_open(path, flags, mode)
    monkeypatch.setattr(minutes.os, "open", private_open)
    api.save(path, parameters)
    with pytest.raises(FileExistsError):
        api.save(path, parameters)
    assert modes == [0o600, 0o600]
    replay = minutes.SnapshotAPI(snapshot=json.loads(path.read_text()))
    assert minutes.collect(replay, **parameters) == first
    monkeypatch.setattr(minutes, "gh_api", lambda _: pytest.fail("Replay must not use network"))
    assert minutes.main(["--snapshot", str(path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == first
    assert minutes.main(["--snapshot", str(path)]) == 0
    assert capsys.readouterr().out.strip() == minutes.render(first)


def test_gh_transport_retries_only_get_and_never_leaks_error(monkeypatch):
    calls = []
    def fake(command, **kwargs):
        calls.append(command)
        if len(calls) < 3:
            raise minutes.subprocess.CalledProcessError(1, command, stderr="private transport detail")
        return '{"ok": true}'
    monkeypatch.setattr(minutes.subprocess, "check_output", fake)
    monkeypatch.setattr(minutes.time, "sleep", lambda _: None)
    assert minutes.gh_api("repos/owner/repo") == {"ok": True}
    assert len(calls) == 3 and all(c[2:4] == ["--method", "GET"] for c in calls)


@pytest.mark.parametrize("key", ["jobs", "artifacts", "actions_caches"])
def test_conflicting_duplicate_native_id_cannot_return_on_a_later_page(key):
    rows = [dict(id=i, size_in_bytes=10) for i in range(1, 101)]
    conflict = dict(rows[0], size_in_bytes=999)
    pages = {1: rows, 2: [conflict, *rows[1:]], 3: [rows[0]]}
    def api(path):
        page = int(parse_qs(urlsplit(path).query)["page"][0])
        return {key: copy.deepcopy(pages[page]), "total_count": 100}
    observed, coverage = minutes.pages(api, "synthetic?per_page=100", key)
    assert {row["id"] for row in observed} == set(range(2, 101))
    assert not coverage["complete"]
    assert "native_row_changed_during_collection" in coverage["gaps"]


def test_conflicting_job_pages_never_present_a_complete_consumption_upper_bound():
    rows = [job(i) for i in range(1, 101)]
    conflict = dict(rows[0], completed_at="2026-10-15T09:01:02Z")
    class Conflict(Api):
        def __call__(self, path):
            if "/jobs?" in path:
                page = int(parse_qs(urlsplit(path).query)["page"][0])
                return {"jobs": copy.deepcopy(rows if page == 1 else [conflict]), "total_count": 100}
            return super().__call__(path)
    result = report(Conflict([run()]))
    assert not result["coverage"]["complete"]
    assert result["totals"]["counted_intervals"] == 99
    assert result["estimate_coverage"]["global_upper_seconds"] is None
    assert all(observation["id"] != 1 for row in result["runs"][0]["intervals"]
               for observation in row["observations"])


def test_conflicting_storage_observations_keep_byte_totals_unknown():
    rows = [dict(id=i, size_in_bytes=10) for i in range(1, 101)]
    class Conflict(Api):
        def __call__(self, path):
            for suffix, key in (("/actions/artifacts?", "artifacts"), ("/actions/caches?", "actions_caches")):
                if suffix in path:
                    page = int(parse_qs(urlsplit(path).query)["page"][0])
                    return {key: copy.deepcopy(rows if page == 1 else [dict(rows[0], size_in_bytes=999)]),
                            "total_count": 100}
            return super().__call__(path)
    result = minutes.storage_snapshot(Conflict(), REPO)
    for key in ("artifacts", "caches"):
        assert not result[key]["coverage"]["complete"]
        assert result[key]["reported_bytes"] is None
        assert all(row["id"] != 1 for row in result[key]["entries"])


@pytest.mark.parametrize("changed", [{"python": "3.11"}, None, {}])
def test_changed_native_matrix_is_unresolved_not_carried_execution(changed):
    first, second = job(), job(2, 2)
    first["matrix"] = {"python": "3.13"}
    if changed is not None:
        second["matrix"] = changed
    result = report(Api([run()], {1: [first, second]}))
    assert result["totals"]["carried_forward_rows"] == 0
    assert result["totals"]["counted_intervals"] == 1
    assert result["totals"]["unresolved_identity_intervals"] == 1
    assert not result["coverage"]["complete"]
    assert result["estimate_coverage"]["global_upper_seconds"] is None
    assert [row["matrix"] for row in result["runs"][0]["intervals"]] == [first["matrix"], changed]


def test_identical_supplied_matrix_with_different_key_order_remains_carried():
    first, second = job(), job(2, 2)
    first["matrix"] = {"python": "3.13", "shard": 1}
    second["matrix"] = {"shard": 1, "python": "3.13"}
    result = report(Api([run()], {1: [first, second]}))
    assert result["coverage"]["complete"]
    assert result["totals"]["carried_forward_rows"] == 1
    assert result["totals"]["counted_intervals"] == 1


def test_accounting_artifact_matches_its_actual_source_blob_and_portable_record():
    record = json.loads((minutes.ROOT / "tracker/ci-accounting-artifact.json").read_text())
    raw = (minutes.ROOT / record["artifact_path"]).read_bytes()
    assert record["revision"] == "df0154b96410296b50bc9d7a00af7847d9a97700"
    assert hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest() == record["source_git_blob"]
    assert hashlib.sha256(raw).hexdigest() == record["sha256"] == minutes._PROVIDER_SHA256
    assert len(raw) == record["bytes"]
    assert record["workflow_engine_adopted"] is False
    assert json.loads((minutes.ROOT / "tracker/engine.json").read_text())["revision"] == record["project_workflow_engine_unchanged"]


def test_adapter_owned_private_root_cannot_be_overridden_before_transport(tmp_path, monkeypatch, capsys):
    output = tmp_path / "raw.json"
    monkeypatch.setattr(minutes, "gh_api", lambda _: pytest.fail("Invalid capture boundary must not perform GETs"))
    assert minutes.main(["--project-root", str(tmp_path), "--save-snapshot", str(output)]) == 1
    assert not output.exists()
    assert "could not be produced" in capsys.readouterr().err


def test_import_api_preserves_acl_capture_root(tmp_path, monkeypatch):
    monkeypatch.setattr(minutes, "ROOT", tmp_path)
    output = tmp_path / "inside-repository.json"
    with pytest.raises(ValueError, match="outside this repository"):
        minutes.SnapshotAPI().save(output, {})
    assert not output.exists()


def _isolated_adapter(tmp_path):
    root = tmp_path / "consumer"
    script = root / "scripts/acl_ci_minutes.py"
    helper = root / ".workflow/tools/tracker_ci_accounting.py"
    script.parent.mkdir(parents=True)
    helper.parent.mkdir(parents=True)
    script.write_bytes((minutes.ROOT / "scripts/acl_ci_minutes.py").read_bytes())
    helper.write_bytes(minutes._PROVIDER.read_bytes())
    return script, helper


def test_actual_cli_uses_acl_defaults_and_replays_without_private_engine(tmp_path):
    script, _ = _isolated_adapter(tmp_path)
    api = minutes.SnapshotAPI(Api([run()], {1: [job()]}))
    parameters = dict(repo=minutes.DEFAULT_REPO, first="2026-10-15", last="2026-10-15", include_storage=False)
    expected = minutes.collect(api, **parameters)
    api.snapshot["parameters"] = parameters
    snapshot = tmp_path / "synthetic.json"
    snapshot.write_text(json.dumps(api.snapshot))
    probe = ("import runpy,sys; namespace=runpy.run_path(sys.argv[1]); "
             "status=namespace['main'](sys.argv[2:]); "
             "assert not any(n=='tracker' or n.startswith('tracker.') for n in sys.modules); "
             "raise SystemExit(status)")
    for flag in (["--json"], []):
        result = subprocess.run([sys.executable, "-I", "-c", probe, str(script), "--snapshot", str(snapshot), *flag],
                                cwd=tmp_path, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        if flag:
            assert json.loads(result.stdout) == expected
        else:
            assert result.stdout.strip() == minutes.render(expected)


def test_changed_artifact_is_refused_before_its_code_can_execute(tmp_path):
    script, helper = _isolated_adapter(tmp_path)
    marker = tmp_path / "must-not-exist"
    with helper.open("a") as output:
        output.write("\nfrom pathlib import Path\nPath(" + repr(str(marker)) + ").write_text('unsafe')\n")
    result = subprocess.run([sys.executable, "-I", str(script), "--help"], cwd=tmp_path,
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert "Canonical accounting artifact changed" in result.stderr
    assert not marker.exists()
