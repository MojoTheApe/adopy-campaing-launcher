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

ACL-1441. Токен выдаётся в выбранном рабочем пространстве, а не в
пространстве логина по умолчанию. Поля API сверены с документацией
Multilogin X (Postman, collection 28533318/2s946h9Cv9, 2026-10-01):
`GET /user/workspaces` → data.workspaces[] {workspace_id, name, role};
`POST /user/refresh_token` {email, refresh_token, workspace_id} →
data.token, data.refresh_token; `GET /workspace/restrictions` →
data.automation_available, data.plan_name.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

#: Адрес API Multilogin X. Тот же, что у дешборда, — на то и общий модуль.
MLX_API = "https://api.multilogin.com"

#: Cloudflare перед api.multilogin.com отвечает «error 1010» на подпись
#: python-requests — поэтому обычный браузерный User-Agent на каждом запросе.
_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 ACL")

_HEADERS = {"Accept": "application/json", "Content-Type": "application/json",
            "User-Agent": _USER_AGENT}

#: ACL-1441. Тексты — дословно из задачи: человек должен понять, что делать.
PLAN_HAS_NO_AUTOMATION = ("This workspace's plan has no automation — "
                          "choose the workspace with your profiles")
ROLE_CANNOT_ISSUE = ("Your role in this workspace cannot issue tokens — "
                     "ask the workspace owner")


@dataclass
class SignIn:
    """Чем кончился вход. Пароля здесь нет и не будет (и refresh_token тоже:
    он живёт только внутри одного вызова `sign_in`)."""

    ok: bool = False
    #: Токен автоматизации — то, ради чего всё затевалось.
    automation_token: str = ""
    #: Токен сессии — уже в выбранном пространстве. Им читают папки и
    #: профили, но запускать их — нет.
    bearer: str = ""
    #: Почему не вошли.
    error: str = ""
    #: Вошли, но токен автоматизации не выдали. Это разные беды — см. ниже.
    token_error: str = ""
    folders: list = field(default_factory=list)
    #: ACL-1441: в каком пространстве выдан токен.
    workspace_id: str = ""
    workspace_name: str = ""
    workspace_role: str = ""
    #: Пространства логина: {workspace_id, name, role, automation: bool|None}.
    workspaces: list = field(default_factory=list)
    #: Пространств с автоматизацией несколько, и какое нужно — решает человек.
    needs_choice: bool = False

    @property
    def working_token(self) -> str:
        return self.automation_token or self.bearer


def sign_in(email: str, password: str, *, workspace_id: str = "",
            remembered_workspace: str = "", requests_module=None,
            timeout: int = 20) -> SignIn:
    """Почта и пароль -> токен автоматизации в нужном рабочем пространстве.

    1. `POST /user/signin` — вход. Пароль Multilogin требует **хешированным
       MD5**, так написано в их документации; это их формат, а не наша защита.
       Ответ: data.token (сессия в пространстве ПО УМОЛЧАНИЮ) и
       data.refresh_token.
    2. `GET /user/workspaces` — список пространств логина. ACL-1441:
       пространство по умолчанию бывает своим пустым Free без автоматизации
       (ndays6443, 2026-10-01) — выдача токена в нём кончается
       «403 Request is not allowed», хотя в командном пространстве с
       профилями тот же логин токен получает.
    3. Выбор: явный `workspace_id` → запомненное слотом (по id или имени) →
       единственное → единственное, где тариф допускает автоматизацию
       (`/workspace/restrictions`). Иначе — `needs_choice`: токен не
       выпускается, человек выбирает и присылает `workspace_id`.
    4. `POST /user/refresh_token` — переключение в выбранное пространство.
    5. `GET /workspace/automation_token?expiration_period=1mo` уже в нём.

    Вход и право выдать токен — разные вещи, и различие обязано дойти до
    человека: без токена автоматизации робот не откроет ни одного профиля.
    Если список пространств не получен — по-старому, в пространстве по
    умолчанию: лучше прежнее поведение, чем отказ на ровном месте.
    """
    email = (email or "").strip()
    password = (password or "").strip()
    if not email or not password:
        return SignIn(error="Enter the Multilogin email and password.")

    req = requests_module
    if req is None:  # pragma: no cover - в тестах модуль подставляют
        import requests as req  # type: ignore

    password_hash = hashlib.md5(password.encode("utf-8")).hexdigest()
    password = ""
    try:
        r = req.post(f"{MLX_API}/user/signin", headers=_HEADERS,
                     json={"email": email, "password": password_hash}, timeout=timeout)
    except Exception as exc:
        return SignIn(error=f"Could not reach Multilogin: {exc}")
    finally:
        password_hash = ""

    if getattr(r, "status_code", 0) != 200:
        return SignIn(error=f"Sign-in failed [{r.status_code}]: {_message(r)}")

    data = _data(r)
    bearer = str(data.get("token") or "").strip()
    refresh = str(data.get("refresh_token") or "").strip()
    if not bearer:
        return SignIn(error="Multilogin signed in but returned no token.")

    out = SignIn(ok=True, bearer=bearer)
    workspaces = _list_workspaces(req, bearer, timeout)
    if not workspaces or not refresh:
        # Нет списка — нечего выбирать: токен в пространстве по умолчанию.
        _issue(req, out, bearer, timeout, automation=None)
        return out

    session = _Session(req, email, refresh, timeout)
    chosen = _pick(workspaces, workspace_id, remembered_workspace)
    if workspace_id and chosen is None:
        out.workspaces = workspaces
        out.needs_choice = True
        out.token_error = "That workspace is no longer in this login's list — choose again."
        return out

    if chosen is None:
        # Несколько пространств и ни одно не названо: прощупать тарифы.
        # Токены здесь не выпускаются — только читаются ограничения.
        for ws in workspaces:
            ws_bearer, _ = session.switch(ws["workspace_id"])
            ws["automation"] = _automation_available(req, ws_bearer, timeout) if ws_bearer else None
        usable = [w for w in workspaces if w["automation"] is not False]
        if len(usable) == 1:
            chosen = usable[0]
        else:
            out.workspaces = workspaces
            out.needs_choice = True
            return out

    if len(workspaces) > 1:
        # Остаётся в ответе: если с выбранным не вышло, выбрать другое.
        out.workspaces = workspaces
    out.workspace_id = chosen["workspace_id"]
    out.workspace_name = chosen["name"]
    out.workspace_role = chosen["role"]

    if chosen.get("automation") is False:
        out.token_error = f"{PLAN_HAS_NO_AUTOMATION} (workspace '{chosen['name']}')."
        return out

    ws_bearer, switch_error = session.switch(chosen["workspace_id"])
    if not ws_bearer:
        out.token_error = f"Could not switch to the workspace '{chosen['name']}'{switch_error}"
        return out
    out.bearer = ws_bearer
    _issue(req, out, ws_bearer, timeout, automation=chosen.get("automation"))
    return out


class _Session:
    """Переключение пространств. refresh_token приходит новый на каждом
    обновлении, поэтому хранится всегда последний — и только здесь."""

    def __init__(self, req, email: str, refresh: str, timeout: int) -> None:
        self._req, self._email, self._refresh, self._timeout = req, email, refresh, timeout

    def switch(self, workspace_id: str) -> tuple[str, str]:
        """(сессия в пространстве, "") или ("", почему не вышло)."""
        try:
            r = self._req.post(f"{MLX_API}/user/refresh_token", headers=_HEADERS,
                               json={"email": self._email, "refresh_token": self._refresh,
                                     "workspace_id": workspace_id},
                               timeout=self._timeout)
        except Exception as exc:
            return "", f": could not reach Multilogin: {exc}"
        if getattr(r, "status_code", 0) != 200:
            detail = _message(r)
            return "", f" [{r.status_code}]" + (f": {detail}" if detail else "")
        data = _data(r)
        token = str(data.get("token") or "").strip()
        self._refresh = str(data.get("refresh_token") or "").strip() or self._refresh
        if not token:
            return "", ": Multilogin returned no session for it."
        return token, ""


def _pick(workspaces: list, workspace_id: str, remembered: str):
    if workspace_id:
        return next((w for w in workspaces if w["workspace_id"] == workspace_id), None)
    remembered = (remembered or "").strip().casefold()
    if remembered:
        hits = [w for w in workspaces
                if remembered in (w["workspace_id"].casefold(), w["name"].casefold())]
        if len(hits) == 1:
            return hits[0]
    if len(workspaces) == 1:
        return workspaces[0]
    return None


def _list_workspaces(req, bearer: str, timeout: int) -> list:
    try:
        r = req.get(f"{MLX_API}/user/workspaces", headers=_auth(bearer), timeout=timeout)
    except Exception:
        return []
    if getattr(r, "status_code", 0) != 200:
        return []
    raw = _data(r).get("workspaces")
    out = []
    for w in raw if isinstance(raw, list) else []:
        if not isinstance(w, dict) or not w.get("workspace_id"):
            continue
        out.append({"workspace_id": str(w["workspace_id"]),
                    "name": str(w.get("name") or w["workspace_id"]),
                    "role": str(w.get("role") or ""),
                    "automation": None})
    return out


def _automation_available(req, bearer: str, timeout: int):
    """True/False из `/workspace/restrictions`, None — если не узнали."""
    try:
        r = req.get(f"{MLX_API}/workspace/restrictions", headers=_auth(bearer), timeout=timeout)
    except Exception:
        return None
    if getattr(r, "status_code", 0) != 200:
        return None
    value = _data(r).get("automation_available")
    return value if isinstance(value, bool) else None


def _issue(req, out: SignIn, bearer: str, timeout: int, *, automation) -> None:
    """Выпустить токен автоматизации там, где находится сессия `bearer`."""
    try:
        r2 = req.get(f"{MLX_API}/workspace/automation_token?expiration_period=1mo",
                     headers=_auth(bearer), timeout=timeout)
    except Exception as exc:
        out.token_error = f"Could not reach Multilogin for the automation token: {exc}"
        return

    if getattr(r2, "status_code", 0) == 200:
        data = _data(r2)
        out.automation_token = str(data.get("token") or data.get("automation_token") or "").strip()
        if not out.automation_token:
            out.token_error = "Multilogin returned no automation token for this account."
        return

    detail = _message(r2)
    if r2.status_code == 403 and out.workspace_id:
        # 403 бывает по двум причинам: тариф без автоматизации или роль без
        # права. Различает их только /workspace/restrictions.
        if automation is None:
            automation = _automation_available(req, bearer, timeout)
        reason = PLAN_HAS_NO_AUTOMATION if automation is False else ROLE_CANNOT_ISSUE
        out.token_error = f"{reason} (workspace '{out.workspace_name}')."
        if detail:
            out.token_error += f" Multilogin: {detail}"
        return
    out.token_error = f"Multilogin refused to issue an automation token [{r2.status_code}]"
    if detail:
        out.token_error += f": {detail}"


def _auth(bearer: str) -> dict:
    return {**_HEADERS, "Authorization": f"Bearer {bearer}"}


def _data(response) -> dict:
    try:
        data = response.json().get("data")
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


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
