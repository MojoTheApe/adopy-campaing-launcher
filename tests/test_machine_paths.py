"""Пути этой машины меняются в дешборде, а не в .env (ACL-1239)."""
from __future__ import annotations

import asyncio
import json

import pytest

from src.web import machine_paths as mp


@pytest.fixture
def machine(tmp_path, monkeypatch):
    config = tmp_path / "ACL"
    config.mkdir()
    (config / ".env").write_text("KEEP_ME=1\n", encoding="utf-8")
    monkeypatch.setenv("ACL_CONFIG_DIR", str(config))
    monkeypatch.delenv("FB_STATS_EXCEL", raising=False)
    monkeypatch.delenv("FB_CREATIVES_ROOT", raising=False)
    return tmp_path, config / ".env"


def test_both_paths_are_saved_to_the_machine_env_and_applied_at_once(machine):
    root, env = machine
    book = root / "stats.xlsx"
    book.write_bytes(b"x")
    result = mp.save(f'"{book}"', str(root / "Docs" / "Creatives"))
    assert result["stats_workbook"] == str(book.resolve()) and result["stats_workbook_found"]
    assert result["creatives_created"] and (root / "Docs" / "Creatives").is_dir()
    text = env.read_text(encoding="utf-8")
    assert "KEEP_ME=1" in text and "FB_STATS_EXCEL=" in text and "FB_CREATIVES_ROOT=" in text
    assert mp.current()["creatives_folder"] == str((root / "Docs" / "Creatives").resolve())


def test_a_bad_workbook_changes_nothing(machine):
    root, env = machine
    with pytest.raises(mp.MachinePathError, match="no file"):
        mp.save(str(root / "missing.xlsx"), str(root / "Creatives"))
    with pytest.raises(mp.MachinePathError, match=r"\.xlsx"):
        mp.save(str(root / "stats.csv"))
    assert env.read_text(encoding="utf-8") == "KEEP_ME=1\n"
    assert not (root / "Creatives").exists()


def test_a_file_is_not_accepted_as_the_creatives_folder(machine):
    root, _ = machine
    (root / "not-a-folder").write_text("x", encoding="utf-8")
    with pytest.raises(mp.MachinePathError, match="not a folder"):
        mp.save(creatives_folder=str(root / "not-a-folder"))


def test_the_route_saves_and_explains_errors(machine, monkeypatch):
    from src.web import app as web

    root, _ = machine
    cleared = []
    monkeypatch.setattr(web, "cache_clear", lambda **kw: cleared.append(kw))

    class Req:
        def __init__(self, body): self.body = body
        async def json(self): return self.body

    bad = asyncio.run(web.api_save_machine_paths(Req({"stats_workbook": str(root / "none.xlsx")})))
    assert bad.status_code == 400 and "no file" in json.loads(bad.body)["error"]
    assert asyncio.run(web.api_save_machine_paths(Req({"stats_workbook": 5}))).status_code == 400
    book = root / "stats.xlsx"
    book.write_bytes(b"x")
    ok = json.loads(asyncio.run(web.api_save_machine_paths(Req({"stats_workbook": str(book)}))).body)
    assert ok["ok"] and ok["stats_changed"] and cleared == [{}]
    ok = json.loads(asyncio.run(web.api_save_machine_paths(Req({"creatives_folder": str(root / "C")}))).body)
    assert ok["creatives_created"] and not ok["stats_changed"] and len(cleared) == 1
    listing = json.loads(web.api_machine_paths().body)
    assert listing["creatives_folder_found"] and listing["stats_workbook_found"]


def test_the_accounts_view_offers_both_fields():
    from pathlib import Path
    html = (Path(__file__).resolve().parents[1] / "src/web/templates/report.html").read_text(encoding="utf-8")
    panel = html[html.index('id="view-accounts"'):html.index('id="view-targets"')]
    assert 'id="mp-stats"' in panel and 'id="mp-creatives"' in panel and 'id="mp-save"' in panel
