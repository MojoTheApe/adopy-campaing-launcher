from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests

from src.meta_campaign_agent.multilogin_config import MultiloginInstanceConfig


class MultiloginLauncherError(RuntimeError):
    pass


@dataclass
class StartedProfile:
    profile_id: str
    folder_id: str
    port: str | None = None
    ws_endpoint: str | None = None


class MultiloginLauncher:
    def __init__(self, config: MultiloginInstanceConfig, *, timeout: float = 120.0):
        self.config = config
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.config.automation_token}",
        }

    def start_profile(self, folder_id: str, profile_id: str, *, headless: bool = False) -> StartedProfile:
        url = (
            f"{self.config.launcher_base_v2}/profile/f/{folder_id}/p/{profile_id}/start"
            f"?automation_type=playwright&headless_mode={'true' if headless else 'false'}"
        )
        r = requests.get(url, headers=self._headers(), timeout=self.timeout)
        try:
            body: dict[str, Any] = r.json()
        except ValueError:
            raise MultiloginLauncherError(f"Start profile: invalid JSON HTTP {r.status_code}: {r.text[:500]}")

        if r.status_code >= 400:
            msg = body.get("status", {}).get("message") if isinstance(body.get("status"), dict) else r.text
            raise MultiloginLauncherError(f"Start profile failed HTTP {r.status_code}: {msg}")

        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        port = data.get("port")
        ws = data.get("wsEndpoint") or data.get("ws_endpoint") or data.get("webSocketDebuggerUrl")
        if isinstance(ws, str) and ws.startswith("ws://"):
            # Playwright connect_over_cdp expects http URL; strip ws:// host part if full URL given
            ws = None
        return StartedProfile(
            profile_id=profile_id,
            folder_id=folder_id,
            port=str(port) if port else None,
            ws_endpoint=str(ws).strip() if ws else None,
        )

    def stop_profile(self, profile_id: str) -> None:
        url = f"{self.config.launcher_base_v1}/profile/stop"
        r = requests.get(
            url,
            headers=self._headers(),
            params={"profile_id": profile_id},
            timeout=self.timeout,
        )
        if r.status_code >= 400:
            try:
                body = r.json()
                msg = body.get("status", {}).get("message") if isinstance(body.get("status"), dict) else r.text
            except ValueError:
                msg = r.text[:500]
            raise MultiloginLauncherError(f"Stop profile failed HTTP {r.status_code}: {msg}")
