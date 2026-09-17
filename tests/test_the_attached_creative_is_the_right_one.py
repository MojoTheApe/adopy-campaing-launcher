# -*- coding: utf-8 -*-
"""Прикрепился ли ИМЕННО наш файл и пригоден ли он (ACL-1171 / ACL-1192).

Проверка «медиа на объявлении есть» пропускает две беды: из медиатеки взяли
чужую плитку (имена там похожи, «2012.mp4» и «2012_story.mp4» стоят рядом) и
файл, который Meta не смогла обработать — объявление с таким медиа не
опубликуется, а шаг отчитается успехом.

Состояния сняты с живого редактора: выбранный файл, готовый файл, обработка,
ошибка обработки.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import meta_dom as md  # noqa: E402


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        try:
            yield b
        finally:
            b.close()


@pytest.fixture
def page(browser, monkeypatch):
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    p = ctx.new_page()
    try:
        yield p
    finally:
        ctx.close()


_READY = ("<div><div>Uploaded media</div><div>1 selected</div>"
          "<div>2012.mp4</div><div>Change selections</div></div>")
_OTHER = ("<div><div>Uploaded media</div><div>1 selected</div>"
          "<div>9_ar_2221_story.mp4</div><div>Change selections</div></div>")
_PROCESSING = ("<div><div>Uploaded media</div><div>2012.mp4</div>"
               "<div>Processing your video</div></div>")
_FAILED = ("<div><div>Uploaded media</div><div>2012.mp4</div>"
           "<div>This media couldn't be processed. Try another file.</div></div>")


def test_our_file_on_the_ad_is_confirmed(page):
    page.set_content(_READY)
    ok, detail = md._verify_ad_media_is_the_file(page, "2012.mp4")
    assert ok is True
    assert detail["state"]["named"] == ["2012.mp4"]


def test_someone_elses_file_is_not_our_creative(page):
    """Взяли соседнюю плитку — это отказ, а не «медиа есть»."""
    page.set_content(_OTHER)
    ok, detail = md._verify_ad_media_is_the_file(page, "2012.mp4")
    assert ok is False
    assert detail["why"] == "other_file_attached"
    assert detail["state"]["others"] == ["9_ar_2221_story.mp4"]


def test_a_file_meta_could_not_process_is_a_failure(page):
    page.set_content(_FAILED)
    ok, detail = md._verify_ad_media_is_the_file(page, "2012.mp4")
    assert ok is False
    assert detail["why"] == "processing_failed"


def test_processing_is_waited_out_and_then_confirmed(page, monkeypatch):
    """Обработка — не отказ: ждём, пока Meta закончит."""
    page.set_content(_PROCESSING)
    seen = {"n": 0}
    real = md._ad_media_state

    def staged(pg, filename):
        seen["n"] += 1
        if seen["n"] == 2:
            pg.set_content(_READY)
        return real(pg, filename)

    monkeypatch.setattr(md, "_ad_media_state", staged)
    ok, detail = md._verify_ad_media_is_the_file(page, "2012.mp4")
    assert ok is True and seen["n"] >= 2


def test_processing_that_never_ends_is_a_failure(page, monkeypatch):
    page.set_content(_PROCESSING)
    monkeypatch.setattr(md, "_MEDIA_PROCESSING_MAX_S", 0)
    ok, detail = md._verify_ad_media_is_the_file(page, "2012.mp4")
    assert ok is False and detail["why"] == "still_processing"


def test_an_unnamed_tile_is_not_called_wrong(page):
    """Meta подписывает не каждую плитку — само по себе это не отказ."""
    page.set_content("<div><div>Uploaded media</div><div>Change selections</div></div>")
    ok, detail = md._verify_ad_media_is_the_file(page, "2012.mp4")
    assert ok is True and detail["why"] == "name_not_shown"
