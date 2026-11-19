"""ACL-2814: Meta's definite refusal (e.g. the pixel is no longer available) is shown, not "Meta did not confirm"."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_api import graph as g  # noqa: E402
from tests.test_acl1761_api_create import ACT, FakeMeta, ScriptedTransport, _draft, _run  # noqa: E402

PIXEL_TITLE = "This ad is paused because the pixel it's promoting is no longer available"


def _pixel_error(**over):
    err = {"message": "Invalid parameter", "type": "OAuthException", "code": 100, "error_subcode": 1784033,
           "is_transient": False, "error_user_title": PIXEL_TITLE, "error_user_msg": "Please select a different pixel."}
    err.update(over)
    return {"status": 500, "body": {"error": err}}


def _client(reply):
    return g.GraphClient(ScriptedTransport([reply]), allow_writes=True, sleep=lambda s: None, min_interval_s=0.0)


class PixelGoneMeta(FakeMeta):
    """Meta refuses the ad POST the way it did for the owner; counts the ad POSTs and the ad readbacks made after the first POST."""

    refuse = True
    pixel_rows = None   # replace the account's pixel list (the fake gives id 222 without a name)

    def __init__(self, reply=None):
        super().__init__()
        self.reply = reply or _pixel_error()
        self.ad_posts = 0
        self.ad_readbacks = 0

    def _get(self, path, params):
        if self.pixel_rows is not None and path.rstrip("/").endswith("/adspixels"):
            return {"data": self.pixel_rows}
        return super()._get(path, params)

    def request(self, method, path, params=None):
        if self.ad_posts and method.upper() == "GET" and path.rstrip("/").endswith("/ads"):
            self.ad_readbacks += 1
        return super().request(method, path, params)

    def write(self, path, params, files=None, filename="", **kw):
        if self.refuse and path.endswith("/ads"):
            self.ad_posts += 1
            return self.reply
        return super().write(path, params, files=files, filename=filename, **kw)


def test_non_transient_1784033_with_http_500_is_a_refusal_not_unknown():
    c = _client(_pixel_error())
    with pytest.raises(g.GraphError) as caught:
        c.post(f"/{ACT}/ads", {"name": "x"})
    assert caught.value.subcode == 1784033 and not c.unreconciled


def test_any_non_transient_refusal_with_meta_text_is_a_refusal():
    c = _client(_pixel_error(error_subcode=999999))
    with pytest.raises(g.GraphError):
        c.post(f"/{ACT}/ads", {"name": "x"})
    assert not c.unreconciled


def test_transient_or_textless_5xx_stays_unknown():
    for reply in (_pixel_error(is_transient=True),
                  _pixel_error(error_subcode=1, error_user_title="", error_user_msg=""),
                  _pixel_error(error_subcode=1784033, is_transient=True, error_user_title="", error_user_msg="")):
        c = _client(reply)
        with pytest.raises(g.WriteUnknown):
            c.post(f"/{ACT}/ads", {"name": "x"})
        assert c.unreconciled


def test_pixel_refusal_on_ad_create_stops_with_metas_text_and_no_resend(tmp_path):
    meta = PixelGoneMeta()
    out, client = _run(meta, _draft(tmp_path, ads=1))
    assert meta.ad_posts == 1                      # no second send
    assert meta.ad_readbacks == 0                  # and no readback of the ads
    assert out.stop_kind == "graph_error" and out.campaign_id
    assert not client.unreconciled
    line = out.failure.text()
    assert "pixel" in line.lower() and "no longer available" in line and "(inactive)" not in line
    assert "activate it or check access in Events Manager, or choose a different pixel in the builder, then Publish again" in line
    assert "did not confirm" not in line and "unknown" not in line.lower()
    assert "campaign" in out.failure.text().lower()  # the "Left in Meta" part is kept
    assert out.failure.left


def test_unknown_other_code_uses_metas_own_title():
    from src.fb_api import plain_errors as pe
    c = _client(_pixel_error(code=3999, error_subcode=999999, error_user_title="Something Meta said"))
    with pytest.raises(g.GraphError) as caught:
        c.post(f"/{ACT}/ads", {"name": "x"})
    f = pe.classify(caught.value, kind="graph_error")
    assert "Something Meta said" in f.line() and "did not confirm" not in f.line()


def test_graph_code_1_or_2_with_user_text_and_no_5xx_stays_unknown():
    for code in (1, 2):
        c = _client({"status": 400, "body": {"error": {"message": "unknown error", "code": code,
                                                        "is_transient": False, "error_user_title": "Meta said",
                                                        "error_user_msg": "something"}}})
        with pytest.raises(g.WriteUnknown):
            c.post(f"/{ACT}/ads", {"name": "x"})
        assert c.unreconciled


def test_a_textless_5xx_on_ad_create_keeps_readback_and_resend(tmp_path):
    reply = _pixel_error(error_subcode=1, error_user_title="", error_user_msg="")
    meta = PixelGoneMeta(reply)
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert meta.ad_posts == 2                      # unknown: readback found nothing, sent once more


_NOPIXEL_LINE = ("Meta: the pixel this ad promotes is no longer available to this ad account - "
                 "check the pixel in Events Manager (status and access), then Publish again")


def test_1784033_names_the_pixel_when_the_account_read_gave_a_name(tmp_path):
    meta = PixelGoneMeta()
    meta.pixel_rows = [{"id": "222", "name": "doc409"}]
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert out.failure.line() == ("Meta: pixel doc409 is no longer available to this ad account - "
                                  "activate it or check access in Events Manager, or choose a different pixel in the builder, "
                                  "then Publish again.")
    assert meta.ad_posts == 1 and meta.ad_readbacks == 0


def test_1784033_names_the_pixel_by_id_when_it_has_no_name(tmp_path):
    meta = PixelGoneMeta()      # the fake's pixel 222 has no name
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert out.failure.line() == ("Meta: pixel 222 is no longer available to this ad account - "
                                  "activate it or check access in Events Manager, or choose a different pixel in the builder, "
                                  "then Publish again.")


def test_1784033_without_any_known_pixel_keeps_the_general_text():
    from src.fb_api import plain_errors as pe
    c = _client(_pixel_error())
    with pytest.raises(g.GraphError) as caught:
        c.post(f"/{ACT}/ads", {"name": "x"})
    for ctx in (None, {"pixel": ""}):
        assert pe.classify(caught.value, kind="graph_error", ctx=ctx).line() == _NOPIXEL_LINE + "."
