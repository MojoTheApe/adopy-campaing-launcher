"""Что подключение аккаунта выводит из профиля Multilogin — в одном месте.

Подключить аккаунт можно из карты страны и со вкладки Accounts, и обе
называют одно и то же: какой аккаунт стоит за профилем и какой у него
рекламный кабинет. До ACL-1151 это считалось дважды и по-разному: сервер
искал «первое имя карты, которое кончается хвостом профиля», а вкладка —
единственное имя книги/статистики с тем же ключом `profile_name_key`. Один
профиль мог получить два разных имени в зависимости от того, с какого
экрана его подключают.

Теперь правило одно и живёт здесь; `/mlx-profile-details` и обзор аккаунтов
зовут его, а страница только показывает ответ.
"""
from __future__ import annotations

import os
import threading
from typing import Iterable

# Имена аккаунтов из книги и статистики, которые видел последний обзор
# аккаунтов. Книга — Google Sheets, и читать её на каждый найденный профиль
# нельзя; поэтому подсказка в карте опирается на то, что обзор уже прочёл,
# плюс карту этой машины. Пусто до первого обзора — тогда хватает карты.
_lock = threading.Lock()
_remembered: set[str] = set()


def remember_labels(labels: Iterable[str]) -> None:
    """Запомнить имена аккаунтов, прочитанные обзором (книга + статистика)."""
    clean = {str(l).strip() for l in labels if str(l or "").strip()}
    if not clean:
        return
    with _lock:
        _remembered.update(clean)


def known_labels(extra: Iterable[str] = ()) -> list[str]:
    """Имена, среди которых ищется аккаунт профиля: карта, обзор, `extra`."""
    from config.paths import get_ad_account_map_path
    from src.meta_campaign_agent.ad_account_map import load_ad_account_map

    path = get_ad_account_map_path()
    try:
        mapped = list(load_ad_account_map(path)) if os.path.isfile(path) else []
    except Exception:  # noqa: BLE001 — битая карта не должна ронять подсказку
        mapped = []
    with _lock:
        seen = set(_remembered)
    out: list[str] = []
    for label in [*mapped, *seen, *extra]:
        label = str(label or "").strip()
        if label and label not in out:
            out.append(label)
    return out


def suggest_account_name(profile_name: str, labels: Iterable[str]) -> str:
    """Имя аккаунта для профиля — или "", если его нельзя назвать уверенно.

    Профиль `KLD_BFL_X84` и аккаунт `FacebookMKTOBFLX84 SV Ait` связывает
    ключ `profile_name_key` — у обоих он `X84`. Один аккаунт часто известен
    дважды: полным именем из книги и коротким из статистики; такие пары
    схлопываются по первому слову (берётся длинное). Если после этого
    остаётся больше одного кандидата, имя не угадывается: подставить молча
    не то — хуже, чем попросить вписать.
    """
    from src.fb_ads_rpa.mlx_profiles import profile_name_key

    key = profile_name_key(profile_name or "")
    if not key:
        return ""
    by_base: dict[str, str] = {}
    for label in labels:
        label = str(label or "").strip()
        if not label or profile_name_key(label) != key:
            continue
        base = label.split()[0].casefold()
        if base not in by_base or len(label) > len(by_base[base]):
            by_base[base] = label
    return next(iter(by_base.values())) if len(by_base) == 1 else ""


def derive(profile_id: str, profile_name: str, labels: Iterable[str] | None = None) -> dict:
    """Всё, что подключение берёт у профиля: имя, кабинет, папку.

    Кабинет читается из истории браузера этого профиля на диске — ничего не
    запускается и в сеть не ходит (опрос живого кабинета через его браузер
    предшествовал чекпоинту 2026-08-25). Профиль, который на этой машине не
    открывали, истории не имеет — тогда кабинет пустой.
    """
    from src.fb_ads_rpa import mlx_profiles

    pid = (profile_id or "").strip()
    try:
        act = mlx_profiles.ad_account_id_for(pid) if pid else ""
    except Exception:  # noqa: BLE001
        act = ""
    try:
        folder = mlx_profiles.folder_id_for(pid) if pid else ""
    except Exception:  # noqa: BLE001
        folder = ""
    names = known_labels() if labels is None else list(labels)
    return {
        "ad_account_id": act,
        "folder_id": folder,
        "opened_here": bool(act or folder),
        "suggested_name": suggest_account_name(profile_name, names),
    }
