"""Public read diagnostics must identify an operation without request data."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from src.fb_api.graph import GraphClient, GraphError, PolicyBlocked, RateLimited


_FIXTURE = Path(__file__).parent / "fixtures/ads_manager/fb_api_permission_denied_2026_10_13.json"
_PRIVATE = "SyntheticPrivateRequestValue"


def denied():
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


class ReadTransport:
    def __init__(self, replies=None):
        self.replies = list(replies or [denied()])
        self.requests = []
        self.reacquires = 0

    def request(self, method, path, params):
        assert method == "GET", "diagnostics must not add a write"
        self.requests.append((method, path, deepcopy(params)))
        return self.replies.pop(0)

    def state(self):
        return {"kind": "ready", "has_token": True}

    def reacquire(self, **kwargs):
        self.reacquires += 1
        return self.state()


def client(transport):
    return GraphClient(transport, min_interval_s=0)


@pytest.mark.parametrize("path, operation", [
    ("/search", "search"),
    ("/act_12345/campaigns", "campaigns"),
    ("/act_12345/promote_pages", "promote_pages"),
    ("/act_12345/adspixels", "adspixels"),
    ("/12345/leadgen_forms", "leadgen_forms"),
    ("/me/adaccounts", "adaccounts"),
    ("/act_12345/insights", "insights"),
])
def test_denied_read_names_only_its_safe_operation(path, operation):
    transport = ReadTransport()
    reader = client(transport)
    with pytest.raises(GraphError) as caught:
        reader.get(path, {"q": _PRIVATE, "fields": _PRIVATE, "access_token": _PRIVATE})
    exc = caught.value
    assert exc.details["method"] == "GET"
    assert exc.details["operation"] == operation
    assert f"operation={operation}" in str(exc) and "method=GET" in str(exc)
    assert exc.code == 10 and exc.status == 400
    assert exc.details["type"] == "OAuthException"
    assert exc.details["credentials"] == "include"
    assert "12345" not in str(exc) and _PRIVATE not in json.dumps(exc.details)
    assert len(transport.requests) == reader.calls == 1
    assert transport.reacquires == 0 and reader.stopped is None


@pytest.mark.parametrize("search_type", ["adgeolocation", "adlocale", "adinterest"])
def test_search_context_distinguishes_only_allowlisted_lookup_types(search_type):
    transport = ReadTransport()
    with pytest.raises(GraphError) as caught:
        client(transport).get("/search", {"type": search_type, "q": _PRIVATE})
    assert caught.value.details["operation"] == "search"
    assert caught.value.details["search_type"] == search_type
    assert f"search_type={search_type}" in str(caught.value)
    assert _PRIVATE not in str(caught.value)
    assert len(transport.requests) == 1


@pytest.mark.parametrize("path", [
    f"https://graph.facebook.com/act_12345/campaigns?access_token={_PRIVATE}",
    f"/search?q={_PRIVATE}", f"/search#{_PRIVATE}",
    f"/act_12345/campaigns/{_PRIVATE}", f"/{_PRIVATE}/campaigns",
    f"/act_12345/campaigns%3Faccess_token%3D{_PRIVATE}",
    "/act_12345/campaigns\n", "/act_１２３４５/campaigns", "/campaigns",
    "/12345/campaigns", "/act_12345/leadgen_forms", "/me/adspixels",
    "/" + _PRIVATE * 100, "", None, [], {},
])
def test_unknown_path_never_becomes_diagnostic_text(path):
    transport = ReadTransport()
    with pytest.raises(GraphError) as caught:
        client(transport).get(path, {"type": _PRIVATE, "q": _PRIVATE})
    exc = caught.value
    assert exc.details["operation"] == "unknown"
    assert exc.details["method"] == "GET"
    assert "search_type" not in exc.details
    text = str(exc) + json.dumps(exc.details)
    assert _PRIVATE not in text and "12345" not in text
    assert "access_token" not in text and "%3D" not in text and "q=" not in text
    assert len(transport.requests) == 1


@pytest.mark.parametrize("search_type", [_PRIVATE, ["adinterest"], {"type": "adlocale"}, None, 10])
def test_unknown_search_type_cannot_echo_or_break_error_handling(search_type):
    with pytest.raises(GraphError) as caught:
        client(ReadTransport()).get("/search", {"type": search_type, "q": _PRIVATE})
    assert caught.value.details["operation"] == "search"
    assert "search_type" not in caught.value.details
    assert _PRIVATE not in str(caught.value)


def test_response_cannot_spoof_local_operation_context():
    reply = denied()
    for data in (reply, reply["endpoint"], reply["body"]["error"]):
        data.update(method="POST", operation=_PRIVATE, search_type=_PRIVATE)
    with pytest.raises(GraphError) as caught:
        client(ReadTransport([reply])).get("/search", {"type": "adlocale"})
    assert caught.value.details["method"] == "GET"
    assert caught.value.details["operation"] == "search"
    assert caught.value.details["search_type"] == "adlocale"
    assert _PRIVATE not in str(caught.value) and "POST" not in str(caught.value)


def test_later_page_error_keeps_operation_without_cursor_or_next_url():
    first = {"body": {"data": [{"id": "synthetic"}], "paging": {
        "cursors": {"after": _PRIVATE}, "next": "https://invalid.example/" + _PRIVATE}}}
    transport = ReadTransport([first, denied()])
    with pytest.raises(GraphError) as caught:
        client(transport).get_all("/act_12345/campaigns", max_pages=2)
    assert caught.value.details["operation"] == "campaigns"
    assert _PRIVATE not in str(caught.value)
    assert len(transport.requests) == 2
    assert transport.requests[1] == ("GET", "/act_12345/campaigns", {"after": _PRIVATE})


@pytest.mark.parametrize("code, stop_class", [(4, RateLimited), (368, PolicyBlocked)])
def test_stop_classes_and_no_retry_survive_context(code, stop_class):
    reply = denied()
    reply["body"]["error"]["code"] = code
    transport = ReadTransport([reply])
    reader = client(transport)
    with pytest.raises(stop_class) as first:
        reader.get("/act_12345/campaigns")
    with pytest.raises(stop_class) as again:
        reader.get("/search")
    assert first.value is again.value is reader.stopped
    assert first.value.code == code
    assert "operation=campaigns" in str(first.value)
    assert len(transport.requests) == 1 and transport.reacquires == 0


def test_reacquired_read_keeps_diagnostic_context_without_extra_retry():
    expired = denied()
    expired["body"]["error"]["code"] = 190
    transport = ReadTransport([expired, denied()])
    reader = client(transport)
    with pytest.raises(GraphError) as caught:
        reader.get("/search", {"type": "adinterest", "q": _PRIVATE})
    assert caught.value.code == 10
    assert caught.value.details["search_type"] == "adinterest"
    assert _PRIVATE not in str(caught.value)
    assert transport.reacquires == 1 and len(transport.requests) == reader.calls == 2
