# -*- coding: utf-8 -*-
"""Тесты не пишут в живой журнал телеметрии (PR #314, ревью, находка 7).

`run_telemetry.TELEMETRY_PATH` по умолчанию — `output/rpa_telemetry.jsonl`
рабочей папки, тот же файл, что читают разбор прогонов и дешборд. Общая
фикстура в tests/conftest.py уводит его (и замок `<путь>.lock`) во
временную папку каждого теста; тест, который ставит путь сам, сохраняет
свой.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import run_telemetry as rt  # noqa: E402
from config.paths import get_runtime_workspace  # noqa: E402

LIVE = (get_runtime_workspace() / "output" / "rpa_telemetry.jsonl").resolve()


def test_record_goes_to_a_temporary_journal():
    # Финальное ревью PR #314 (e): путь — ДО записи. Сломанная фикстура
    # должна уронить тест, не оставив в живом журнале ни строки; и ни один
    # вызов `record` не идёт, пока не доказано, что журнал временный.
    path = rt.TELEMETRY_PATH.resolve()
    assert path != LIVE, "фикстура не увела журнал: запись ушла бы в живой"
    assert LIVE.parent not in path.parents, "журнал теста лежит в живой папке output/"
    # Строка узнаётся по своей метке, а не по размеру живого файла: робот,
    # работающий на этой машине, может писать туда в это же время.
    marker = f"guard-{uuid.uuid4().hex}"
    rt.record("test_only_step", False, note=marker)
    assert marker in path.read_text(encoding="utf-8")
    assert not LIVE.exists() or marker not in LIVE.read_text(encoding="utf-8", errors="replace")


def test_a_test_that_sets_its_own_path_keeps_it(tmp_path, monkeypatch):
    own = tmp_path / "mine.jsonl"
    monkeypatch.setattr(rt, "TELEMETRY_PATH", own)
    rt.record("test_only_step", None)
    assert own.exists() and (tmp_path / "mine.jsonl.lock").exists()


def test_the_journal_is_not_in_tmp_path(tmp_path):
    assert tmp_path not in rt.TELEMETRY_PATH.parents
