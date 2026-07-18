"""IVHP intent_runner — Campaign name verify/evidence (no live browser)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.fb_ads_rpa import intent_runner as ir


def test_load_campaign_name_intent():
    intent = ir.load_intent("campaign_name")
    assert intent["id"] == "campaign_name"
    assert intent["risk"] == "safe"
    assert "Campaign name" in intent["verify"]["labels"]
    assert intent["strategies"]


def test_values_match_normalize():
    assert ir.values_match("PE_AIT_All_Video_Day_35_17.07", "PE_AIT_All_Video_Day_35_17.07")
    assert ir.values_match("  foo  ", "foo")
    assert not ir.values_match("foo", "bar")
    assert not ir.values_match("", "foo")


def test_verify_campaign_name_reads_label(monkeypatch):
    page = MagicMock()
    monkeypatch.setattr(
        ir.md,
        "read_input_near_label",
        lambda _p, _labels: "PE_AIT_All_Video_Day_35_17.07",
    )
    ok, current = ir.verify_input_near_label(
        page,
        labels=["Campaign name", "Nombre de la campaña"],
        expected="PE_AIT_All_Video_Day_35_17.07",
    )
    assert ok is True
    assert "PE_AIT" in current


def test_verify_fails_when_empty(monkeypatch):
    page = MagicMock()
    monkeypatch.setattr(ir.md, "read_input_near_label", lambda *_a, **_k: "")
    ok, current = ir.verify_input_near_label(
        page,
        labels=["Campaign name"],
        expected="PE_AIT_x",
    )
    assert ok is False
    assert current == ""


def test_write_evidence_pack(tmp_path: Path):
    page = MagicMock()
    page.url = "https://adsmanager.facebook.com/adsmanager/manage/campaigns"
    pack = ir.write_evidence_pack(
        page,
        step_id="campaign_name",
        run_dir=tmp_path / "run1",
        strategies_tried=[{"id": "label_fill", "ok": False}],
        detail="value empty",
        aria_text="- textbox: Campaign name",
        screenshot_bytes=b"fake-png",
    )
    assert pack.is_dir()
    assert (pack / "url.txt").read_text(encoding="utf-8").startswith("https://")
    assert (pack / "strategies_tried.json").is_file()
    assert (pack / "aria_snapshot.md").read_text(encoding="utf-8").startswith("- textbox")
    assert (pack / "screenshot.png").read_bytes() == b"fake-png"
    assert (pack / "detail.txt").is_file()


def test_run_campaign_name_intent_success_promotes(monkeypatch, tmp_path: Path):
    page = MagicMock()
    page.url = "https://adsmanager.facebook.com/edit/standalone"

    monkeypatch.setattr(ir.md, "wait_editor_saved", lambda *_a, **_k: None)
    monkeypatch.setattr(
        ir.md,
        "fill_input_near_label_verified",
        lambda *_a, **_k: True,
    )
    monkeypatch.setattr(
        ir.md,
        "read_input_near_label",
        lambda *_a, **_k: "MyCamp",
    )
    ranks_path = tmp_path / "ranks.json"
    ranks_path.write_text(
        '{"version":1,"steps":{}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(ir, "RANKS_PATH", ranks_path)
    monkeypatch.setattr(ir, "_EVIDENCE_ROOT", tmp_path / "evidence")

    outcome = ir.run_intent(
        page,
        "campaign_name",
        value="MyCamp",
        run_id="test_run",
    )
    assert outcome.ok is True
    assert outcome.verified is True
    assert outcome.strategy
    data = ir.load_ranks(ranks_path)
    stats = data["steps"]["campaign_name"]["stats"]
    assert stats[outcome.strategy]["wins"] >= 1


def test_run_campaign_name_intent_fail_writes_evidence(monkeypatch, tmp_path: Path):
    page = MagicMock()
    page.url = "https://adsmanager.facebook.com/adsmanager/manage/ads"
    page.accessibility = MagicMock()
    page.accessibility.snapshot = MagicMock(return_value={"role": "RootWebArea", "name": "x"})

    monkeypatch.setattr(ir.md, "wait_editor_saved", lambda *_a, **_k: None)
    monkeypatch.setattr(ir.md, "fill_input_near_label_verified", lambda *_a, **_k: False)
    monkeypatch.setattr(ir.md, "fill_input_near_label", lambda *_a, **_k: False)
    monkeypatch.setattr(ir.md, "read_input_near_label", lambda *_a, **_k: "")
    monkeypatch.setattr(ir, "RANKS_PATH", tmp_path / "ranks.json")
    monkeypatch.setattr(ir, "_EVIDENCE_ROOT", tmp_path / "evidence")
    (tmp_path / "ranks.json").write_text('{"version":1,"steps":{}}', encoding="utf-8")

    with patch.object(ir, "_capture_screenshot_bytes", return_value=b"png"):
        outcome = ir.run_intent(
            page,
            "campaign_name",
            value="MyCamp",
            run_id="fail_run",
        )
    assert outcome.ok is False
    assert outcome.verified is False
    assert outcome.evidence_dir is not None
    assert Path(outcome.evidence_dir).joinpath("aria_snapshot.md").is_file()
