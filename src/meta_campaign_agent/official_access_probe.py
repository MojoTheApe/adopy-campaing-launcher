"""Explicit, read-only official Meta HTTP pilot; no browser or machine config.

The public operation verifies one account then reads its bounded spend report.
It never discovers credentials, follows pagination/redirects, retries or writes.
Upstream text, exception messages and credentials never enter the result.
"""
from __future__ import annotations

import json
import re
import ssl
import time
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum
from http.client import HTTPException
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener

API_VERSION = "v26.0"
SOCKET_TIMEOUT_S = 5.0
BODY_DEADLINE_S = 10.0
MAX_BODY_BYTES = 64 * 1024
MAX_PERIOD_DAYS = 31
_ACCOUNT = re.compile(r"(?:act_)?([0-9]{5,30})\Z")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_BEARER = re.compile(r"[A-Za-z0-9._~+/-]+=*\Z")
_RATE_CODES = {4, 17, 32, 613, *range(80000, 80015)}


class FailureKind(str, Enum):
    INVALID_INPUT = "invalid_input"
    INVALID_CREDENTIAL = "invalid_credential"
    PERMISSION = "permission"
    RATE_LIMIT = "rate_limit"
    NETWORK = "network"
    ACCOUNT_MISMATCH = "account_mismatch"
    UNEXPECTED_RESPONSE = "unexpected_response"


@dataclass(frozen=True)
class AccessToken:
    value: str = field(repr=False)

    def __post_init__(self) -> None:
        if (not isinstance(self.value, str) or not 1 <= len(self.value) <= 8192
                or not _BEARER.fullmatch(self.value)):
            raise ValueError("Supply a nonempty official access token without whitespace.")


@dataclass(frozen=True)
class ProbeRequest:
    account_id: str
    since: str
    until: str

    def __post_init__(self) -> None:
        match = _ACCOUNT.fullmatch(self.account_id) if isinstance(self.account_id, str) else None
        if not match:
            raise ValueError("Account ID must contain only digits, optionally prefixed with act_.")
        object.__setattr__(self, "account_id", "act_" + match.group(1))
        try:
            if not all(isinstance(s, str) and _DATE.fullmatch(s) for s in (self.since, self.until)):
                raise ValueError
            first, last = date.fromisoformat(self.since), date.fromisoformat(self.until)
            if not 0 <= (last - first).days < MAX_PERIOD_DAYS:
                raise ValueError
        except (ValueError, TypeError):
            raise ValueError("Use ISO dates for an inclusive period of 1 to 31 days.") from None


@dataclass(frozen=True)
class AccountRead:
    account_id: str
    currency: str
    timezone_name: str
    account_status: int


@dataclass(frozen=True)
class SpendRead:
    since: str
    until: str
    currency: str
    amount: str | None  # No rows is explicitly no_data, not an invented zero.


@dataclass(frozen=True)
class ProbeFailure:
    kind: FailureKind
    step: str
    message: str
    http_status: int | None = None
    meta_code: int | None = None
    meta_subcode: int | None = None


@dataclass(frozen=True)
class ProbeResult:
    request: ProbeRequest
    account: AccountRead | None = None
    spend: SpendRead | None = None
    failure: ProbeFailure | None = None

    def to_dict(self) -> dict[str, Any]:
        """Only validated fields and locally authored diagnostics leave the probe."""
        from dataclasses import asdict

        return {
            "status": "ok" if self.failure is None else "partial" if self.account else "failed",
            "request": asdict(self.request),
            "api_version": API_VERSION,
            "account": asdict(self.account) if self.account else None,
            "spend": ({**asdict(self.spend), "state": "no_data" if self.spend.amount is None else "reported"}
                      if self.spend else None),
            "failure": asdict(self.failure) if self.failure else None,
            "publishing_access": "not_checked",
        }


class _Stop(Exception):
    def __init__(self, failure: ProbeFailure):
        self.failure = failure
        super().__init__(failure.message)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _number(value: Any) -> int | None:
    return value if type(value) is int and 0 <= value <= 2**31 - 1 else None


def _reject(step: str, message: str, kind: FailureKind = FailureKind.UNEXPECTED_RESPONSE) -> None:
    raise _Stop(ProbeFailure(kind, step, message))


def _api_failure(status: int, body: Any, step: str) -> ProbeFailure | None:
    error = body.get("error") if isinstance(body, dict) else None
    if 200 <= status < 300 and error is None:
        return None
    error = error if isinstance(error, dict) else {}
    code, subcode = _number(error.get("code")), _number(error.get("error_subcode"))
    if code in {102, 190} or status == 401:
        kind, message = FailureKind.INVALID_CREDENTIAL, "Meta rejected the credential; no retry was attempted."
    elif code in _RATE_CODES or status == 429:
        kind, message = FailureKind.RATE_LIMIT, "Meta limited this read; stop and check the app/account usage."
    elif code == 10 or (code is not None and 200 <= code < 300) or status == 403:
        kind, message = FailureKind.PERMISSION, "Meta denied this read; verify app permissions and assigned assets."
    elif status >= 500:
        kind, message = FailureKind.NETWORK, "Meta is unavailable; no retry was attempted."
    else:
        kind, message = FailureKind.UNEXPECTED_RESPONSE, "Meta did not return a successful read; review the numeric error code."
    return ProbeFailure(kind, step, message, status, code, subcode)


class OfficialAccessProbe:
    """One public read operation. An injected opener is for offline transport tests."""

    def __init__(self, credential: AccessToken, *, opener: Any = None):
        self._credential = credential
        # No ambient proxy, browser cookies, netrc auth or redirect forwarding.
        self._opener = opener if opener is not None else build_opener(
            ProxyHandler({}), HTTPSHandler(context=ssl.create_default_context()), _NoRedirect(),
        )

    def read(self, request: ProbeRequest) -> ProbeResult:
        account = None
        try:
            body = self._get(request, "account", {
                "fields": "id,account_id,currency,timezone_name,account_status",
            })
            account = self._account(body, request)
            body = self._get(request, "spend", {
                "fields": "account_id,account_currency,date_start,date_stop,spend",
                "level": "account", "limit": "1",
                "time_range": json.dumps({"since": request.since, "until": request.until}, separators=(",", ":")),
            })
            spend = self._spend(body, request, account)
            return ProbeResult(request, account, spend)
        except _Stop as error:
            return ProbeResult(request, account, failure=error.failure)

    def _get(self, query: ProbeRequest, step: str, params: dict[str, str]) -> dict:
        # Callers never supply URLs, methods or Graph paths. The only edges are
        # this exact account and its account-level Insights; paging is not followed.
        path = query.account_id + ("/insights" if step == "spend" else "")
        url = f"https://graph.facebook.com/{API_VERSION}/{path}?{urlencode(params)}"
        request = Request(url, method="GET", headers={
            "Authorization": "Bearer " + self._credential.value,
            "Accept": "application/json", "Accept-Encoding": "identity",
        })
        try:
            try:
                response = self._opener.open(request, timeout=SOCKET_TIMEOUT_S)
            except HTTPError as error:
                response = error  # Read only a bounded body, never its repr or URL.
            with response:
                status = response.code
                deadline = time.monotonic() + BODY_DEADLINE_S
                raw = bytearray()
                while True:
                    if time.monotonic() >= deadline:
                        raise TimeoutError
                    # read1 returns available bytes instead of waiting to fill a
                    # large chunk; the socket timeout also bounds a stalled read.
                    chunk = response.read1(min(4096, MAX_BODY_BYTES + 1 - len(raw)))
                    if not chunk:
                        break
                    raw.extend(chunk)
                    if len(raw) > MAX_BODY_BYTES:
                        _reject(step, "Meta response exceeded the diagnostic size limit.")
        except (URLError, OSError, TimeoutError, HTTPException):
            # Exceptions can contain the complete Request, URL or credential.
            raise _Stop(ProbeFailure(FailureKind.NETWORK, step, "HTTP connection failed or timed out; no retry was attempted.")) from None
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeError):
            body = None
        failure = _api_failure(status, body, step)
        if failure:
            raise _Stop(failure)
        if not isinstance(body, dict):
            _reject(step, "Meta returned an invalid JSON object.")
        return body

    def _account(self, body: dict, query: ProbeRequest) -> AccountRead:
        if body.get("id") != query.account_id or body.get("account_id") != query.account_id[4:]:
            _reject("account", "Returned account does not match the explicitly selected account.", FailureKind.ACCOUNT_MISMATCH)
        currency, timezone = body.get("currency"), body.get("timezone_name")
        status = _number(body.get("account_status"))
        if (not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency)
                or not isinstance(timezone, str) or not re.fullmatch(r"[A-Za-z0-9_+./-]{1,80}", timezone)
                or status is None or self._credential.value in currency or self._credential.value in timezone):
            _reject("account", "Account metadata is incomplete or malformed.")
        return AccountRead(query.account_id, currency, timezone, status)

    @staticmethod
    def _spend(body: dict, query: ProbeRequest, account: AccountRead) -> SpendRead:
        paging = body.get("paging")
        if paging is not None and (not isinstance(paging, dict) or paging.get("next")):
            _reject("spend", "Spend response is incomplete; this pilot does not follow pagination.")
        rows = body.get("data")
        if not isinstance(rows, list) or len(rows) > 1:
            _reject("spend", "Expected one account-level spend row or an empty report.")
        amount = None
        if rows:
            row = rows[0]
            if not isinstance(row, dict):
                _reject("spend", "Spend row is malformed.")
            if row.get("account_id") != query.account_id[4:]:
                _reject("spend", "Spend belongs to a different account.", FailureKind.ACCOUNT_MISMATCH)
            if (row.get("account_currency") != account.currency or row.get("date_start") != query.since
                    or row.get("date_stop") != query.until):
                _reject("spend", "Spend currency or date range does not match this request.")
            value = row.get("spend")
            if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,20}(?:\.[0-9]{1,6})?", value):
                _reject("spend", "Spend amount is missing or invalid.")
            amount = format(Decimal(value), "f")
        return SpendRead(query.since, query.until, account.currency, amount)
