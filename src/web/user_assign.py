"""ACL-2900: назначение аккаунтов пользователю через хаб (Cloudflare Worker acl-hub).

Владелец вставляет столбец имён, номеров кабинетов или ссылок Ads Manager;
здесь каждая строка превращается в аккаунт из его карты (или из общего списка
аккаунтов) и уходит в хаб одной пачкой. Ключ владельца живёт только на сервере
дешборда: браузер говорит с локальными маршрутами, а они — с хабом.

Модуль не знает ни про FastAPI, ни про сеть: HTTP-вызов передаётся снаружи
(`request`), поэтому тесты обходятся без сети.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

#: Поля строки, которые хаб принимает. Всё остальное (proxy_*, tracking_link) — 400.
ROW_FIELDS = (
    "account_name", "ad_account_id", "page_id", "page_name", "country",
    "multilogin_instance", "folder_id", "profile_id",
)

USER_AGENT = "acl-dashboard/1"
DISABLED_MESSAGE = "Assigning accounts to users is not available on this computer."

#: (method, url, body | None, headers) -> (status, json-словарь)
Request = Callable[[str, str, "bytes | None", dict], "tuple[int, dict]"]


class HubError(Exception):
    """Ошибка хаба, уже по-английски и пригодная для показа как есть. `status` — HTTP-код хаба (0 — не было ответа)."""

    def __init__(self, message: str = "", status: int = 0):
        super().__init__(message)
        self.status = status


# ── настройки ────────────────────────────────────────────────────────────

def owner_key() -> str:
    return (os.environ.get("ACL_HUB_OWNER_KEY") or "").strip()


from src.web.user_accounts import hub_url  # noqa: E402  — общий адрес хаба из ACL-2899


def enabled() -> bool:
    """Управление назначениями видно только там, где есть ключ владельца."""
    return bool(owner_key())


# ── хаб ──────────────────────────────────────────────────────────────────

def default_request(method: str, url: str, body: "bytes | None", headers: dict) -> "tuple[int, dict]":
    # Без своего User-Agent Cloudflare режет Python-urllib (403, код 1010).
    req = urllib.request.Request(url, data=body, method=method,
                                 headers={"User-Agent": USER_AGENT, **headers})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except Exception:
            return exc.code, {}


class HubClient:
    def __init__(self, base: str = "", key: str = "", request: "Request | None" = None):
        self.base = (base or hub_url()).rstrip("/")
        self.key = key or owner_key()
        self._request = request or default_request

    def _call(self, method: str, path: str, payload: "dict | None" = None) -> dict:
        if not self.base:
            raise HubError("ACL_HUB_URL is not set on this computer.")
        headers = {"Authorization": f"Bearer {self.key}"}
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            status, data = self._request(method, self.base + path, body, headers)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise HubError(f"Could not reach the hub ({type(exc).__name__}). Check the connection and try again.")
        data = data if isinstance(data, dict) else {}
        if status in (401, 403):
            raise HubError(f"The hub refused the owner key (HTTP {status}). Check ACL_HUB_OWNER_KEY on this computer.",
                           status)
        if status != 200:
            detail = str(data.get("error") or "").strip()
            raise HubError(f"The hub answered HTTP {status}" + (f": {detail}" if detail else "."), status)
        return data

    def users(self, name: str = "") -> list[dict]:
        path = "/api/users" + ("?name=" + urllib.parse.quote(name, safe="") if name else "")
        users = self._call("GET", path).get("users")
        return [u for u in (users or []) if isinstance(u, dict)]

    def assignments(self, user_email: str) -> list[dict]:
        """ACL-2920: строки назначений юзера, активные и снятые (у снятых removed_at)."""
        data = self._call("GET", "/api/assignments?user_email=" + urllib.parse.quote(user_email, safe=""))
        return [a for a in (data.get("accounts") or []) if isinstance(a, dict)]

    def assign(self, user_email: str, rows: list[dict], user_name: str = "") -> dict:
        payload: dict = {"user_email": user_email, "rows": rows}
        if user_name.strip():
            payload["user_name"] = user_name.strip()
        return self._call("POST", "/api/assignments", payload)

    def remove(self, user_email: str, account_names: list[str]) -> dict:
        return self._call("POST", "/api/assignments/remove",
                          {"user_email": user_email, "account_names": account_names})


# ── разбор строк ─────────────────────────────────────────────────────────

def parse_lines(text: str) -> list[str]:
    """Непустые строки столбца, без пробелов по краям."""
    return [ln.strip() for ln in (text or "").splitlines() if ln.strip()]


_ID_LIKE = re.compile(r"^(?:act[_=]\s*)?\d+$", re.I)


def looks_like_id(line: str) -> bool:
    """Ссылка, act_123 или голые цифры — но не имя, в котором случайно есть цифры."""
    t = line.strip()
    if re.match(r"^https?://", t, re.I) or re.search(r"[?&]act=\d", t) or "facebook.com" in t.lower():
        return True
    return bool(_ID_LIKE.match(t))


def _entry_row(name: str, entry: Any) -> dict:
    """Строка для хаба: только разрешённые поля, без прокси и tracking_link."""
    def get(attr: str) -> str:
        return str(getattr(entry, attr, "") or "").strip() if entry is not None else ""

    row = {k: get(k) for k in ROW_FIELDS if k != "account_name"}
    row["account_name"] = name
    return {k: row[k] for k in ROW_FIELDS}


# ── ACL-2945: «название, код страны» для аккаунта, которого нет в карте ─────────

def _country_name_table() -> dict[str, str]:
    """Двухбуквенный код → название страны (первое из имён в справочнике дашборда)."""
    from src.analysis_agent.country_names import COUNTRY_CODES

    out: dict[str, str] = {}
    for name, code in COUNTRY_CODES.items():
        out.setdefault(code, name.title())
    return out


def country_name_for(code: str, account_map: Mapping[str, Any]) -> str:
    """Название страны по коду. Предпочитаем написание, которое карта уже использует (Saudi Arabia, а не SAUDI ARABIA)."""
    from src.analysis_agent.country_names import COUNTRY_CODES

    code = (code or "").strip().upper()
    if len(code) != 2 or not code.isalpha():
        return ""
    for entry in account_map.values():
        used = str(getattr(entry, "country", "") or "").strip()
        if used and COUNTRY_CODES.get(used.lower()) == code:
            return used
    return _country_name_table().get(code, "")


def parse_add_line(line: str) -> "dict | None":
    """«Название, PE» или «Название, PE, act_123» → {name, code, act}. Без запятой — None (строка как раньше)."""
    parts = [p.strip() for p in (line or "").split(",")]
    parts = [p for p in parts if p]
    if len(parts) < 2:
        return None
    name, code, act = parts[0], "", ""
    for extra in parts[1:]:
        if looks_like_id(extra) and not act:
            act = extra
        elif not code:
            code = extra
        else:
            return None
    return {"name": name, "code": code, "act": act}


def resolve_lines(
    lines: Iterable[str],
    account_map: Mapping[str, Any],
    sheet_labels: Iterable[str] = (),
    *,
    normalise: Callable[[str], str],
) -> list[dict]:
    """Состояние каждой строки: found / not_found / ambiguous / duplicate / clash.

    `account_map` — карта владельца (имя → запись с ad_account_id, profile_id…),
    `sheet_labels` — имена из общего списка аккаунтов (у них нет номеров).
    """
    # Имена: карта первой, затем общий список; регистр не различаем.
    names: dict[str, str] = {}
    for n in list(account_map) + [s for s in sheet_labels if s]:
        n = (n or "").strip()
        if n:
            names.setdefault(n.lower(), n)
    by_act: dict[str, list[str]] = {}
    for n, e in account_map.items():
        act = str(getattr(e, "ad_account_id", "") or "").strip()
        if act:
            by_act.setdefault(act, []).append(n)

    def match(text: str) -> "tuple[list[str], bool, str]":
        """(кандидаты, найдено_по_части_имени, нормализованный номер кабинета)."""
        if looks_like_id(text):
            act = normalise(text)
            return (sorted(by_act.get(act, [])) if act else []), False, act
        low = text.lower()
        if low in names:
            return [names[low]], False, ""
        if len(low) >= 4:
            # Неточное совпадение: часть имени. Одно попадание принимаем,
            # несколько — это ambiguous, угадывать нельзя.
            return sorted(v for k, v in names.items() if low in k), True, ""
        return [], False, ""

    out: list[dict] = []
    seen: dict[str, str] = {}
    for line in lines:
        res: dict = {"input": line, "state": "not_found", "account_name": "", "candidates": [], "row": None}
        candidates, partial, act_in = match(line)
        if looks_like_id(line):
            res["ad_account_id"] = act_in
        add = None
        if not candidates:
            add = parse_add_line(line)
            if add:
                # Название из «название, страна» может уже быть в карте — тогда это обычная строка.
                candidates, partial, _ = match(add["name"])
        if len(candidates) > 1:
            res.update(state="ambiguous", candidates=candidates)
        elif len(candidates) == 1:
            name = candidates[0]
            entry = account_map.get(name)
            act = str(getattr(entry, "ad_account_id", "") or "").strip() if entry is not None else ""
            other = [n for n in by_act.get(act, []) if n != name] if act else []
            res["account_name"] = name
            if name.lower() in seen:
                res.update(state="duplicate", duplicate_of=seen[name.lower()])
            elif other:
                # Тот же кабинет записан за другим аккаунтом: как и /account-map-patch, не гадаем.
                res.update(state="clash", candidates=other,
                           error=f"Ad account {act} is also linked to \"{other[0]}\".")
            else:
                res.update(state="found", row=_entry_row(name, entry))
                if partial:
                    # Ревью ACL-2900: найдено по части имени — владелец должен это видеть до отправки
                    res["match"] = "partial"
                seen[name.lower()] = line
        elif add:
            _resolve_new_account(res, add, account_map, by_act, seen, normalise, line)
        out.append(res)
    return out


def _resolve_new_account(res: dict, add: dict, account_map, by_act, seen, normalise, line: str) -> None:
    """Аккаунта нет в карте, строка «название, код страны»: state `add` или понятное «Not found»."""
    code = add["code"].upper()
    country = country_name_for(code, account_map)
    if not country:
        res["error"] = f'Unknown country code "{add["code"]}". Use a 2-letter code such as PE or SA.'
        return
    act = ""
    if add["act"]:
        act = normalise(add["act"])
        if not act:
            res["error"] = f'Could not read the ad account number "{add["act"]}".'
            return
        other = by_act.get(act, [])
        if other:
            res.update(state="clash", candidates=other, account_name=add["name"],
                       error=f'Ad account {act} is also linked to "{other[0]}".')
            return
    name = add["name"]
    res["account_name"] = name
    if name.lower() in seen:
        res.update(state="duplicate", duplicate_of=seen[name.lower()])
        return
    row = {k: "" for k in ROW_FIELDS}
    row.update(account_name=name, ad_account_id=act, country=country)
    res.update(state="add", country=country, row=row)
    seen[name.lower()] = line


def found_rows(results: Iterable[dict]) -> list[dict]:
    """Строки для хаба: найденные в карте и (ACL-2945) добавляемые в неё."""
    return [r["row"] for r in results if r.get("state") in ("found", "add") and r.get("row")]


def clean_email(value: str) -> str:
    email = (value or "").strip().lower()
    return email if "@" in email and " " not in email else ""


# ── ACL-2920: «Logged as» → пользователь хаба ───────────────────────────────

_BUYER_MAP_FILE = "user_assign_buyers.json"


class NeedEmail(Exception):
    """Почту байера надо ввести руками: пользователя в хабе ещё нет, либо имя носят несколько юзеров."""

    def __init__(self, buyer: str = "", ambiguous: bool = False):
        super().__init__(buyer)
        self.buyer = buyer
        self.ambiguous = ambiguous


def _buyer_key(name: str) -> str:
    return " ".join(str(name or "").split()).casefold()


def _buyer_map_path() -> Path:
    from config import paths

    return Path(paths.get_local_config_dir()) / _BUYER_MAP_FILE


def _load_buyer_map() -> dict:
    try:
        data = json.loads(_buyer_map_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def remember_buyer(buyer: str, email: str) -> bool:
    """Запомнить на этой машине «имя байера → почта» (новая почта заменяет прежнюю). Страховка на случай,
    когда имя из Google в хабе отличается от имени в «Logged as» и поиск по имени не находит юзера.
    True — записано."""
    key = _buyer_key(buyer)
    if not key or not clean_email(email):
        return False
    try:
        from config.paths import _atomic_write

        data = _load_buyer_map()
        data[key] = clean_email(email)
        path = _buyer_map_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))
        return True
    except Exception:  # noqa: BLE001 - при назначении запоминание необязательно
        return False


def find_user_email(client: "HubClient", buyer: str, email_hint: str = "") -> str:
    """Почта пользователя хаба для байера из «Logged as».

    Порядок: почта, которую только что ввёл владелец; запомненная на машине; поиск в хабе по имени
    (совпадение без учёта регистра). Нет нигде — `NeedEmail`. Несколько юзеров с одним именем —
    тоже `NeedEmail(ambiguous=True)`: угадывать нельзя, почту вводят руками (ACL-2924), и она запоминается."""
    hint = clean_email(email_hint)
    if hint:
        return hint
    remembered = _load_buyer_map().get(_buyer_key(buyer), "")
    if clean_email(remembered):
        return clean_email(remembered)
    # Старый хаб игнорирует ?name= и отдаёт всех: имя сверяем сами, точно и без учёта регистра.
    want = _buyer_key(buyer)
    found = [u for u in client.users(buyer)
             if clean_email(u.get("email", "")) and _buyer_key(str(u.get("name") or "")) == want]
    if len(found) > 1:
        raise NeedEmail(buyer, ambiguous=True)
    if not found:
        raise NeedEmail(buyer)
    return clean_email(found[0]["email"])


def account_view(rows: Iterable[dict]) -> list[dict]:
    """Строки назначений для показа: только нужное странице, активные и снятые (removed=True)."""
    out = []
    for r in rows:
        name = str(r.get("account_name") or "").strip()
        if name:
            out.append({"account_name": name, "ad_account_id": str(r.get("ad_account_id") or ""),
                        "removed": bool(r.get("removed_at")), "assigned_by": str(r.get("assigned_by") or "")})
    return out
