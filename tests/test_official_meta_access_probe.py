"""Official HTTP access contract, exercised entirely with fake HTTP responses."""
from __future__ import annotations

import io
import json
from email.message import Message
from http.client import IncompleteRead
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import HTTPSHandler
from urllib.response import addinfourl

import pytest

from scripts import acl_meta_access_probe as cli
from src.meta_campaign_agent import official_access_probe as probe

TOKEN = "synthetic-access-token"
QUERY = probe.ProbeRequest("act_12345", "2026-10-01", "2026-10-02")


def account(**overrides):
    return {"id": "act_12345", "account_id": "12345", "currency": "USD",
            "timezone_name": "America/New_York", "account_status": 1, **overrides}


def spend(**overrides):
    return {"data": [{"account_id": "12345", "account_currency": "USD", "date_start": QUERY.since,
                      "date_stop": QUERY.until, "spend": "12.34", **overrides}]}


class Reply(io.BytesIO):
    def __init__(self, body, code=200):
        super().__init__(body if isinstance(body, bytes) else json.dumps(body).encode())
        self.code = code


class FakeHTTP:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def open(self, request, *, timeout):
        self.calls.append((request, timeout))
        assert request.get_method() == "GET", "This probe must never write."
        assert request.data is None
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def run(*replies):
    http = FakeHTTP(*replies)
    result = probe.OfficialAccessProbe(probe.AccessToken(TOKEN), opener=http).read(QUERY)
    return result, http


def test_exact_account_and_spend_are_verified_with_two_bounded_gets():
    replies = [Reply(account(extra=TOKEN)), Reply(spend())]
    result, http = run(*replies)
    out = result.to_dict()
    assert out["status"] == "ok"
    assert out["account"] == {"account_id": "act_12345", "currency": "USD",
                              "timezone_name": "America/New_York", "account_status": 1}
    assert out["spend"] == {"amount": "12.34", "currency": "USD", "since": QUERY.since,
                            "until": QUERY.until, "state": "reported"}
    assert out["publishing_access"] == "not_checked"
    assert TOKEN not in json.dumps(out)
    assert len(http.calls) == 2
    for request, timeout in http.calls:
        url = urlsplit(request.full_url)
        assert url.scheme == "https" and url.hostname == "graph.facebook.com"
        assert url.path in {"/v26.0/act_12345", "/v26.0/act_12345/insights"}
        assert request.get_header("Authorization") == "Bearer " + TOKEN
        assert not request.has_header("Cookie") and TOKEN not in request.full_url
        assert 0 < timeout <= 5
    params = parse_qs(urlsplit(http.calls[1][0].full_url).query)
    assert params["level"] == ["account"]
    assert json.loads(params["time_range"][0]) == {"since": QUERY.since, "until": QUERY.until}
    assert all(reply.closed for reply in replies)


@pytest.mark.parametrize("fields", [{"id": "act_54321"}, {"account_id": "54321"}, {"id": None}])
def test_account_identity_mismatch_stops_before_insights(fields):
    result, http = run(Reply(account(**fields)))
    assert result.failure.kind == probe.FailureKind.ACCOUNT_MISMATCH
    assert result.account is None and result.spend is None
    assert len(http.calls) == 1


@pytest.mark.parametrize("code,status,kind", [
    (190, 400, "invalid_credential"), (102, 400, "invalid_credential"),
    (10, 400, "permission"), (200, 400, "permission"), (None, 403, "permission"),
    (4, 400, "rate_limit"), (80004, 400, "rate_limit"), (None, 429, "rate_limit"),
    (None, 401, "invalid_credential"), (2, 503, "network"), (100, 400, "unexpected_response"),
])
def test_api_errors_are_typed_without_raw_messages_or_retries(code, status, kind):
    error = {"error": {"code": code, "error_subcode": 463, "message": TOKEN,
                       "error_user_msg": "access_token=" + TOKEN, "fbtrace_id": TOKEN}}
    response = HTTPError("https://graph.facebook.com/?access_token=" + TOKEN, status, TOKEN, {},
                         io.BytesIO(json.dumps(error).encode()))
    result, http = run(response)
    assert result.failure.kind.value == kind
    assert result.failure.meta_subcode == 463
    assert len(http.calls) == 1
    assert TOKEN not in repr(result) and TOKEN not in json.dumps(result.to_dict())


@pytest.mark.parametrize("error", [URLError(TOKEN), TimeoutError(TOKEN), OSError(TOKEN), IncompleteRead(TOKEN.encode())])
def test_transport_errors_do_not_expose_credential_or_retry(error):
    result, http = run(error)
    assert result.failure.kind == probe.FailureKind.NETWORK
    assert TOKEN not in repr(result)
    assert len(http.calls) == 1


def test_spend_failure_preserves_verified_metadata_as_partial_not_success():
    result, http = run(Reply(account()), Reply({"error": {"code": 200, "message": TOKEN}}, 403))
    assert result.to_dict()["status"] == "partial"
    assert result.account.account_id == QUERY.account_id
    assert result.spend is None
    assert result.failure.step == "spend" and result.failure.kind == probe.FailureKind.PERMISSION
    assert len(http.calls) == 2


@pytest.mark.parametrize("field,value,kind", [
    ("account_id", "54321", "account_mismatch"), ("account_currency", "EUR", "unexpected_response"),
    ("date_start", "2026-09-30", "unexpected_response"), ("date_stop", "2026-10-03", "unexpected_response"),
    ("spend", "NaN", "unexpected_response"), ("spend", "-1", "unexpected_response"),
    ("spend", None, "unexpected_response"), ("spend", TOKEN, "unexpected_response"),
])
def test_wrong_report_identity_range_currency_or_amount_is_never_accepted(field, value, kind):
    result, _ = run(Reply(account()), Reply(spend(**{field: value})))
    assert result.failure.kind.value == kind and result.spend is None
    assert TOKEN not in json.dumps(result.to_dict())


def test_empty_report_is_no_data_not_zero():
    result, _ = run(Reply(account()), Reply({"data": []}))
    assert result.failure is None
    assert result.to_dict()["spend"]["state"] == "no_data"
    assert result.spend.amount is None


@pytest.mark.parametrize("payload", [
    {"data": [], "paging": {"next": "https://other.invalid/?access_token=" + TOKEN}},
    {"data": [{}, {}]}, {"data": ["malformed"]}, {}, b"not JSON", b"[1, 2]",
])
def test_incomplete_and_malformed_reports_stop_without_following_links(payload):
    result, http = run(Reply(account()), Reply(payload))
    assert result.failure.kind == probe.FailureKind.UNEXPECTED_RESPONSE
    assert len(http.calls) == 2 and result.spend is None
    assert TOKEN not in repr(result)


def test_redirect_does_not_make_a_second_request():
    response = HTTPError("https://graph.facebook.com/", 302, TOKEN,
                         {"Location": "https://other.invalid/"}, io.BytesIO(b""))
    result, http = run(response)
    assert result.failure.kind == probe.FailureKind.UNEXPECTED_RESPONSE
    assert len(http.calls) == 1
    assert probe._NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.invalid") is None


def test_default_opener_disables_ambient_proxy_and_cookie_handlers(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://other.invalid:9999")
    client = probe.OfficialAccessProbe(probe.AccessToken(TOKEN))
    # These are the configured urllib handlers actually used for live reads.
    from urllib.request import HTTPCookieProcessor, ProxyHandler, HTTPRedirectHandler, HTTPSHandler
    for handler in client._opener.handlers:
        assert not isinstance(handler, HTTPCookieProcessor)
        if isinstance(handler, ProxyHandler):
            assert not handler.proxies
        if isinstance(handler, HTTPRedirectHandler):
            assert isinstance(handler, probe._NoRedirect)
        if isinstance(handler, HTTPSHandler):
            assert handler._context.check_hostname


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_real_urllib_handler_chain_refuses_redirect_without_network(monkeypatch, status):
    calls = []

    class InMemoryHTTPS(HTTPSHandler):
        def https_open(self, request):
            calls.append(request)
            headers = Message()
            headers["Location"] = "https://other.invalid/?access_token=" + TOKEN
            response = addinfourl(io.BytesIO(b"redirect"), headers, request.full_url, status)
            response.msg = "Redirect"
            return response

    monkeypatch.setattr(probe, "HTTPSHandler", InMemoryHTTPS)
    result = probe.OfficialAccessProbe(probe.AccessToken(TOKEN)).read(QUERY)
    assert result.failure.kind == probe.FailureKind.UNEXPECTED_RESPONSE
    assert len(calls) == 1 and urlsplit(calls[0].full_url).hostname == "graph.facebook.com"
    assert TOKEN not in repr(result)


def test_real_urllib_handler_chain_never_replays_response_cookies(monkeypatch):
    calls = []
    bodies = [account(), spend()]

    class InMemoryHTTPS(HTTPSHandler):
        def https_open(self, request):
            calls.append(request)
            headers = Message()
            headers["Set-Cookie"] = "browser_session=synthetic; Path=/; Secure"
            response = addinfourl(io.BytesIO(json.dumps(bodies.pop(0)).encode()), headers, request.full_url, 200)
            response.msg = "OK"
            return response

    monkeypatch.setattr(probe, "HTTPSHandler", InMemoryHTTPS)
    result = probe.OfficialAccessProbe(probe.AccessToken(TOKEN)).read(QUERY)
    assert result.failure is None and len(calls) == 2
    assert all(not request.has_header("Cookie") and request.get_method() == "GET" for request in calls)


def test_response_size_is_bounded_and_closed():
    response = Reply(b"x" * (probe.MAX_BODY_BYTES + 1))
    result, http = run(response)
    assert result.failure.kind == probe.FailureKind.UNEXPECTED_RESPONSE
    assert len(http.calls) == 1 and response.closed


def test_body_deadline_stops_stream_even_when_each_read_answers(monkeypatch):
    ticks = iter([0, 0, probe.BODY_DEADLINE_S + 1])
    monkeypatch.setattr(probe.time, "monotonic", lambda: next(ticks))
    result, http = run(Reply(b" " * 5000))
    assert result.failure.kind == probe.FailureKind.NETWORK
    assert len(http.calls) == 1


@pytest.mark.parametrize("fields", [
    {"currency": "bad"}, {"timezone_name": None}, {"account_status": True}, {"timezone_name": TOKEN},
])
def test_invalid_or_credential_echoing_metadata_is_not_reported(fields):
    result, _ = run(Reply(account(**fields)))
    assert result.account is None and result.failure is not None
    assert TOKEN not in repr(result)


@pytest.mark.parametrize("account_id,since,until", [
    ("https://other.invalid", QUERY.since, QUERY.until), ("act_12345/ads", QUERY.since, QUERY.until),
    ("act_12345?method=POST", QUERY.since, QUERY.until), ("act_12345\n", QUERY.since, QUERY.until),
    ("act_12345", "2026-10-02", "2026-10-01"), ("act_12345", "2026-01-01", "2026-02-01"),
    ("act_12345", "2026-W01-1", "2026-01-01"), ("act_12345", "2026-02-30", "2026-02-30"),
])
def test_invalid_request_cannot_reach_transport(account_id, since, until):
    with pytest.raises(ValueError):
        probe.ProbeRequest(account_id, since, until)


@pytest.mark.parametrize("value", ["", "token\r\nInjected: yes", "has space", "x" * 8193])
def test_invalid_credential_header_is_rejected(value):
    with pytest.raises(ValueError):
        probe.AccessToken(value)
    assert TOKEN not in repr(probe.AccessToken(TOKEN))


def args(*extra):
    return ["--account", QUERY.account_id, "--since", QUERY.since, "--until", QUERY.until, *extra]


def test_cli_does_not_discover_credential_in_existing_meta_env(monkeypatch, capsys):
    monkeypatch.setenv("META_ACCESS_TOKEN", TOKEN)
    monkeypatch.setenv(cli.TOKEN_ENV, TOKEN)
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "")
    assert cli.main(args()) == 2
    out = capsys.readouterr()
    assert "invalid_credential" in out.out and TOKEN not in out.out + out.err


@pytest.mark.parametrize("option", ["--token", "--access-token", "--token-env"])
def test_cli_never_echoes_a_token_pasted_as_an_argument(option, capsys):
    with pytest.raises(SystemExit) as error:
        cli.main(args(option, TOKEN))
    assert error.value.code == 2
    out = capsys.readouterr()
    assert TOKEN not in out.out + out.err


def test_cli_rejects_invalid_input_before_prompt_or_network(monkeypatch, capsys):
    def forbidden(*a, **k):
        pytest.fail("Invalid input must not prompt or create a network client.")
    monkeypatch.setattr(cli.getpass, "getpass", forbidden)
    monkeypatch.setattr(cli, "OfficialAccessProbe", forbidden)
    assert cli.main(["--account", "bad", "--since", QUERY.since, "--until", QUERY.until]) == 2
    assert "invalid_input" in capsys.readouterr().out


@pytest.mark.parametrize("via_env", [False, True])
def test_cli_explicit_secret_input_uses_the_same_read_only_operation(monkeypatch, capsys, via_env):
    http = FakeHTTP(Reply(account()), Reply(spend()))
    monkeypatch.setattr(cli, "OfficialAccessProbe", lambda credential: probe.OfficialAccessProbe(credential, opener=http))
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: TOKEN)
    monkeypatch.setenv(cli.TOKEN_ENV, TOKEN)
    assert cli.main(args(*(["--token-env", cli.TOKEN_ENV] if via_env else []))) == 0
    out = capsys.readouterr()
    assert json.loads(out.out)["status"] == "ok" and TOKEN not in out.out + out.err
    assert len(http.calls) == 2


def test_cli_refuses_echoed_input_fallback(monkeypatch, capsys):
    def no_terminal(prompt):
        import warnings
        warnings.warn(TOKEN, cli.getpass.GetPassWarning)
        pytest.fail("Must stop before getpass falls back to echoed input.")
    monkeypatch.setattr(cli.getpass, "getpass", no_terminal)
    assert cli.main(args()) == 2
    out = capsys.readouterr()
    assert TOKEN not in out.out + out.err


def test_cli_unexpected_platform_error_is_redacted(monkeypatch, capsys):
    def broken(credential):
        raise RuntimeError("Authorization: Bearer " + TOKEN)
    monkeypatch.setattr(cli, "OfficialAccessProbe", broken)
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: TOKEN)
    assert cli.main(args()) == 2
    out = capsys.readouterr()
    assert TOKEN not in out.out + out.err and "unexpected_response" in out.out
