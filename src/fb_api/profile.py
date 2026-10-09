"""Профиль Multilogin для Graph-вызовов: поднять или прицепиться, без Playwright.

Запуск — тем же `MultiloginLauncher` (MLX launcher API), что у робота и
дешборда; он на `requests`, без Playwright. Аккаунт вне Multilogin не
открываем никогда: нет порта отладки от MLX — нет работы.

Остановки нет вовсе (решение координатора, ревью PR #759, S2): пилот только
цепляется к работающему профилю или поднимает его, и НИКОГДА не останавливает.
Список «запущенных» MLX может не показать профиль робота, дешборда или
человека (схема ответа, гонка) — тогда «наш» профиль оказался бы чужим.
Поднятый пилотом профиль остаётся открытым, пилот просит закрыть его в
Multilogin руками. kill — тем более никогда.

Профиль, открытый руками в MLX Desktop, не трогаем: у него нет порта CDP,
MLX ответит «уже запущен» без порта — это отказ с понятной причиной.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


class ProfileError(RuntimeError):
    """Профиль не поднят и не найден работающим с портом отладки."""


@dataclass
class OpenedProfile:
    profile_id: str
    folder_id: str
    instance: str
    port: str
    ws_endpoint: str | None
    started_by_us: bool
    launcher: Any = None

    @property
    def cdp_url(self) -> str:
        ws = self.ws_endpoint or ""
        if ws and not ws.startswith("/"):
            ws = "/" + ws
        return f"http://127.0.0.1:{self.port}{ws}"


def _default_launcher(instance: str) -> Any:
    from src.meta_campaign_agent.multilogin_config import get_multilogin_instance
    from src.meta_campaign_agent.multilogin_launcher import MultiloginLauncher

    return MultiloginLauncher(get_multilogin_instance(instance))


def running_state(launcher: Any, profile_id: str) -> tuple[str, str | None]:
    """('running', порт) / ('not_running', None) / ('unknown', None).

    Те же адреса «запущенных профилей», что у `MultiloginLauncher`; разница —
    «не ответил» отличается от «профиля нет в списке».
    """
    import requests

    answered = False
    for endpoint in (
        f"{launcher.config.launcher_base_v2}/profile/running",
        f"{launcher.config.launcher_base_v1}/profile/running",
        f"{launcher.config.launcher_base_v2}/profiles/running",
    ):
        try:
            r = requests.get(endpoint, headers=launcher._headers(), timeout=10)
            if r.status_code != 200:
                continue
            items = r.json()
            if isinstance(items, dict):
                items = items.get("data") or []
            if not isinstance(items, list):
                continue
            answered = True
            for item in items:
                if not isinstance(item, dict):
                    continue
                if (item.get("profile_id") or item.get("id") or "") == profile_id:
                    port = item.get("port") or item.get("debug_port")
                    return "running", (str(port) if port else None)
        except Exception:
            continue
    return ("not_running", None) if answered else ("unknown", None)


def open_profile(profile_id: str, folder_id: str = "", instance: str = "A", *,
                 launcher_factory: Callable[[str], Any] | None = None) -> OpenedProfile:
    """Профиль с портом отладки от MLX: уже работающий или поднятый нами."""
    profile_id = (profile_id or "").strip()
    folder_id = (folder_id or "").strip()
    instance = (instance or "A").strip().upper() or "A"
    if not profile_id:
        raise ProfileError("no Multilogin profile id for this account")
    launcher = (launcher_factory or _default_launcher)(instance)

    state, port = running_state(launcher, profile_id)
    if state == "running":
        if not port:
            raise ProfileError(
                "the profile is open but has no automation port (opened by hand in MLX Desktop?). "
                "Close it in MLX Desktop or open it from the dashboard, then retry. Nothing was stopped."
            )
        return OpenedProfile(profile_id, folder_id, instance, port, None, False, launcher)

    started = launcher.start_profile(folder_id or None, profile_id, headless=False)
    if not started.port:
        raise ProfileError("Multilogin did not return a debug port; nothing was stopped.")
    return OpenedProfile(
        profile_id=profile_id,
        folder_id=started.folder_id or folder_id,
        instance=instance,
        port=str(started.port),
        ws_endpoint=started.ws_endpoint,
        # Только для сообщения человеку: остановки всё равно нет.
        started_by_us=(state == "not_running"),
        launcher=launcher,
    )


def release_profile(opened: OpenedProfile | None) -> str:
    """Профиль не останавливается никогда; ответ — что сказать человеку."""
    if opened is None:
        return "nothing"
    if opened.started_by_us:
        return "left_open_started_by_pilot"
    return "left_running"
