from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

import requests


# Версия Marketing API по умолчанию (ACL-1783). v22.0 уже не в списке поддерживаемых,
# v24.0 снят с поддержки 2026-10-06. Переопределяется env META_GRAPH_API_VERSION;
# позже значение будет читаться из реестра правил Meta (config/meta_rules/meta.json).
DEFAULT_GRAPH_VERSION = "v26.0"


class MetaGraphError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, meta_code: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.meta_code = meta_code


@dataclass
class MetaClientConfig:
    access_token: str
    graph_version: str = DEFAULT_GRAPH_VERSION
    timeout: float = 120.0
    proxies: dict[str, str] | None = None


class MetaClient:
    def __init__(self, config: MetaClientConfig):
        self.config = config
        self._session = requests.Session()
        if config.proxies:
            self._session.proxies.update(config.proxies)

    @property
    def base_url(self) -> str:
        version = self.config.graph_version.strip().lstrip("/") or DEFAULT_GRAPH_VERSION
        return f"https://graph.facebook.com/{version}"

    def _extract_error(self, payload: dict[str, Any]) -> str | None:
        err = payload.get("error")
        if not isinstance(err, dict):
            return None
        msg = err.get("error_user_msg") or err.get("message") or "Meta API error"
        code = err.get("code")
        return f"{msg} (code={code})" if code is not None else str(msg)

    def post_form(self, path: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = dict(data or {})
        payload["access_token"] = self.config.access_token
        url = path if path.startswith("http") else f"{self.base_url}/{path.lstrip('/')}"
        r = self._session.post(url, data=payload, timeout=self.config.timeout)
        body = r.json() if r.content else {}
        api_err = self._extract_error(body) if isinstance(body, dict) else None
        if api_err:
            err_obj = body.get("error") if isinstance(body, dict) else {}
            raise MetaGraphError(api_err, status_code=r.status_code, meta_code=err_obj.get("code"))
        if r.status_code >= 400:
            raise MetaGraphError(f"HTTP {r.status_code}", status_code=r.status_code)
        return body if isinstance(body, dict) else {"raw": body}

    def post_multipart(self, path: str, data: dict[str, Any], files: dict[str, Any]) -> dict[str, Any]:
        payload = dict(data)
        payload["access_token"] = self.config.access_token
        url = path if path.startswith("http") else f"{self.base_url}/{path.lstrip('/')}"
        r = self._session.post(url, data=payload, files=files, timeout=self.config.timeout)
        body = r.json() if r.content else {}
        api_err = self._extract_error(body) if isinstance(body, dict) else None
        if api_err:
            err_obj = body.get("error") if isinstance(body, dict) else {}
            raise MetaGraphError(api_err, status_code=r.status_code, meta_code=err_obj.get("code"))
        if r.status_code >= 400:
            raise MetaGraphError(f"HTTP {r.status_code}", status_code=r.status_code)
        return body if isinstance(body, dict) else {"raw": body}

    def create_campaign(self, ad_account_id: str, *, name: str, objective: str, status: str) -> str:
        resp = self.post_form(
            f"{ad_account_id}/campaigns",
            {
                "name": name,
                "objective": objective,
                "status": status,
                "special_ad_categories": json.dumps([]),
                "is_adset_budget_sharing_enabled": "false",
            },
        )
        cid = resp.get("id")
        if not cid:
            raise MetaGraphError(f"Campaign create returned no id: {resp}")
        return str(cid)

    def create_ad_set(
        self,
        ad_account_id: str,
        *,
        name: str,
        campaign_id: str,
        daily_budget_cents: int,
        optimization_goal: str,
        billing_event: str,
        bid_strategy: str,
        targeting: dict[str, Any],
        promoted_object: dict[str, Any],
        status: str,
        destination_type: str = "WEBSITE",
    ) -> str:
        resp = self.post_form(
            f"{ad_account_id}/adsets",
            {
                "name": name,
                "campaign_id": campaign_id,
                "daily_budget": str(daily_budget_cents),
                "optimization_goal": optimization_goal,
                "billing_event": billing_event,
                "bid_strategy": bid_strategy,
                "targeting": json.dumps(targeting),
                "promoted_object": json.dumps(promoted_object),
                "status": status,
                "destination_type": destination_type,
            },
        )
        sid = resp.get("id")
        if not sid:
            raise MetaGraphError(f"Ad set create returned no id: {resp}")
        return str(sid)

    def create_ad_creative(
        self,
        ad_account_id: str,
        *,
        name: str,
        page_id: str,
        link: str,
        title: str,
        body: str,
        link_description: str,
        image_hash: str | None = None,
        video_id: str | None = None,
    ) -> str:
        if image_hash:
            object_story_spec = {
                "page_id": page_id,
                "link_data": {
                    "link": link,
                    "message": body,
                    "name": title,
                    "description": link_description,
                    "image_hash": image_hash,
                    "call_to_action": {"type": "LEARN_MORE", "value": {"link": link}},
                },
            }
        elif video_id:
            object_story_spec = {
                "page_id": page_id,
                "video_data": {
                    "video_id": video_id,
                    "title": title,
                    "message": body,
                    "link_description": link_description,
                    "call_to_action": {"type": "LEARN_MORE", "value": {"link": link}},
                },
            }
        else:
            raise MetaGraphError("Ad creative requires image_hash or video_id")

        resp = self.post_form(
            f"{ad_account_id}/adcreatives",
            {
                "name": name,
                "object_story_spec": json.dumps(object_story_spec),
            },
        )
        cid = resp.get("id")
        if not cid:
            raise MetaGraphError(f"Ad creative create returned no id: {resp}")
        return str(cid)

    def create_ad(self, ad_account_id: str, *, name: str, adset_id: str, creative_id: str, status: str) -> str:
        resp = self.post_form(
            f"{ad_account_id}/ads",
            {
                "name": name,
                "adset_id": adset_id,
                "creative": json.dumps({"creative_id": creative_id}),
                "status": status,
            },
        )
        aid = resp.get("id")
        if not aid:
            raise MetaGraphError(f"Ad create returned no id: {resp}")
        return str(aid)


def meta_client_from_env(*, proxies: dict[str, str] | None = None) -> MetaClient:
    token = (os.environ.get("META_ACCESS_TOKEN") or "").strip()
    if not token:
        raise MetaGraphError("META_ACCESS_TOKEN is not set in .env")
    version = (os.environ.get("META_GRAPH_API_VERSION") or DEFAULT_GRAPH_VERSION).strip()
    return MetaClient(MetaClientConfig(access_token=token, graph_version=version, proxies=proxies))
