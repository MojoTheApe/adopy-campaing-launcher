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
                               "context_card": {"cover_photo": {"id": PHOTO}},
                               "background_image_option": "UPLOAD"}
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


# ───────────── ACL-2579 round 2: background_image_option=UPLOAD (owner live check 12.11, Doc284) ─────────────

def _body(**over):
    t = ft.validate(tpl(**{**COVER, **over}))[0]
    return t, ft.form_params(t)


def _old_hash(t):
    import hashlib
    body = {k: t.get(k) for k in sorted(t) if k not in ("id", "name")}
    return hashlib.sha1(json.dumps(body, ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":")).encode("utf-8")).hexdigest()[:6]


def test_a_cover_form_sends_background_image_option_upload_and_none_without_a_cover():
    _t, with_cover = _body()
    assert with_cover["background_image_option"] == "UPLOAD"
    assert with_cover["context_card"]["cover_photo_id"] == PHOTO
    assert "background_image_option" not in ft.form_params(ft.validate(tpl())[0])
    # ACL-2571 local-picture path: the photo id uploaded at publish time gets the option too
    local = ft.form_params(ft.validate(tpl())[0], cover_photo_id="4242")
    assert local["background_image_option"] == "UPLOAD" and local["context_card"]["cover_photo_id"] == "4242"


def test_a_picture_without_intro_text_still_sends_the_card():
    t = {**ft.validate(tpl(**COVER))[0], "intro": ""}
    p = ft.form_params(t)
    assert p["context_card"]["cover_photo_id"] == PHOTO and p["context_card"]["content"] == [t["headline"]]
    assert p["background_image_option"] == "UPLOAD"


def test_the_posted_form_carries_the_option(tmp_path):
    _t, meta, out = _published(tmp_path)
    assert out.error == "", out.error
    assert meta.form_posts[0][1]["background_image_option"] == "UPLOAD"


def test_policy_allows_the_option_only_as_upload_and_only_with_a_cover():
    from src.fb_api import write_policy as wp
    _t, p = _body()
    path = f"/{PAGE}/leadgen_forms"
    wp.check_form_create(PAGE, "POST", path, p, pinned_page=PAGE)
    for bad in ("USE_AD_IMAGE", "", None, True):
        with pytest.raises(wp.WriteRefused):
            wp.check_form_create(PAGE, "POST", path, {**p, "background_image_option": bad}, pinned_page=PAGE)
    no_photo = json.loads(json.dumps(p))
    no_photo["context_card"].pop("cover_photo_id")
    with pytest.raises(wp.WriteRefused):
        wp.check_form_create(PAGE, "POST", path, no_photo, pinned_page=PAGE)
    no_card = {k: v for k, v in p.items() if k != "context_card"}
    with pytest.raises(wp.WriteRefused):
        wp.check_form_create(PAGE, "POST", path, no_card, pinned_page=PAGE)
    assert wp.policy_for_form_create(PAGE)["background_options"] == ["UPLOAD"]
    assert "background_image_option" in wp.policy_for_form_create(PAGE)["keys"]


def test_the_mirror_allows_upload_with_a_cover_and_refuses_the_rest(monkeypatch):
    _t, p = _body()
    other = {**p, "background_image_option": "USE_AD_IMAGE"}
    no_photo = json.loads(json.dumps(p))
    no_photo["context_card"].pop("cover_photo_id")
    monkeypatch.setattr(t1776, "FORM_MATRIX", [("ok", PAGE, "leadgen_forms", p, True),
                                               ("other", PAGE, "leadgen_forms", other, False),
                                               ("nophoto", PAGE, "leadgen_forms", no_photo, False)])
    got = t1776._node_form_matrix()
    assert got["ok"].get("body") == {"id": "5"}, got["ok"]
    assert got["other"].get("error") == "write_refused" and got["nophoto"].get("error") == "write_refused"


def test_the_mirror_fails_closed_on_an_old_table_without_background_options(monkeypatch):
    from src.fb_api import write_policy as wp
    _t, p = _body()
    old = wp.policy_for_page(t1776.ACT, PAGE)
    old["form_create"].pop("background_options")
    monkeypatch.setattr(t1776, "FORM_MATRIX", [("ok", PAGE, "leadgen_forms", p, False)])
    got = t1776._node_form_matrix(old)
    assert got["ok"].get("error") == "write_refused", got["ok"]


def test_readback_asks_for_the_option_and_flags_a_form_without_it():
    from src.fb_api import form_create as fc
    t = ft.validate(tpl(**COVER))[0]
    assert "background_image_option" in fc.readback_fields(t)
    assert "background_image_option" not in fc.readback_fields(ft.validate(tpl())[0])
    card = {"cover_photo": {"id": PHOTO}}
    assert fc.compare_cover(t, {"context_card": card, "background_image_option": "UPLOAD"}) == []
    for opt in (None, "", "USE_AD_IMAGE"):
        assert fc.compare_cover(t, {"context_card": card, "background_image_option": opt}) == ["background image"]
    assert fc.compare_cover(t, {"context_card": card}) == ["background image"]


def test_existing_cover_present_but_option_missing_counts_as_missing():
    from src.fb_api import form_create as fc
    t = ft.validate(tpl(**COVER))[0]
    row = {"id": "1", "context_card": {"cover_photo": {"id": PHOTO}}}
    assert fc.existing_cover_state(t, row, PHOTO) == "missing"
    assert fc.existing_cover_state(t, {**row, "background_image_option": "UPLOAD"}, PHOTO) == "ok"
    assert "background_image_option" in fc.COVER_ONLY_FIELDS


def test_a_same_name_form_with_the_photo_but_no_option_is_not_reused(tmp_path):
    saved = ft.save(tpl(**COVER))
    t = ft.get(saved["id"])
    meta = CoverMeta(rows=[{"id": "7000050", "name": ft.form_name(t), "status": "ACTIVE", "locale": "en_US"}])
    meta.details["7000050"] = {**meta.rows[0], "page": {"id": PAGE}, "questions": {"data": ft.expected_questions(t)},
                               "context_card": {"cover_photo": {"id": PHOTO}}}
    out, _ = _run(meta, _tpl_draft(tmp_path, [saved["id"]]))
    assert "will not reuse it" in out.error and not meta.form_posts


def test_the_name_hash_has_a_version_marker_only_for_templates_with_a_picture():
    t = ft.validate(tpl(**COVER))[0]
    assert ft.content_hash(t) != _old_hash(t)
    plain = ft.validate(tpl())[0]
    assert ft.content_hash(plain) == _old_hash(plain)


def test_a_form_made_before_the_option_was_sent_has_another_name(tmp_path):
    """A form on the Page under the OLD name (no marker) is not matched: a new form is created."""
    saved = ft.save(tpl(**COVER))
    t = ft.get(saved["id"])
    old_name = f"{t['name']} · ACL {_old_hash(t)}"
    assert old_name != ft.form_name(t)
    meta = CoverMeta(rows=[{"id": "7000050", "name": old_name, "status": "ACTIVE", "locale": "en_US"}])
    out, _ = _run(meta, _tpl_draft(tmp_path, [saved["id"]]))
    assert out.error == "", out.error
    assert meta.form_posts and meta.form_posts[0][1]["background_image_option"] == "UPLOAD"
