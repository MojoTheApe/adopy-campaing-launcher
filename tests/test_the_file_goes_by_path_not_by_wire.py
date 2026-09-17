# -*- coding: utf-8 -*-
"""Файл передаётся путём, а не содержимым, задолго до 50 МБ (acl-1166).

Playwright отказывается передавать содержимое файла больше 50 МБ — это мы
знали и порог стоял на 45. Но 15.09 выяснилось, что есть вторая граница, куда
более близкая, и упираемся мы в неё, а не в первую.

У `set_files` свой таймаут в **30 секунд**, а содержимое идёт по CDP через
Multilogin со скоростью, намеренной в тот день, — **0.13 МБ/с**. Тридцать
секунд на такой скорости это около четырёх мегабайт. Ролик на 24 МБ отказывал
трижды подряд:

    попытка 1  chooser: True   FileChooser.set_files: Timeout 30000ms exceeded
    попытка 2  окно выбора уже не приходит вовсе
    попытка 3  то же самое

— и с включённым поиском по медиатеке, и с выключенным, то есть дело именно
здесь, а не в соседнем шаге.

Тяжёлая дорога этой беды не знает: она передаёт **путь**, Chrome читает файл с
диска сам, и скорость CDP значения не имеет.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import meta_dom as md  # noqa: E402

#: Намерено 15.09 на живом прогоне: столько содержимого уходит по CDP.
MEASURED_MB_PER_S = 0.13


def test_the_threshold_fits_inside_the_set_files_timeout():
    """Главное число этой починки, и оно выводится, а не выбирается.

    Порог обязан быть таким, чтобы передача укладывалась в чужой таймаут на
    измеренной скорости. Иначе шаг отказывает не «сломавшись», а упёршись.
    """
    budget_s = 30  # умолчание Playwright для set_files
    fits_mb = budget_s * MEASURED_MB_PER_S
    assert md._PW_FILE_LIMIT_MB <= fits_mb, (
        f"порог {md._PW_FILE_LIMIT_MB} МБ не укладывается в {budget_s}с "
        f"на {MEASURED_MB_PER_S} МБ/с — это ровно поломка 15.09"
    )


def test_the_threshold_is_well_below_the_playwright_hard_limit():
    """Прежние 45 МБ были про отказ Playwright передавать вообще. Та граница
    никуда не делась, просто мы до неё больше не доходим."""
    assert md._PW_FILE_LIMIT_MB < 45


def test_small_files_still_take_the_simple_road():
    """Медиана креатива — 0.8 МБ. Гнать мелочь через перехват диалога и
    отдельную CDP-сессию значило бы усложнить то, что работает сотню раз."""
    assert md._PW_FILE_LIMIT_MB >= 1


def test_the_heavy_road_is_chosen_for_anything_above_the_threshold():
    """Решение принимается по размеру, и именно этим сравнением."""
    assert 24.4 > md._PW_FILE_LIMIT_MB, "ролик 15.09 обязан идти тяжёлой дорогой"
    assert 0.8 <= md._PW_FILE_LIMIT_MB, "мелочь обязана идти простой"


def test_set_files_is_given_more_than_playwrights_default():
    """Страховка для того, что всё-таки идёт простой дорогой на медленной сети.

    Три минуты — это четыре мегабайта даже на 0.02 МБ/с, вдесятеро хуже
    намеренного.
    """
    assert md._PW_SET_FILES_MS >= 120_000
    slowest = md._PW_FILE_LIMIT_MB / (md._PW_SET_FILES_MS / 1000)
    assert slowest <= 0.05, "на медленной сети всё равно не успеет"


def test_the_threshold_can_be_moved_without_touching_code(monkeypatch):
    """Скорость сети меняется. Если четырёх мегабайт однажды окажется много,
    менять надо числом, а не гаданием по симптомам."""
    import importlib

    monkeypatch.setenv("FB_RPA_PW_FILE_LIMIT_MB", "2")
    reloaded = importlib.reload(md)
    try:
        assert reloaded._PW_FILE_LIMIT_MB == 2
    finally:
        monkeypatch.delenv("FB_RPA_PW_FILE_LIMIT_MB", raising=False)
        importlib.reload(md)


@pytest.mark.parametrize('raw', ['bad', 'nan', 'inf', '-1', '0', '100'])
def test_invalid_threshold_uses_safe_default(monkeypatch, raw):
    monkeypatch.setenv('FB_RPA_PW_FILE_LIMIT_MB', raw)
    assert md._pw_file_limit_mb() == 3.0


def test_small_mounted_input_receives_explicit_timeout():
    calls = []
    class Handle:
        def set_input_files(self, path, **kwargs): calls.append((path, kwargs))
    assert md._feed_mounted_file(None, Handle(), 'small.mp4', 0.8, {})
    assert calls == [('small.mp4', {'timeout': md._PW_SET_FILES_MS})]


def test_large_mounted_input_never_falls_back_to_bytes(monkeypatch):
    class Handle:
        def set_input_files(self, *a, **kw): raise AssertionError('large file sent as bytes')
    seen = []
    def refuse(page, path, diag, click):
        seen.append(path)
        return False
    monkeypatch.setattr(md, '_upload_heavy_via_chooser_event', refuse)
    assert md._feed_mounted_file(None, Handle(), 'large.mp4', 24.4, {}) is False
    assert seen == ['large.mp4']
