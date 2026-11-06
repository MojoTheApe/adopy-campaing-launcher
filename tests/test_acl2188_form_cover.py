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


# ───────────────────────── readback и создание ─────────────────────────

import copy  # noqa: E402

from src.fb_api import creator as cr  # noqa: E402
from src.fb_api import form_create as fc  # noqa: E402
from tests.test_acl1776_create_forms import (  # noqa: E402,F401
    FormsMeta, _creator, _edges, _library, _run, _tpl_draft)


class CoverMeta(FormsMeta):
    """FormsMeta, который при создании формы с картинкой отдаёт её обратно в context_card."""

    def __init__(self, *a, readback_cover="same", **kw):
        super().__init__(*a, **kw)
        self.readback_cover = readback_cover

    def write(self, path, params, files=None, filename="", on_ad=False, form_page="", **k):
        res = super().write(path, params, files, filename, on_ad, form_page, **k)
        if form_page and isinstance(res.get("body"), dict) and res["body"].get("id") in self.details:
            card = copy.deepcopy(params.get("context_card") or {})
            photo = card.pop("cover_photo_id", None)
            if photo and self.readback_cover == "same":
                card["cover_photo"] = {"id": photo}
            elif photo and self.readback_cover == "other":
                card["cover_photo"] = {"id": "1"}
            self.details[res["body"]["id"]]["context_card"] = card
        return res


def covered(**over):
    return ft.validate(tpl(**{**COVER, **over}))[0]


def test_readback_fields_without_a_cover_is_todays_string():
    assert fc.readback_fields(ft.validate(tpl())[0]) == fc.READBACK_FIELDS
    assert fc.readback_fields(ft.validate(TEMPLATE)[0]) == fc.READBACK_FIELDS
    assert fc.readback_fields(covered()).startswith(fc.READBACK_FIELDS)
    assert "context_card{cover_photo{id}}" in fc.readback_fields(covered())


def test_compare_cover_match_different_missing():
    t = covered()
    assert fc.compare_cover(t, {"context_card": {"cover_photo": {"id": PHOTO}}}) == []
    assert fc.compare_cover(t, {"context_card": {"cover_photo": {"id": "5"}}}) == ["background image"]
    assert fc.compare_cover(t, {"context_card": {}}) == ["background image"]
    assert fc.compare_cover(t, {}) == ["background image"]
    assert fc.compare_cover(ft.validate(tpl())[0], {}) == []


def test_refused_message_names_the_form_for_a_cover():
    t = covered()
    msg = fc.refused_message(RuntimeError("Invalid photo"), t)
    assert ft.form_name(t) in msg and "Invalid photo" in msg and "background image" in msg
    assert fc.refused_message(RuntimeError("x")) == fc.MSG_REFUSED.format(why="x")
    assert fc.refused_message(RuntimeError("x"), ft.validate(tpl())[0]) == fc.MSG_REFUSED.format(why="x")


def test_a_cover_of_another_page_stops_at_check_plan_with_no_writes(tmp_path):
    tid = ft.save(tpl(cover_photo_id=PHOTO, cover_page_id="777"))["id"]
    meta = CoverMeta()
    out, _ = _run(meta, _tpl_draft(tmp_path, [tid]))
    assert "belongs to another Page" in out.error and not meta.posts and not meta.form_posts


def test_other_page_stops_even_when_a_form_of_that_name_exists(tmp_path):
    saved = ft.save(tpl(cover_photo_id=PHOTO, cover_page_id="777"))
    t = ft.get(saved["id"])
    meta = FormsMeta(rows=[{"id": "7000050", "name": ft.form_name(t), "status": "ACTIVE", "locale": "en_US"}])
    out, _ = _run(meta, _tpl_draft(tmp_path, [saved["id"]]))
    assert "belongs to another Page" in out.error and not meta.posts


def test_a_new_form_with_a_cover_is_created_and_read_back(tmp_path):
    tid = ft.save(tpl(**COVER))["id"]
    meta = CoverMeta()
    out, _ = _run(meta, _tpl_draft(tmp_path, [tid]))
    assert out.error == "", out.error
    assert meta.form_posts[0][1]["context_card"]["cover_photo_id"] == PHOTO
    assert any("read back, ACTIVE, on the Page" in s for s in out.steps)


def test_a_new_form_whose_cover_does_not_read_back_stops(tmp_path):
    tid = ft.save(tpl(**COVER))["id"]
    meta = CoverMeta(readback_cover="missing")
    out, _ = _run(meta, _tpl_draft(tmp_path, [tid]))
    assert out.stop_kind == "form_mismatch" and "background image" in out.error
    assert "stays on the Page" in out.error
    assert "adcreatives" not in _edges(meta)


def test_a_reused_form_with_a_missing_cover_notes_and_continues(tmp_path):
    saved = ft.save(tpl(**COVER))
    t = ft.get(saved["id"])
    meta = CoverMeta(rows=[{"id": "7000050", "name": ft.form_name(t), "status": "ACTIVE", "locale": "en_US"}])
    meta.details["7000050"] = {**meta.rows[0], "page": {"id": PAGE},
                               "questions": {"data": ft.expected_questions(t)}, "context_card": {}}
    out, _ = _run(meta, _tpl_draft(tmp_path, [saved["id"]]))
    assert out.error == "", out.error
    assert not meta.form_posts
    assert any("the background image could not be confirmed" in s for s in out.steps)


def test_a_meta_refusal_of_a_cover_names_the_form(tmp_path):
    tid = ft.save(tpl(**COVER))["id"]
    meta = CoverMeta()
    meta.refuse = {"code": 100, "message": "Invalid photo id"}
    out, _ = _run(meta, _tpl_draft(tmp_path, [tid]))
    assert out.stop_kind == "form_refused"
    assert "with its background image" in out.error and "Invalid photo id" in out.error


def test_estimate_create_calls_is_the_same_with_and_without_a_cover(tmp_path):
    a = ft.save(tpl())["id"]
    b = ft.save(tpl(name="Covered", **COVER))["id"]
    plan_a = cr.plan_from_draft(_tpl_draft(tmp_path, [a]))
    plan_b = cr.plan_from_draft(_tpl_draft(tmp_path, [b]))
    assert cr.estimate_create_calls(plan_a) == cr.estimate_create_calls(plan_b)


# ───────────────────────── чтение картинок Страницы ─────────────────────────

import asyncio  # noqa: E402
import json  # noqa: E402
from contextlib import contextmanager  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from src.fb_api import forms as fm  # noqa: E402
from src.fb_api import graph as g  # noqa: E402

URL = "https://scontent.example.com/secret-image.jpg"


class PhotoMeta:
    """Transport: `/{page}/photos` и `/{page}/leadgen_forms` с обложками; каждый источник можно уронить."""

    def __init__(self, photos=None, forms=None, photos_error=None, forms_error=None, more=False):
        self.photos = [{"id": "11", "width": 1200, "height": 628, "created_time": "2026-10-20T10:00:00+0000",
                        "images": [{"source": URL}]},
                       {"id": "12", "width": 800, "height": 800, "created_time": "2026-10-21T10:00:00+0000"}] \
            if photos is None else photos
        self.forms = [{"id": "f1", "name": "Form A", "context_card": {"cover_photo": {"id": "12", "width": 800,
                                                                                       "height": 800}}},
                      {"id": "f2", "name": "No cover"}] if forms is None else forms
        self.photos_error, self.forms_error, self.more = photos_error, forms_error, more
        self.gets: list[tuple[str, dict]] = []
        self.posts = []

    def state(self):
        return {"kind": "ads_manager", "has_token": True}

    def request(self, method, path, params=None):
        if method != "GET":
            self.posts.append(path)
        self.gets.append((path, dict(params or {})))
        rows, err = (self.photos, self.photos_error) if path.endswith("/photos") else (self.forms, self.forms_error)
        if err:
            return {"status": 400, "body": {"error": err}}
        body = {"data": rows}
        if self.more:
            body["paging"] = {"next": "https://graph/next", "cursors": {"after": "x"}}
        return {"status": 200, "body": body}


def photos_client(meta):
    return g.GraphClient(meta, sleep=lambda s: None, min_interval_s=0.0, max_calls=20)


def test_photos_merge_by_id_and_carry_no_url():
    meta = PhotoMeta()
    read = fm.read_page_photos(photos_client(meta), PAGE)
    assert read.ok and [p.id for p in read.photos] == ["12", "11"]
    assert read.photos[0].form_name == "Form A" and read.photos[0].created_time == "2026-10-21T10:00:00+0000"
    assert URL not in json.dumps(read.as_dict()) and "images" not in json.dumps(read.as_dict())
    assert all("images" not in p["fields"] for _, p in meta.gets) and not meta.posts
    assert all(m[1]["limit"] in (50, 100) for m in meta.gets)


def test_one_refused_source_does_not_fail_the_other():
    err = {"code": 200, "message": "(#200) denied"}
    only_forms = fm.read_page_photos(photos_client(PhotoMeta(photos_error=err)), PAGE)
    assert only_forms.ok and [p.id for p in only_forms.photos] == ["12"]
    only_photos = fm.read_page_photos(photos_client(PhotoMeta(forms_error=err)), PAGE)
    assert only_photos.ok and [p.id for p in only_photos.photos] == ["12", "11"]


def test_both_refused_and_empty_give_the_same_text():
    err = {"code": 200, "message": "(#200) denied"}
    both = fm.read_page_photos(photos_client(PhotoMeta(photos_error=err, forms_error=err)), PAGE)
    assert both.state == fm.UNREADABLE and not both.ok and both.message == fm.MSG_NO_PHOTOS
    empty = fm.read_page_photos(photos_client(PhotoMeta(photos=[], forms=[])), PAGE)
    assert empty.state == fm.EMPTY and empty.ok and empty.message == fm.MSG_NO_PHOTOS


def test_truncated_when_a_next_page_exists_and_bad_page_makes_no_call():
    meta = PhotoMeta(more=True)
    assert fm.read_page_photos(photos_client(meta), PAGE).truncated is True
    assert fm.read_page_photos(photos_client(PhotoMeta()), PAGE).truncated is False
    meta = PhotoMeta()
    assert fm.read_page_photos(photos_client(meta), "12/../x").state == fm.NO_PAGE and meta.gets == []


def test_a_stop_of_the_client_is_not_swallowed_by_the_photos_read():
    class Stopping(PhotoMeta):
        def request(self, method, path, params=None):
            return {"status": 400, "body": {"error": {"code": 4, "message": "limit"}}}

    with pytest.raises(g.RateLimited):
        fm.read_page_photos(photos_client(Stopping()), PAGE)


def _patch_session(monkeypatch, meta, pages=(PAGE,)):
    from src.analysis_agent import account_spend as _as
    from src.analysis_agent import account_spend_api as _api
    from src.web import account_pages

    client = photos_client(meta)

    @contextmanager
    def fake_session(account):
        yield SimpleNamespace(client=client, act="act_1", account=account, notes={})

    monkeypatch.setattr(_api, "_session", fake_session)
    monkeypatch.setattr(_as, "serialized_browser", lambda **kw: (lambda fn: fn))
    monkeypatch.setattr(account_pages, "pages_from_client", lambda c, act, **k: [{"id": p, "name": p} for p in pages])
    return client


def test_read_photos_refuses_a_page_outside_the_account_and_cannot_write(monkeypatch):
    from src.web import page_forms

    meta = PhotoMeta()
    client = _patch_session(monkeypatch, meta)
    out = page_forms.read_photos("Acc", PAGE)
    assert out["ok"] is True and [p["id"] for p in out["photos"]] == ["12", "11"] and out["account"] == "Acc"
    assert client.writes == 0 and not client.allow_writes and not meta.posts
    meta2 = PhotoMeta()
    _patch_session(monkeypatch, meta2)
    bad = page_forms.read_photos("Acc", "999")
    assert bad["ok"] is False and "not available for this account" in bad["message"] and meta2.gets == []


def test_the_photos_endpoint_needs_account_page_and_the_api_backend(monkeypatch):
    from src.web import app as web
    from src.web import page_forms
    from tests.test_acl1775_instant_forms import _Req

    calls = []
    monkeypatch.setattr(page_forms, "read_photos", lambda a, p: calls.append((a, p)) or {"ok": True, "photos": []})
    monkeypatch.setenv("ACL_FB_BACKEND", "rpa")
    assert asyncio.run(web.api_page_photos(_Req({"account_label": "A", "page_id": PAGE}))).status_code == 409
    monkeypatch.setenv("ACL_FB_BACKEND", "api")
    assert asyncio.run(web.api_page_photos(_Req({"page_id": PAGE}))).status_code == 400
    assert asyncio.run(web.api_page_photos(_Req({"account_label": "A"}))).status_code == 400
    assert asyncio.run(web.api_page_photos(_Req({"account_label": "A", "page_id": PAGE}))).status_code == 200
    assert calls == [("A", PAGE)]


# ───────────────────────── страница ─────────────────────────

import re  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "src" / "web" / "templates" / "report.html"


def _node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    return node


def test_the_page_validates_the_cover_exactly_like_the_server():
    cases = {
        "ok": tpl(**COVER),
        "plain": tpl(),
        "photo_only": tpl(cover_photo_id=PHOTO),
        "page_only": tpl(cover_page_id=PAGE),
        "letters": tpl(cover_photo_id="12ab", cover_page_id=PAGE),
        "page_letters": tpl(cover_photo_id=PHOTO, cover_page_id="p1"),
        "no_description": {**TEMPLATE, "intro": "", **COVER},
        "half_no_description": {**TEMPLATE, "intro": "", "cover_photo_id": PHOTO},
        "blank_pair": tpl(cover_photo_id="", cover_page_id=""),
    }
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
        import json
        json.dump(cases, fh)
        path = fh.name
    try:
        done = subprocess.run([_node(), str(ROOT / "tests" / "form_templates_harness.cjs"), str(HTML), path],
                              capture_output=True, text=True, encoding="utf-8", timeout=60)
    finally:
        Path(path).unlink(missing_ok=True)
    assert done.returncode == 0, done.stderr
    js = json.loads(done.stdout)
    for name, t in cases.items():
        assert js[name] == ft.validate(t)[1], name


def test_the_picker_runs_for_real():
    done = subprocess.run([_node(), str(ROOT / "tests" / "cover_picker_harness.cjs"), str(HTML)],
                          capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "ok"


def test_the_picker_loads_no_remote_image_and_the_csp_is_not_relaxed():
    page = HTML.read_text(encoding="utf-8")
    start = page.index("window.formCoverPicker = function")
    picker = page[start:page.index("function coverContext", start)]
    assert "<img" not in picker and "createElement('img')" not in picker and ".src" not in picker
    assert "https://" not in picker and "innerHTML" not in picker
    app = (ROOT / "src" / "web" / "app.py").read_text(encoding="utf-8")
    assert "img-src 'self' data: blob:;" in app


def test_the_builder_panel_template_carries_the_cover_pair():
    page = HTML.read_text(encoding="utf-8")
    assert 'class="cb-fc-label cb-fc-cover"' in page
    body = page[page.index("window.cbFormPanelTemplate = function"):]
    body = body[:body.index("window.cbFormCreateOpen")]
    assert "coverOf(panel)" in body and "cover.get()" in body
    # Страница билдера - та же, что у Refresh forms
    assert "window.cbResolvedPage ? window.cbResolvedPage(slotEl)" in page[page.index("function coverContext"):][:400]


def test_the_forms_tab_uses_the_account_pages_endpoint_and_one_picker():
    page = HTML.read_text(encoding="utf-8")
    assert 'id="fm-cover-account"' in page and 'id="fm-cover-page"' in page
    assert len(re.findall(r"fetch\('/api/page-photos'", page)) == 1, "чтение картинок должно быть в одном месте"
    assert "fetch('/api/account-pages'" in page
