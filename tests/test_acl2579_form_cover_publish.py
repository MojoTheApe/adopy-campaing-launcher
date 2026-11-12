"""ACL-2579: a template with a background must reach Meta as a form WITH the background.

Through the real publish path (`creator` + Python write policy), then the same posted body through the
in-page helper mirror; readback of the cover; a same-name form without the background is never reused.
"""
from __future__ import annotations

import json

import pytest

from src.fb_api import form_templates as ft
from tests import test_acl1776_create_forms as t1776
from tests.test_acl1776_create_forms import PAGE, _run, _tpl_draft
from tests.test_acl2188_form_cover import COVER, PHOTO, CoverMeta, tpl
from tests.test_acl2054_forms_library import local  # noqa: F401
from tests.test_acl2571_shared_form_background import _config  # noqa: F401


def _published(tmp_path, **meta_kw):
    tid = ft.save(tpl(**COVER))["id"]
    meta = CoverMeta(**meta_kw)
    out, _ = _run(meta, _tpl_draft(tmp_path, [tid]))
    return ft.get(tid), meta, out


def test_publish_posts_the_cover_inside_the_context_card(tmp_path):
    t, meta, out = _published(tmp_path)
    assert out.error == "", out.error
    (path, params), = meta.form_posts
    assert path == f"/{PAGE}/leadgen_forms"
    assert params["context_card"]["cover_photo_id"] == PHOTO
    assert params["name"] == ft.form_name(t)


def test_the_posted_body_passes_the_in_page_policy_mirror_with_the_cover(tmp_path, monkeypatch):
    _t, meta, out = _published(tmp_path)
    assert out.error == "", out.error
    params = meta.form_posts[0][1]
    # The node harness records the request body that the helper really sends.
    node = t1776._NODE.replace("calls.push(String(url));", "calls.push(String(url)); bodies.push(String(init.body));")
    node = node.replace("const calls = [];", "const calls = []; const bodies = [];")
    node = node.replace("out.calls = calls.length;", "out.calls = calls.length; out.bodies = bodies;")
    monkeypatch.setattr(t1776, "_NODE", node)
    monkeypatch.setattr(t1776, "FORM_MATRIX", [("posted", PAGE, "leadgen_forms", params, True)])
    got = t1776._node_form_matrix()
    assert got["posted"].get("body") == {"id": "5"}, got["posted"]
    sent = [b for b in got["bodies"] if "context_card" in b]
    assert sent, got["bodies"]
    from urllib.parse import parse_qs
    card = json.loads(parse_qs(sent[0])["context_card"][0])
    assert card["cover_photo_id"] == PHOTO


def test_the_mirror_refuses_a_cover_that_is_not_digits(tmp_path, monkeypatch):
    _t, meta, _out = _published(tmp_path)
    params = json.loads(json.dumps(meta.form_posts[0][1]))
    params["context_card"]["cover_photo_id"] = "12ab"
    monkeypatch.setattr(t1776, "FORM_MATRIX", [("bad", PAGE, "leadgen_forms", params, False)])
    assert t1776._node_form_matrix()["bad"].get("error") == "write_refused"


@pytest.mark.parametrize("mode", ["missing", "other"])
def test_readback_stops_when_the_created_form_has_no_or_another_cover(tmp_path, mode):
    _t, meta, out = _published(tmp_path, readback_cover=mode)
    assert out.stop_kind == "form_mismatch" and "background image" in out.error
    assert not any("adcreatives" in p for p, _ in meta.posts)


def test_readback_asks_meta_for_the_cover(tmp_path):
    t, _meta, _out = _published(tmp_path)
    from src.fb_api import form_create as fc
    assert "context_card{cover_photo{id}}" in fc.readback_fields(t)


def test_the_hash_of_the_form_name_includes_the_cover():
    with_cover = ft.form_name(ft.validate(tpl(**COVER))[0])
    without = ft.form_name(ft.validate(tpl())[0])
    other = ft.form_name(ft.validate(tpl(cover_photo_id="999", cover_page_id=PAGE))[0])
    assert len({with_cover, without, other}) == 3


@pytest.mark.parametrize("card", [{}, {"cover_photo": {"id": "5"}}])
def test_a_same_name_form_without_or_with_another_cover_is_not_reused(tmp_path, card):
    saved = ft.save(tpl(**COVER))
    t = ft.get(saved["id"])
    meta = CoverMeta(rows=[{"id": "7000050", "name": ft.form_name(t), "status": "ACTIVE", "locale": "en_US"}])
    meta.details["7000050"] = {**meta.rows[0], "page": {"id": PAGE}, "questions": {"data": ft.expected_questions(t)},
                               "context_card": card}
    out, _ = _run(meta, _tpl_draft(tmp_path, [saved["id"]]))
    assert "will not reuse it" in out.error
    assert not meta.posts and not meta.form_posts


def test_a_same_name_form_with_the_right_cover_is_reused(tmp_path):
    saved = ft.save(tpl(**COVER))
    t = ft.get(saved["id"])
    meta = CoverMeta(rows=[{"id": "7000050", "name": ft.form_name(t), "status": "ACTIVE", "locale": "en_US"}])
    meta.details["7000050"] = {**meta.rows[0], "page": {"id": PAGE}, "questions": {"data": ft.expected_questions(t)},
                               "context_card": {"cover_photo": {"id": PHOTO}}}
    out, _ = _run(meta, _tpl_draft(tmp_path, [saved["id"]]))
    assert out.error == "", out.error
    assert not meta.form_posts


def test_a_template_without_a_cover_still_reuses_its_form_with_no_extra_read(tmp_path):
    saved = ft.save(tpl())
    t = ft.get(saved["id"])
    meta = CoverMeta(rows=[{"id": "7000050", "name": ft.form_name(t), "status": "ACTIVE", "locale": "en_US"}])
    meta.details["7000050"] = {**meta.rows[0], "page": {"id": PAGE}, "questions": {"data": ft.expected_questions(t)}}
    out, _ = _run(meta, _tpl_draft(tmp_path, [saved["id"]]))
    assert out.error == "", out.error


# ───────────────────── ACL-2571 local picture: a form made without the file never answers to the picture's name ─────

def test_a_form_made_without_the_local_file_is_not_reused_once_the_file_is_here(tmp_path, _config):
    from pathlib import Path

    from src.fb_api import form_cover_store as store
    from tests.test_acl2571_shared_form_background import SHA, UploadMeta, _publish, save_with_picture

    tid = save_with_picture()["id"]
    bin_path = Path(store.store_dir()) / f"{tid}-{SHA}.bin"
    blob = bin_path.read_bytes()
    bin_path.unlink()
    for sub in ("a", "b"):
        (tmp_path / sub).mkdir()
    meta = UploadMeta()
    first, _ = _publish(meta, tmp_path / "a", tid)
    assert first.error == "" and len(meta.form_posts) == 1
    assert "cover_photo_id" not in meta.form_posts[0][1]["context_card"]      # the form without a background
    bin_path.write_bytes(blob)                                                 # the file is back on this computer
    meta2 = UploadMeta(rows=list(meta.rows))                                   # a new campaign, the same Page forms
    meta2.details.update(meta.details)
    second, _ = _publish(meta2, tmp_path / "b", tid)
    assert second.error == "", second.error
    assert len(meta2.form_posts) == 1, "a new form is created: the backgroundless one was not reused"
    assert meta2.form_posts[0][1]["name"] != meta.form_posts[0][1]["name"]
    assert meta2.form_posts[0][1]["context_card"]["cover_photo_id"]
