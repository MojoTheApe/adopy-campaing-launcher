"""Единственное место, где читается `ACL_FB_BACKEND` (ACL-2785).

Решение владельца 2026-11-18: по умолчанию работает Graph API изнутри профиля.
Пустое или отсутствующее значение — это `api`; RPA выбирается только явным
`ACL_FB_BACKEND=rpa` (оставлен для ручного вмешательства на аккаунте).
"""
from __future__ import annotations

import os

BACKEND_ENV = "ACL_FB_BACKEND"


def backend() -> str:
    """`api` (по умолчанию) или `rpa` (только явное `rpa`). Остальное — `api`."""
    value = (os.environ.get(BACKEND_ENV) or "").strip().lower()
    return "rpa" if value == "rpa" else "api"


def api_enabled() -> bool:
    return backend() == "api"
