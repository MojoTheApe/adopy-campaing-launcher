# -*- coding: utf-8 -*-
"""Отказ загрузки не должен оплачиваться трижды и ронять соседние шаги.

Замер 15.09 по телеметрии пяти живых прогонов: 48 минут всего, **23.4 из них
(49%) оплачены шагами, которые в итоге отказали**. Почти всё — загрузка
креатива: у одной кампании три объявления с одним и тем же файлом на 247 МБ, и
после провала на первом остальные два платили по 178 секунд без единого шанса.

Второе: шаг, который сдался, оставлял открытый диалог. Один отказ загрузки
превращался в четыре (`ad_destination`, `ad2_name`, ...), у каждого в
диагностике `dialogs: 1`, и каждый добавлял 15-25 секунд. Настоящая причина
при этом тонула среди одинаковых «не нашёл элемент».
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import meta_dom as md  # noqa: E402
from src.fb_ads_rpa import run_telemetry as rt  # noqa: E402

FILE = r"C:\creatives\heavy.mp4"


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    md._failed_uploads.clear()
    rt.set_current_run("run-1", "CAMP")
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    yield
    md._failed_uploads.clear()


def test_the_same_file_is_not_uploaded_again_after_it_failed(monkeypatch):
    tries: list[str] = []
    monkeypatch.setattr(md, "upload_ad_creative",
                        lambda *a, **k: tries.append("try") or False)
    monkeypatch.setattr(md, "_close_creative_setup_modal", lambda page: True)
    monkeypatch.setattr(md, "_verify_ad_media_present", lambda page: False)
    monkeypatch.setattr(md, "_upload_stalled", lambda page: False)
    monkeypatch.setattr(md, "reuse_before_upload", lambda page, path: None)

    assert md.upload_ad_creative_with_retry(object(), "video", FILE) is False
    spent = len(tries)
    assert spent >= 1

    tries.clear()
    assert md.upload_ad_creative_with_retry(object(), "video", FILE) is False
    assert tries == [], "второе объявление не должно платить за ту же загрузку"


def test_a_copy_that_appeared_meanwhile_still_wins(monkeypatch):
    """Быстрый отказ — про загрузку, не про медиатеку.

    Пока шло первое объявление, копия могла появиться в медиатеке. Тогда файл
    вообще никуда не поедет, и отказывать нечему.
    """
    monkeypatch.setattr(md, "upload_ad_creative", lambda *a, **k: False)
    monkeypatch.setattr(md, "_close_creative_setup_modal", lambda page: True)
    monkeypatch.setattr(md, "_verify_ad_media_present", lambda page: False)
    monkeypatch.setattr(md, "_upload_stalled", lambda page: False)
    monkeypatch.setattr(md, "reuse_before_upload", lambda page, path: None)
    assert md.upload_ad_creative_with_retry(object(), "video", FILE) is False

    monkeypatch.setattr(md, "reuse_before_upload", lambda page, path: True)
    assert md.upload_ad_creative_with_retry(object(), "video", FILE) is True


def test_a_different_file_is_not_punished(monkeypatch):
    uploads: list[str] = []
    monkeypatch.setattr(md, "upload_ad_creative",
                        lambda page, t, path, **k: uploads.append(path) or False)
    monkeypatch.setattr(md, "_close_creative_setup_modal", lambda page: True)
    monkeypatch.setattr(md, "_verify_ad_media_present", lambda page: False)
    monkeypatch.setattr(md, "_upload_stalled", lambda page: False)
    monkeypatch.setattr(md, "reuse_before_upload", lambda page, path: None)

    md.upload_ad_creative_with_retry(object(), "video", FILE)
    uploads.clear()
    md.upload_ad_creative_with_retry(object(), "video", r"C:\creatives\other.mp4")
    assert uploads, "другой файл обязан получить свою попытку"


def test_the_verdict_does_not_survive_into_the_next_run(monkeypatch):
    """Между прогонами меняются кабинет, сеть и профиль — приговор не переносим."""
    monkeypatch.setattr(md, "upload_ad_creative", lambda *a, **k: False)
    monkeypatch.setattr(md, "_close_creative_setup_modal", lambda page: True)
    monkeypatch.setattr(md, "_verify_ad_media_present", lambda page: False)
    monkeypatch.setattr(md, "_upload_stalled", lambda page: False)
    monkeypatch.setattr(md, "reuse_before_upload", lambda page, path: None)

    md.upload_ad_creative_with_retry(object(), "video", FILE)
    assert md.upload_already_failed(FILE) is True

    rt.set_current_run("run-2", "CAMP")
    assert md.upload_already_failed(FILE) is False


def test_a_failed_upload_closes_the_dialog_behind_it(monkeypatch):
    """Иначе один отказ стоит ещё четырёх, и причина тонет среди них."""
    closed: list[str] = []
    monkeypatch.setattr(md, "upload_ad_creative", lambda *a, **k: False)
    monkeypatch.setattr(md, "_close_creative_setup_modal",
                        lambda page: closed.append("close") or True)
    monkeypatch.setattr(md, "_verify_ad_media_present", lambda page: False)
    monkeypatch.setattr(md, "_upload_stalled", lambda page: False)
    monkeypatch.setattr(md, "reuse_before_upload", lambda page, path: None)

    md.upload_ad_creative_with_retry(object(), "video", FILE)
    assert closed, "после окончательного отказа диалог обязан быть закрыт"
