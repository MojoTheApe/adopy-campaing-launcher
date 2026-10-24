"""ACL-1305: setup checks the name against the workbook's CM column and explains a missing workbook."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from openpyxl import Workbook

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "packaging" / "windows"))

import setup_wizard as sw  # noqa: E402
from src.analysis_agent.stats_workbook import STATS_COLUMNS  # noqa: E402


def make_workbook(path: Path, managers=(("Alex Sample", "Acc One"), ("Jordan Test", "Acc Two"))) -> str:
    wb = Workbook()
    ws = wb.active
    columns = sorted(STATS_COLUMNS)
    ws.append(columns)
    for cm, alias in managers:
        row = {c: "" for c in columns}
        row.update({"CM": cm, "Account Alias": alias})
        ws.append([row[c] for c in columns])
    wb.save(path)
    return str(path)


def test_exact_and_case_only_difference_pass(tmp_path):
    book = make_workbook(tmp_path / "s.xlsx")
    assert sw.build_manual_setup(campaign_manager_name="alex SAMPLE ", stats_workbook=book).accounts


def test_one_letter_off_names_the_close_match(tmp_path):
    book = make_workbook(tmp_path / "s.xlsx")
    with pytest.raises(sw.WizardValidationError) as exc:
        sw.build_manual_setup(campaign_manager_name="Alex Sampel", stats_workbook=book)
    assert "Alex Sample" in str(exc.value)
    assert "not in the CM column" in str(exc.value)


def test_unknown_name_says_what_to_do(tmp_path):
    book = make_workbook(tmp_path / "s.xlsx")
    with pytest.raises(sw.WizardValidationError) as exc:
        sw.build_manual_setup(campaign_manager_name="Zzzz Qqqq", stats_workbook=book)
    assert "team lead" in str(exc.value)


def test_missing_workbook_explains_where_it_comes_from(tmp_path):
    for value in ("", str(tmp_path / "nope.xlsx")):
        with pytest.raises(sw.WizardValidationError) as exc:
            sw.validate_stats_workbook(value)
        assert "team lead" in str(exc.value)


def test_default_workbook_prefilled_only_when_present(tmp_path, monkeypatch):
    monkeypatch.setenv("FB_STATS_EXCEL", str(tmp_path / "absent.xlsx"))
    assert sw.default_stats_workbook() == ""
    book = make_workbook(tmp_path / "here.xlsx")
    monkeypatch.setenv("FB_STATS_EXCEL", book)
    assert sw.default_stats_workbook() == book
