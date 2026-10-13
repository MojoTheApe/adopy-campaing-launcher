# -*- coding: utf-8 -*-
"""ACL-1637: одно видео на нескольких объявлениях кампании — медиа у всех.

Живьём 06.10 (Doc120, B97): объявление 1 получало видео, объявления 2-4 — нет.
Телеметрия каждого из них:

    ad_creative_reuse   not_in_library      looked_for: <имя исходника>
    ad_creative_reuse   library_has_no_copy
    ad_creative_upload  False               same_file_already_uploaded_this_run

Разбор. В Meta уезжает не исходник (247 МБ), а его сжатая копия из кэша —
`<имя>.<ключ>.mp4` (25 МБ), и в медиатеке плитка подписана ИМЕНЕМ КОПИИ. Путь
«файл уже грузили» искал плитку только по имени исходника и поэтому не находил
её никогда; а правило «второй раз не грузим» оставляло объявление пустым.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import creative_compress as cc  # noqa: E402
from src.fb_ads_rpa import failure_policy as fp  # noqa: E402
from src.fb_ads_rpa import meta_dom as md  # noqa: E402
from src.fb_ads_rpa import simple_create as sc  # noqa: E402

FILE = "C:/creatives/sample_video_01_post.mp4"
DERIVED = "sample_video_01_post.0123456789abcdef.mp4"


@pytest.fixture
def quiet(monkeypatch):
    said: list[tuple] = []
    monkeypatch.setattr(md, "_close_creative_setup_modal", lambda page: True)
    monkeypatch.setattr(md, "_verify_ad_media_present", lambda page: False)
    monkeypatch.setattr(md, "_upload_stalled", lambda page: False)
    monkeypatch.setattr(md, "_media_block_filled_now", lambda page: False)
    monkeypatch.setattr(md, "_wizard_past_media", lambda page: False)
    monkeypatch.setattr(md, "_creative_wizard_open", lambda page: False)
    monkeypatch.setattr(md, "_page_read_trouble", lambda page, s: "")
    monkeypatch.setattr(md, "_sleep_h", lambda s: None)
    monkeypatch.setattr(md._telemetry, "record",
                        lambda step, ok=None, **k: said.append((step, ok, k.get("note"))))
    md._telemetry.set_current_run("run-1637", "camp")
    md._uploaded_ok.clear()
    md._failed_uploads.clear()
    yield said
    md._set_upload_why(None)  # причина не должна утекать в соседние тесты
    md._uploaded_ok.clear()
    md._failed_uploads.clear()


# ── имя сжатой копии из кэша ───────────────────────────────────────────────

def test_the_cached_copy_name_is_found_without_ffmpeg(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "cache_dir", lambda: tmp_path / "cache")
    src = tmp_path / "big.mp4"
    src.write_bytes(b"x" * 4096)
    assert cc.cached_copy_name(str(src), limit_mb=0.001) == ""      # копии ещё нет
    dst = cc.cache_path(str(src), cc.DEFAULT_TARGET_MB)
    dst.parent.mkdir(parents=True)
    dst.write_bytes(b"y" * 1024)
    assert cc.cached_copy_name(str(src), limit_mb=0.001) == dst.name
    # Небольшой файл не сжимался — копии под другим именем нет.
    assert cc.cached_copy_name(str(src), limit_mb=1.0) == ""


def test_an_empty_or_oversized_cache_file_is_not_a_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "cache_dir", lambda: tmp_path / "cache")
    src = tmp_path / "big.mp4"
    src.write_bytes(b"x" * 4096)
    dst = cc.cache_path(str(src), cc.DEFAULT_TARGET_MB)
    dst.parent.mkdir(parents=True)
    dst.write_bytes(b"")
    assert cc.cached_copy_name(str(src), limit_mb=0.001) == "", "пустой файл — не копия"
    dst.write_bytes(b"y" * 3000)     # больше лимита (~1048 байт)
    assert cc.cached_copy_name(str(src), limit_mb=0.001) == "", "копия крупнее лимита — не копия"


# ── медиатека: копия под именем сжатого файла, в том числе после обновления ─

def _library(monkeypatch, *, visible_from_round: int, names: list[str]):
    """Медиатека, в которой копия по `names` видна с N-го обращения."""
    asked: list[str] = []

    def fake(page, filename):
        asked.append(filename)
        rounds = sum(1 for n in asked if n in names)
        return md.REUSE_ATTACHED if filename in names and rounds >= visible_from_round else md.REUSE_ABSENT

    monkeypatch.setattr(md, "_try_reuse_library_media", fake)
    monkeypatch.setattr(cc, "cached_copy_name", lambda path, **k: DERIVED)
    return asked


def test_the_library_copy_is_looked_up_under_the_compressed_name(quiet, monkeypatch):
    asked = _library(monkeypatch, visible_from_round=1, names=[DERIVED])
    assert md._reuse_known_copy(object(), FILE, True) is True
    assert asked == [DERIVED]


def test_a_copy_that_appears_after_a_refresh_is_taken(quiet, monkeypatch):
    """Свежая загрузка в сетке видна не сразу: второй заход её находит."""
    asked = _library(monkeypatch, visible_from_round=2, names=[DERIVED])
    assert md._reuse_known_copy(object(), FILE, True) is True
    assert asked == [DERIVED, DERIVED]


def test_no_copy_anywhere_is_none_after_two_rounds(quiet, monkeypatch):
    asked = _library(monkeypatch, visible_from_round=1, names=[])
    assert md._reuse_known_copy(object(), FILE, True) is None
    assert asked == [DERIVED, DERIVED, "sample_video_01_post.mp4"]


def test_no_second_library_round_when_the_step_budget_is_nearly_spent(quiet, monkeypatch):
    asked = _library(monkeypatch, visible_from_round=1, names=[])
    monkeypatch.setattr(md, "_creative_left", lambda: 5.0)
    assert md._reuse_known_copy(object(), FILE, True) is None
    assert asked == [DERIVED], "только первый заход"


def test_a_dirty_pick_is_a_refusal_not_a_reupload(quiet, monkeypatch):
    monkeypatch.setattr(md, "_try_reuse_library_media", lambda page, name: md.REUSE_DIRTY)
    monkeypatch.setattr(cc, "cached_copy_name", lambda path, **k: DERIVED)
    assert md._reuse_known_copy(object(), FILE, True) is False


def test_an_image_has_no_compressed_name(quiet, monkeypatch):
    asked = _library(monkeypatch, visible_from_round=1, names=[])
    md._reuse_known_copy(object(), "C:/creatives/pic.png", False)
    assert DERIVED not in asked


# ── один файл на три объявления ────────────────────────────────────────────

def _ads(monkeypatch, library_has_copy: bool, upload_works: bool = True):
    calls: list[str] = []

    def fake_upload(page, creative_type, creative_path, *, edit_url="", reuse_only=False):
        calls.append("reuse" if reuse_only else "upload")
        if reuse_only:
            if library_has_copy:
                return True
            md._set_upload_why(md.LIBRARY_HAS_NO_COPY)
            return False
        if not upload_works:
            md._set_upload_why("file_not_landed")
        return upload_works

    monkeypatch.setattr(md, "upload_ad_creative", fake_upload)
    return calls


def test_three_ads_one_file_the_library_copy_is_reused(quiet, monkeypatch):
    calls = _ads(monkeypatch, library_has_copy=True)
    results = [md.upload_ad_creative_with_retry(object(), "video", FILE) for _ in range(3)]
    assert results == [True, True, True]
    assert calls == ["upload", "reuse", "reuse"], "the file goes up once"


def test_no_library_copy_means_one_fresh_upload_per_ad(quiet, monkeypatch):
    calls = _ads(monkeypatch, library_has_copy=False)
    results = [md.upload_ad_creative_with_retry(object(), "video", FILE) for _ in range(3)]
    assert results == [True, True, True], "no ad is left without media"
    assert calls == ["upload", "reuse", "upload", "reuse", "upload"]
    assert ("ad_creative_upload", None, "reupload_library_has_no_copy") in quiet


def test_the_reupload_is_tried_once_per_ad_and_the_failure_is_named(quiet, monkeypatch):
    calls = _ads(monkeypatch, library_has_copy=False, upload_works=False)
    md.remember_upload_succeeded(FILE)
    assert md.upload_ad_creative_with_retry(object(), "video", FILE) is False
    assert calls == ["reuse", "upload"], "one reupload, not the two attempts of a first upload"
    assert md.last_upload_why() == "file_not_landed"
    assert sc.creative_why_text("file_not_landed").startswith("This ad has no media")


def test_a_wizard_that_did_not_open_is_not_followed_by_a_reupload(quiet, monkeypatch):
    """Мастер не открылся — заливка пошла бы через тот же мастер."""
    calls: list[str] = []

    def fake_upload(page, ct, cp, *, edit_url="", reuse_only=False):
        calls.append("reuse" if reuse_only else "upload")
        md._set_upload_why("wizard_not_opened")
        return False

    monkeypatch.setattr(md, "upload_ad_creative", fake_upload)
    md.remember_upload_succeeded(FILE)
    assert md.upload_ad_creative_with_retry(object(), "video", FILE) is False
    assert calls == ["reuse"]
    assert md.last_upload_why() == "wizard_not_opened"


def test_a_file_that_never_reached_meta_gets_one_reupload_for_ad2(quiet, monkeypatch):
    """B97 5f05cf961d37: объявление 1 — `file_not_landed`, и объявления 2-3
    получали `same_file_already_failed_this_run` без единой попытки."""
    calls = _ads(monkeypatch, library_has_copy=False)
    md._set_upload_why("file_not_landed")
    md._remember_upload_failed(FILE)
    md._set_upload_why(None)
    assert md.upload_ad_creative_with_retry(object(), "video", FILE) is True
    assert calls == ["reuse", "upload"]


def test_a_file_meta_refused_is_not_uploaded_again_and_keeps_a_named_reason(quiet, monkeypatch):
    calls = _ads(monkeypatch, library_has_copy=False)
    md._set_upload_why("media_refused_on_ad")
    md._remember_upload_failed(FILE)
    md._set_upload_why(None)
    assert md.upload_ad_creative_with_retry(object(), "video", FILE) is False
    assert calls == ["reuse"], "ни одной новой заливки"
    assert md.last_upload_why() == "same_file_already_failed_this_run"
    assert ("ad_creative_upload", False, "same_file_already_failed_this_run") in quiet
    assert md.failed_upload_reason(FILE) == "media_refused_on_ad", "причина провала сохранена"


@pytest.mark.parametrize("why", ["crop_wizard_stuck", "upload_failed_unnamed", "page_closed"])
def test_other_failures_where_meta_saw_the_file_are_not_reuploaded(quiet, monkeypatch, why):
    calls = _ads(monkeypatch, library_has_copy=False)
    md._set_upload_why(why)
    md._remember_upload_failed(FILE)
    md._set_upload_why(None)
    assert md.upload_ad_creative_with_retry(object(), "video", FILE) is False
    assert calls == ["reuse"]


def test_a_dirty_library_pick_is_not_followed_by_a_reupload(quiet, monkeypatch):
    calls: list[str] = []

    def fake_upload(page, ct, cp, *, edit_url="", reuse_only=False):
        calls.append("reuse" if reuse_only else "upload")
        md._set_upload_why("library_pick_left_selection")
        return False

    monkeypatch.setattr(md, "upload_ad_creative", fake_upload)
    md.remember_upload_succeeded(FILE)
    assert md.upload_ad_creative_with_retry(object(), "video", FILE) is False
    assert calls == ["reuse"]


@pytest.mark.parametrize("probe", ["_media_block_filled_now", "_wizard_past_media"])
def test_media_already_on_the_ad_is_checked_before_any_reupload(quiet, monkeypatch, probe):
    calls = _ads(monkeypatch, library_has_copy=False)
    monkeypatch.setattr(md, probe, lambda page: True)
    monkeypatch.setattr(md, "_crop_gate", lambda *a, **k: False)
    md.remember_upload_succeeded(FILE)
    assert md.upload_ad_creative_with_retry(object(), "video", FILE) is True
    assert calls == ["reuse"], "медиа уже на объявлении — заливки нет"


def test_media_seen_after_the_wizard_is_closed_is_checked_before_any_reupload(quiet, monkeypatch):
    calls = _ads(monkeypatch, library_has_copy=False)
    monkeypatch.setattr(md, "_creative_wizard_open", lambda page: True)
    seen = iter([False, True])
    monkeypatch.setattr(md, "_media_block_filled_now", lambda page: next(seen))
    monkeypatch.setattr(md, "_crop_gate", lambda *a, **k: False)
    md.remember_upload_succeeded(FILE)
    assert md.upload_ad_creative_with_retry(object(), "video", FILE) is True
    assert calls == ["reuse"]


def test_no_time_left_ends_with_a_named_reason_not_a_reupload(quiet, monkeypatch):
    calls = _ads(monkeypatch, library_has_copy=False)
    monkeypatch.setattr(md, "_creative_left", lambda: 5.0)
    md.remember_upload_succeeded(FILE)
    assert md.upload_ad_creative_with_retry(object(), "video", FILE) is False
    assert calls == ["reuse"]
    assert ("ad_creative_upload", False, "same_file_already_uploaded_this_run") in quiet


# ── причина для человека ───────────────────────────────────────────────────

@pytest.mark.parametrize("why", [
    "library_has_no_copy", "same_file_already_uploaded_this_run",
    "same_file_already_failed_this_run", "file_not_landed", "wizard_not_opened",
])
def test_an_ad_without_media_ends_blocking_with_an_english_reason(why):
    text = sc.creative_why_text(why)
    assert text.startswith("This ad has no media") and text.isascii()
    md._set_upload_why(why)
    try:
        row = md.creative_soft_fail("ad3", "Ad 3")
    finally:
        md._set_upload_why(None)
    assert row == f"ad3_creative_upload:{why} - Ad 3: {text}"
    assert fp.severity(row) == fp.BLOCKING
    problem = fp.problem(row)
    assert problem["ad_index"] == 3 and problem["title"].startswith("Ad media - ")
