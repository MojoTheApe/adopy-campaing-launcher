"""ACL defaults for the exact released, engine-free Actions accounting helper.

The canonical artifact owns interval identity, pagination and report semantics.
This adapter retains ACL's CLI, privacy boundary and orchestration names only.
"""
from pathlib import Path
import hashlib
import importlib.util

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPO = "MojoTheApe/adopy-campaing-launcher"
OVERHEAD = {"Classify and check project tooling", "ACL verification", "Process instructions",
            "Verify build inputs", "descriptor", "tracker-link", "Installer verification"}
_PROVIDER = ROOT / ".workflow/tools/tracker_ci_accounting.py"
_PROVIDER_SHA256 = "c9c8d12aa05ced4775956fecae7bfa69bbb945e6a940a5bd7fbd326b68f28aed"
if hashlib.sha256(_PROVIDER.read_bytes()).hexdigest() != _PROVIDER_SHA256:
    raise ValueError("Canonical accounting artifact changed; obtain a reviewed source refresh")
_spec = importlib.util.spec_from_file_location("acl_canonical_ci_accounting", _PROVIDER)
_canonical = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_canonical)

# Preserve existing local report imports without a second accounting algorithm.
APIError = _canonical.APIError
gh_api = _canonical.gh_api
job_seconds, job_minutes = _canonical.job_seconds, _canonical.job_minutes
platform, is_windows = _canonical.platform, _canonical.is_windows
list_runs, pages = _canonical.list_runs, _canonical.pages
storage_snapshot, totals, render = _canonical.storage_snapshot, _canonical.totals, _canonical.render
_time, _iso, _bounds = _canonical._time, _canonical._iso, _canonical._bounds
os, subprocess, time = _canonical.os, _canonical.subprocess, _canonical.time


class SnapshotAPI(_canonical.SnapshotAPI):
    def save(self, path, parameters):
        return super().save(path, parameters, project_root=ROOT)


def execution_rows(jobs):
    return _canonical.execution_rows(jobs, overhead_names=OVERHEAD)


def collect(api, repo, first, last, until=None, *, grouping="created", lookback_days=30, include_storage=True):
    return _canonical.collect(api, repo, first, last, until, grouping=grouping,
                              lookback_days=lookback_days, include_storage=include_storage,
                              overhead_names=OVERHEAD)


def main(argv=None):
    return _canonical.main(argv, default_repository=DEFAULT_REPO, project_root=ROOT,
                           overhead_names=OVERHEAD, transport=gh_api)


if __name__ == "__main__":
    raise SystemExit(main())
