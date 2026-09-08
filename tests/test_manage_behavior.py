"""Executable integration checks for the merged country view."""
from copy import deepcopy
from pathlib import Path

import pytest

from tests.test_review_ui_regressions import run_node
from tests.test_web_build_upload import _minimal_report, _render_html


@pytest.mark.parametrize("scenario", ["accounts", "manual_scope", "navigation", "profiles", "persistence"])
def test_manage_handlers_use_the_rendered_panels(tmp_path, scenario):
    report = _minimal_report()
    second = deepcopy(report["countries"][0])
    second["country"] = "Brazil"
    second["open_recs"] = {"BR-acc-1": next(iter(second["open_recs"].values()))}
    report["countries"].append(second)
    rendered = tmp_path / "manage.html"
    rendered.write_text(_render_html(report), encoding="utf-8")
    run_node([Path(__file__).with_name("manage_ui_harness.cjs"), rendered, scenario])
