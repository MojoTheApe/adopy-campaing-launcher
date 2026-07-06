from __future__ import annotations

from dataclasses import dataclass

from src.meta_campaign_agent.ad_account_map import AccountMapEntry
from src.meta_campaign_agent.multilogin_config import MultiloginInstanceConfig, get_multilogin_instance
from src.meta_campaign_agent.multilogin_launcher import MultiloginLauncher, MultiloginLauncherError, StartedProfile
from src.meta_campaign_agent.profile_proxy import ProfileProxy


@dataclass
class MultiloginPublishContext:
    account_name: str
    entry: AccountMapEntry
    ml_config: MultiloginInstanceConfig
    proxy: ProfileProxy
    started: StartedProfile | None = None


class MultiloginSession:
    """Start Multilogin profile before publish; stop after (one account at a time)."""

    def __init__(self, entry: AccountMapEntry, account_name: str):
        self.entry = entry
        self.account_name = account_name
        self.ml_config = get_multilogin_instance(entry.multilogin_instance)
        self.launcher = MultiloginLauncher(self.ml_config)
        self.proxy = ProfileProxy.from_csv_fields(
            proxy_type=entry.proxy_type,
            host=entry.proxy_host,
            port=entry.proxy_port,
            username=entry.proxy_user,
            password=entry.proxy_pass,
        )
        self.started: StartedProfile | None = None

    def __enter__(self) -> MultiloginPublishContext:
        if not self.entry.folder_id or not self.entry.profile_id:
            raise MultiloginLauncherError(
                f"Account {self.account_name!r}: folder_id and profile_id required in ad_account_map.csv"
            )
        self.started = self.launcher.start_profile(self.entry.folder_id, self.entry.profile_id)
        return MultiloginPublishContext(
            account_name=self.account_name,
            entry=self.entry,
            ml_config=self.ml_config,
            proxy=self.proxy,
            started=self.started,
        )

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.entry.profile_id:
            try:
                self.launcher.stop_profile(self.entry.profile_id)
            except MultiloginLauncherError:
                if exc_type is None:
                    raise
