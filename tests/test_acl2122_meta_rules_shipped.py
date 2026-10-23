"""ACL-2122: the installed build ships the Meta rules registry and fails closed without it."""
from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.acl_ci_scope import is_bundled
from src.campaign_bridge import meta_rules as mr
from src.web import publish_preflight as pf

ROOT = Path(__file__).resolve().parents[1]
MESSAGE = "Meta rules registry is missing — reinstall or update ACL"


def test_spec_bundles_the_registry_directory():
    text = (ROOT / "packaging/windows/acl_windows.spec").read_text(encoding="utf-8")
    assert re.search(r'PROJECT_ROOT / "config" / "meta_rules"\), "config/meta_rules"\)', text)
    for name in ("meta.json", "rules.json", "sources.json"):
        assert (ROOT / "config" / "meta_rules" / name).is_file()
        assert is_bundled(f"config/meta_rules/{name}")


def test_bundle_test_and_smoke_check_the_registry():
    ps1 = (ROOT / "packaging/windows/test_bundle.ps1").read_text(encoding="utf-8")  # linux-ok: reads the packaging script as data
    for name in ("meta.json", "rules.json", "sources.json"):
        assert f'"{name}"' in ps1
    assert "_internal/config/meta_rules/" in ps1
    assert "load_rules" in (ROOT / "scripts/acl_smoke.py").read_text(encoding="utf-8")


@pytest.mark.parametrize("error", [mr.MetaRulesError("cannot read"), OSError("gone"), ValueError("bad")])
def test_missing_registry_blocks_every_draft(monkeypatch, error):
    monkeypatch.setattr(mr, "load_rules", lambda *a, **k: (_ for _ in ()).throw(error))
    drafts = [SimpleNamespace(name="A"), SimpleNamespace(name="B")]
    out = pf.meta_rules_errors([], account_label="x", country="US", drafts=drafts)
    assert out == {"A": [MESSAGE], "B": [MESSAGE]}


def test_missing_registry_blocks_slots_without_drafts(monkeypatch, tmp_path):
    monkeypatch.setattr(mr, "RULES_DIR", tmp_path / "nowhere")
    out = pf.meta_rules_errors([{"name": "Camp"}], account_label="x", country="US")
    assert out == {"Camp": [MESSAGE]}
