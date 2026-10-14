"""One explicit /debug_token inspection; no account, browser or config access.

The subject token and App ID|App Secret inspector credential are separate query
parameters, as in Meta's first-party diagnostic. Only typed allowlisted evidence
leaves this module. A matching App ID establishes no advertising permission.
"""
from __future__ import annotations

import json
import math
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

MAX_BODY_BYTES = 65536
MAX_REQUEST_BYTES = 32768
TIMEOUT_SECONDS = 10.0
_MAX_TIMESTAMP = 253402300799
_ID = re.compile(r"[1-9][0-9]{0,39}")
_VERSION = re.compile(r"v[1-9][0-9]{0,2}\.[0-9]{1,2}")
_SCOPE = re.compile(r"[a-z][a-z0-9_]{0,63}")


class Outcome(str, Enum):
    APP_MATCH = "app_match"
    APP_MISMATCH = "app_mismatch"
    SUBJECT_INVALID = "subject_invalid"
    EXPIRED = "expired"
    UNSUPPORTED_TYPE = "unsupported_type"
    INVALID_INPUT = "invalid_input"
    INSPECTOR_REJECTED = "inspector_rejected"
    INSPECTION_ERROR = "inspection_error"
    PERMISSION_DENIED = "permission_denied"
    RATE_LIMITED = "rate_limited"
    NETWORK_ERROR = "network_error"
    TIMEOUT = "timeout"
    REDIRECTED = "redirected"
    OVERSIZED = "oversized"
    MALFORMED = "malformed_response"
    UNEXPECTED = "unexpected_response"


class TokenType(str, Enum):
    USER = "USER"
    PAGE = "PAGE"
    APP = "APP"
    SYSTEM_USER = "SYSTEM_USER"


class ExpiryState(str, Enum):
    UNKNOWN = "unknown"
    ZERO = "zero"
    FUTURE = "future"
    EXPIRED = "expired"


class ScopeComparison(str, Enum):
    NOT_REQUESTED = "not_requested"
    UNKNOWN = "unknown"
    MET = "met"
    MISSING = "missing"


@dataclass(frozen=True)
class Secret:
    value: str = field(repr=False)

    def __str__(self) -> str:
        return "<protected input>"


@dataclass(frozen=True)
class ExpiryEvidence:
    state: ExpiryState = ExpiryState.UNKNOWN
    timestamp: int | None = None


@dataclass(frozen=True)
class TokenVerification:
    outcome: Outcome
    expected_app_id: str | None = None
    api_version: str | None = None
    verified_at: int | None = None
    subject_valid: bool | None = None
    verified_token_app_id: str | None = None
    app_matches: bool | None = None
    token_type: TokenType | None = None
    expires: ExpiryEvidence = ExpiryEvidence()
    data_access_expires: ExpiryEvidence = ExpiryEvidence()
    scopes: tuple[str, ...] | None = None
    required_scopes: ScopeComparison = ScopeComparison.NOT_REQUESTED
    missing_scopes: tuple[str, ...] = ()
    http_status: int | None = None
    graph_code: int | None = None
    graph_subcode: int | None = None

    def as_dict(self) -> dict:
        return {
            "outcome": self.outcome.value, "expected_app_id": self.expected_app_id,
            "api_version": self.api_version, "verified_at": self.verified_at,
            "subject_valid": self.subject_valid, "verified_token_app_id": self.verified_token_app_id,
            "app_matches": self.app_matches, "token_type": self.token_type.value if self.token_type else None,
            "expires": {"state": self.expires.state.value, "timestamp": self.expires.timestamp},
            "data_access_expires": {"state": self.data_access_expires.state.value, "timestamp": self.data_access_expires.timestamp},
            "scopes": list(self.scopes) if self.scopes is not None else None,
            "required_scopes": self.required_scopes.value, "missing_scopes": list(self.missing_scopes),
            "http_status": self.http_status, "graph_code": self.graph_code, "graph_subcode": self.graph_subcode,
            "account_access": "not_checked", "read_readiness": "not_checked", "publishing": "not_checked",
        }


class _Failure(Exception):
    def __init__(self, outcome: Outcome):
        super().__init__(outcome.value)
        self.outcome = outcome


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _Request(urllib.request.Request):
    def __repr__(self) -> str:
        return "<MetaDebugTokenRequest GET graph.facebook.com>"


def _opener():
    context = ssl.create_default_context()
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    # Explicit empty proxy handler also disables urllib's environment discovery.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                      urllib.request.HTTPSHandler(context=context, debuglevel=0), _NoRedirect())


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _json(body: bytes):
    return json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError()))


def _scopes(value) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or len(value) > 128:
        raise ValueError
    if any(not isinstance(scope, str) or not _SCOPE.fullmatch(scope) for scope in value):
        raise ValueError
    return tuple(sorted(set(value)))


def _expiry(data: dict, key: str, now: int) -> ExpiryEvidence:
    if key not in data:
        return ExpiryEvidence()
    value = data[key]
    if type(value) is not int or not 0 <= value <= _MAX_TIMESTAMP:
        raise ValueError
    state = ExpiryState.ZERO if value == 0 else ExpiryState.EXPIRED if value <= now else ExpiryState.FUTURE
    return ExpiryEvidence(state, value)


def _error_codes(error: dict) -> tuple[int | None, int | None]:
    if not isinstance(error, dict):
        raise ValueError
    values = []
    for key in ("code", "error_subcode"):
        value = error.get(key)
        if value is not None and (type(value) is not int or not 0 <= value <= 2147483647):
            raise ValueError
        values.append(value)
    return values[0], values[1]


def _error_outcome(status: int, code: int | None) -> Outcome:
    if status == 401:
        return Outcome.INSPECTOR_REJECTED
    if status == 429 or code in {4, 17, 32, 613} or code is not None and 80000 <= code <= 80014:
        return Outcome.RATE_LIMITED
    if status == 403 or code in {10, 200}:
        return Outcome.PERMISSION_DENIED
    # A top-level OAuth error does not prove the inspected subject is revoked.
    return Outcome.INSPECTION_ERROR


class OfficialTokenVerifier:
    def __init__(self, *, opener_factory: Callable = _opener,
                 clock: Callable = time.time, monotonic: Callable = time.monotonic):
        self._opener_factory = opener_factory
        self._clock = clock
        self._monotonic = monotonic

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            raise _Failure(Outcome.TIMEOUT)
        return remaining

    def _read(self, response, url: str, deadline: float) -> tuple[int, bytes]:
        status = response.getcode()
        if type(status) is not int or not 100 <= status <= 599:
            raise _Failure(Outcome.UNEXPECTED)
        if 300 <= status <= 399 or response.geturl() != url:
            raise _Failure(Outcome.REDIRECTED)
        headers = response.headers
        if headers.get_content_type() != "application/json" or headers.get("Content-Encoding", "identity").lower() != "identity":
            raise _Failure(Outcome.UNEXPECTED)
        lengths = headers.get_all("Content-Length", [])
        if len(lengths) > 1 or lengths and (not re.fullmatch(r"[0-9]{1,10}", lengths[0]) or int(lengths[0]) > MAX_BODY_BYTES):
            raise _Failure(Outcome.OVERSIZED if len(lengths) == 1 and lengths[0].isdigit() and int(lengths[0]) > MAX_BODY_BYTES else Outcome.UNEXPECTED)
        stream = response.fp if isinstance(response, urllib.error.HTTPError) else response
        content = bytearray()
        while True:
            remaining = self._remaining(deadline)
            # urllib HTTPS responses expose HTTPResponse.fp (BufferedReader) and
            # its socket. read1 performs one receive; reset to the total remaining
            # deadline so a continuously dripping body cannot extend the budget.
            sock = getattr(getattr(getattr(stream, "fp", None), "raw", None), "_sock", None)
            if sock is None:
                raise _Failure(Outcome.UNEXPECTED)
            sock.settimeout(remaining)
            part = stream.read1(min(4096, MAX_BODY_BYTES + 1 - len(content)))
            self._remaining(deadline)
            if not isinstance(part, bytes):
                raise _Failure(Outcome.UNEXPECTED)
            content.extend(part)
            if len(content) > MAX_BODY_BYTES:
                raise _Failure(Outcome.OVERSIZED)
            if not part or getattr(stream, "isclosed", lambda: False)():
                break
        if lengths and len(content) != int(lengths[0]):
            raise _Failure(Outcome.MALFORMED)
        return status, bytes(content)

    def verify(self, expected_app_id: str, subject_token: Secret, app_secret: Secret, *,
               api_version: str, required_scopes=()) -> TokenVerification:
        try:
            if (not isinstance(expected_app_id, str) or not _ID.fullmatch(expected_app_id)
                    or not isinstance(api_version, str) or not _VERSION.fullmatch(api_version)
                    or not isinstance(subject_token, Secret) or not isinstance(app_secret, Secret)):
                raise ValueError
            token, secret = subject_token.value, app_secret.value
            if any(not isinstance(value, str) or not 16 <= len(value) <= 4096
                   or not all(33 <= ord(c) <= 126 for c in value) for value in (token, secret)) or "|" in secret:
                raise ValueError
            required = _scopes(required_scopes)
            inspector = expected_app_id + "|" + secret
            variants = {value for raw in (token, secret, inspector)
                        for value in (raw, urllib.parse.quote(raw, safe=""), urllib.parse.quote_plus(raw))}
            if any(any(value in safe for value in variants) for safe in (expected_app_id, api_version, *required)):
                raise ValueError
            instant = self._clock()
            if type(instant) not in (int, float) or not math.isfinite(instant) or not 0 <= instant <= _MAX_TIMESTAMP:
                raise ValueError
            now = int(instant)
        except Exception:
            return TokenVerification(Outcome.INVALID_INPUT)
        base = {"expected_app_id": expected_app_id, "api_version": api_version, "verified_at": now}
        url = "https://graph.facebook.com/" + api_version + "/debug_token?" + urllib.parse.urlencode({
            "input_token": token, "access_token": inspector})
        if len(url.encode("ascii")) > MAX_REQUEST_BYTES:
            return TokenVerification(Outcome.INVALID_INPUT)
        request = _Request(url, method="GET", headers={"Accept": "application/json", "Accept-Encoding": "identity"})
        try:
            deadline = self._monotonic() + TIMEOUT_SECONDS
            opener = self._opener_factory()
            try:
                response = opener.open(request, timeout=self._remaining(deadline))
            except urllib.error.HTTPError as error:
                response = error
            with response:
                status, body = self._read(response, url, deadline)
            payload = _json(body)
            if not isinstance(payload, dict):
                raise ValueError
            base["http_status"] = status
            if status != 200 or "error" in payload:
                if "data" in payload:
                    raise ValueError
                code, subcode = _error_codes(payload.get("error", {}))
                return TokenVerification(_error_outcome(status, code), **base, graph_code=code, graph_subcode=subcode)
            data = payload.get("data")
            if not isinstance(data, dict) or type(data.get("is_valid")) is not bool:
                raise ValueError
            valid = data["is_valid"]
            app_id = data.get("app_id")
            if app_id is not None and (not isinstance(app_id, str) or not _ID.fullmatch(app_id)):
                raise ValueError
            if valid and app_id is None:
                raise ValueError
            raw_type = data.get("type")
            if raw_type is not None and not isinstance(raw_type, str) or valid and raw_type is None:
                raise ValueError
            token_type = TokenType(raw_type) if raw_type in TokenType._value2member_map_ else None
            expires, data_expires = _expiry(data, "expires_at", now), _expiry(data, "data_access_expires_at", now)
            scopes = _scopes(data["scopes"]) if "scopes" in data else None
            if any(any(value in safe for value in variants) for safe in (app_id or "", raw_type or "", *(scopes or ()))):
                raise ValueError
            missing = tuple(scope for scope in required if scopes is not None and scope not in scopes)
            comparison = ScopeComparison.NOT_REQUESTED if not required else ScopeComparison.UNKNOWN if scopes is None else ScopeComparison.MISSING if missing else ScopeComparison.MET
            metadata = dict(base, subject_valid=valid, verified_token_app_id=app_id,
                            app_matches=app_id == expected_app_id if app_id is not None else None,
                            token_type=token_type, expires=expires, data_access_expires=data_expires,
                            scopes=scopes, required_scopes=comparison, missing_scopes=missing)
            if not valid:
                outcome = Outcome.SUBJECT_INVALID
            elif token_type is None:
                outcome = Outcome.UNSUPPORTED_TYPE
            elif app_id != expected_app_id:
                outcome = Outcome.APP_MISMATCH
            elif ExpiryState.EXPIRED in (expires.state, data_expires.state):
                outcome = Outcome.EXPIRED
            else:
                outcome = Outcome.APP_MATCH
            return TokenVerification(outcome, **metadata)
        except _Failure as error:
            return TokenVerification(error.outcome, **base)
        except (TimeoutError, socket.timeout):
            return TokenVerification(Outcome.TIMEOUT, **base)
        except urllib.error.URLError as error:
            return TokenVerification(Outcome.TIMEOUT if isinstance(error.reason, TimeoutError) else Outcome.NETWORK_ERROR, **base)
        except (ValueError, UnicodeError, TypeError):
            return TokenVerification(Outcome.MALFORMED, **base)
        except Exception:
            return TokenVerification(Outcome.NETWORK_ERROR, **base)
