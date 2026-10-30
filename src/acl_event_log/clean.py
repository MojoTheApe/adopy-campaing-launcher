"""Чистка событий журнала перед записью в ящик.

Обычный `telemetry.scrub` здесь не годится: он заменяет числа от 8 цифр на
`<id>` и уничтожил бы id кампаний, адсетов и объявлений, ради которых журнал и
ведётся. Поэтому своя чистка: секреты и значения параметров ссылок вырезаются,
номера остаются как есть.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from src.fb_api.telemetry import strip_tokens

# Ключи, значение которых скрывается целиком (без учёта регистра).
SECRET_KEYS = frozenset({
    "password", "passwd", "proxy_user", "proxy_pass", "cookie", "cookies",
    "authorization", "secret", "token", "access_token",
})

_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.I)
_QUERY_VALUE_RE = re.compile(r"([?&][^=&#\s]+)=([^&#\s]*)")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")


def _cut_url(match: "re.Match[str]") -> str:
    return _QUERY_VALUE_RE.sub(r"\1=<v>", match.group(0))


def clean_text(text: str) -> str:
    """Свободный текст: значения параметров в ссылках -> `<v>`, почты -> `<email>`."""
    text = _URL_RE.sub(_cut_url, text)
    return _EMAIL_RE.sub("<email>", text)


def clean_value(value: Any) -> Any:
    """Рекурсивно очистить payload. Числа, None и id не меняются."""
    if isinstance(value, dict):
        return {
            k: ("<hidden>" if isinstance(k, str) and k.lower() in SECRET_KEYS
                else clean_value(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [clean_value(v) for v in value]
    if isinstance(value, str):
        return clean_text(strip_tokens(value))
    return value


def link_fingerprint(url: str) -> dict:
    """Домен, путь, имена параметров и sha256[:12] их значений; сами значения не хранятся."""
    empty = {"domain": "", "path": "", "param_names": [], "param_hashes": {}}
    try:
        parts = urlsplit(str(url or "").strip())
        pairs = parse_qsl(parts.query, keep_blank_values=True)
    except Exception:
        return empty
    names: list[str] = []
    hashes: dict[str, str] = {}
    for name, val in pairs:
        if name not in hashes:
            names.append(name)
            hashes[name] = hashlib.sha256(val.encode("utf-8")).hexdigest()[:12]
    return {
        "domain": (parts.hostname or "").lower(),
        "path": parts.path or "",
        "param_names": names,
        "param_hashes": hashes,
    }


def canonical_hash(obj: Any) -> str:
    raw = json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def plan_hash(country: str, slots: list) -> str:
    """Один хэш для `job.created` и `plan.checked`: по нему проверка связана с заданием."""
    return canonical_hash({"country": country, "slots": slots})
