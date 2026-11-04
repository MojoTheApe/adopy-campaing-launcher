"""ACL-2188: фоновая картинка (cover) шаблона Instant Form.

Картинка - уже существующее фото Страницы, выбранное по id (`context_card.cover_photo_id`).
Без загрузки файла и без новых рёбер записи. Шаблон без картинки не меняется ни байтом.
"""
from __future__ import annotations

import pytest

from src.analysis_agent import team_library as tl
from src.fb_api import form_templates as ft
from tests.test_acl1776_create_forms import PAGE, TEMPLATE
from tests.test_acl2054_forms_library import local, seeded, team  # noqa: F401

PHOTO = "122118707799486042"
WITH_INTRO = {**TEMPLATE, "intro": "Tell us about your project."}
COVER = {"cover_photo_id": PHOTO, "cover_page_id": PAGE}


def tpl(**over):
    return {**WITH_INTRO, **over}


# ───────────────────────── validate ─────────────────────────

def test_a_cover_pair_is_kept_as_digit_strings():
    clean, errors = ft.validate(tpl(**COVER))
    assert errors == []
    assert clean["cover_photo_id"] == PHOTO and clean["cover_page_id"] == PAGE


def test_a_template_without_a_cover_has_no_cover_keys():
    clean, errors = ft.validate(tpl(cover_photo_id="", cover_page_id=""))
    assert errors == []
    assert "cover_photo_id" not in clean and "cover_page_id" not in clean


@pytest.mark.parametrize("over", [{"cover_photo_id": PHOTO}, {"cover_page_id": PAGE}])
def test_half_a_pair_is_refused(over):
    _, errors = ft.validate(tpl(**over))
    assert "Background image needs both a Photo ID and a Page" in errors


@pytest.mark.parametrize("bad", ["12ab", "1 2", "-5", "x" * 3, "1" * 26])
def test_a_photo_id_must_be_digits(bad):
    _, errors = ft.validate(tpl(cover_photo_id=bad, cover_page_id=PAGE))
    assert "Photo ID must contain digits only" in errors


def test_a_cover_needs_a_description():
    _, errors = ft.validate({**TEMPLATE, "intro": "", **COVER})
    assert "Background image needs a Description (Meta shows it on the intro screen)" in errors


def test_a_stray_cover_photo_key_is_dropped():
    clean, errors = ft.validate(tpl(cover_photo={"id": PHOTO}))
    assert errors == [] and "cover_photo" not in clean


# ───────────────────────── hash ─────────────────────────

def test_the_hash_follows_the_image():
    base = ft.form_name(ft.validate(tpl())[0])
    a = ft.form_name(ft.validate(tpl(**COVER))[0])
    same = ft.form_name(ft.validate(tpl(**COVER))[0])
    other_photo = ft.form_name(ft.validate(tpl(cover_photo_id="999", cover_page_id=PAGE))[0])
    other_page = ft.form_name(ft.validate(tpl(cover_photo_id=PHOTO, cover_page_id="777"))[0])
    assert a == same
    assert len({base, a, other_photo, other_page}) == 4


# ───────────────────────── form_params ─────────────────────────

def test_form_params_puts_the_id_inside_the_context_card_only():
    p = ft.form_params(ft.validate(tpl(**COVER))[0])
    assert p["context_card"]["cover_photo_id"] == PHOTO
    assert "cover_photo" not in p and "cover_photo" not in p["context_card"]
    assert "cover_photo_id" not in p and "cover_page_id" not in p


def test_form_params_without_a_cover_is_unchanged():
    p = ft.form_params(ft.validate(tpl())[0])
    assert "cover_photo_id" not in p["context_card"]


def test_summary_mentions_the_background_image():
    assert ft.summary(ft.validate(tpl(**COVER))[0]).endswith("· background image")
    assert "background" not in ft.summary(ft.validate(tpl())[0])


def test_resolve_template_keeps_the_pair():
    from src.fb_api import form_locale as fl
    clean, _ = ft.validate(tpl(locale="AUTO", **COVER))
    out, _locale = fl.resolve_template(clean, "English", "US")
    assert out["cover_photo_id"] == PHOTO and out["cover_page_id"] == PAGE


# ───────────────────────── library: Remove round trip, duplicate ─────────────────────────

def _roundtrip(local):
    saved = ft.save(tpl(**COVER))
    row = next(r for r in ft.listing()["rows"] if r["id"] == saved["id"])
    return saved, row


def test_remove_really_removes_the_cover(local):
    saved, row = _roundtrip(local)
    with_cover = ft.form_name(ft.get(saved["id"]))
    assert ft.get(saved["id"])["cover_photo_id"] == PHOTO
    ft.save({**tpl(), "id": saved["id"], "_expected_revision": row["revision"]})
    again = ft.get(saved["id"])
    assert "cover_photo_id" not in again and "cover_page_id" not in again
    assert ft.form_name(again) != with_cover
    assert ft.form_name(again) == ft.form_name(ft.validate(tpl())[0]).replace(
        ft.validate(tpl())[0]["name"], again["name"])


def test_duplicate_keeps_the_cover(local):
    saved, _ = _roundtrip(local)
    copy = ft.duplicate(saved["id"])
    assert copy["cover_photo_id"] == PHOTO and copy["cover_page_id"] == PAGE


def test_team_library_round_trip(team):
    backend, on = team
    on("a")
    saved = ft.save(tpl(**COVER))
    on("b")
    assert ft.get(saved["id"])["cover_photo_id"] == PHOTO


# ───────────────────────── политика записи ─────────────────────────

def test_the_policy_table_carries_the_photo_id_pattern():
    from src.fb_api import write_policy as wp
    t = wp.policy_for_form_create(PAGE)
    assert t["photo_id_re"] == wp.FORM_PHOTO_ID_RE
    assert "cover_photo_id" in t["nested"]["context_card"]
    assert "cover_photo" not in t["nested"]["context_card"]


def test_the_template_with_a_cover_emits_only_allowed_keys():
    from src.fb_api import write_policy as wp
    clean, errors = ft.validate(tpl(**COVER))
    assert errors == []
    params = ft.form_params(clean)
    assert set(params) <= wp.FORM_KEYS
    wp.check_form_create(PAGE, "POST", f"/{PAGE}/leadgen_forms", params, pinned_page=PAGE)


def test_the_page_helper_refuses_a_policy_without_the_photo_pattern():
    from tests.test_acl1776_create_forms import ACT, _node_form_matrix
    from src.fb_api import write_policy as wp
    policy = wp.policy_for_page(ACT, PAGE)
    del policy["form_create"]["photo_id_re"]
    assert _node_form_matrix(policy)["ok"].get("error") == "write_refused"
