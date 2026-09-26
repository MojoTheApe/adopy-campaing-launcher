# -*- coding: utf-8 -*-
"""Прикрепился ли ИМЕННО наш файл и пригоден ли он (ACL-1171 / ACL-1192).

Проверка «медиа на объявлении есть» пропускает две беды: из медиатеки взяли
чужую плитку (имена там похожи, «2012.mp4» и «2012_story.mp4» стоят рядом) и
файл, который Meta не смогла обработать — объявление с таким медиа не
опубликуется, а шаг отчитается успехом.

Готовый Media-блок записан с живой страницы. Имена и состояния ошибки/обработки
ниже — синтетические мутации для регрессионных проверок.

Порт ACL-1284, вариант A (решение владельца 24.09: живой код — правда, ролики
с жёлтым треугольником берутся): `_verify_ad_media_is_the_file(page, name)`
читает имя и состояние медиа, а личность файла по миниатюре сверяет шаг
медиатеки (`_try_reuse_library_media`, тесты в test_media_attachment_identity
и test_reuse_never_stacks_creatives). Остаются инварианты: чужой файл
никогда не «наш креатив» (в том числе похожее имя), обработка без конца и
файл, который Meta не обработала или отклонила («Video rejected», решение
владельца 25.09), — отказ. Иначе, чем в строгом варианте #238: имя, которое
не показано, — не отказ (`name_not_shown`); нечитаемое состояние тоже не
отказ (наличие медиа проверено отдельно).
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


_RECORDED = (Path(__file__).parent / "fixtures/meta/ad_media_attached.html").read_text()
# Captured ready structure with explicit synthetic filename/error states.
_READY = _RECORDED.replace('>', '><div>2012.mp4</div>', 1)
_OTHER = _READY.replace('2012.mp4', '9_ar_2221_story.mp4')
_PROCESSING = _READY.replace('>', '><div>Processing your video</div>', 1)
_FAILED = _READY.replace('>', "><div>This media couldn't be processed.</div>", 1)
_REJECTED = _READY.replace('>', "><div>Video rejected</div>", 1)


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


def test_an_unnamed_tile_is_not_a_refusal(page):
    """Вариант A: Meta подписывает не каждую плитку — без имени это не отказ.

    Строгий #238 требовал здесь `media_identity_unavailable`. В живом коде
    личность по миниатюре сверяет шаг медиатеки, а эта проверка отвечает
    только за имя и состояние.
    """
    page.set_content(_RECORDED)
    ok, detail = md._verify_ad_media_is_the_file(page, "2012.mp4")
    assert ok is True and detail["why"] == "name_not_shown"


@pytest.mark.parametrize("other", ["2012_story.mp4", "2012.jpg", "prefix2012.mp4", "12012.mp4"])
def test_a_similar_name_is_not_the_requested_file(page, other):
    """Порт ACL-1284: вхождение основы имени считало эти файлы нашими."""
    page.set_content(_READY.replace("2012.mp4", other))
    ok, detail = md._verify_ad_media_is_the_file(page, "2012.mp4")
    assert ok is False
    assert detail["why"] == "other_file_attached"


@pytest.mark.parametrize("shown", ["2012", "2012_1.mp4", "2012 (1).mp4", "Uploading video 2012.mp4"])
def test_the_names_meta_gives_our_file_are_ours(page, shown):
    """Как Meta показывает наш файл: без расширения, с суффиксом копии, в фразе."""
    page.set_content(_READY.replace("2012.mp4", shown))
    ok, detail = md._verify_ad_media_is_the_file(page, "2012.mp4")
    assert ok is True, detail
    assert detail["state"]["others"] == []


def test_a_rejected_video_is_a_failure(page):
    """Порт ACL-1284: объявление с отклонённым видео не запустится."""
    page.set_content(_REJECTED)
    ok, detail = md._verify_ad_media_is_the_file(page, "2012.mp4")
    assert ok is False
    assert detail["why"] == "media_rejected"


def test_the_word_rejected_elsewhere_is_not_about_our_media(page):
    """Статус «Rejected» у другой строки — не про медиа этого объявления."""
    page.set_content("<div>Rejected</div><div>Ad rejected</div>" + _READY)
    ok, detail = md._verify_ad_media_is_the_file(page, "2012.mp4")
    assert ok is True, detail


def test_an_unreadable_media_state_is_not_a_refusal():
    """Вариант A: DOM не прочитался — не отказ, наличие медиа проверено отдельно."""
    class Unreadable:
        def evaluate(self, *args):
            raise RuntimeError("synthetic unavailable DOM")
    ok, detail = md._verify_ad_media_is_the_file(Unreadable(), "2012.mp4")
    assert ok is True
    assert detail["why"] == "name_not_shown"
    assert "error" in detail["state"]
