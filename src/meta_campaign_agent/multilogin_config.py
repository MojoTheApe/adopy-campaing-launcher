from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class MultiloginInstanceConfig:
    instance_id: str
    automation_token: str
    launcher_base_v2: str

    @property
    def launcher_base_v1(self) -> str:
        if "/api/v2" in self.launcher_base_v2:
            return self.launcher_base_v2.replace("/api/v2", "/api/v1")
        return self.launcher_base_v2.rstrip("/") + "/api/v1"


def get_multilogin_instance(instance_id: str = "A") -> MultiloginInstanceConfig:
    key = (instance_id or "A").strip().upper()
    if key not in ("A", "B"):
        raise ValueError(f"multilogin_instance must be A or B, got {instance_id!r}")

    token = (os.environ.get(f"MULTILOGIN_AUTOMATION_TOKEN_{key}") or "").strip()
    if not token:
        raise ValueError(f"MULTILOGIN_AUTOMATION_TOKEN_{key} is not set in .env")

    default_launcher = "https://launcher.mlx.yt:45001/api/v2"
    launcher = (os.environ.get(f"MULTILOGIN_LAUNCHER_BASE_{key}") or default_launcher).strip().rstrip("/")
    return MultiloginInstanceConfig(instance_id=key, automation_token=token, launcher_base_v2=launcher)
