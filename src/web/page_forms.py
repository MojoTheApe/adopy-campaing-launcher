"""Список форм Страницы для выбора в билдере (ACL-1775).

Только чтение и только по явному нажатию «Refresh forms»: ни опроса, ни
таймеров. Путь тот же, что у «Check plan» и чтений ACL-1760: замок чтения
профиля, сессия Ads Manager в своей вкладке, клиент БЕЗ права записи.

Условия Lead Ads не принимаются никогда; форм здесь не создают (ACL-1776).
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from src.fb_api import forms as _forms

#: Вызовов Graph на одно нажатие: страницы кабинета + список форм + условия.
READ_MAX_CALLS = 8


def page_for(entry_page_id: str, client: Any, act: str) -> str:
    """Страница аккаунта: из карты; пусто - первая из списка кабинета, затем профиля (ACL-2105)."""
    page_id = (entry_page_id or "").strip()
    if page_id:
        return page_id
    from src.fb_api import pages as _pg

    started = time.monotonic()
    merged = _pg.read_merged(client, act, always_profile=False, account_max_pages=1)
    _pg.record_counts(merged, where="forms", started=started)
    return merged.rows[0]["id"] if merged.rows else ""


def _read_in_session(account_label: str, selected_page: str | None = None) -> dict[str, Any]:
    from src.analysis_agent import account_spend as _as
    from src.analysis_agent import account_spend_api as _api

    entry = _as._entry_for(account_label)
    entry_page = str(getattr(entry, "page_id", "") or "") if entry is not None else ""
    with _api._session(account_label) as sess:
        client = sess.client
        if client.allow_writes:
            raise AssertionError("the forms read must not be able to write")
        client.reset_budget(READ_MAX_CALLS)
        if selected_page is None:
            page_id = page_for(entry_page, client, sess.act)
        else:
            from src.web.account_pages import pages_from_client, resolve_selection
            # Тот же объединённый список (кабинет + профиль), что и в выборе Страницы билдера.
            page_id = resolve_selection(pages_from_client(client, sess.act), selected_page)["id"]
        if not page_id:
            read = _forms.forms_capability("", [])
        else:
            read = _forms.read_page_forms(client, page_id)
        if client.writes:
            raise AssertionError("the forms read must not write")
    return read.as_dict()


def read_forms(account_label: str, selected_page: str | None = None) -> dict[str, Any]:
    """Формы Страницы аккаунта. Всегда словарь: `ok`, `forms`, `message`, `state`."""
    from src.analysis_agent import account_spend as _as
    from src.analysis_agent import account_spend_api as _api

    started = time.monotonic()
    run = _as.serialized_browser(key=_as._profile_key, reading=True)(
        lambda account: _read_in_session(account, selected_page))
    from src.fb_api.connection import user_started_action
    try:
        with user_started_action():      # ACL-2272: "only by the person's press" (see /api/page-forms)
            out = run(account_label)
        error = ""
    except Exception as exc:  # noqa: BLE001 - отказ профиля/страницы: текстом человеку
        error = _api._failure(account_label, exc)[0]
        out = {"state": "failed", "ok": False, "page_id": "", "forms": [], "message": error}
    out["account"] = account_label
    out["refreshed_at"] = datetime.now(timezone.utc).isoformat()
    # Журнал не должен ломать чтение (`_record` сам глотает сбои).
    _as._record("api_forms_read", not error and out.get("ok") is True, time.monotonic() - started,
                {"account": account_label, "source": "api", "state": out.get("state"),
                 "forms": len(out.get("forms") or [])})
    return out


def _read_photos_in_session(account_label: str, selected_page: str) -> dict[str, Any]:
    from src.analysis_agent import account_spend_api as _api
    from src.web.account_pages import pages_from_client, resolve_selection

    with _api._session(account_label) as sess:
        client = sess.client
        if client.allow_writes:
            raise AssertionError("the page pictures read must not be able to write")
        client.reset_budget(READ_MAX_CALLS)
        # Страница должна принадлежать аккаунту (тот же объединённый список, что и в выборе билдера).
        page_id = resolve_selection(pages_from_client(client, sess.act), selected_page)["id"]
        read = _forms.read_page_photos(client, page_id)
        if client.writes:
            raise AssertionError("the page pictures read must not write")
    return read.as_dict()


def read_photos(account_label: str, selected_page: str) -> dict[str, Any]:
    """Картинки Страницы аккаунта (ACL-2188). Только по нажатию; всегда словарь без ссылок на картинки."""
    from src.analysis_agent import account_spend as _as
    from src.analysis_agent import account_spend_api as _api

    started = time.monotonic()
    run = _as.serialized_browser(key=_as._profile_key, reading=True)(
        lambda account: _read_photos_in_session(account, selected_page))
    from src.fb_api.connection import user_started_action
    try:
        with user_started_action():      # ACL-2272: only by the person's press
            out = run(account_label)
        error = ""
    except Exception as exc:  # noqa: BLE001 - отказ профиля/страницы: текстом человеку
        error = _api._failure(account_label, exc)[0]
        out = {"state": "failed", "ok": False, "page_id": "", "photos": [], "truncated": False, "message": error}
    out["account"] = account_label
    _as._record("api_page_photos_read", not error and out.get("ok") is True, time.monotonic() - started,
                {"account": account_label, "source": "api", "state": out.get("state"),
                 "photos": len(out.get("photos") or [])})
    return out


# ───────────────────────── фон формы: загрузка файла (ACL-2533) ─────────────────────────

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def sniff_image(data: bytes) -> str:
    """Тип по первым байтам (имя и тип от браузера не доверяем): `image/jpeg`, `image/png` или пусто."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == _PNG_MAGIC:
        return "image/png"
    return ""


def check_photo_file(data: bytes) -> tuple[str, str]:
    """(тип, текст отказа). Пустой отказ - файл годится. Те же пределы, что в политике записи."""
    from src.fb_api import write_policy as _wp

    if not data:
        return "", "The image file is empty."
    mime = sniff_image(data)
    if not mime:
        return "", "The background image must be a JPEG or PNG file."
    if len(data) > _wp.FORM_PHOTO_MAX_BYTES:
        return "", f"The background image is larger than {_wp.FORM_PHOTO_MAX_BYTES // (1024 * 1024)} MB."
    return mime, ""


def _upload_in_session(account_label: str, selected_page: str, data: bytes, mime: str) -> dict[str, Any]:
    import hashlib

    from src.analysis_agent import account_spend_api as _api
    from src.fb_api import write_policy as _wp
    from src.web.account_pages import pages_from_client, resolve_selection

    with _api._session(account_label, photo=True) as sess:
        client = sess.client
        if client.allow_writes:
            raise AssertionError("the form-photo session must not be able to write to Graph")
        # Чтения списка Страниц + одна загрузка (+1 в оценке вызовов); больше этого сессия не сделает.
        client.reset_budget(READ_MAX_CALLS + _wp.FORM_PHOTO_UPLOAD_CALLS)
        # Страница должна принадлежать аккаунту (тот же объединённый список, что и в выборе билдера).
        page_id = resolve_selection(pages_from_client(client, sess.act), selected_page)["id"]
        sess.client.transport.pin_photo_page(page_id)
        got = client.upload_form_photo(page_id, data, mime=mime)
        if client.writes != 1:
            raise AssertionError("the form-photo session must make exactly one upload")
    return {"ok": True, "state": "uploaded", "page_id": page_id, "photo_id": got["photo_id"],
            "width": got["width"], "height": got["height"],
            "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data), "message": ""}


def upload_photo(account_label: str, selected_page: str, data: bytes) -> dict[str, Any]:
    """Загрузить фон формы с вкладки профиля (ACL-2533). Только по нажатию; всегда словарь.

    Файл уходит ТОЛЬКО через сессию Ads Manager своего профиля; картинка из Meta не читается назад.
    В ответе - id фото, Страница, размеры и sha256 файла; в журнале - без id.
    """
    from src.analysis_agent import account_spend as _as
    from src.analysis_agent import account_spend_api as _api

    started = time.monotonic()
    mime, refusal = check_photo_file(data)
    if refusal:
        return {"ok": False, "state": "refused", "page_id": "", "photo_id": "", "message": refusal,
                "account": account_label}
    run = _as.serialized_browser(key=_as._profile_key, reading=True)(
        lambda account: _upload_in_session(account, selected_page, data, mime))
    from src.fb_api.connection import user_started_action
    from src.fb_api.graph import GraphError, WriteUnknown
    try:
        with user_started_action():      # ACL-2272: only by the person's press
            out = run(account_label)
        error, state = "", "uploaded"
    except GraphError as exc:            # Meta refused the picture: its own text
        error, state = str(exc), "refused"
    except WriteUnknown:
        error = ("The upload result is unknown. Nothing was retried; if the picture is wanted, "
                 "press Upload again.")
        state = "unknown"
    except Exception as exc:  # noqa: BLE001 - отказ профиля/страницы: текстом человеку
        error, state = _api._failure(account_label, exc)[0], "failed"
    if error:
        out = {"state": state, "ok": False, "page_id": "", "photo_id": "", "message": error}
    out["account"] = account_label
    _as._record("api_form_photo_upload", not error, time.monotonic() - started,
                {"account": account_label, "source": "web_session", "state": out.get("state"),
                 "bytes": len(data), "width": out.get("width") or 0, "height": out.get("height") or 0})
    return out
