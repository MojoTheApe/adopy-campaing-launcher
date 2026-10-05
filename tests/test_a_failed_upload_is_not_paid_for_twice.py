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

FILE = r"C:\creatives\heavy.mp4"  # linux-ok: a Windows path as opaque data, never touched on disk


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    md._failed_uploads.clear()
    rt.set_current_run("run-1", "CAMP")
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    yield
    md._failed_uploads.clear()


def _offered_and_failed(*a, **k) -> bool:
    """Файл подали Meta, и он не встал. Только такой отказ запоминается
    (ACL-1348): отказ мастера до подачи файла — не приговор файлу."""
    if not k.get("reuse_only"):
        md._note_file_offered()
    return False


def test_the_same_file_is_not_uploaded_again_after_it_failed(monkeypatch):
    tries: list[dict] = []
    monkeypatch.setattr(md, "upload_ad_creative",
                        lambda *a, **k: tries.append(k) or _offered_and_failed(*a, **k))
    monkeypatch.setattr(md, "_close_creative_setup_modal", lambda page: True)
    monkeypatch.setattr(md, "_verify_ad_media_present", lambda page: False)
    monkeypatch.setattr(md, "_upload_stalled", lambda page: False)
    monkeypatch.setattr(md, "reuse_before_upload", lambda page, path: None)

    assert md.upload_ad_creative_with_retry(object(), "video", FILE) is False
    spent = [t for t in tries if not t.get("reuse_only")]
    assert len(spent) >= 1

    tries.clear()
    assert md.upload_ad_creative_with_retry(object(), "video", FILE) is False
    # Второе объявление идёт за копией через мастер (acl-1192) — это не
    # заливка: `reuse_only` останавливает шаг до неё.
    assert all(t.get("reuse_only") for t in tries), (
        "второе объявление не должно платить за ту же загрузку"
    )


def test_a_copy_that_appeared_meanwhile_still_wins(monkeypatch):
    """Быстрый отказ — про загрузку, не про медиатеку.

    Пока шло первое объявление, копия могла появиться в медиатеке. Тогда файл
    вообще никуда не поедет, и отказывать нечему.
    """
    copy_there = {"yes": False}
    # Копия ищется внутри мастера (acl-1192): `reuse_only`-заход отвечает
    # «прикрепили», когда она появилась.
    monkeypatch.setattr(md, "upload_ad_creative",
                        lambda *a, **k: bool(k.get("reuse_only") and copy_there["yes"])
                        or _offered_and_failed(*a, **k))
    monkeypatch.setattr(md, "_close_creative_setup_modal", lambda page: True)
    monkeypatch.setattr(md, "_verify_ad_media_present", lambda page: False)
    monkeypatch.setattr(md, "_upload_stalled", lambda page: False)
    monkeypatch.setattr(md, "reuse_before_upload", lambda page, path: None)
    assert md.upload_ad_creative_with_retry(object(), "video", FILE) is False

    copy_there["yes"] = True
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
    md.upload_ad_creative_with_retry(object(), "video", r"C:\creatives\other.mp4")  # linux-ok: a Windows path as opaque data, never touched on disk
    assert uploads, "другой файл обязан получить свою попытку"


def test_the_verdict_does_not_survive_into_the_next_run(monkeypatch):
    """Между прогонами меняются кабинет, сеть и профиль — приговор не переносим."""
    monkeypatch.setattr(md, "upload_ad_creative", _offered_and_failed)
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


# ── удачная загрузка тоже не повторяется ─────────────────────────────────
#
# Замер 15.09 по восьми живым прогонам: 83 минуты, 39 из них (47%) впустую, и
# 35 минут из этих 39 — объявления 2 и 3, заливающие файл, который Meta уже не
# примет. `ad2_creative_upload`: 130 секунд в среднем, сто процентов впустую.


def _quiet(monkeypatch):
    monkeypatch.setattr(md, "_close_creative_setup_modal", lambda page: True)
    monkeypatch.setattr(md, "_verify_ad_media_present", lambda page: False)
    monkeypatch.setattr(md, "_upload_stalled", lambda page: False)
    monkeypatch.setattr(md, "reuse_before_upload", lambda page, path: None)


def test_the_second_ad_does_not_re_upload_what_already_went(monkeypatch):
    """Meta не берёт вторую заливку того же файла — пытаться незачем.

    Но за копией в медиатеку идти надо — через мастер, где медиатека и живёт
    (acl-1192): второй заход открывает мастер с `reuse_only=True` и не грузит.
    """
    md._uploaded_ok.clear()
    tries: list[dict] = []

    def fake_upload(*a, **k):
        tries.append(k)
        return not k.get("reuse_only")

    monkeypatch.setattr(md, "upload_ad_creative", fake_upload)
    _quiet(monkeypatch)

    assert md.upload_ad_creative_with_retry(object(), "video", FILE) is True
    assert len(tries) == 1 and not tries[0].get("reuse_only")

    tries.clear()
    assert md.upload_ad_creative_with_retry(object(), "video", FILE) is False
    assert [t.get("reuse_only") for t in tries] == [True], (
        "второе объявление должно открыть мастер за копией, но не заливать"
    )
    md._uploaded_ok.clear()


def test_the_library_still_wins_over_the_fast_refusal(monkeypatch):
    """Единственный работающий путь для объявлений 2 и 3 — взять копию.

    Быстрый отказ не должен его перекрывать: сначала спрашиваем медиатеку.
    """
    md._uploaded_ok.clear()
    monkeypatch.setattr(md, "upload_ad_creative", lambda *a, **k: True)
    _quiet(monkeypatch)
    md.upload_ad_creative_with_retry(object(), "video", FILE)

    monkeypatch.setattr(md, "reuse_before_upload", lambda page, path: True)
    assert md.upload_ad_creative_with_retry(object(), "video", FILE) is True
    md._uploaded_ok.clear()


def test_the_two_refusals_are_named_apart(monkeypatch):
    """«Уже грузили и не вышло» и «уже загрузили» — разные вещи, и в
    телеметрии их путать нельзя: первая про поломку, вторая про правило Meta."""
    md._uploaded_ok.clear(); md._failed_uploads.clear()
    said: list[dict] = []
    monkeypatch.setattr(md._telemetry, "record",
                        lambda step, ok, **kw: said.append({"step": step, **kw}))
    # Первая заливка удаётся; заход за копией (`reuse_only`) — нет.
    monkeypatch.setattr(md, "upload_ad_creative", lambda *a, **k: not k.get("reuse_only"))
    _quiet(monkeypatch)
    md.upload_ad_creative_with_retry(object(), "video", FILE)
    md.upload_ad_creative_with_retry(object(), "video", FILE)
    assert said and said[-1]["note"] == "same_file_already_uploaded_this_run"
    md._uploaded_ok.clear()


def test_a_new_run_starts_clean(monkeypatch):
    md._uploaded_ok.clear()
    monkeypatch.setattr(md, "upload_ad_creative", lambda *a, **k: True)
    _quiet(monkeypatch)
    md.upload_ad_creative_with_retry(object(), "video", FILE)
    assert md.upload_already_succeeded(FILE) is True
    rt.set_current_run("run-next", "CAMP")
    assert md.upload_already_succeeded(FILE) is False
    md._uploaded_ok.clear()
