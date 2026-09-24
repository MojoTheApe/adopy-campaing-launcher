# -*- coding: utf-8 -*-
"""Вход в Multilogin по почте и паролю — одним местом на дешборд и установку.

ACL-1303. Мастер установки просил «Multilogin access code» — токен, которого
у человека нет и взять неоткуда: их никому не выдают. При этом дешборд уже
умеет ровно то, что нужно: спросить почту и пароль, получить у Multilogin
токен автоматизации и сохранить **только его**. Владелец, настраивавший всё
сам, ни разу не вводил токен руками — поэтому дыру и увидели только тогда,
когда прошли установку с чистого листа (24.09).

Почему отдельный модуль, а не вызов дешборда из мастера: мастер работает
**до** того, как дешборд вообще существует. А держать две копии одного входа
— значит однажды починить одну и забыть вторую; подробность про MD5 и адрес
их API должны жить в одном файле.

Пароль не сохраняется никуда: он нужен ровно на один запрос.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

#: Адрес API Multilogin X. Тот же, что у дешборда, — на то и общий модуль.
MLX_API = "https://api.multilogin.com"

_HEADERS = {"Accept": "application/json", "Content-Type": "application/json"}


@dataclass
class SignIn:
    """Чем кончился вход. Пароля здесь нет и не будет."""

    ok: bool = False
    #: Токен автоматизации — то, ради чего всё затевалось.
    automation_token: str = ""
    #: Токен сессии. Им можно читать профили, но запускать их — нет.
    bearer: str = ""
    #: Почему не вошли.
    error: str = ""
    #: Вошли, но токен автоматизации не выдали. Это разные беды — см. ниже.
    token_error: str = ""
    folders: list = field(default_factory=list)

    @property
    def working_token(self) -> str:
        return self.automation_token or self.bearer


def sign_in(email: str, password: str, *, requests_module=None, timeout: int = 20) -> SignIn:
    """Почта и пароль -> токен автоматизации.

    Две ступени, и они отвечают за разное:

    1. `/user/signin` — вход. Пароль Multilogin требует **хешированным MD5**,
       так написано в их документации; это их формат, а не наша защита.
    2. `/workspace/automation_token` — выдача токена автоматизации. Права на
       это есть только у владельца рабочего пространства: правильный пароль
       участника команды проходит первую ступень и упирается во вторую.

    Различие обязано дойти до человека. Без токена автоматизации робот не
    откроет ни одного профиля, и «вошли» без токена — это то же самое, что
    не вошли, только выглядит успехом.
    """
    email = (email or "").strip()
    password = (password or "").strip()
    if not email or not password:
        return SignIn(error="Enter the Multilogin email and password.")

    req = requests_module
    if req is None:  # pragma: no cover - в тестах модуль подставляют
        import requests as req  # type: ignore

    password_hash = hashlib.md5(password.encode("utf-8")).hexdigest()
    try:
        r = req.post(f"{MLX_API}/user/signin", headers=_HEADERS,
                     json={"email": email, "password": password_hash}, timeout=timeout)
    except Exception as exc:
        return SignIn(error=f"Could not reach Multilogin: {exc}")

    if getattr(r, "status_code", 0) != 200:
        return SignIn(error=f"Sign-in failed [{r.status_code}]: {_message(r)}")

    try:
        bearer = ((r.json().get("data") or {}).get("token") or "").strip()
    except Exception:
        bearer = ""
    if not bearer:
        return SignIn(error="Multilogin signed in but returned no token.")

    out = SignIn(ok=True, bearer=bearer)
    auth = {**_HEADERS, "Authorization": f"Bearer {bearer}"}
    try:
        r2 = req.get(f"{MLX_API}/workspace/automation_token?expiration_period=1mo",
                     headers=auth, timeout=timeout)
    except Exception as exc:
        out.token_error = f"Could not reach Multilogin for the automation token: {exc}"
        return out

    if getattr(r2, "status_code", 0) == 200:
        try:
            data = r2.json().get("data") or {}
        except Exception:
            data = {}
        out.automation_token = (data.get("token") or data.get("automation_token") or "").strip()
        if not out.automation_token:
            out.token_error = "Multilogin returned no automation token for this account."
    else:
        detail = _message(r2)
        out.token_error = f"Multilogin refused to issue an automation token [{r2.status_code}]"
        if detail:
            out.token_error += f": {detail}"
    return out


def _message(response) -> str:
    """Что ответил Multilogin, его же словами."""
    try:
        body = response.json()
        message = ((body.get("status") or {}).get("message") or "").strip()
        if message:
            return message
    except Exception:
        pass
    return (getattr(response, "text", "") or "")[:200]
