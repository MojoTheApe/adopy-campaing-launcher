# -*- coding: utf-8 -*-
"""Прикрепился ли ИМЕННО наш файл и пригоден ли он (ACL-1171 / ACL-1192).

Проверка «медиа на объявлении есть» пропускает две беды: из медиатеки взяли
чужую плитку (имена там похожи, «2012.mp4» и «2012_story.mp4» стоят рядом) и
файл, который Meta не смогла обработать — объявление с таким медиа не
опубликуется, а шаг отчитается успехом.

Готовый Media-блок записан с живой страницы. Имена и состояния ошибки/обработки
ниже — синтетические мутации для регрессионных проверок.
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
    p.route("**/*", lambda route: route.abort())
    try:
        yield p
    finally:
        ctx.close()


IDENTITY = "813948786_38351125907864671_2841079256059848182_n"
_RECORDED = (Path(__file__).parent / "fixtures/meta/ad_media_attached.html").read_text()
# Captured ready structure with explicit synthetic filename/error states.
_READY = _RECORDED.replace('>', '><div>2012.mp4</div>', 1)
_OTHER = _READY.replace('2012.mp4', '9_ar_2221_story.mp4')
_PROCESSING = _READY.replace('>', '><div>Processing your video</div>', 1)
_FAILED = _READY.replace('>', "><div>This media couldn't be processed.</div>", 1)


def test_our_file_on_the_ad_is_confirmed(page):
    page.set_content(_READY)
    ok, detail = md._verify_ad_media_is_the_file(page, "2012.mp4", [IDENTITY])
    assert ok is True
    assert detail["state"]["named"] == ["2012.mp4"]


def test_someone_elses_file_is_not_our_creative(page):
    """Взяли соседнюю плитку — это отказ, а не «медиа есть»."""
    page.set_content(_OTHER)
    ok, detail = md._verify_ad_media_is_the_file(page, "2012.mp4", [IDENTITY])
    assert ok is False
    assert detail["why"] == "other_file_attached"
    assert detail["state"]["others"] == ["9_ar_2221_story.mp4"]


def test_a_file_meta_could_not_process_is_a_failure(page):
    page.set_content(_FAILED)
    ok, detail = md._verify_ad_media_is_the_file(page, "2012.mp4", [IDENTITY])
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
    ok, detail = md._verify_ad_media_is_the_file(page, "2012.mp4", [IDENTITY])
    assert ok is True and seen["n"] >= 2


def test_processing_that_never_ends_is_a_failure(page, monkeypatch):
    page.set_content(_PROCESSING)
    monkeypatch.setattr(md, "_MEDIA_PROCESSING_MAX_S", 0)
    ok, detail = md._verify_ad_media_is_the_file(page, "2012.mp4", [IDENTITY])
    assert ok is False and detail["why"] == "still_processing"


def test_an_unnamed_tile_cannot_confirm_the_intended_file(page):
    """Missing identity is unverified, even when generic media is present."""
    page.set_content(_RECORDED)
    ok, detail = md._verify_ad_media_is_the_file(page, "2012.mp4")
    assert ok is False and detail["why"] == "media_identity_unavailable"


@pytest.mark.parametrize("other", ["2012_story.mp4", "2012.jpg", "prefix2012.mp4"])
def test_a_similar_name_is_not_the_requested_file(page, other):
    page.set_content(_READY.replace("2012.mp4", other))
    ok, detail = md._verify_ad_media_is_the_file(page, "2012.mp4", [IDENTITY])
    assert ok is False
    assert detail["why"] == "other_file_attached"


def test_a_matching_label_cannot_override_conflicting_media(page):
    page.set_content("<nav>2012.mp4</nav>" + _READY.replace("2012.mp4", "wrong.mp4"))
    ok, detail = md._verify_ad_media_is_the_file(page, "2012.mp4", [IDENTITY])
    assert ok is False
    assert detail["why"] == "other_file_attached"


def test_unreadable_media_cannot_confirm_identity():
    class Unreadable:
        def evaluate(self, *args):
            raise RuntimeError("synthetic unavailable DOM")
    ok, detail = md._verify_ad_media_is_the_file(Unreadable(), "2012.mp4")
    assert ok is False
    assert detail["why"] == "media_state_unavailable"


@pytest.mark.parametrize("state", [None, {}, [], {"named": ["2012.mp4"]}])
def test_incomplete_media_state_is_not_positive_evidence(monkeypatch, state):
    monkeypatch.setattr(md, "_ad_media_state", lambda *args: state)
    ok, detail = md._verify_ad_media_is_the_file(None, "2012.mp4")
    assert ok is False
    assert detail["why"] == "media_state_unavailable"
