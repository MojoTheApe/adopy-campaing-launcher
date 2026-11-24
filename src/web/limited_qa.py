"""DEV-only ACM client and local request history for Limited_QA duplication.

The browser never receives the n8n token.  This module is the same-origin,
server-side boundary between the local ACM dashboard and the dedicated DEV
n8n webhook.  It deliberately refuses to start unless every DEV boundary is
explicit and the webhook URL has the expected random ``acm-dev-`` path.
"""
from __future__ import annotations

import json
import os
import re
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests

from config.paths import _atomic_write, get_local_config_dir, machine_file_lock


_HISTORY_NAME = "limited_qa_requests_dev.json"
_LOCK_NAME = "limited_qa_requests_dev.lock"
_ALLOWED_HOSTS = frozenset({"maragents.cloud", "n8n.maragents.cloud"})
_ROOT_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_TERMINAL = frozenset({"succeeded", "failed", "mixed"})
_STATUSES = frozenset({"accepted", "running", "paused", "succeeded", "failed", "mixed", "unknown"})
_SECOND_LEVEL_CCTLD = frozenset({
    "ac", "asn", "co", "com", "edu", "firm", "gen", "go", "gob", "gov",
    "id", "ind", "mil", "ne", "net", "nom", "or", "org",
})
_STORE_LOCK = threading.RLock()


class LimitedQaError(RuntimeError):
    """A safe, user-facing integration error."""


class LimitedQaConfigError(LimitedQaError):
    pass


class LimitedQaValidationError(LimitedQaError):
    pass


class LimitedQaUpstreamError(LimitedQaError):
    def __init__(self, message: str, *, ambiguous: bool = False, status_code: int = 502) -> None:
        super().__init__(message)
        self.ambiguous = ambiguous
        self.status_code = status_code


@dataclass(frozen=True)
class LimitedQaConfig:
    environment: str
    intake_url: str
    token: str

    @property
    def status_base_url(self) -> str:
        return self.intake_url.rstrip("/") + "-status"


def _valid_dev_webhook_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    path = parsed.path.rstrip("/")
    return (
        parsed.scheme == "https"
        and (parsed.hostname or "").lower() in _ALLOWED_HOSTS
        and parsed.port in (None, 443)
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
        and path.startswith("/webhook/acm-dev-")
        and "/status" not in path
        and len(path) <= 180
    )


def load_config() -> LimitedQaConfig:
    environment = os.environ.get("ACL_LIMITED_QA_ENV", "").strip().lower()
    intake_url = os.environ.get("ACL_LIMITED_QA_INTAKE_URL", "").strip()
    token = os.environ.get("ACL_LIMITED_QA_TOKEN", "").strip()
    if environment != "dev":
        raise LimitedQaConfigError("LP duplication is disabled: ACL_LIMITED_QA_ENV must be dev.")
    if not _valid_dev_webhook_url(intake_url):
        raise LimitedQaConfigError("LP duplication is disabled: the DEV n8n webhook URL is not trusted.")
    if len(token) < 32 or token.startswith("__"):
        raise LimitedQaConfigError("LP duplication is disabled: the DEV integration token is missing.")
    return LimitedQaConfig(environment="dev", intake_url=intake_url, token=token)


def availability() -> dict[str, Any]:
    try:
        config = load_config()
    except LimitedQaConfigError as exc:
        return {"enabled": False, "environment": "dev", "reason": str(exc)}
    return {
        "enabled": True,
        "environment": config.environment,
        "reason": "",
    }


def _normalize_source_url(value: Any) -> str:
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        raise LimitedQaValidationError("Every source LP must be an absolute HTTP(S) URL.") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise LimitedQaValidationError("Every source LP must be an absolute HTTP(S) URL.")
    if parsed.port not in (None, 80, 443):
        raise LimitedQaValidationError("Source LP URLs cannot use a custom port.")
    return parsed._replace(fragment="").geturl()


def _host_from_target(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        raise LimitedQaValidationError("Target domain is required.")
    try:
        if "://" in raw:
            parsed = urlsplit(raw)
            if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
                raise ValueError(raw)
            if parsed.port not in (None, 80, 443) or parsed.query or parsed.fragment:
                raise ValueError(raw)
            if parsed.path not in {"", "/"}:
                raise ValueError(raw)
            host = parsed.hostname or ""
        else:
            if any(ch in raw for ch in "/?#@:"):
                raise ValueError(raw)
            host = raw
    except ValueError as exc:
        raise LimitedQaValidationError("Target must be one root domain, without a page path or port.") from exc
    host = host.strip(".")
    if not _ROOT_RE.fullmatch(host) or ".." in host:
        raise LimitedQaValidationError("Target domain is invalid.")
    labels = host.split(".")
    if len(labels) < 2 or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels):
        raise LimitedQaValidationError("Target domain is invalid.")
    return host


def canonical_root_domain(value: Any) -> str:
    host = _host_from_target(value)
    if host.startswith("www."):
        host = host[4:]
    elif host.startswith("lpa.") or host.startswith("lpr."):
        host = host[4:]
    labels = host.split(".")
    ordinary_root = len(labels) == 2
    country_root = len(labels) == 3 and len(labels[2]) == 2 and labels[1] in _SECOND_LEVEL_CCTLD
    if host.startswith(("www.", "lpa.", "lpr.")) or not (ordinary_root or country_root):
        raise LimitedQaValidationError("Target must resolve unambiguously to a root domain.")
    return host


def _normalize_country(value: Any) -> str:
    country = re.sub(r"\s+", " ", str(value or "").strip())
    if not country or len(country) > 80 or country[0].isdigit():
        raise LimitedQaValidationError("Target country is required.")
    if any(ch.isdigit() or (not ch.isalpha() and ch not in " .'-") for ch in country):
        raise LimitedQaValidationError("Target country is invalid.")
    return country


def _normalize_account(value: Any) -> str | None:
    if value is None or not str(value).strip():
        return None
    account = re.sub(r"\s+", " ", str(value).strip())
    account = re.sub(r"^facebook[\s:_-]*", "", account, flags=re.IGNORECASE).strip()
    if not account or len(account) > 100:
        raise LimitedQaValidationError("Facebook account name is invalid.")
    return account


def normalize_submission(data: Any, *, request_id: str | None = None, idempotency_key: str | None = None) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise LimitedQaValidationError("A JSON request is required.")
    source_values = data.get("source_urls")
    if not isinstance(source_values, list) or not 1 <= len(source_values) <= 3:
        raise LimitedQaValidationError("Provide between one and three source LP URLs.")
    source_urls = [_normalize_source_url(value) for value in source_values]
    if len(source_urls) != len(set(source_urls)):
        raise LimitedQaValidationError("Duplicate source LP URLs are not allowed.")
    rid = str(request_id or uuid.uuid4()).lower()
    if not _UUID_RE.fullmatch(rid):
        raise LimitedQaValidationError("Request ID is invalid.")
    idem = str(idempotency_key or f"acm-dev:{rid}").strip()
    if not re.fullmatch(r"acm-dev:[A-Za-z0-9][A-Za-z0-9._:-]{7,151}", idem):
        raise LimitedQaValidationError("Idempotency key is invalid.")
    return {
        "request_id": rid,
        "idempotency_key": idem,
        "source_urls": source_urls,
        "target_domain": canonical_root_domain(data.get("target_domain")),
        "country": _normalize_country(data.get("country")),
        "facebook_account": _normalize_account(data.get("facebook_account")),
    }


def _headers(config: LimitedQaConfig) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-ACM-DEV-Token": config.token,
        "X-ACM-DEV-Environment": "dev",
    }


def _json_response(response: requests.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise LimitedQaUpstreamError("The DEV automation returned an invalid response.", status_code=502) from exc
    if not isinstance(payload, dict):
        raise LimitedQaUpstreamError("The DEV automation returned an invalid response.", status_code=502)
    if response.status_code >= 400:
        message = str(payload.get("error") or "The DEV automation rejected the request.")[:500]
        raise LimitedQaUpstreamError(message, status_code=502)
    return payload


class LimitedQaClient:
    def __init__(self, config: LimitedQaConfig, session: requests.Session | Any | None = None) -> None:
        self.config = config
        self.session = session or requests.Session()

    def submit(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self.session.post(
                self.config.intake_url,
                headers=_headers(self.config),
                json=payload,
                timeout=(3.05, 35),
            )
        except requests.RequestException as exc:
            raise LimitedQaUpstreamError(
                "The DEV automation did not confirm whether the request was accepted. Retry uses the same request ID.",
                ambiguous=True,
                status_code=502,
            ) from exc
        return _json_response(response)

    def status(self, request_id: str) -> dict[str, Any]:
        rid = str(request_id or "").lower()
        if not _UUID_RE.fullmatch(rid):
            raise LimitedQaValidationError("Request ID is invalid.")
        try:
            response = self.session.get(
                self.config.status_base_url,
                headers=_headers(self.config),
                params={"request_id": rid},
                timeout=(3.05, 20),
            )
        except requests.RequestException as exc:
            raise LimitedQaUpstreamError("Could not read the DEV automation status.", ambiguous=True) from exc
        return _json_response(response)


def _history_path() -> Path:
    return Path(get_local_config_dir()) / _HISTORY_NAME


def _load_unlocked() -> list[dict[str, Any]]:
    try:
        payload = json.loads(_history_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        return []
    rows = payload.get("requests") if isinstance(payload, dict) else None
    return [row for row in (rows or []) if isinstance(row, dict) and _UUID_RE.fullmatch(str(row.get("request_id") or ""))]


def list_requests() -> list[dict[str, Any]]:
    with _STORE_LOCK, machine_file_lock(Path(get_local_config_dir()) / _LOCK_NAME):
        rows = _load_unlocked()
    return sorted(rows, key=lambda row: str(row.get("created_at") or ""), reverse=True)[:100]


def get_request(request_id: str) -> dict[str, Any] | None:
    rid = str(request_id or "").lower()
    if not _UUID_RE.fullmatch(rid):
        raise LimitedQaValidationError("Request ID is invalid.")
    return next((row for row in list_requests() if row.get("request_id") == rid), None)


def save_request(record: dict[str, Any]) -> dict[str, Any]:
    rid = str(record.get("request_id") or "").lower()
    if not _UUID_RE.fullmatch(rid):
        raise LimitedQaValidationError("Request ID is invalid.")
    safe = dict(record)
    safe["request_id"] = rid
    safe.pop("token", None)
    with _STORE_LOCK, machine_file_lock(Path(get_local_config_dir()) / _LOCK_NAME):
        rows = _load_unlocked()
        rows = [row for row in rows if row.get("request_id") != rid]
        rows.append(safe)
        rows = sorted(rows, key=lambda row: str(row.get("created_at") or ""), reverse=True)[:100]
        _atomic_write(_history_path(), json.dumps({"version": 1, "requests": rows}, ensure_ascii=False, indent=2).encode("utf-8"))
    return safe


def merge_status(local: dict[str, Any], upstream: dict[str, Any]) -> dict[str, Any]:
    status = str(upstream.get("status") or local.get("status") or "unknown").lower()
    if status not in _STATUSES:
        status = "unknown"
    merged = dict(local)
    for key in (
        "jira_issue_key", "jira_issue_url", "jira_status", "dispatch_state", "result_urls",
        "result_items", "error", "created_at", "updated_at", "completed_at",
    ):
        if key in upstream:
            merged[key] = upstream[key]
    merged["status"] = status
    merged["terminal"] = status in _TERMINAL
    return merged
