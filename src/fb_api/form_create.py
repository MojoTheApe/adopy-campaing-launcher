"""Создание Instant Form из шаблона: чистые куски (ACL-1776).

Ни сети, ни записи здесь нет: поиск своей формы по точному имени, сверка
созданной формы с шаблоном, понятные тексты отказов. Саму запись делает
`CampaignCreator` (`_write` -> `GraphClient.post(form_page=...)`) под отдельной
цепочкой политики `write_policy.check_form_create`.

Условия Lead Ads робот не принимает НИКОГДА: Страница без принятых условий -
остановка с просьбой к человеку принять их в Meta.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.fb_api import form_templates as ft
from src.fb_api import forms as _forms

log = logging.getLogger(__name__)

#: Поля чтения созданной формы для сверки.
READBACK_FIELDS = "id,name,status,locale,page{id},questions{key,label,type,options}"

#: ACL-2188: картинка шаблона принадлежит другой Странице - стоп на Check plan, до любой записи.
MSG_COVER_OTHER_PAGE = ("Form template {name!r}: the background image belongs to another Page. Duplicate the "
                        "template and choose a picture of this Page, or remove the image.")
MSG_COVER_REFUSED = ("Meta refused the form {form_name!r} with its background image: {why}. "
                     "Choose another picture or remove the image, then publish again.")
#: ACL-2579: the one-field read of an existing form's background (preflight).
COVER_ONLY_FIELDS = "id,context_card{cover_photo{id}}"
MSG_COVER_UNCONFIRMED = "the background image could not be confirmed"
#: ACL-2579: a form of this exact name is already on the Page but Meta shows it without the template's background.
MSG_COVER_MISSING_ON_EXISTING = (
    "A form named {name!r} is already on this Page, but it has no background image (or another one), while the "
    "template has a background. The robot will not reuse it. Archive that form in Ads Manager (or change the "
    "template), then publish again.")
#: ACL-2571: the picture of a shared template is a file on one computer; each Page gets its own upload at publish.
MSG_COVER_ON_OTHER_COMPUTER = ("the background picture of form template {name!r} is on another computer, so this form "
                               "is created without a background. Choose the picture again in the template on this "
                               "computer to use it")
MSG_COVER_FILE_LOST = ("The background picture of form template {name!r} is no longer on this computer. Choose it again "
                       "in the template. Nothing was created.")
MSG_COVER_UPLOAD_REFUSED = ("The background picture of form template {name!r} could not be uploaded to the Page: "
                            "{why}. Nothing was created; fix this or remove the picture from the template.")

MSG_TERMS_STOP = ("This Page has not accepted the Lead Ads terms, so the robot can't create a form. "
                  "Accept the terms for this Page in Meta (the robot never accepts them), then publish again")
MSG_NO_PERMISSION = ("The captured Ads Manager session can't create forms on this Page "
                     "(the pages_manage_ads permission is missing). The robot does not ask for or store other "
                     "tokens: create the form in Meta once and choose it with 'Use existing form'")
MSG_REFUSED = ("Meta refused to create the form: {why}. The captured Ads Manager session may not be allowed "
               "to create forms on this Page; the robot does not ask for or store other tokens. "
               "Create the form in Meta once and choose it with 'Use existing form'")


@dataclass
class FormStep:
    """Что с шаблоном будет/было на Странице."""

    template_id: str
    template_name: str
    form_name: str
    existing_id: str = ""      # найдена ACTIVE-форма с точным именем
    form_id: str = ""          # id после создания/нахождения

    @property
    def action(self) -> str:
        return "reuse" if self.existing_id else "create"

    def as_dict(self) -> dict[str, Any]:
        return {"template": self.template_name, "form_name": self.form_name, "action": self.action,
                "form_id": self.existing_id or self.form_id}


def match_existing(read: _forms.FormsRead, tpl: dict[str, Any]) -> tuple[str, str]:
    """Своя форма по ТОЧНОМУ имени -> (id ACTIVE-формы, причина отказа).

    Форма с таким именем, но не ACTIVE (архив), не переиспользуется и не
    дублируется: человек сам решает (вернуть форму или поменять шаблон).
    """
    name = ft.form_name(tpl)
    same = [f for f in read.forms if f.name == name]
    active = sorted((f for f in same if f.usable), key=lambda f: f.id)
    if active:
        return active[0].id, ""
    if same:
        return "", (f"A form named {name!r} already exists on the Page but is "
                    f"{same[0].status.lower() or 'not active'}. Restore it in Meta or change the template "
                    "(a changed template gets a new form name)")
    return "", ""


def compare_form(tpl: dict[str, Any], page_id: str, row: dict[str, Any]) -> list[str]:
    """Прочитанная форма против шаблона: пустой список - всё совпало."""
    bad: list[str] = []
    if not isinstance(row, dict) or not row.get("id"):
        return ["the form could not be read back"]
    if str(row.get("name") or "") != ft.form_name(tpl):
        bad.append("name")
    if str(row.get("status") or "").upper() != _forms.USABLE_STATUS:
        bad.append(f"status is {row.get('status')!r}, expected ACTIVE")
    page = row.get("page")
    if str((page or {}).get("id") if isinstance(page, dict) else "") != str(page_id):
        bad.append("the form does not belong to this Page")
    got = row.get("questions")
    got = got.get("data") if isinstance(got, dict) else got
    want = ft.expected_questions(tpl)
    got = got if isinstance(got, list) else []
    if len(got) != len(want):
        bad.append(f"questions: expected {len(want)}, found {len(got)}")
    else:
        for i, (w, g) in enumerate(zip(want, got), start=1):
            if str(g.get("type") or "").upper() != w["type"]:
                bad.append(f"question {i}: type")
            elif w["type"] == "CUSTOM":
                if str(g.get("label") or "") != w["label"]:
                    bad.append(f"question {i}: text")
                wo = [o["value"] for o in w.get("options", [])]
                go = g.get("options")
                go = [str(o.get("value") if isinstance(o, dict) else o) for o in (go or [])]
                if wo != go:
                    bad.append(f"question {i}: options")
    return bad


def readback_fields(tpl: dict[str, Any] | None = None) -> str:
    """Поля чтения созданной формы. Без картинки - прежняя строка байт в байт.

    Неизвестное поле роняет всё чтение, поэтому константу не расширяем: `context_card` просим только у
    шаблона с картинкой.
    """
    if tpl and tpl.get("cover_photo_id"):
        return READBACK_FIELDS + ",context_card{cover_photo{id}}"
    return READBACK_FIELDS


def compare_cover(tpl: dict[str, Any], row: dict[str, Any]) -> list[str]:
    """Картинка прочитанной формы против шаблона (отдельно от `compare_form`)."""
    want = str(tpl.get("cover_photo_id") or "")
    if not want:
        return []
    card = row.get("context_card") if isinstance(row, dict) else None
    photo = card.get("cover_photo") if isinstance(card, dict) else None
    got = str(photo.get("id") or "") if isinstance(photo, dict) else ""
    return [] if got == want else ["background image"]


def existing_cover_state(tpl: dict[str, Any], row: Any, want: str = "") -> str:
    """Cover of an EXISTING same-name form against the template: "ok" | "missing" | "different" | "unknown".

    `want` is the Page photo id the template resolves to (empty when only "some picture" is expected, as for a
    local picture that is not uploaded to this Page yet). "unknown" = the row could not be read: the caller keeps
    the old behaviour (reuse, with a note).
    """
    if not isinstance(row, dict) or not row.get("id"):
        return "unknown"
    card = row.get("context_card")
    photo = card.get("cover_photo") if isinstance(card, dict) else None
    got = str(photo.get("id") or "") if isinstance(photo, dict) else ""
    if not got:
        return "missing"
    return "different" if want and got != want else "ok"


def refused_message(exc: Exception, tpl: dict[str, Any] | None = None) -> str:
    """Текст человеку, когда Meta отказала в создании формы."""
    text = str(exc)
    if _forms.mentions_terms(text):
        return MSG_TERMS_STOP
    if tpl and tpl.get("cover_photo_id"):
        return MSG_COVER_REFUSED.format(form_name=ft.form_name(tpl), why=text[:300])
    return MSG_REFUSED.format(why=text[:300])


# ───────────────── «форма ещё появляется»: метка после неизвестного результата ─────────────────
#
# Запись формы оборвалась (`WriteUnknown`), список форм её ещё не показывает. Повторный
# запуск за это время создал бы вторую форму: помним (кабинет, Страница, имя, время) локально.

PENDING_WINDOW_S = 15 * 60
MSG_NO_STATE = ("Couldn't record the form state locally - not creating a form. "
                "Check that the ACL data folder is writable, then run again")
MSG_PENDING = ("A form with this name may still be appearing at Meta - wait a few minutes and run again")


def pending_path() -> Path:
    from config.paths import get_local_config_dir

    return Path(get_local_config_dir()) / "pending_forms.json"


def _pending_key(act: str, page_id: str, form_name: str) -> str:
    return "|".join((str(act), str(page_id), str(form_name)))


def _pending_load() -> dict[str, float]:
    try:
        data = json.loads(pending_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): float(v) for k, v in data.items()
            if isinstance(data, dict) and isinstance(v, (int, float))} if isinstance(data, dict) else {}


def _pending_save(data: dict[str, float]) -> None:
    """Атомарно: временный файл в той же папке, затем `os.replace`. Сбой - `OSError` наружу."""
    path = pending_path()
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        log.error("pending form marker was not saved (%s): %s", path, exc)
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def record_pending(act: str, page_id: str, form_name: str) -> None:
    """Записать метку ДО отправки формы. `OSError` - метку сохранить не удалось: формы не создаём."""
    now = time.time()
    data = {k: v for k, v in _pending_load().items() if now - v < PENDING_WINDOW_S}
    data[_pending_key(act, page_id, form_name)] = now
    _pending_save(data)


def is_pending(act: str, page_id: str, form_name: str) -> bool:
    ts = _pending_load().get(_pending_key(act, page_id, form_name))
    return ts is not None and 0 <= time.time() - ts < PENDING_WINDOW_S


def clear_pending(act: str, page_id: str, form_name: str) -> None:
    data = _pending_load()
    if data.pop(_pending_key(act, page_id, form_name), None) is not None:
        try:
            _pending_save(data)
        except OSError:
            pass  # устаревшая метка лишь подождёт до конца окна; форма найдётся и сотрёт её
