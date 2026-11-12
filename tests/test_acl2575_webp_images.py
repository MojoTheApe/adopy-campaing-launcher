"""ACL-2575: WebP/AVIF/HEIC ad images are converted locally; a definite FileTypeNotSupported is a refusal."""
from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_api import creator as cr  # noqa: E402
from src.fb_api import graph as g  # noqa: E402
from src.fb_api import image_convert as ic  # noqa: E402
from tests.test_acl1761_api_create import ACT, FakeMeta, ScriptedTransport, _draft, _publisher, _run  # noqa: E402

PIL = pytest.importorskip("PIL.Image")


def _webp(mode="RGB", size=(8, 6)) -> bytes:
    buf = io.BytesIO()
    PIL.new(mode, size, (200, 10, 10, 128) if mode == "RGBA" else (200, 10, 10)).save(buf, "WEBP")
    return buf.getvalue()


def _with_webp(tmp_path, data: bytes, name="1000_img.webp", ads=1):
    d = _draft(tmp_path, ads=ads)
    ad = d.ad_sets[0].ads[0]
    f = tmp_path / name
    f.write_bytes(data)
    ad.creative_path, ad.creative_filename = str(f), name
    return d, f


class RefusingMeta(FakeMeta):
    """Meta answers the image upload the way it did for the owner: HTTP 500, 100/1487411, not transient."""

    refuse = True

    def write(self, path, params, files=None, filename="", **kw):
        if self.refuse and path.endswith("/adimages"):
            self.posts.append((path, dict(params)))
            return {"status": 500, "body": {"error": {
                "message": "Invalid parameter", "type": "OAuthException", "code": 100, "error_subcode": 1487411,
                "is_transient": False, "error_user_title": "FileTypeNotSupported",
                "error_user_msg": "The type of file is not supported"}}}
        return super().write(path, params, files=files, filename=filename, **kw)


# -- conversion --

def test_webp_is_converted_to_jpeg_before_upload_and_the_original_is_untouched(tmp_path):
    original = _webp()
    d, f = _with_webp(tmp_path, original)
    meta = FakeMeta()
    out, _ = _run(meta, d)
    assert out.error == "" and out.readback["ok"] is True, (out.error, out.mismatches)
    assert f.read_bytes() == original
    name = next(iter(meta.images.values()))
    assert name.startswith("acl-") and name.endswith(".jpg")
    assert "ad 1: WebP converted to JPEG for Meta." in out.warnings


def test_transparent_webp_becomes_png(tmp_path):
    d, _ = _with_webp(tmp_path, _webp("RGBA"))
    meta = FakeMeta()
    out, _ = _run(meta, d)
    assert out.error == ""
    assert next(iter(meta.images.values())).endswith(".png")
    assert "ad 1: WebP converted to PNG for Meta." in out.warnings


def test_webp_saved_under_a_jpg_name_is_still_converted(tmp_path):
    d, _ = _with_webp(tmp_path, _webp(), name="1000_img.jpg")
    out, _ = _run(FakeMeta(), d)
    assert out.error == "" and any("WebP converted" in w for w in out.warnings)


def test_converted_bytes_count_toward_the_same_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "IMAGE_MAX_BYTES", 400)
    big = _webp(size=(200, 200))
    d, _ = _with_webp(tmp_path, big)
    assert len(big) <= 400                         # the WebP fits, its JPEG/PNG does not
    meta = FakeMeta()
    out, _ = _run(meta, d)
    assert out.stop_kind == "plan" and "after converting WebP" in out.error and meta.posts == []


def test_ordinary_images_are_not_converted():
    png = io.BytesIO()
    PIL.new("RGB", (4, 4)).save(png, "PNG")
    assert ic.convert("a.png", png.getvalue(), 10**6, "ad 1") is None
    assert ic.convert("a.jpg", b"creative-0", 10**6, "ad 1") is None   # unknown bytes keep today's behaviour


# -- refusal before any write --

def test_an_undecodable_webp_is_refused_before_any_write(tmp_path):
    d, _ = _with_webp(tmp_path, b"RIFF\x00\x00\x00\x00WEBPnot really")
    meta = FakeMeta()
    out, client = _run(meta, d)
    assert out.stop_kind == "plan" and meta.posts == [] and client.writes == 0
    assert "1000_img.webp" in out.error and "cannot be converted" in out.error and "Nothing was created" in out.error


def test_heic_without_a_decoder_is_refused_before_any_write(tmp_path):
    d, _ = _with_webp(tmp_path, b"\x00\x00\x00\x18ftypheic" + b"\x00" * 20, name="1000_img.heic")
    meta = FakeMeta()
    out, _ = _run(meta, d)
    assert out.stop_kind == "plan" and meta.posts == [] and "1000_img.heic" in out.error


# -- plan check --

def test_check_plan_shows_the_conversion_note_and_writes_nothing(tmp_path):
    d, _ = _with_webp(tmp_path, _webp())
    meta = FakeMeta()
    res = _publisher(meta, dry_run=True).publish_draft(d)
    assert meta.posts == [] and res["success"] is True
    assert "ad 1: WebP converted to JPEG for Meta." in res["warnings"]
    ad = res["plan"]["ad_sets"][0]["ads"][0]
    assert ad["library_name"].endswith(".jpg")


def test_check_plan_refuses_an_undecodable_webp(tmp_path):
    d, _ = _with_webp(tmp_path, b"RIFF\x00\x00\x00\x00WEBPbroken")
    meta = FakeMeta()
    res = _publisher(meta, dry_run=True).publish_draft(d)
    assert meta.posts == [] and res["success"] is False and "1000_img.webp" in res["error"]
    assert "cannot be converted" in res["failure"]["details"]["raw"]


# -- classification --

def _graph_with(reply):
    return g.GraphClient(ScriptedTransport([reply]), allow_writes=True, sleep=lambda s: None, min_interval_s=0.0)


def _file_error(**over):
    err = {"message": "Invalid parameter", "code": 100, "error_subcode": 1487411, "is_transient": False,
           "error_user_title": "FileTypeNotSupported", "error_user_msg": "The type of file is not supported"}
    err.update(over)
    return {"status": 500, "body": {"error": err}}


def test_graph_treats_a_non_transient_file_type_answer_as_a_refusal():
    c = _graph_with(_file_error())
    with pytest.raises(g.GraphError) as caught:
        c.post(f"/{ACT}/adimages", {}, files={"filename": b"x"}, filename="a.png")
    assert caught.value.subcode == 1487411 and not c.unreconciled


def test_a_transient_or_other_500_stays_unknown():
    for reply in (_file_error(is_transient=True), _file_error(error_subcode=1)):
        c = _graph_with(reply)
        with pytest.raises(g.WriteUnknown):
            c.post(f"/{ACT}/adimages", {}, files={"filename": b"x"}, filename="a.png")
    c = _graph_with(_file_error())
    with pytest.raises(g.WriteUnknown):
        c.post(f"/{ACT}/adcreatives", {"name": "x"})


def test_file_type_refusal_is_reported_with_the_file_name_and_no_ads_manager_advice(tmp_path):
    d, _ = _with_webp(tmp_path, _webp())
    meta = RefusingMeta()
    out, client = _run(meta, d)
    assert out.stop_kind == "graph_error" and out.campaign_id
    assert out.failure.key == "file_type_refused"
    line = out.failure.text()
    assert "Meta refused the file of ad 1 (1000_img.webp): The type of file is not supported" in line
    assert "Ads Manager" not in line and "10 min" not in line and "unknown" not in line.lower()
    assert "campaign" in line.lower()                      # what was left in Meta is listed as before
    assert not client.unreconciled


def test_republish_reuses_the_paused_campaign_and_ad_set(tmp_path):
    d, _ = _with_webp(tmp_path, _webp())
    meta = RefusingMeta()
    first, _ = _run(meta, d)
    assert first.stop_kind == "graph_error"
    camps = [o["id"] for o in meta.objects.values() if o["_kind"] == "campaign"]
    sets = [o["id"] for o in meta.objects.values() if o["_kind"] == "adset"]
    assert len(camps) == 1 and len(sets) == 1
    meta.refuse = False
    second, _ = _run(meta, _with_webp(tmp_path, _webp())[0])
    assert second.error == "" and second.campaign_id == camps[0]
    assert [o["id"] for o in meta.objects.values() if o["_kind"] == "campaign"] == camps
    assert [o["id"] for o in meta.objects.values() if o["_kind"] == "adset"] == sets
    assert len([o for o in meta.objects.values() if o["_kind"] == "ad"]) == 1
