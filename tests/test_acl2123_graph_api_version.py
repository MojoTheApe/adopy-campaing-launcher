"""ACL-2123: Graph API version comes from meta.json; the v22.0 pin is dropped."""
from __future__ import annotations

import importlib
import json
import re
from pathlib import Path

import pytest

from src.campaign_bridge.meta_rules import RULES_DIR, graph_api_version
from src.meta_campaign_agent.meta_client import meta_client_from_env

paths = importlib.import_module("config.paths")
ROOT = Path(__file__).resolve().parents[1]


def _registry_version() -> str:
    return json.loads((RULES_DIR / "meta.json").read_text(encoding="utf-8"))["api_version_current"]


def test_fresh_install_has_no_v22_pin_and_uses_v26():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert not re.search(r"^\s*META_GRAPH_API_VERSION\s*=\s*v22", text, re.M)
    assert not re.search(r"^\s*META_GRAPH_API_VERSION\s*=\s*\S", text, re.M)  # no active pin at all
    assert graph_api_version(None) == "v26.0"


def test_meta_json_is_the_single_source(tmp_path):
    (tmp_path / "meta.json").write_text(json.dumps({
        "api_version_current": "v31.0",
        "api_versions": [{"version": "v31.0", "released": "2027-01-01", "end_of_support": None}],
        "checked_date": "2027-01-02", "sources": ["x"]}), encoding="utf-8")
    assert graph_api_version(None, tmp_path) == "v31.0"
    assert graph_api_version("v26.0", tmp_path) == "v31.0"  # older env does not win
    assert graph_api_version("v32.0", tmp_path) == "v32.0"  # newer env does
    assert graph_api_version("junk", tmp_path) == "v31.0"


def test_client_follows_registry_not_hardcoded(monkeypatch):
    monkeypatch.setenv("META_ACCESS_TOKEN", "t")
    monkeypatch.setenv("META_GRAPH_API_VERSION", "v22.0")
    assert meta_client_from_env().base_url.endswith("/" + _registry_version())


@pytest.fixture()
def machine(tmp_path, monkeypatch):
    monkeypatch.setenv("ACL_CONFIG_DIR", str(tmp_path / "ACL"))
    monkeypatch.delenv("META_GRAPH_API_VERSION", raising=False)
    (tmp_path / "ACL").mkdir()
    return tmp_path / "ACL" / ".env"


def test_old_v22_pin_is_normalised_with_a_log_line(machine):
    machine.write_text("META_ACCESS_TOKEN=abc\nMETA_GRAPH_API_VERSION=v22.0\nFB_RPA_ENGINE=recorded_flow\n",
                       encoding="utf-8")
    note = paths.migrate_legacy_env()
    assert "v22.0" in note and "cleared" in note
    body = machine.read_text(encoding="utf-8")
    assert "v22.0" not in body and "META_ACCESS_TOKEN=abc" in body
    assert paths.migrate_legacy_env() == ""  # one-time: second run is silent


def test_other_pins_are_left_alone(machine):
    machine.write_text("META_GRAPH_API_VERSION=v27.0\nFB_RPA_ENGINE=recorded_flow\n", encoding="utf-8")
    assert paths.migrate_legacy_env() == ""
    assert "v27.0" in machine.read_text(encoding="utf-8")
