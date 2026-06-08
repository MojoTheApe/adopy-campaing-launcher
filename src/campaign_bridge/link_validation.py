"""Classify Accounts sheet Link values for pre-flight checks."""
from __future__ import annotations

import os
from typing import Literal
from urllib.parse import urlparse

LinkStatus = Literal["empty", "jira", "tracking"]


def _jira_host_allowlist() -> list[str]:
    raw = (os.environ.get("JIRA_LINK_HOSTS") or "").strip()
    if raw:
        return [h.strip().lower() for h in raw.split(",") if h.strip()]
    return ["atlassian.net"]


def _host_matches_allowlist(host: str, allowlist: list[str]) -> bool:
    host = (host or "").strip().lower()
    if not host:
        return False
    for pattern in allowlist:
        pattern = pattern.strip().lower()
        if not pattern:
            continue
        if pattern.startswith("*."):
            suffix = pattern[2:]
            if host == suffix or host.endswith("." + suffix):
                return True
        elif host == pattern or host.endswith("." + pattern):
            return True
    return False


def classify_link(link: str | None) -> LinkStatus:
    """Return empty, jira (needs Keitaro), or tracking (OK)."""
    text = (link or "").strip()
    if not text:
        return "empty"
    parsed = urlparse(text if "://" in text else f"https://{text}")
    host = (parsed.hostname or "").lower()
    if not host:
        return "empty"
    if _host_matches_allowlist(host, _jira_host_allowlist()):
        return "jira"
    return "tracking"


def link_error_message(status: LinkStatus) -> str | None:
    if status == "empty":
        return "Open Jira Task — provision asset/link in Jira first"
    if status == "jira":
        return "Open Keitaro — replace Jira URL with tracking link"
    return None
