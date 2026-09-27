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


# ── Вход сессией, без токена автоматизации (ACL-1335) ─────────────────────
#
# 27.09 Multilogin перестал выдавать токены автоматизации кому бы то ни было:
# `/workspace/automation_token` отвечает 403 FORBIDDEN_REQUEST даже владельцу.
# А выданный на одной машине токен на другой профили не запускает («can't lock
# profile»). Зато обычный вход работает, `/user/workspaces` перечисляет
# рабочие пространства с ролями, а `/user/refresh_token` переключает
# пространство и выдаёт свежий токен сессии. На этом и строится подключение:
# каждая машина входит сама и продлевает вход сама.

#: Роли, которым Multilogin разрешает запускать профили. Пустая роль — «не
#: сказали»: не отказываем, пусть решит сам запуск.
LAUNCH_ROLES = frozenset({"owner", "admin", "manager", "launcher"})


@dataclass
class Session:
    """Токен сессии и чем его продлевать. Пароля здесь нет и не будет."""

    ok: bool = False
    email: str = ""
    bearer: str = ""
    refresh_token: str = ""
    error: str = ""
    #: Multilogin сказал «нет» (а не сеть подвела): продлевать тем же
    #: refresh-токеном бессмысленно, нужен новый вход.
    rejected: bool = False


@dataclass
class Workspace:
    workspace_id: str
    name: str = ""
    role: str = ""

    @property
    def can_launch(self) -> bool:
        return not self.role or self.role in LAUNCH_ROLES

    def as_dict(self) -> dict:
        return {"workspace_id": self.workspace_id, "name": self.name,
                "role": self.role, "can_launch": self.can_launch}


def _http(requests_module):
    if requests_module is not None:
        return requests_module
    import requests  # type: ignore  # pragma: no cover - в тестах модуль подставляют

    return requests  # pragma: no cover


def _tokens(response) -> tuple[str, str]:
    """(token, refresh_token) из ответа Multilogin; пустые строки, если нет."""
    try:
        data = response.json().get("data") or {}
    except Exception:
        return "", ""
    if not isinstance(data, dict):
        return "", ""
    token = data.get("token") or data.get("access_token") or ""
    refresh = data.get("refresh_token") or data.get("refreshToken") or ""
    return (token.strip() if isinstance(token, str) else "",
            refresh.strip() if isinstance(refresh, str) else "")


def open_session(email: str, password: str, *, requests_module=None, timeout: int = 20) -> Session:
    """Почта и пароль -> токен сессии и refresh-токен.

    Та же ступень 1, что в `sign_in`, но без просьбы о токене автоматизации:
    его больше не выдают (27.09 — 403 всем).
    """
    email = (email or "").strip()
    password = (password or "").strip()
    if not email or not password:
        return Session(error="Enter the Multilogin email and password.")
    req = _http(requests_module)
    password_hash = hashlib.md5(password.encode("utf-8")).hexdigest()
    try:
        r = req.post(f"{MLX_API}/user/signin", headers=_HEADERS,
                     json={"email": email, "password": password_hash}, timeout=timeout)
    except Exception as exc:
        return Session(email=email, error=f"Could not reach Multilogin: {exc}")
    if getattr(r, "status_code", 0) != 200:
        return Session(email=email, rejected=True,
                       error=f"Sign-in failed [{r.status_code}]: {_message(r)}")
    bearer, refresh = _tokens(r)
    if not bearer:
        return Session(email=email, error="Multilogin signed in but returned no token.")
    if not refresh:
        return Session(email=email, error="Multilogin signed in but returned no refresh token, "
                                          "so the sign-in could not renew itself.")
    return Session(ok=True, email=email, bearer=bearer, refresh_token=refresh)


def _role(value) -> str:
    if isinstance(value, dict):
        value = value.get("name") or value.get("role") or ""
    return str(value or "").strip().lower()


def list_workspaces(bearer: str, *, requests_module=None, timeout: int = 20) -> tuple[list[Workspace], str]:
    """Рабочие пространства входа с ролями: (список, ошибка).

    Форму ответа разбираем терпимо: `data` списком или словарём со списком
    внутри, id под любым из известных имён. Запись без id пропускаем —
    переключиться на неё нельзя.
    """
    req = _http(requests_module)
    try:
        r = req.get(f"{MLX_API}/user/workspaces",
                    headers={**_HEADERS, "Authorization": f"Bearer {bearer}"}, timeout=timeout)
    except Exception as exc:
        return [], f"Could not reach Multilogin: {exc}"
    if getattr(r, "status_code", 0) != 200:
        return [], f"Multilogin did not list the workspaces [{r.status_code}]: {_message(r)}"
    try:
        data = r.json().get("data")
    except Exception:
        data = None
    if isinstance(data, dict):
        data = data.get("workspaces") or data.get("items") or data.get("data") or []
    out: list[Workspace] = []
    seen: set[str] = set()
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        ws = item.get("workspace") if isinstance(item.get("workspace"), dict) else {}
        wid = (item.get("workspace_id") or item.get("workspaceId") or item.get("uuid")
               or item.get("id") or ws.get("workspace_id") or ws.get("id") or "")
        wid = str(wid).strip()
        if not wid or wid in seen:
            continue
        seen.add(wid)
        name = (item.get("name") or item.get("workspace_name") or item.get("workspaceName")
                or ws.get("name") or "")
        role = _role(item.get("role") or item.get("user_role") or item.get("role_name")
                     or item.get("userRole") or ws.get("role"))
        out.append(Workspace(workspace_id=wid, name=str(name).strip(), role=role))
    if not out:
        return [], "Multilogin lists no workspaces for this account."
    return out, ""


def refresh_session(email: str, refresh_token: str, workspace_id: str, *,
                    requests_module=None, timeout: int = 20) -> Session:
    """Продлить вход (и заодно переключиться на `workspace_id`).

    Ответ несёт новый токен сессии и, возможно, новый refresh-токен: Multilogin
    вправе их вращать. Не прислал нового — продолжаем старым.
    """
    email = (email or "").strip()
    if not (email and refresh_token and workspace_id):
        return Session(email=email, rejected=True,
                       error="The saved Multilogin sign-in is incomplete.")
    req = _http(requests_module)
    try:
        r = req.post(f"{MLX_API}/user/refresh_token", headers=_HEADERS,
                     json={"email": email, "refresh_token": refresh_token,
                           "workspace_id": workspace_id}, timeout=timeout)
    except Exception as exc:
        return Session(email=email, error=f"Could not reach Multilogin: {exc}")
    status = getattr(r, "status_code", 0)
    if status != 200:
        # 5xx и 429 — беда Multilogin, а не входа: вход не объявляем мёртвым.
        return Session(email=email, rejected=status in (400, 401, 403, 404),
                       error=f"Multilogin refused to renew the sign-in [{status}]: {_message(r)}")
    bearer, rotated = _tokens(r)
    if not bearer:
        return Session(email=email, error="Multilogin renewed the sign-in but returned no token.")
    return Session(ok=True, email=email, bearer=bearer, refresh_token=rotated or refresh_token)


def token_expiry(token: str) -> float | None:
    """Срок токена из его `exp` (токены Multilogin — JWT), без сети."""
    import base64
    import json as _json

    parts = (token or "").split(".")
    if len(parts) != 3:
        return None
    try:
        raw = parts[1] + "=" * (-len(parts[1]) % 4)
        exp = _json.loads(base64.urlsafe_b64decode(raw)).get("exp")
    except Exception:
        return None
    return float(exp) if isinstance(exp, (int, float)) else None
