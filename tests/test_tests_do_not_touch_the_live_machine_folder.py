# -*- coding: utf-8 -*-
r"""Тесты не пишут в живую машинную папку ACL (ревью v3 PR #314, находка 7).

`%LOCALAPPDATA%\ACL` — машинная конфигурация: `draft_status.json`, реестры,
карта аккаунтов. Общая фикстура в tests/conftest.py ставит на время теста
`ACL_CONFIG_DIR` во временную папку.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import paths  # noqa: E402

LIVE = Path(os.environ.get("LOCALAPPDATA", "") or Path.home() / ".config") / (
    "ACL" if os.environ.get("LOCALAPPDATA") else "acl")


def test_the_machine_folder_is_temporary():
    here = Path(paths.get_local_config_dir()).resolve()
    assert here != LIVE.resolve()
    assert Path(paths.get_draft_status_path()).resolve().parent == here


def test_draft_status_goes_to_the_temporary_folder():
    from src.web import publish_jobs as pj

    live = LIVE / "draft_status.json"
    before = live.stat().st_mtime if live.exists() else None
    pj._write_draft_status("TestOnlyAccount", [], [], expected_total=1)
    assert Path(paths.get_draft_status_path()).exists()
    assert (live.stat().st_mtime if live.exists() else None) == before
