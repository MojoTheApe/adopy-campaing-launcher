"""Public inspection against urllib's actual handlers, without a network socket."""
import concurrent.futures
import http.client
import io
import json
import socket
import ssl
import subprocess
import sys
import getpass
import warnings
from pathlib import Path
import urllib.parse
import urllib.request

import pytest

from src.meta_campaign_agent import official_token_verifier as m
from scripts import acl_meta_token_probe as cli

TOKEN = "synthetic_subject/+token_879"
SECRET = "synthetic_inspector/+secret_879"
APP = "1234567890123456"
NOW = 1800000000


class Raw(io.RawIOBase):
    def __init__(self, sock, body):
        self._sock = sock
        self.body = io.BytesIO(body)

    def readable(self):
        return True

    def readinto(self, target):
        part = self.body.read(len(target))
        target[:len(part)] = part
        return len(part)


class Socket:
    def __init__(self, body):
        self.body = body
        self.timeouts = []

    def makefile(self, mode):
        return io.BufferedReader(Raw(self, self.body))

    def settimeout(self, value):
        self.timeouts.append(value)

    def close(self):
        pass


@pytest.fixture
def wire(monkeypatch):
    state = {"requests": [], "connections": [], "status": 200, "headers": [],
             "payload": {"data": {"is_valid": True, "app_id": APP, "type": "USER"}}}

    class Connection:
        debuglevel = 0
        def __init__(self, host, timeout=None, context=None, **kwargs):
            assert host == "graph.facebook.com"
            assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
            self.sock = None
            self.timeout = timeout
            self.response = None
            state["connections"].append(self)

        def set_debuglevel(self, level):
            assert level == 0

        def request(self, method, url, body, headers, **kwargs):
            state["requests"].append((method, url, body, headers))
            if state.get("error"):
                raise state["error"]

        def getresponse(self):
            body = state.get("body", json.dumps(state["payload"]).encode())
            headers = list(state["headers"])
            if not state.get("no_length"):
                headers.append(("Content-Length", str(len(body))))
            if not state.get("no_type"):
                headers.append(("Content-Type", "application/json"))
            message = (f"HTTP/1.1 {state['status']} Test\r\n" +
                       "".join(f"{k}: {v}\r\n" for k, v in headers) + "\r\n").encode() + body
            self.socket = Socket(message)
            self.sock = self.socket
            self.response = http.client.HTTPResponse(self.socket, method="GET")
            self.response.begin()
            return self.response

        def close(self):
            if self.response:
                self.response.close()

    monkeypatch.setattr(http.client, "HTTPSConnection", Connection)
    monkeypatch.setenv("HTTPS_PROXY", "http://ambient-proxy.invalid:9999")
    monkeypatch.setenv("HTTP_PROXY", "http://ambient-proxy.invalid:9999")
    monkeypatch.setenv("ALL_PROXY", "http://ambient-proxy.invalid:9999")
    monkeypatch.setattr(urllib.request, "getproxies", lambda: pytest.fail("ambient proxy discovery"))
    return state


def inspect(wire, **kwargs):
    return m.OfficialTokenVerifier(clock=lambda: NOW).verify(
        kwargs.pop("app", APP), m.Secret(kwargs.pop("token", TOKEN)),
        m.Secret(kwargs.pop("secret", SECRET)), api_version=kwargs.pop("version", "v24.0"), **kwargs)


def assert_private(result, *values):
    output = repr(result) + json.dumps(result.as_dict())
    for raw in (TOKEN, SECRET, APP + "|" + SECRET, *values):
        for value in (raw, urllib.parse.quote(raw, safe=""), urllib.parse.quote_plus(raw)):
            assert value not in output


def test_actual_handlers_fixed_get_and_typed_evidence(wire):
    wire["payload"]["data"].update(scopes=["ads_read", "ads_read"], expires_at=0,
                                  data_access_expires_at=NOW+100, user_id="private_user",
                                  granular_scopes=[{"target_ids": ["private_target"]}])
    result = inspect(wire, required_scopes=["ads_read", "ads_management"])
    assert result.outcome == m.Outcome.APP_MATCH
    assert result.expires.state == m.ExpiryState.ZERO
    assert result.data_access_expires.state == m.ExpiryState.FUTURE
    assert result.required_scopes == m.ScopeComparison.MISSING
    assert result.missing_scopes == ("ads_management",)
    assert len(wire["requests"]) == 1
    method, url, body, headers = wire["requests"][0]
    parsed = urllib.parse.urlsplit(url)
    assert method == "GET" and body is None and parsed.path == "/v24.0/debug_token"
    assert urllib.parse.parse_qs(parsed.query) == {"input_token": [TOKEN], "access_token": [APP+"|"+SECRET]}
    assert not any(key.lower() in {"cookie", "authorization"} for key in headers)
    assert wire["connections"][0].response.isclosed()
    assert 0 < wire["connections"][0].timeout <= m.TIMEOUT_SECONDS
    assert all(0 < value <= m.TIMEOUT_SECONDS for value in wire["connections"][0].socket.timeouts)
    assert "private_user" not in repr(result) and "private_target" not in repr(result)
    assert {result.as_dict()[key] for key in ("account_access", "read_readiness", "publishing")} == {"not_checked"}
    assert_private(result)


@pytest.mark.parametrize("token_type", ["USER", "PAGE", "APP", "SYSTEM_USER"])
def test_known_types_and_unknown_expiry(wire, token_type):
    wire["payload"]["data"]["type"] = token_type
    result = inspect(wire, required_scopes=["ads_read"])
    assert result.outcome == m.Outcome.APP_MATCH and result.token_type.value == token_type
    assert result.expires.state == m.ExpiryState.UNKNOWN
    assert result.scopes is None and result.required_scopes == m.ScopeComparison.UNKNOWN


def test_reported_required_scopes_and_exact_app_identity(wire):
    wire["payload"]["data"].update(scopes=["ads_read", "public_profile"])
    result = inspect(wire, required_scopes=["ads_read"])
    assert result.required_scopes == m.ScopeComparison.MET and not result.missing_scopes
    wire["payload"]["data"]["app_id"] = "12345678901234560"
    assert inspect(wire).outcome == m.Outcome.APP_MISMATCH


@pytest.mark.parametrize("payload", [{"error": {"code": True}}, {"error": {"code": "190"}},
    {"error": {"error_subcode": -1}}, {"error": "unsafe"},
    {"error": {"code": 190}, "data": {"is_valid": True, "app_id": APP, "type": "USER"}}])
def test_malformed_inspector_error_cannot_succeed(wire, payload):
    wire["payload"] = payload
    assert inspect(wire).outcome == m.Outcome.MALFORMED


@pytest.mark.parametrize("change,expected", [
    ({"app_id": "9876543210987654"}, m.Outcome.APP_MISMATCH),
    ({"is_valid": False}, m.Outcome.SUBJECT_INVALID),
    ({"type": "NEW_UNKNOWN"}, m.Outcome.UNSUPPORTED_TYPE),
    ({"expires_at": NOW}, m.Outcome.EXPIRED),
    ({"data_access_expires_at": NOW-1}, m.Outcome.EXPIRED),
    ({"scopes": []}, m.Outcome.APP_MATCH),
    ({"scopes": ["ads_read"]}, m.Outcome.APP_MATCH),
    ({"is_valid": "true"}, m.Outcome.MALFORMED),
    ({"is_valid": 1}, m.Outcome.MALFORMED),
    ({"app_id": int(APP)}, m.Outcome.MALFORMED),
    ({"app_id": None}, m.Outcome.MALFORMED),
    ({"type": None}, m.Outcome.MALFORMED),
    ({"type": []}, m.Outcome.MALFORMED),
    ({"expires_at": True}, m.Outcome.MALFORMED),
    ({"expires_at": -1}, m.Outcome.MALFORMED),
    ({"expires_at": 1.5}, m.Outcome.MALFORMED),
    ({"expires_at": "0"}, m.Outcome.MALFORMED),
    ({"expires_at": None}, m.Outcome.MALFORMED),
    ({"scopes": None}, m.Outcome.MALFORMED),
    ({"scopes": "ads_read"}, m.Outcome.MALFORMED),
    ({"scopes": [1]}, m.Outcome.MALFORMED),
    ({"scopes": ["invalid.scope"]}, m.Outcome.MALFORMED),
])
def test_strict_public_metadata(wire, change, expected):
    wire["payload"]["data"].update(change)
    assert inspect(wire).outcome == expected
    assert len(wire["requests"]) == 1


@pytest.mark.parametrize("payload,expected", [({}, m.Outcome.MALFORMED),
    ({"data": {}}, m.Outcome.MALFORMED), ({"data": {"is_valid": False}}, m.Outcome.SUBJECT_INVALID),
    ({"data": []}, m.Outcome.MALFORMED), ([], m.Outcome.MALFORMED)])
def test_missing_data_and_validity(wire, payload, expected):
    wire["payload"] = payload
    assert inspect(wire).outcome == expected


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_no_redirect_or_cookie_replay(wire, code):
    wire["status"] = code
    wire["headers"] = [("Location", "https://other.invalid/steal"), ("Set-Cookie", "session=unsafe")]
    assert inspect(wire).outcome == m.Outcome.REDIRECTED
    assert len(wire["requests"]) == 1
    wire["status"], wire["headers"] = 200, []
    assert inspect(wire).outcome == m.Outcome.APP_MATCH
    assert not any(k.lower() == "cookie" for k in wire["requests"][1][3])


@pytest.mark.parametrize("status,code,expected", [(401,190,m.Outcome.INSPECTOR_REJECTED),
    (400,190,m.Outcome.INSPECTION_ERROR), (200,190,m.Outcome.INSPECTION_ERROR),
    (403,200,m.Outcome.PERMISSION_DENIED), (400,10,m.Outcome.PERMISSION_DENIED),
    (429,4,m.Outcome.RATE_LIMITED), (400,613,m.Outcome.RATE_LIMITED),
    (500,1,m.Outcome.INSPECTION_ERROR)])
def test_inspector_errors_do_not_revoke_subject(wire, status, code, expected):
    wire["status"] = status
    wire["payload"] = {"error": {"code": code, "error_subcode": 123, "message": TOKEN+SECRET}}
    result = inspect(wire)
    assert result.outcome == expected and result.subject_valid is None
    assert result.graph_code == code and result.graph_subcode == 123
    assert len(wire["requests"]) == 1
    assert_private(result)


@pytest.mark.parametrize("kwargs", [{"app": "act_123"}, {"app": "0"}, {"version": ""},
    {"version": "v24.0/other"}, {"version": "https://other.invalid"}, {"token": "short"},
    {"token": "x"*4097}, {"token": "newline\n"*20}, {"secret": "x"*16+"|"},
    {"required_scopes": ["ads.read"]}, {"required_scopes": "ads_read"}])
def test_invalid_local_inputs_zero_requests(wire, kwargs):
    assert inspect(wire, **kwargs).outcome == m.Outcome.INVALID_INPUT
    assert not wire["connections"] and not wire["requests"]


@pytest.mark.parametrize("body", [b'null', b'{"data":{"is_valid":true,"is_valid":false}}',
    b'{"data":{"is_valid":NaN}}', b'not json', b'\xff'])
def test_malformed_bodies(wire, body):
    wire["body"] = body
    result = inspect(wire)
    assert result.outcome == m.Outcome.MALFORMED
    assert_private(result)


def test_oversized_body_with_and_without_length(wire):
    wire["body"] = b"x"*(m.MAX_BODY_BYTES+1)
    assert inspect(wire).outcome == m.Outcome.OVERSIZED
    wire["no_length"] = True
    assert inspect(wire).outcome == m.Outcome.OVERSIZED
    assert all(conn.response.isclosed() for conn in wire["connections"])


@pytest.mark.parametrize("headers,no_type", [([("Content-Encoding", "gzip")], False),
    ([("Content-Type", "text/html")], True), ([("Content-Length", "999")], False)])
def test_unexpected_headers(wire, headers, no_type):
    wire["headers"], wire["no_type"] = headers, no_type
    assert inspect(wire).outcome == m.Outcome.UNEXPECTED


@pytest.mark.parametrize("error,expected", [(socket.timeout(TOKEN), m.Outcome.TIMEOUT),
    (OSError(SECRET), m.Outcome.NETWORK_ERROR)])
def test_network_failure_no_retry_or_unsafe_exception(wire, error, expected, caplog):
    wire["error"] = error
    result = inspect(wire)
    assert result.outcome == expected and len(wire["requests"]) == 1
    assert_private(result)
    assert TOKEN not in caplog.text and SECRET not in caplog.text


def test_total_body_deadline_and_close(wire, monkeypatch):
    tick = [0.0]
    original = http.client.HTTPResponse.read1
    def dripping(response, size):
        part = original(response, 1)
        tick[0] += 4
        return part
    monkeypatch.setattr(http.client.HTTPResponse, "read1", dripping)
    result = m.OfficialTokenVerifier(clock=lambda: NOW, monotonic=lambda: tick[0]).verify(
        APP, m.Secret(TOKEN), m.Secret(SECRET), api_version="v24.0")
    assert result.outcome == m.Outcome.TIMEOUT
    assert wire["connections"][0].socket.timeouts == [10, 6, 2]
    assert wire["connections"][0].response.isclosed()


def test_secret_reprs_and_untrusted_echo(wire):
    assert TOKEN not in repr(m.Secret(TOKEN)) and SECRET not in str(m.Secret(SECRET))
    wire["payload"]["data"]["scopes"] = ["synthetic_inspector_secret_879"]
    result = inspect(wire, secret="synthetic_inspector_secret_879")
    assert result.outcome == m.Outcome.MALFORMED
    assert_private(result, "synthetic_inspector_secret_879")
    assert TOKEN not in repr(m._Request("https://graph.facebook.com/?input_token="+TOKEN))


def test_independent_parallel_invocations(wire):
    credentials = [(TOKEN+str(i), SECRET+str(i)) for i in range(2)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda pair: inspect(wire, token=pair[0], secret=pair[1]), credentials))
    assert all(result.outcome == m.Outcome.APP_MATCH for result in results)
    actual = {(urllib.parse.parse_qs(urllib.parse.urlsplit(req[1]).query)["input_token"][0],
               urllib.parse.parse_qs(urllib.parse.urlsplit(req[1]).query)["access_token"][0]) for req in wire["requests"]}
    assert actual == {(token, APP+"|"+secret) for token, secret in credentials}


def run_cli(wire, args=(), **kwargs):
    output, errors = io.StringIO(), io.StringIO()
    prompts = iter((TOKEN, SECRET))
    code = cli.main(["--app-id", APP, "--api-version", "v24.0", *args],
                    verifier=cli.OfficialTokenVerifier(clock=lambda: NOW),
                    prompt=kwargs.pop("prompt", lambda _: next(prompts)),
                    stdout=output, stderr=errors, **kwargs)
    return code, output.getvalue(), errors.getvalue()


def test_cli_hidden_prompt_actual_fake_http(wire):
    code, output, errors = run_cli(wire)
    assert code == 0 and not errors
    assert json.loads(output)["outcome"] == "app_match"
    assert len(wire["requests"]) == 1
    assert TOKEN not in output and SECRET not in output


def test_cli_explicit_env_only_and_scope_exit(wire):
    class NamedOnly:
        def __getitem__(self, name):
            assert name in {"PROBE_TOKEN", "PROBE_SECRET"}
            return TOKEN if name == "PROBE_TOKEN" else SECRET
    wire["payload"]["data"]["scopes"] = []
    code, output, errors = run_cli(wire, ["--token-env", "PROBE_TOKEN", "--app-secret-env", "PROBE_SECRET",
                                        "--require-scope", "ads_read"], environ=NamedOnly(),
                                 prompt=lambda _: pytest.fail("must not prompt"))
    assert code == 4 and not errors
    assert json.loads(output)["required_scopes"] == "missing"


@pytest.mark.parametrize("args", [["--token", TOKEN], [TOKEN], ["--app-id", TOKEN],
    ["--api-version", SECRET], ["--token-env", TOKEN], ["--require-scope", TOKEN]])
def test_cli_rejected_arguments_never_echo_or_request(wire, args):
    code, output, errors = run_cli(wire, args, prompt=lambda _: pytest.fail("must not prompt"))
    assert code == 2 and not output and "Invalid diagnostic arguments" in errors
    assert not wire["connections"]
    assert TOKEN not in errors and SECRET not in errors


def test_cli_refuses_hidden_input_fallback_and_missing_env(wire, monkeypatch):
    echoed = []
    def unavailable(prompt):
        warnings.warn("echo fallback", getpass.GetPassWarning)
        echoed.append(True)
        return TOKEN
    monkeypatch.setattr(getpass, "getpass", unavailable)
    code, output, errors = run_cli(wire, prompt=cli._hidden_input)
    assert code == 2 and not echoed and not output and "No request made" in errors
    assert not wire["connections"]
    code, output, errors = run_cli(wire, ["--token-env", "ABSENT"], environ={})
    assert code == 2 and not output and not wire["connections"]


def test_cli_safe_exception_and_exit_outcomes(wire):
    wire["error"] = OSError(TOKEN+urllib.parse.quote(SECRET, safe=""))
    code, output, errors = run_cli(wire)
    assert code == 3 and not errors and json.loads(output)["outcome"] == "network_error"
    assert TOKEN not in output and urllib.parse.quote(SECRET, safe="") not in output
    wire.pop("error")
    wire["payload"]["data"]["app_id"] = "9876543210987654"
    assert run_cli(wire)[0] == 1


def test_real_cli_entry_with_fake_https_and_no_config_import():
    root = Path(__file__).resolve().parents[1]
    # Real script __main__, real urllib chain; ONLY HTTPSConnection/getpass are
    # replaced in the child. No live socket or production config is loaded.
    code = '''
import sys, runpy, getpass, builtins
sys.path.insert(0, "tests")
import pytest, test_acl1879_official_token_verifier as t
patch=pytest.MonkeyPatch()
wire=t.wire.__wrapped__(patch)
values=iter((t.TOKEN,t.SECRET))
getpass.getpass=lambda label: next(values)
original=builtins.__import__
def guarded(name,*args,**kwargs):
    if name.startswith(("config", "dotenv", "src.web", "src.fb_ads_rpa")):
        raise AssertionError("forbidden config/browser import")
    return original(name,*args,**kwargs)
builtins.__import__=guarded
sys.argv=["scripts/acl_meta_token_probe.py","--app-id",t.APP,"--api-version","v24.0"]
try:
    runpy.run_path(sys.argv[0],run_name="__main__")
except SystemExit as error:
    assert len(wire["requests"])==1
    raise
'''
    completed = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0 and not completed.stderr
    result = json.loads(completed.stdout)
    assert result["outcome"] == "app_match" and result["publishing"] == "not_checked"
    assert TOKEN not in completed.stdout and SECRET not in completed.stdout


@pytest.mark.parametrize("args,code", [(["--help"], 0), (["--token", TOKEN], 2), ([], 2)])
def test_real_cli_help_and_parser_errors_zero_network(args, code):
    root = Path(__file__).resolve().parents[1]
    bootstrap = '''
import sys, runpy, socket
def forbidden(*args,**kwargs):
    raise AssertionError("unexpected request")
socket.create_connection=forbidden
sys.argv=["scripts/acl_meta_token_probe.py",*sys.argv[1:]]
runpy.run_path(sys.argv[0],run_name="__main__")
'''
    completed = subprocess.run([sys.executable, "-c", bootstrap, *args], cwd=root,
                               capture_output=True, text=True, timeout=30)
    assert completed.returncode == code
    assert TOKEN not in completed.stdout+completed.stderr and "Traceback" not in completed.stderr
