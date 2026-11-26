"""Input validation and the two HTTP calls of the Limited_QA DEV API.

Rules follow ACM_LIMITED_QA_DEVELOPER_HANDOFF.md. Nothing here retries and
nothing here logs the token: callers decide what a failure means.
"""
from __future__ import annotations

import ipaddress
import os
import re
import unicodedata
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests

TOKEN_ENV = "ACM_DEV_TOKEN"
URL_ENV = "ACM_LIMITED_QA_URL"
TIMEOUT_S = 20

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_COUNTRY_OK = re.compile(r"^[\w .'\-]+$", re.UNICODE)


class ValidationError(ValueError):
    """The manager's input cannot be sent. Message is safe to show."""


class NotConfigured(RuntimeError):
    """Token or endpoint URL missing on this machine."""


class Unreachable(RuntimeError):
    """Network failure or timeout: the outcome of a POST is unknown."""


@dataclass
class Reply:
    http: int
    body: dict[str, Any]


# --------------------------------------------------------------- validation --

def new_request_id() -> str:
    return str(uuid.uuid4())


def idempotency_key(request_id: str) -> str:
    return f"acm-dev:{request_id}"


def normalize_request_id(value: str) -> str:
    rid = str(value or "").strip().lower()
    if not _UUID_RE.match(rid):
        raise ValidationError("Invalid request id.")
    return rid


def normalize_source_urls(urls: Any) -> list[str]:
    if not isinstance(urls, (list, tuple)) or not 1 <= len(urls) <= 3:
        raise ValidationError("Provide 1 to 3 landing page URLs.")
    out: list[str] = []
    for raw in urls:
        text = str(raw or "").strip()
        if not text or any(c.isspace() for c in text) or "\\" in text:
            raise ValidationError(f"Invalid landing page URL: {text[:80]!r}")
        parts = urlsplit(text)
        if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
            raise ValidationError(f"Landing page URL must be http(s): {text[:80]!r}")
        if "@" in parts.netloc:
            raise ValidationError("Landing page URL must not contain user information.")
        try:
            port = parts.port
        except ValueError:
            port = -1
        if port is not None or ":" in parts.netloc.rsplit("]", 1)[-1]:
            raise ValidationError("Landing page URL must not contain a port.")
        norm = urlunsplit((parts.scheme.lower(), parts.hostname.lower(), parts.path, parts.query, ""))
        if norm in out:
            raise ValidationError("Landing page URLs must be different.")
        out.append(norm)
    return out


def normalize_target_domain(value: Any) -> str:
    """Bare root hostname only; the service decides whether it is a root."""
    text = str(value or "").strip().lower()
    if not text or any(c in text for c in "/:@\\ ?#") or text.endswith("."):
        raise ValidationError("Target domain must be a bare root domain like domain.com.")
    try:
        ipaddress.ip_address(text)
    except ValueError:
        pass
    else:
        raise ValidationError("Target domain must not be an IP address.")
    labels = text.split(".")
    if len(labels) < 2 or not all(_LABEL_RE.match(label) for label in labels):
        raise ValidationError("Target domain must be a bare root domain like domain.com.")
    return text


def normalize_country(value: Any) -> str:
    text = " ".join(unicodedata.normalize("NFC", str(value or "")).split())
    if not 1 <= len(text) <= 80 or not _COUNTRY_OK.match(text) or any(c.isdigit() or c == "_" for c in text):
        raise ValidationError("Country must be 1-80 letters.")
    return text


def normalize_facebook_account(value: Any) -> str | None:
    text = " ".join(str(value or "").split())
    if text[:8].lower() == "facebook":
        text = text[8:].strip()
    if not text:
        return None
    if len(text) > 100:
        raise ValidationError("Facebook account is too long (max 100).")
    return text


def build_body(request_id: str, source_urls: Any, target_domain: Any, country: Any,
               facebook_account: Any = None) -> dict[str, Any]:
    """The exact POST body. Persisted before the first network call."""
    rid = normalize_request_id(request_id)
    return {
        "request_id": rid,
        "idempotency_key": idempotency_key(rid),
        "source_urls": normalize_source_urls(source_urls),
        "target_domain": normalize_target_domain(target_domain),
        "country": normalize_country(country),
        "facebook_account": normalize_facebook_account(facebook_account),
    }


# --------------------------------------------------------------------- HTTP --

def _config() -> tuple[str, str]:
    token = (os.environ.get(TOKEN_ENV) or "").strip()
    url = (os.environ.get(URL_ENV) or "").strip().rstrip("/")
    if not token or not url:
        raise NotConfigured("Limited_QA is not configured on this machine.")
    return token, url


def is_configured() -> bool:
    try:
        _config()
    except NotConfigured:
        return False
    return True


def _headers(token: str, *, json_body: bool) -> dict[str, str]:
    headers = {"X-ACM-DEV-Environment": "dev", "X-ACM-DEV-Token": token}
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


def _reply(resp: requests.Response) -> Reply:
    try:
        body = resp.json()
    except ValueError:
        body = {}
    return Reply(resp.status_code, body if isinstance(body, dict) else {})


def post_request(body: dict[str, Any]) -> Reply:
    token, url = _config()
    try:
        resp = requests.post(url, json=body, headers=_headers(token, json_body=True), timeout=TIMEOUT_S)
    except requests.RequestException as exc:
        raise Unreachable(type(exc).__name__) from None  # never chain: urls/headers stay out of logs
    return _reply(resp)


def get_status(request_id: str) -> Reply:
    token, url = _config()
    try:
        resp = requests.get(f"{url}-status", params={"request_id": normalize_request_id(request_id)},
                            headers=_headers(token, json_body=False), timeout=TIMEOUT_S)
    except requests.RequestException as exc:
        raise Unreachable(type(exc).__name__) from None
    return _reply(resp)
