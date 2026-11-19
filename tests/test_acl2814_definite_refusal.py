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
    """Meta refuses the ad POST the way it did for the owner; counts the ad POSTs and the ad readbacks."""

    refuse = True

    def __init__(self, reply=None):
        super().__init__()
        self.reply = reply or _pixel_error()
        self.ad_posts = 0

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
    assert out.stop_kind == "graph_error" and out.campaign_id
    assert not client.unreconciled
    line = out.failure.text()
    assert "pixel" in line.lower() and "no longer available" in line
    assert "Choose a different pixel, then Publish again" in line
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


def test_a_textless_5xx_on_ad_create_keeps_readback_and_resend(tmp_path):
    reply = _pixel_error(error_subcode=1, error_user_title="", error_user_msg="")
    meta = PixelGoneMeta(reply)
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert meta.ad_posts == 2                      # unknown: readback found nothing, sent once more
