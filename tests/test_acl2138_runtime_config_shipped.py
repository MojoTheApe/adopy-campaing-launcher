"""ACL-2138: every config/*.json that src/ reads at runtime ships in the installed build."""
from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
from pathlib import Path

from scripts.acl_ci_scope import is_bundled
from src.analysis_agent import account_names

ROOT = Path(__file__).resolve().parents[1]
#: Files in config/ that src/ mentions but the installed build does not need (dev/ops tools only).
DEV_ONLY: set[str] = {
    "google_credentials.json",  # machine files, never shipped
    "token_accounts.json",  # machine files, never shipped
}
_READ = re.compile(r'"config"\s*/\s*"([\w.-]+\.json)"|config/([\w.-]+\.json)')


def _tracked_config_json() -> set[str]:
    """Only git-tracked files count: gitignored machine files on a dev box are not candidates."""
    out = subprocess.run(["git", "ls-files", "config/*.json"], cwd=ROOT, capture_output=True, text=True, check=True)
    return {Path(line).name for line in out.stdout.splitlines() if line.strip()}


def _src_mentions() -> set[str]:
    found: set[str] = set()
    for path in (ROOT / "src").rglob("*.py"):
        for a, b in _READ.findall(path.read_text(encoding="utf-8", errors="replace")):
            found.add(a or b)
    return found


def test_every_runtime_config_json_is_packaged_or_dev_only():
    spec = (ROOT / "packaging/windows/acl_windows.spec").read_text(encoding="utf-8")
    missing = []
    tracked = _tracked_config_json()
    assert "account_name_rules.json" in tracked
    for name in sorted(_src_mentions()):
        if name not in tracked or name in DEV_ONLY:
            continue
        if f'"config" / "{name}"' not in spec or not is_bundled(f"config/{name}"):
            missing.append(name)
    assert not missing, f"config files read by src/ but not packaged (add to the spec or DEV_ONLY): {missing}"


def test_account_name_rules_file_is_packaged_and_checked():
    spec = (ROOT / "packaging/windows/acl_windows.spec").read_text(encoding="utf-8")
    assert '"config" / "account_name_rules.json"), "config")' in spec
    assert "account_name_rules.json" in (ROOT / "packaging/windows/test_bundle.ps1").read_text(encoding="utf-8")  # linux-ok: reads the packaging script as data
    assert "rules_status" in (ROOT / "scripts/acl_smoke.py").read_text(encoding="utf-8")


def test_bi_cm_map_is_packaged_checked_and_resolves_from_installed_module(tmp_path):
    spec = (ROOT / "packaging/windows/acl_windows.spec").read_text(encoding="utf-8")
    assert '"config" / "bi_cm_map.json"), "config")' in spec
    assert is_bundled("config/bi_cm_map.json")
    bundle_check = (ROOT / "packaging/windows/test_bundle.ps1").read_text(encoding="utf-8")  # linux-ok: reads the packaging script as data; does not execute PowerShell
    assert '"_internal/config/bi_cm_map.json"' in bundle_check
    assert 'throw "config/bi_cm_map.json is missing from the bundle."' in bundle_check

    # Exercise the real loader at its frozen-layout location, without a BI connection.
    installed = tmp_path / "_internal"
    module_path = installed / "src/analysis_agent/bi_stats.py"
    module_path.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / "src/analysis_agent/bi_stats.py", module_path)
    mapping = installed / "config/bi_cm_map.json"
    mapping.parent.mkdir()
    shutil.copyfile(ROOT / "config/bi_cm_map.json", mapping)
    module_spec = importlib.util.spec_from_file_location("acl_bi_stats_installed", module_path)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    assert module.CM_MAP_PATH == mapping
    expected = module.load_cm_map(ROOT / "config/bi_cm_map.json")
    assert expected and module.load_cm_map() == expected
    mapping.unlink()
    assert module.load_cm_map() == {}, "the installed-map check must catch this otherwise silent loss"


def test_rules_status_reports_missing_and_broken(monkeypatch, tmp_path):
    assert account_names.rules_status()["ok"] is True
    monkeypatch.setattr(account_names, "_RULES_PATH", tmp_path / "none.json")
    st = account_names.rules_status()
    assert st["ok"] is False and "reinstall or update ACL" in st["error"]
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    monkeypatch.setattr(account_names, "_RULES_PATH", bad)
    assert account_names.rules_status()["ok"] is False


def test_missing_file_warns_and_does_not_crash(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(account_names, "_RULES_PATH", tmp_path / "none.json")
    monkeypatch.setitem(account_names._CACHE, "stamp", None)
    with caplog.at_level("WARNING"):
        assert account_names._snapshot() == []
    assert any("not found" in r.getMessage() and "reinstall or update ACL" in r.getMessage() for r in caplog.records)


def test_diagnostics_summary_carries_the_rules_status():
    import importlib.util

    spec = importlib.util.spec_from_file_location("acl_diagnostics_2138", ROOT / "packaging" / "windows" / "diagnostics.py")
    diagnostics = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(diagnostics)
    out = diagnostics._account_name_rules_summary()
    assert out["ok"] is True and out["rules"] >= 1
