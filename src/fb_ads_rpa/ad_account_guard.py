"""Проверка рекламного кабинета перед Create (ACL-1697).

Если у Facebook-пользователя профиля нет доступа к кабинету из черновика (или
Meta помнит прошлый кабинет), Ads Manager молча открывает ДРУГОЙ кабинет, а
робот строил бы кампанию в нём. Поэтому перед каждым нажатием Create (и после
каждой перезагрузки или свежей вкладки) сверяем кабинет, который открыт на
самом деле, с кабинетом черновика.

Откуда читается открытый кабинет: `act=<id>` в адресе страницы после всех
редиректов — Ads Manager держит его в адресе на каждом экране (список
кампаний, редакторы). Адрес — чтение без сети и без касания страницы.
Не прочитали (адреса нет, страница мертва, `act=` пропал) — это тоже стоп:
создавать «на авось» нельзя.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from src.fb_ads_rpa import profile_trouble

_ACT_IN_URL = re.compile(r"[?&#]act=(?:act_)?(\d+)")
_DIGITS = re.compile(r"(\d+)")


def act_digits(value: str) -> str:
    """Цифры кабинета из `act_123`, `123` или адреса; "" — не нашли."""
    text = str(value or "")
    m = _ACT_IN_URL.search(text) or _DIGITS.search(text)
    return m.group(1) if m else ""


def opened_act(page) -> str:
    """Кабинет, который сейчас открыт в `page` (цифры), или "" — не прочитать."""
    try:
        url = page.url or ""
    except Exception:
        return ""
    m = _ACT_IN_URL.search(url)
    return m.group(1) if m else ""


def on_ads_manager(page) -> bool:
    """Страница сейчас на adsmanager.facebook.com (иначе кабинет не судим)."""
    try:
        host = (urlsplit(page.url or "").hostname or "").lower()
    except Exception:
        return False
    return host == "adsmanager.facebook.com"


def mismatch(page, expected: str) -> str:
    """"" — открыт нужный кабинет; иначе описание для отказа.

    Формат: `expected act_X, opened act_Y` (или `opened unknown`).
    """
    want = act_digits(expected)
    if not want:
        return "expected unknown, opened unknown"
    got = opened_act(page)
    if got == want:
        return ""
    return f"expected act_{want}, opened " + (f"act_{got}" if got else "unknown")


def stop_if_wrong(page, expected: str, where: str = "") -> bool:
    """Можно ли жать Create: True — открыт нужный кабинет.

    False — страница не на Ads Manager (chrome-error://, about:blank, чужой
    хост: обрыв сети после перезагрузки, свежая вкладка без перехода). Кабинет
    тут не «чужой», он просто не открыт: Create не жмём, а восстановление
    (список заново, свежая вкладка, диагноз сети) делает вызывающий.
    Ads Manager открыт, а `act=` нет или он другой — `ProfileStop(wrong_ad_account)`.
    """
    if not on_ads_manager(page):
        return False
    bad = mismatch(page, expected)
    if not bad:
        return True
    try:
        from src.fb_ads_rpa import run_telemetry as _rt

        _rt.record(profile_trouble.WRONG_AD_ACCOUNT, False, note=where or "create",
                   diag={"detail": bad})
    except Exception:
        pass
    raise profile_trouble.ProfileStop(profile_trouble.WRONG_AD_ACCOUNT, bad)
