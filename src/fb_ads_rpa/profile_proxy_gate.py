"""Есть ли у профиля Multilogin прокси — до того, как робот к нему прикоснётся (ACL-1138).

Гейт в `MultiloginDirectCDPProvider.open` не даёт роботу *самому* поднять
профиль без прокси. Вторая половина не проверялась: что прокси есть у
профиля, к которому мы цепляемся или который поднимает MLX. Мы считали это
данностью, а профиль, заведённый в Multilogin без прокси, проходил молча — и
выходил в Facebook с настоящего IP машины.

Разведка 16.10 (только чтение, MLX API): в пространстве A из 1848 профилей
34 без прокси, в B из 520 — трое, и среди них рекламные, запускавшиеся в
октябре. Так что это не теория.

Источник правды — конфигурация профиля в MLX API, а не флаг
`--proxy-server` в командной строке браузера: часть типов прокси MLX может
применять через своего агента, и жёсткий гейт по флагу заблокировал бы
здоровые профили. Флаг мы только пишем в лог — как свидетельство на будущее.

* `POST /profile/search` с `profile_ids` — работает в обоих пространствах.
  `/profile/metas` в пространстве B отвечает 501, поэтому не он.
* Прокси нет (`proxy: null` или пустой `host`) — `profile_no_proxy`,
  остановка на весь аккаунт, профиль не поднимаем и не трогаем.
* API не ответил — гейт пропускает с предупреждением: сбой проверки не
  должен останавливать здоровые прогоны, а мисконфиг API отдаёт
  детерминированно, и следующий прогон его поймает.

Выключатель машины: `FB_RPA_PROXY_GATE=0`.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger(__name__)

SEARCH_URL = "https://api.multilogin.com/profile/search"

#: Сколько секунд помним «у профиля прокси есть». Отказ не кэшируем: человек
#: добавит прокси и сразу нажмёт Publish снова.
OK_TTL_S = 600.0
#: Сколько помним «проверить не удалось»: при лежащем API каждое открытие
#: иначе ждало бы таймаут заново (ревью ACL-1138, M3).
UNKNOWN_TTL_S = 60.0
TIMEOUT_S = 5.0

_ok_cache: dict[str, float] = {}
_unknown_cache: dict[str, tuple[float, "ProxyConfig"]] = {}


@dataclass(frozen=True)
class ProxyConfig:
    """Что MLX знает о прокси профиля. `known=False` — узнать не удалось."""

    known: bool
    has_proxy: bool = False
    proxy_type: str = ""
    reason: str = ""


def gate_enabled() -> bool:
    raw = (os.environ.get("FB_RPA_PROXY_GATE") or "").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _parse(profile: dict[str, Any]) -> ProxyConfig:
    proxy = profile.get("proxy")
    if not isinstance(proxy, dict) or not str(proxy.get("host") or "").strip():
        return ProxyConfig(known=True, has_proxy=False)
    return ProxyConfig(known=True, has_proxy=True, proxy_type=str(proxy.get("type") or ""))


def fetch_proxy_config(profile_id: str, token: str, *, post: Callable[..., Any] | None = None,
                       timeout: float = TIMEOUT_S) -> ProxyConfig:
    """Прочитать прокси профиля из MLX API. Ничего не меняет и в Facebook не ходит."""
    if not token:
        return ProxyConfig(known=False, reason="no MLX token")
    if post is None:
        import requests

        post = requests.post
    try:
        r = post(
            SEARCH_URL,
            headers={"Accept": "application/json", "Content-Type": "application/json",
                     "Authorization": f"Bearer {token}"},
            json={"limit": 5, "offset": 0, "search_text": "", "is_removed": False,
                  "profile_ids": [profile_id]},
            timeout=timeout,
        )
        if r.status_code != 200:
            return ProxyConfig(known=False, reason=f"MLX API HTTP {r.status_code}")
        profiles = ((r.json() or {}).get("data") or {}).get("profiles") or []
    except Exception as exc:
        return ProxyConfig(known=False, reason=f"MLX API: {type(exc).__name__}")
    want = profile_id.strip().lower()
    match = next((p for p in profiles
                  if isinstance(p, dict) and str(p.get("id") or "").strip().lower() == want), None)
    if match is None:
        # Профиль из другого пространства или удалён — о прокси это ничего не говорит.
        return ProxyConfig(known=False, reason="profile not found in this Multilogin workspace")
    return _parse(match)


def _token_for(instance: str) -> str:
    try:
        from src.meta_campaign_agent.multilogin_config import get_multilogin_instance

        return get_multilogin_instance(instance).automation_token
    except Exception:
        return ""


def _instances_for(entry: Any) -> tuple[str, ...]:
    """Пространство из карты; не указано (прицеп без карты) — оба, A затем B."""
    inst = (getattr(entry, "multilogin_instance", "") or "").strip().upper()
    return (inst,) if inst in ("A", "B") else ("A", "B")


def ensure_profile_has_proxy(entry: Any, account_label: str, *,
                             fetch: Callable[[str, str], ProxyConfig] | None = None,
                             token: str | None = None,
                             now: Callable[[], float] = time.monotonic) -> ProxyConfig | None:
    """Остановить прогон, если профиль настроен в Multilogin без прокси.

    Зовётся до подключения и до запуска профиля. `None` — гейт выключен.
    """
    from src.fb_ads_rpa import profile_trouble

    profile_id = (getattr(entry, "profile_id", "") or "").strip()
    if not profile_id or not gate_enabled():
        return None
    seen = _ok_cache.get(profile_id)
    if seen is not None and now() - seen < OK_TTL_S:
        return ProxyConfig(known=True, has_proxy=True, reason="cached")
    held = _unknown_cache.get(profile_id)
    if held is not None and now() - held[0] < UNKNOWN_TTL_S:
        return held[1]
    tokens = [token] if token is not None else [_token_for(i) for i in _instances_for(entry)]
    cfg = ProxyConfig(known=False, reason="no MLX token")
    reasons = []
    for tok in tokens:
        cfg = (fetch or fetch_proxy_config)(profile_id, tok)
        if cfg.known:
            break
        reasons.append(cfg.reason)
    if not cfg.known and len(reasons) > 1:
        # Причина по каждому пространству: «не найден в A» важнее «нет токена B».
        cfg = ProxyConfig(known=False, reason="; ".join(reasons))
    if not cfg.known:
        log.warning("%s: could not check the profile's proxy in Multilogin (%s); continuing",
                    account_label, cfg.reason)
        _report_unchecked(account_label, cfg.reason)
        _unknown_cache[profile_id] = (now(), cfg)
        return cfg
    if not cfg.has_proxy:
        log.error("%s: Multilogin profile %s has no proxy configured; refusing to open it",
                  account_label, profile_id)
        raise profile_trouble.ProfileStop(profile_trouble.NO_PROXY, f"profile {profile_id}")
    _ok_cache[profile_id] = now()
    log.info("%s: profile proxy configured in Multilogin (%s)", account_label, cfg.proxy_type or "?")
    return cfg


def _report_unchecked(account_label: str, reason: str) -> None:
    """Пропуск без проверки — в телеметрию: истёкший токен иначе молча выключил бы гейт."""
    try:
        from src.fb_ads_rpa import run_telemetry

        run_telemetry.record("proxy_gate_unchecked", False,
                             diag={"account": account_label, "reason": reason[:120]})
    except Exception:
        pass


def cmdline_has_proxy_flag(cmdline: list[str] | None) -> bool:
    """Есть ли `--proxy-server=` в командной строке браузера профиля."""
    return any(isinstance(a, str) and a.startswith("--proxy-server=") for a in (cmdline or []))
