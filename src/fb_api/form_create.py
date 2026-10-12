"""Создание Instant Form из шаблона: чистые куски (ACL-1776).

Ни сети, ни записи здесь нет: поиск своей формы по точному имени, сверка
созданной формы с шаблоном, понятные тексты отказов. Саму запись делает
`CampaignCreator` (`_write` -> `GraphClient.post(form_page=...)`) под отдельной
цепочкой политики `write_policy.check_form_create`.

Условия Lead Ads робот не принимает НИКОГДА: Страница без принятых условий -
остановка с просьбой к человеку принять их в Meta.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.fb_api import form_templates as ft
from src.fb_api import forms as _forms

#: Поля чтения созданной формы для сверки.
READBACK_FIELDS = "id,name,status,locale,page{id},questions{key,label,type,options}"

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


def refused_message(exc: Exception) -> str:
    """Текст человеку, когда Meta отказала в создании формы."""
    text = str(exc)
    if _forms.mentions_terms(text):
        return MSG_TERMS_STOP
    return MSG_REFUSED.format(why=text[:300])
