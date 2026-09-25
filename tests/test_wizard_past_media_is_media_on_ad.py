# -*- coding: utf-8 -*-
"""Мастер креатива уже на шаге Text — медиа на объявлении есть (ACL-1313, 25.09).

Живой прогон 25.09 (CL Day, 956d0d, 2-е объявление): Meta сама перенесла
ролик в новое объявление и открыла мастер сразу на шаге Text, с галочкой у
Media (скриншот rpa_screenshots/956d0d184138/20260925_065254_recorded_fail_
ad2_creative_upload.png). Проверка медиа читает страницу объявления, а она под
модалкой, — и шаг записал `same_file_already_uploaded_this_run`, а с защитой
`no_creative` объявление потеряло бы ещё и текст.

Снимок страницы того шага обрезан, поэтому разметка здесь синтетическая,
по скриншоту: строка шагов «Creative setup · Media · Text · Creative
generation · Enhancements · Translation», заголовок «Text» с подзаголовком
«Add multiple text options and we'll show the one we predict will perform
best when your ad is delivered.», поля Primary text / Headline / Description,
кнопки Cancel и Next, крестик в углу.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import meta_dom as md  # noqa: E402
from src.fb_ads_rpa import run_telemetry  # noqa: E402

_STEPS = """
<div class="steps">
  <span>Creative setup</span> <span>Media</span> <span>Text</span>
  <span>Creative generation</span> <span>Enhancements</span> <span>Translation</span>
  <div role="button" aria-label="Close">×</div>
</div>
"""

_TEXT_STEP = """
<div><span>Text</span></div>
<div><span>Add multiple text options and we'll show the one we predict will perform best when your ad is delivered.</span></div>
<label>Primary text</label><div contenteditable="true" aria-label="Tell people what your ad is about"></div>
<label>Headline</label><input placeholder="Write a short headline">
<label>Description</label><textarea placeholder="Include additional details"></textarea>
"""

_MEDIA_STEP = """
<div><span>Media</span></div>
<div><span>Please specify the media for your ad.</span></div>
"""

_NAV = '<div role="button">Cancel</div><div role="button">Next</div>'

_AD_PAGE = """
<div id="ad-page">
  <div><span>Ad setup</span></div>
  <div><span>Creative setup (0/1)</span></div>
  <div><span>Primary text</span></div>
</div>
"""


def _wizard(body: str, *, dialog: bool = True, hidden: bool = False) -> str:
    role = ' role="dialog"' if dialog else ""
    style = ' style="display:none"' if hidden else ""
    return f'<div id="wizard"{role}{style}>{_STEPS}{body}{_NAV}</div>'


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        try:
            yield b
        finally:
            b.close()


@pytest.fixture
def page(browser, monkeypatch, tmp_path):
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    # Телеметрия — во временный файл, не в живой output/.
    monkeypatch.setattr(run_telemetry, "TELEMETRY_PATH", tmp_path / "telemetry.jsonl")
    ctx = browser.new_context(viewport={"width": 1500, "height": 900})
    p = ctx.new_page()
    p.route("**/*", lambda route: route.abort())
    try:
        yield p
    finally:
        ctx.close()


def _rows(tmp_path, step="ad_creative_upload"):
    f = tmp_path / "telemetry.jsonl"
    if not f.exists():
        return []
    return [r for r in (json.loads(line) for line in f.read_text(encoding="utf-8").splitlines() if line.strip())
            if r["step"] == step]


# ── распознавание ─────────────────────────────────────────────────────────

def test_the_wizard_on_its_text_step_means_the_media_is_chosen(page):
    """Раскладка скриншота 956d0d: модалка-диалог на шаге Text."""
    page.set_content(_AD_PAGE + _wizard(_TEXT_STEP))
    assert md._creative_wizard_open(page) is True
    assert md._wizard_past_media(page) is True


def test_the_text_step_without_a_dialog_role_is_read_page_wide(page):
    """Модалка мастера бывает и без role=dialog — тогда читаем всю страницу."""
    page.set_content(_AD_PAGE + _wizard(_TEXT_STEP, dialog=False))
    assert md._wizard_past_media(page) is True


def test_the_media_step_is_not_past_media(page):
    page.set_content(_AD_PAGE + _wizard(_MEDIA_STEP))
    assert md._creative_wizard_open(page) is True
    assert md._wizard_past_media(page) is False


def test_a_media_error_on_the_text_step_is_not_past_media(page):
    page.set_content(_AD_PAGE + _wizard(_TEXT_STEP + _MEDIA_STEP.replace("<div><span>Media</span></div>", "")))
    assert md._wizard_past_media(page) is False


def test_the_subtitle_without_the_wizard_is_not_enough(page):
    """Шагов мастера на экране нет — подзаголовок сам по себе ничего не значит."""
    page.set_content(_AD_PAGE + _TEXT_STEP)
    assert md._creative_wizard_open(page) is False
    assert md._wizard_past_media(page) is False


def test_the_subtitle_outside_the_open_dialog_does_not_count(page):
    """Открыт диалог на шаге Media, а подзаголовок Text лежит вне его."""
    page.set_content(_AD_PAGE + _TEXT_STEP + _wizard(_MEDIA_STEP.replace(
        "<div><span>Please specify the media for your ad.</span></div>", "")))
    assert md._creative_wizard_open(page) is True
    assert md._wizard_past_media(page) is False


def test_a_hidden_dialog_on_the_text_step_does_not_count(page):
    """Спрятанный диалог — не открытый: считаем только видимые."""
    page.set_content(_AD_PAGE + _wizard(_TEXT_STEP, hidden=True)
                     + _wizard(_MEDIA_STEP.replace("<div><span>Please specify the media for your ad.</span></div>", "")))
    assert md._wizard_past_media(page) is False


# ── шаг заливки для уже поднятого файла ───────────────────────────────────

@pytest.fixture
def known_file(page, monkeypatch):
    """Файл уже поднят в этом прогоне: путь «через медиатеку» ничего не дал."""
    closes = []
    monkeypatch.setattr(md, "upload_already_succeeded", lambda path: True)
    monkeypatch.setattr(md, "upload_already_failed", lambda path: False)
    monkeypatch.setattr(md, "upload_ad_creative", lambda *a, **k: False)
    monkeypatch.setattr(md, "_media_block_filled_now", lambda p: False)

    def close(p):
        closes.append(True)
        p.evaluate("document.getElementById('wizard').remove()")
        return True

    monkeypatch.setattr(md, "_close_creative_setup_modal", close)
    return closes


def test_a_known_file_with_the_wizard_at_text_is_a_success(page, known_file, tmp_path):
    page.set_content(_AD_PAGE + _wizard(_TEXT_STEP))
    assert md.upload_ad_creative_with_retry(page, "Video", "C:/media/clip.mp4") is True
    assert known_file == [True], "мастер не закрыли"
    assert page.locator("#wizard").count() == 0
    row = _rows(tmp_path)[-1]
    assert row["ok"] is True and row["note"] == "media_already_on_ad_wizard_at_text"
    assert row["diag"]["file"] == "clip.mp4"
    assert row["diag"]["page_shows_media"] is False


def test_a_known_file_with_the_wizard_at_media_is_still_a_failure(page, known_file, tmp_path):
    page.set_content(_AD_PAGE + _wizard(_MEDIA_STEP))
    assert md.upload_ad_creative_with_retry(page, "Video", "C:/media/clip.mp4") is False
    # Мастер закрывают, чтобы спросить само объявление (ACL-1313, 09:55):
    # медиа там нет — отказ остаётся отказом.
    assert known_file == [True]
    row = _rows(tmp_path)[-1]
    assert row["ok"] is False and row["note"] == "same_file_already_uploaded_this_run"


def test_a_wizard_on_another_step_is_closed_and_the_ad_decides(page, known_file, tmp_path, monkeypatch):
    """SA Investing 25.09 09:55: мастер открылся на «Creative setup» с галочкой у
    Media. Шаг Text не показан, но объявление после закрытия мастера медиа
    показывает — это успех, а не «нет медиа»."""
    page.set_content(_AD_PAGE + _wizard(_MEDIA_STEP))
    monkeypatch.setattr(md, "_media_block_filled_now",
                        lambda p: p.locator("#wizard").count() == 0)
    assert md.upload_ad_creative_with_retry(page, "Image", "C:/media/pic.jpg") is True
    assert known_file == [True]
    row = _rows(tmp_path)[-1]
    assert row["ok"] is True and row["note"] == "media_already_on_ad_after_wizard_closed"
