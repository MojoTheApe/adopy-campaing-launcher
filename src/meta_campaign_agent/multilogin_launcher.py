from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import requests

from src.meta_campaign_agent.multilogin_config import MultiloginInstanceConfig

log = logging.getLogger(__name__)


class MultiloginLauncherError(RuntimeError):
    pass


@dataclass
class StartedProfile:
    profile_id: str
    folder_id: str
    port: str | None = None
    ws_endpoint: str | None = None
    # Аддитивный признак (ACL-1760): True только когда запуск был настоящим стартом.
    # Ответ 409 / «уже запущен» его не ставит: профиль чужой, закрывать его нельзя.
    newly_started: bool = False


class MultiloginLauncher:
    def __init__(self, config: MultiloginInstanceConfig, *, timeout: float = 120.0):
        self.config = config
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.config.automation_token}",
        }

    def _resolve_folder_id(self, profile_id: str) -> str | None:
        """Try to find folder_id for a profile_id via the workspace profiles API."""
        endpoints = [
            f"{self.config.launcher_base_v2.split('/api/')[0]}/profile?limit=100",
            f"https://api.multilogin.com/profile?limit=100",
        ]
        for ep in endpoints:
            try:
                r = requests.get(ep, headers=self._headers(), timeout=15)
                if r.status_code != 200:
                    continue
                body = r.json()
                data = body.get("data") or {}
                profiles = data.get("profiles") if isinstance(data, dict) else (data if isinstance(data, list) else [])
                for p in (profiles or []):
                    pid = p.get("profile_id") or p.get("id") or ""
                    if pid == profile_id:
                        return p.get("folder_id") or p.get("folderID") or None
            except Exception:
                continue
        return None

    def start_profile(self, folder_id: str | None, profile_id: str, *, headless: bool = False) -> StartedProfile:
        headless_str = 'true' if headless else 'false'
        resolved_folder = (folder_id or "").strip() or None

        # If no folder_id, try to look it up via profiles API
        if not resolved_folder:
            resolved_folder = self._resolve_folder_id(profile_id)

        if not resolved_folder:
            raise MultiloginLauncherError(
                f"profile_id={profile_id}: could not determine folder_id. "
                "Please open the Multilogin app, find the profile, copy its Folder ID "
                "and save it via the dashboard."
            )

        url = (
            f"{self.config.launcher_base_v2}/profile/f/{resolved_folder}/p/{profile_id}/start"
            f"?automation_type=playwright&headless_mode={headless_str}"
        )
        r = requests.get(url, headers=self._headers(), timeout=self.timeout)
        try:
            body: dict[str, Any] = r.json()
        except ValueError:
            raise MultiloginLauncherError(f"Start profile: invalid JSON HTTP {r.status_code}: {r.text[:500]}")

        if r.status_code >= 400:
            status_block = body.get("status") or {}
            msg = status_block.get("message") if isinstance(status_block, dict) else r.text
            error_code = (status_block.get("error_code") or "").upper() if isinstance(status_block, dict) else ""

            # Profile already running — Multilogin returns port in data even on error
            already_running = (
                "ALREADY_RUNNING" in error_code
                or "PROFILE_ALREADY_RUNNING" in error_code
                or "ALREADY RUNNING" in (msg or "").upper()
                or "already running" in (msg or "").lower()
                or "browser process is running" in (msg or "").lower()
                or "can't lock profile" in (msg or "").lower()
                or "cant lock profile" in (msg or "").lower()
                or "cannot lock" in (msg or "").lower()
                or r.status_code == 409
            )
            if already_running:
                data = body.get("data") if isinstance(body.get("data"), dict) else {}
                port = data.get("port")
                ws = data.get("wsEndpoint") or data.get("ws_endpoint") or data.get("webSocketDebuggerUrl")
                if port:
                    # Reuse the running profile's debug port
                    if isinstance(ws, str) and ws.startswith("ws://"):
                        ws = None
                    return StartedProfile(
                        profile_id=profile_id,
                        folder_id=resolved_folder or "",
                        port=str(port),
                        ws_endpoint=str(ws).strip() if ws else None,
                    )
                # No port in response — try to get it from the running-profiles endpoint
                running_port = self._get_running_profile_port(profile_id)
                if running_port:
                    return StartedProfile(
                        profile_id=profile_id,
                        folder_id=resolved_folder or "",
                        port=running_port,
                    )
            raise MultiloginLauncherError(f"Start profile failed HTTP {r.status_code}: {msg}")

        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        port = data.get("port")
        ws = data.get("wsEndpoint") or data.get("ws_endpoint") or data.get("webSocketDebuggerUrl")
        if isinstance(ws, str) and ws.startswith("ws://"):
            # Playwright connect_over_cdp expects http URL; strip ws:// host part if full URL given
            ws = None
        return StartedProfile(
            profile_id=profile_id,
            folder_id=resolved_folder or "",
            port=str(port) if port else None,
            ws_endpoint=str(ws).strip() if ws else None,
            newly_started=True,
        )

    def _get_running_profile_port(self, profile_id: str) -> str | None:
        """Query the launcher for the debug port of an already-running profile."""
        # Multilogin X: GET /profile/f/{folder}/p/{profile_id} or /running-profiles
        for endpoint in [
            f"{self.config.launcher_base_v2}/profile/running",
            f"{self.config.launcher_base_v1}/profile/running",
            f"{self.config.launcher_base_v2}/profiles/running",
        ]:
            try:
                r = requests.get(endpoint, headers=self._headers(), timeout=10)
                if r.status_code != 200:
                    continue
                items = r.json()
                if isinstance(items, dict):
                    items = items.get("data") or []
                if not isinstance(items, list):
                    continue
                for item in items:
                    pid = item.get("profile_id") or item.get("id") or ""
                    if pid == profile_id:
                        port = item.get("port") or item.get("debug_port")
                        if port:
                            return str(port)
            except Exception:
                continue
        return None

    def stop_profile(self, profile_id: str, folder_id: str | None = None) -> None:
        """Stop a running MLX profile through the launcher.

        The address is `GET /api/v1/profile/stop/p/{profile_id}` — no folder in
        the path, unlike start. Taken from Multilogin's own Postman collection
        and confirmed on launcher 12.13.0 (2026-09-16): a profile that is not
        running answers 500 "profile already stopped", which is a structured
        reply and not a routing miss.

        Before that the code tried three other addresses, and every one of
        them was "404 page not found" on the launcher in use. The caller then
        fell back to killing Mimic for the profile, and the run survived — but
        a kill is not a stop: the profile's state is whatever Chrome flushed,
        and Multilogin's own "in use by" for it is left to time out. The first
        live run after ACL-1200 went that way (FacebookDQ501, 12:08:36).

        "Already stopped" is treated as done: the caller wanted the profile
        not running, and it is not. The old addresses stay as a tail, so a
        launcher that still serves one of them keeps working; a 404 is logged
        with the address it came from, once, so the next move of the API can
        be read off the log instead of rediscovered.
        """
        candidates = [f"{self.config.launcher_base_v1}/profile/stop/p/{profile_id}"]
        resolved_folder = (folder_id or "").strip() or None
        if resolved_folder:
            candidates.append(
                f"{self.config.launcher_base_v2}/profile/f/{resolved_folder}/p/{profile_id}/stop"
            )
        candidates += [
            f"{self.config.launcher_base_v2}/profile/{profile_id}/stop",
            f"{self.config.launcher_base_v1}/profile/stop",
        ]

        last_err: str = ""
        for url in candidates:
            params = {"profile_id": profile_id} if url.endswith("/profile/stop") else {}
            try:
                r = requests.get(url, headers=self._headers(), params=params, timeout=self.timeout)
            except Exception as exc:
                last_err = str(exc)
                continue
            if r.status_code < 400:
                return
            try:
                body = r.json()
                status = body.get("status") if isinstance(body, dict) else None
                msg = status.get("message") if isinstance(status, dict) else r.text
            except ValueError:
                msg = r.text[:200]
            msg = msg or ""
            if "already stopped" in msg.lower():
                return
            last_err = f"HTTP {r.status_code}: {msg}"
            if r.status_code == 404:
                log.info("MLX stop address not served here: %s", url.split("/api/", 1)[-1])
                continue
            break
        raise MultiloginLauncherError(f"Stop profile failed {last_err}")
