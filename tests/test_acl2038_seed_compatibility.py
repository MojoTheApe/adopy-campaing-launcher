"""Runtime seed extraction preserves CLI forms and dashboard connect scope."""
from __future__ import annotations

import copy

from src.analysis_agent import team_library as tl
from src.analysis_agent import team_library_connect as connect
from src.analysis_agent import team_library_seed as seed
from src.fb_api import form_templates as ft
from tests.test_acl2038_connect_team_library import TOKEN, fake_lib, machine  # noqa: F401
from tests.test_acl2054_forms_library import TEMPLATE


def form(identifier, name):
    return ft.validate({**TEMPLATE, "id": identifier, "name": name})[0]


def test_cli_form_seed_adds_missing_preserves_team_and_deleted_and_is_idempotent(fake_lib):
    from scripts import seed_team_library as cli

    original = form("111111111111", "Team form")
    deleted = form("444444444444", "Deleted form")
    remote = tl.empty("form_templates")
    remote["templates"][original["id"]] = original
    remote["deleted"][deleted["id"]] = {
        "data": deleted, "deleted_by": "Synthetic manager", "deleted_at": "2026-10-23"}
    fake_lib.put("form_templates.json", tl._dump(remote), None, "Synthetic initial team data")
    local = tl.empty("form_templates")
    local["templates"] = {
        original["id"]: form(original["id"], "Local change must not overwrite team"),
        "222222222222": form("222222222222", "New local form"),
        "333333333333": form("333333333333", "Team form"),
        deleted["id"]: deleted,
    }
    local_before = copy.deepcopy(local)

    added, _ = cli.seed("form_templates", add_missing=True, dry_run=False, local=local)

    assert added == 1
    saved = tl._parse("form_templates", fake_lib.get("form_templates.json"))
    assert set(saved["templates"]) == {original["id"], "222222222222"}
    assert saved["templates"][original["id"]] == original
    assert saved["deleted"] == remote["deleted"]
    assert local == local_before
    commits_before = len(fake_lib.commits)
    assert cli.seed("form_templates", add_missing=True, dry_run=False, local=local)[0] == 0
    assert len(fake_lib.commits) == commits_before


def test_dashboard_connect_keeps_its_targets_and_ad_texts_scope_when_cli_gains_forms(fake_lib, monkeypatch):
    reads = []

    def forms_snapshot():
        reads.append("forms")
        data = tl.empty("form_templates")
        data["templates"]["222222222222"] = form("222222222222", "Local-only form")
        return data

    monkeypatch.setitem(seed.KINDS, "form_templates", (
        forms_snapshot, "templates", str.lower,
        lambda row: row["name"].casefold() + ft.content_hash(ft.validate(row)[0])))

    result = connect.connect(TOKEN)

    assert result["seeded"] is True
    assert result["added"] == {"targets": 0, "ad_texts": 0}
    assert fake_lib.calls["check"] == [("MojoTheApe/acl-lib", "main")]
    assert reads == []
    assert fake_lib.get("form_templates.json").text is None
