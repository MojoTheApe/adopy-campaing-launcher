from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProfileProxy:
    proxy_type: str
    host: str
    port: int
    username: str | None = None
    password: str | None = None

    def to_requests_proxies(self) -> dict[str, str]:
        scheme = (self.proxy_type or "http").lower()
        if scheme == "socks5":
            scheme = "socks5h"
        auth = ""
        if self.username:
            auth = f"{self.username}:{self.password or ''}@"
        url = f"{scheme}://{auth}{self.host}:{self.port}"
        return {"http": url, "https": url}

    @classmethod
    def from_csv_fields(
        cls,
        *,
        proxy_type: str | None,
        host: str | None,
        port: str | int | None,
        username: str | None = None,
        password: str | None = None,
    ) -> ProfileProxy:
        if not host or not port:
            raise ValueError("proxy_host and proxy_port are required for --via-multilogin")
        try:
            port_int = int(port)
        except (TypeError, ValueError) as e:
            raise ValueError(f"invalid proxy_port: {port!r}") from e
        return cls(
            proxy_type=(proxy_type or "http").lower(),
            host=str(host).strip(),
            port=port_int,
            username=(username or "").strip() or None,
            password=(password or "").strip() or None,
        )
