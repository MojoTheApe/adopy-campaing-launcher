"""Чем ACL работает с Meta: Graph API изнутри профиля (`api`) или RPA (`rpa`).

ACL-2668, решение 38 (владелец, 16.11): по умолчанию — `api` везде. RPA для
кампаний выведен из работы (21.10), поэтому `rpa` — только аварийный
выключатель: строка `ACL_FB_BACKEND=rpa` в `.env` машины. Любое другое
значение, пустое или отсутствующее — `api`.

Единственное место, где решается бэкенд: чтения (`account_spend_api`),
публикация (`publish_api`), команды, тёплые сессии и страница берут его
отсюда, чтобы значение по умолчанию больше не расходилось между модулями.
"""
from __future__ import annotations

import os

BACKEND_ENV = "ACL_FB_BACKEND"
API = "api"
RPA = "rpa"
DEFAULT = API


def backend() -> str:
    """`api` (по умолчанию) или `rpa` — только если в окружении явно `ACL_FB_BACKEND=rpa`."""
    value = (os.environ.get(BACKEND_ENV) or "").strip().lower()
    return RPA if value == RPA else DEFAULT


def api_enabled() -> bool:
    return backend() == API
