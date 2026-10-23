"""ACL-2138: every config/*.json that src/ reads at runtime ships in the installed build."""
from __future__ import annotations

import json
import re
from pathlib import Path

from scripts.acl_ci_scope import is_bundled
from src.analysis_agent import account_names

ROOT = Path(__file__).resolve().parents[1]
#: Files in config/ that src/ mentions but the installed build does not need (dev/ops tools only).
DEV_ONLY: set[str] = set()
_READ = re.compile(r'"config"\s*/\s*"([\w.-]+\.json)"|config/([\w.-]+\.json)')


def _src_mentions() -> set[str]:
    found: set[str] = set()
    for path in (ROOT / "src").rglob("*.py"):
        for a, b in _READ.findall(path.read_text(encoding="utf-8", errors="replace")):
            found.add(a or b)
    return found


def test_every_runtime_config_json_is_packaged_or_dev_only():
    spec = (ROOT / "packaging/windows/acl_windows.spec").read_text(encoding="utf-8")
    missing = []
    for name in sorted(_src_mentions()):
        if not (ROOT / "config" / name).is_file() or name in DEV_ONLY:
            continue
        if f'"config" / "{name}"' not in spec or not is_bundled(f"config/{name}"):
            missing.append(name)
    assert not missing, f"config files read by src/ but not packaged (add to the spec or DEV_ONLY): {missing}"


def test_account_name_rules_file_is_packaged_and_checked():
    spec = (ROOT / "packaging/windows/acl_windows.spec").read_text(encoding="utf-8")
    assert '"config" / "account_name_rules.json"), "config")' in spec
    assert "account_name_rules.json" in (ROOT / "packaging/windows/test_bundle.ps1").read_text(encoding="utf-8")  # linux-ok: reads the packaging script as data
    assert "rules_status" in (ROOT / "scripts/acl_smoke.py").read_text(encoding="utf-8")


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
