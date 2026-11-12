"""ACL-2571: one shared form template, one background picture, uploaded to each Page at publish.

The picture is chosen without an account or a Page, kept on THIS computer (machine config folder, keyed by
template id + content hash) and sent to the campaign's Page from inside the account's profile only when a
campaign is published. The team library holds only a hash. Offline: no Meta, no Multilogin.
"""
from __future__ import annotations

import base64
import json
import struct
import zlib
from pathlib import Path

import pytest

from src.fb_api import form_cover_store as store
from src.fb_api import form_create as fc
from src.fb_api import form_templates as ft
from src.fb_api import graph as g
from tests.test_acl1775_instant_forms import PAGE
from tests.test_acl1776_create_forms import TEMPLATE, _creator, _library, _tpl_draft  # noqa: F401
from tests.test_acl2188_form_cover import CoverMeta
from tests.test_acl2054_forms_library import local, seeded, team  # noqa: F401

WITH_INTRO = {**TEMPLATE, "intro": "Tell us about your project."}


def _png(width: int = 600, height: int = 400) -> bytes:
    """A valid PNG that is large enough to be recognised in a text dump if it ever leaked."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    rnd = __import__("random").Random(width)                 # incompressible, so the file is big like a real photo
    raw = b"".join(b"\x00" + rnd.randbytes(width * 3) for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


PICTURE = _png()
SHA = __import__("hashlib").sha256(PICTURE).hexdigest()
OTHER = _png(300, 200)
OTHER_SHA = __import__("hashlib").sha256(OTHER).hexdigest()


def tpl(**over):
    return {**WITH_INTRO, **over}


@pytest.fixture(autouse=True)
def _config(tmp_path, monkeypatch):
    """Everything machine-local goes to a temp folder; the team library is the local file one."""
    from src.analysis_agent import team_library as tl

    tl.use_backend(None)
    monkeypatch.setenv("ACL_CONFIG_DIR", str(tmp_path / "acl"))
    return tmp_path / "acl"


def save_with_picture(data: bytes = PICTURE, **over) -> dict:
    staged = store.stage(data)
    assert staged["ok"], staged
    saved = ft.save(tpl(cover_local_sha256=staged["sha256"], **over))
    assert store.adopt(saved["id"], staged["sha256"])
    return saved


# ───────────────────────── storage: this computer only ─────────────────────────


def test_a_picture_is_kept_by_template_id_and_hash_in_the_machine_folder(_config):
    saved = save_with_picture()
    path = _config / "form_covers" / f"{saved['id']}-{SHA}.bin"
    assert path.is_file() and path.read_bytes() == PICTURE
    assert store.exists(saved["id"], SHA) and store.read(saved["id"], SHA) == PICTURE
    assert not (_config / "form_covers" / "_staged" / f"{SHA}.bin").exists(), "the staged copy moved, not copied"


def test_a_replaced_picture_removes_the_old_file_of_that_template(_config):
    saved = save_with_picture()
    staged = store.stage(OTHER)
    store.adopt(saved["id"], staged["sha256"])
    files = sorted(p.name for p in (_config / "form_covers").glob("*.bin"))
    assert files == [f"{saved['id']}-{OTHER_SHA}.bin"]


def test_a_stored_file_that_no_longer_matches_its_hash_is_not_used(_config):
    saved = save_with_picture()
    (_config / "form_covers" / f"{saved['id']}-{SHA}.bin").write_bytes(b"tampered")
    assert store.read(saved["id"], SHA) is None


@pytest.mark.parametrize("data,why", [
    (b"", "The image file is empty."),
    (b"GIF89a....", "The background image must be a JPEG or PNG file."),
    (PICTURE + b"\0" * (8 * 1024 * 1024), "The background image is larger than 8 MB."),
], ids=["empty", "gif", "too-big"])
def test_a_wrong_file_is_refused_before_it_is_kept(data, why, _config):
    out = store.stage(data)
    assert out == {"ok": False, "message": why}
    assert not (_config / "form_covers").exists()


@pytest.mark.parametrize("tid,sha", [("../x", SHA), ("aaaaaaaaaaaa", "../../etc/passwd"), ("", ""), ("aaaaaaaaaaaa", "A" * 64)])
def test_ids_that_could_leave_the_folder_are_refused(tid, sha):
    assert store.read(tid, sha) is None and not store.exists(tid, sha) and not store.adopt(tid, sha)


def test_the_photo_id_is_cached_per_page_and_picture():
    assert store.cached_photo("111", SHA) == ""
    store.remember_photo("111", SHA, "9001")
    store.remember_photo("222", SHA, "9002")
    store.remember_photo("111", OTHER_SHA, "9003")
    assert (store.cached_photo("111", SHA), store.cached_photo("222", SHA), store.cached_photo("111", OTHER_SHA)) == \
        ("9001", "9002", "9003"), "a photo id works only on its own Page"
    store.forget_photo("111", SHA)
    assert store.cached_photo("111", SHA) == "" and store.cached_photo("222", SHA) == "9002"


# ───────────────────────── the template and the team library ─────────────────────────


def test_a_template_with_a_local_picture_needs_no_account_or_page():
    clean, errors = ft.validate(tpl(cover_local_sha256=SHA))
    assert errors == [] and clean["cover_local_sha256"] == SHA
    assert "cover_photo_id" not in clean and "cover_page_id" not in clean
    assert "background image" in ft.summary(clean)


@pytest.mark.parametrize("over,error", [
    ({"cover_local_sha256": "xyz"}, "Background picture checksum must be 64 hex characters"),
    ({"cover_local_sha256": SHA, "cover_photo_id": "123", "cover_page_id": "456"},
     "Choose either a Page photo or a picture from this computer for the background, not both"),
    ({"cover_local_sha256": SHA, "intro": ""}, "Background image needs a Description (Meta shows it on the intro screen)"),
])
def test_the_local_picture_is_validated(over, error):
    _, errors = ft.validate(tpl(**over))
    assert error in errors


def test_the_picture_changes_the_form_name_and_is_the_same_on_every_page():
    plain, with_pic = ft.validate(tpl())[0], ft.validate(tpl(cover_local_sha256=SHA))[0]
    assert ft.form_name(plain) != ft.form_name(with_pic)
    # The Page photo id is injected at publish and does not touch the name (one name for every Page).
    a, b = ft.form_params(with_pic, "9001"), ft.form_params(with_pic, "9002")
    assert a["name"] == b["name"] == ft.form_name(with_pic)
    assert a["context_card"]["cover_photo_id"] == "9001" and b["context_card"]["cover_photo_id"] == "9002"
    assert "cover_photo_id" not in ft.form_params(with_pic)["context_card"]


def test_remove_drops_the_hash_from_the_record():
    saved = save_with_picture()
    rows = ft.listing()["rows"]
    row = next(r for r in rows if r["id"] == saved["id"])
    again = ft.save({**tpl(), "id": saved["id"], "_expected_revision": row["revision"]})      # no picture key = Remove
    assert "cover_local_sha256" not in again and "cover_local_sha256" not in ft.get(saved["id"])


def test_the_team_library_never_holds_image_bytes(team, _config):
    backend, on = team
    on("Anna")
    saved = save_with_picture()
    blob = "".join(text for text, _sha in backend.files.values())
    record = json.loads(backend.files["form_templates.json"][0])["templates"][saved["id"]]
    assert record["cover_local_sha256"] == SHA
    # Not the bytes, not a base64/hex copy of them, not a data: URL - and the whole library stays tiny.
    for needle in (PICTURE[:64], base64.b64encode(PICTURE[:48]).decode(), PICTURE[:32].hex(), "data:image"):
        assert (needle.decode("latin-1") if isinstance(needle, bytes) else needle) not in blob
    assert "\x89PNG" not in blob and len(blob) < 20_000 < len(PICTURE)
    # Every key of the record is a short text: no field can carry a file.
    assert all(len(json.dumps(v)) < 2000 for v in record.values())
    cfg = Path(__import__("os").environ["ACL_CONFIG_DIR"])
    assert not any(p.suffix in (".bin", ".png", ".jpg") for p in cfg.rglob("*") if "form_covers" not in p.parts)


def test_a_template_edited_on_another_machine_keeps_the_hash_but_has_no_file(team, tmp_path):
    backend, on = team
    on("Anna")
    saved = save_with_picture()
    on("Boris")                                    # another computer: its own config folder, no picture file
    got = ft.get(saved["id"])
    assert got["cover_local_sha256"] == SHA
    assert not store.exists(saved["id"], SHA) and store.read(saved["id"], SHA) is None


# ───────────────────────── dashboard routes ─────────────────────────


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from src.web import app as web_app

    return TestClient(web_app.app, base_url="http://127.0.0.1:9090"), {"Origin": "http://127.0.0.1:9090"}


def test_stage_save_and_serve_the_picture_without_an_account_or_a_page(client, _config):
    http, hdr = client
    r = http.post("/api/form-templates/cover-file", files={"file": ("any.jpg", PICTURE, "image/jpeg")}, headers=hdr)
    assert r.status_code == 200 and r.json()["sha256"] == SHA and r.json()["mime"] == "image/png"
    saved = http.post("/api/form-templates", json=tpl(cover_local_sha256=SHA), headers=hdr).json()["template"]
    got = http.get("/api/form-templates/cover-file", params={"id": saved["id"], "sha": SHA})
    assert got.status_code == 200 and got.content == PICTURE and got.headers["content-type"] == "image/png"
    assert got.headers["cache-control"] == "no-store" and got.headers["x-content-type-options"] == "nosniff"


def test_a_picture_that_is_not_on_this_computer_is_a_404_and_a_wrong_file_is_a_400(client):
    http, hdr = client
    assert http.get("/api/form-templates/cover-file", params={"id": "aaaaaaaaaaaa", "sha": SHA}).status_code == 404
    assert http.get("/api/form-templates/cover-file", params={"id": "../..", "sha": SHA}).status_code == 404
    bad = http.post("/api/form-templates/cover-file", files={"file": ("a.gif", b"GIF89a....", "image/gif")}, headers=hdr)
    assert bad.status_code == 400 and bad.json()["message"] == "The background image must be a JPEG or PNG file."
    assert http.post("/api/form-templates/cover-file", data={"x": "1"}, headers=hdr).status_code == 400


def test_a_duplicated_template_keeps_its_picture(client):
    http, hdr = client
    saved = save_with_picture()
    copy = http.post("/api/form-templates/duplicate", json={"id": saved["id"]}, headers=hdr).json()["template"]
    assert copy["id"] != saved["id"] and copy["cover_local_sha256"] == SHA
    assert store.read(copy["id"], SHA) == PICTURE


# ───────────────────────── publish: upload to the Page, then the form ─────────────────────────


class UploadMeta(CoverMeta):
    """CoverMeta + the web-session upload of the form background; records the order of everything."""

    def __init__(self, *a, upload_reply=None, **kw):
        super().__init__(*a, **kw)
        self.upload_reply = upload_reply
        self.uploads: list[tuple[str, str, str]] = []
        self.order: list[str] = []

    def upload_form_photo(self, page_id, data, *, mime):
        self.order.append("upload")
        self.uploads.append((page_id, __import__("hashlib").sha256(data).hexdigest(), mime))
        return self.upload_reply or {"ok": True, "photo_id": str(9000000 + len(self.uploads)), "width": 600, "height": 400}

    def write(self, path, params, files=None, filename="", on_ad=False, form_page="", **k):
        self.order.append("form" if form_page else "write:" + path.rsplit("/", 1)[-1])
        return super().write(path, params, files, filename, on_ad, form_page, **k)


def _publish(meta, tmp_path, tid):
    from tests.test_acl1776_create_forms import _run

    return _run(meta, _tpl_draft(tmp_path, [tid]))


def test_publish_uploads_the_picture_to_the_page_before_anything_is_created_then_makes_the_form(tmp_path):
    tid = save_with_picture()["id"]
    meta = UploadMeta()
    out, client = _publish(meta, tmp_path, tid)
    assert out.error == "", out.error
    assert meta.uploads == [(PAGE, SHA, "image/png")], "the bytes of the stored file went to this campaign's Page"
    assert meta.order[0] == "upload", "upload must come before the first object is created"
    assert meta.order.index("upload") < meta.order.index("form")
    assert not any(o.startswith("write:") for o in meta.order[:meta.order.index("upload")])
    photo = meta.form_posts[0][1]["context_card"]["cover_photo_id"]
    assert photo == "9000001"
    assert any("background picture uploaded to the Page" in s for s in out.steps)
    assert any("read back, ACTIVE, on the Page" in s for s in out.steps)
    assert store.cached_photo(PAGE, SHA) == "9000001"
    assert client.writes >= 2


def test_a_second_publish_to_the_same_page_reuses_the_cached_photo_without_an_upload(tmp_path):
    tid = save_with_picture()["id"]
    first = UploadMeta()
    out, _ = _publish(first, tmp_path, tid)
    assert out.error == "" and len(first.uploads) == 1
    second = UploadMeta()                                         # a fresh Page state: the form is not there yet
    (tmp_path / "again").mkdir()
    out, _ = _publish(second, tmp_path / "again", tid)
    assert out.error == "", out.error
    assert second.uploads == [], "no second upload for the same Page and picture"
    assert second.form_posts[0][1]["context_card"]["cover_photo_id"] == "9000001"
    assert any("already on this Page, reused (no upload)" in s for s in out.steps)


def test_two_pages_get_one_upload_each_and_both_forms_get_the_background(tmp_path, monkeypatch):
    """The same template on two Pages: each Page gets its own photo id, once."""
    from tests.test_acl1776_create_forms import _run

    tid = save_with_picture()["id"]
    page2 = "222"

    class TwoPages(UploadMeta):
        def _get(self, path, params):
            parts = path.strip("/").split("/")
            if len(parts) == 2 and parts[1] == "promote_pages":          # the account can advertise from both Pages
                return {"data": [{"id": PAGE}, {"id": page2}]}
            return super()._get(path.replace(f"/{page2}", f"/{PAGE}", 1) if path.startswith(f"/{page2}") else path, params)

        def write(self, path, params, files=None, filename="", on_ad=False, form_page="", **k):
            if form_page == page2:
                path = path.replace(f"/{page2}/", f"/{PAGE}/", 1)
                res = super().write(path, params, files, filename, on_ad, PAGE, **k)
                fid = res["body"]["id"]
                self.details[fid]["page"] = {"id": page2}
                return res
            return super().write(path, params, files, filename, on_ad, form_page, **k)

    first = UploadMeta()
    out1, _ = _publish(first, tmp_path, tid)
    (tmp_path / "p2").mkdir()
    d2 = _tpl_draft(tmp_path / "p2", [tid])
    d2.page_id = page2
    second = TwoPages(upload_reply={"ok": True, "photo_id": "9100001", "width": 600, "height": 400})
    out2, _ = _run(second, d2)
    assert out1.error == "", out1.error
    assert out2.error == "", out2.error
    assert [u[0] for u in first.uploads] == [PAGE] and [u[0] for u in second.uploads] == [page2]
    assert first.form_posts[0][1]["context_card"]["cover_photo_id"] != second.form_posts[0][1]["context_card"]["cover_photo_id"]
    assert store.cached_photo(PAGE, SHA) and store.cached_photo(page2, SHA)
    assert first.form_posts[0][1]["name"] == second.form_posts[0][1]["name"], "one template -> one form name"


@pytest.mark.parametrize("reply,text", [
    ({"error": "upload_refused", "text": "Photo Upload Failed: too small", "status": 200}, "Photo Upload Failed: too small"),
    ({"error": "no_session"}, "Ads Manager did not show the session values"),
    ({"error": "network"}, "the upload result is unknown"),
], ids=["meta-refused", "no-session", "unknown"])
def test_an_upload_refusal_stops_the_campaign_before_any_object_with_metas_answer(tmp_path, reply, text):
    tid = save_with_picture()["id"]
    meta = UploadMeta(upload_reply=reply)
    out, _ = _publish(meta, tmp_path, tid)
    assert out.stop_kind == "form_cover_refused", out.stop_kind
    assert text in out.error and "Nothing was created" in out.error
    assert meta.posts == [] and meta.form_posts == [], "nothing at all was created"
    assert not out.created
    assert store.cached_photo(PAGE, SHA) == "", "a failed upload is not cached"


def test_a_form_refused_with_a_cached_photo_forgets_the_cache_so_the_next_run_uploads_again(tmp_path):
    tid = save_with_picture()["id"]
    store.remember_photo(PAGE, SHA, "555")
    meta = UploadMeta()
    meta.refuse = {"code": 100, "message": "Invalid photo id"}
    out, _ = _publish(meta, tmp_path, tid)
    assert out.stop_kind == "form_refused" and "Invalid photo id" in out.error
    assert meta.uploads == [] and store.cached_photo(PAGE, SHA) == ""


def test_without_the_file_on_this_computer_the_form_has_no_background_and_never_a_wrong_photo(tmp_path):
    tid = save_with_picture()["id"]
    (Path(store.store_dir()) / f"{tid}-{SHA}.bin").unlink()
    store.remember_photo("999", SHA, "777")                                    # another Page's photo is in the cache
    meta = UploadMeta()
    out, _ = _publish(meta, tmp_path, tid)
    assert out.error == "", out.error
    assert meta.uploads == []
    assert "cover_photo_id" not in meta.form_posts[0][1]["context_card"]
    assert any(fc.MSG_COVER_ON_OTHER_COMPUTER.format(name=TEMPLATE["name"]) in w for w in out.warnings), out.warnings
    # It is the same form as the template without a picture (name from the template without the hash).
    plain = ft.validate(tpl())[0]
    assert meta.form_posts[0][1]["name"] == ft.form_name(plain)


def test_a_template_without_a_picture_publishes_as_before(tmp_path):
    tid = ft.save(tpl())["id"]
    meta = UploadMeta()
    out, _ = _publish(meta, tmp_path, tid)
    assert out.error == "" and meta.uploads == []
    assert "cover_photo_id" not in meta.form_posts[0][1]["context_card"]


def test_the_call_estimate_counts_one_upload_per_template_with_a_picture(tmp_path):
    from src.fb_api import creator as cr

    with_pic, plain = save_with_picture()["id"], ft.save(tpl(name="Other form"))["id"]
    a = cr.plan_from_draft(_tpl_draft(tmp_path, [with_pic]))
    b = cr.plan_from_draft(_tpl_draft(tmp_path, [plain]))
    assert cr.estimate_create_calls(a) == cr.estimate_create_calls(b) + 1


def test_the_write_budget_is_a_hard_stop_for_the_upload(tmp_path):
    """The client counts the upload as one call; with no calls left it is refused before sending."""
    tid = save_with_picture()["id"]
    meta = UploadMeta()
    client = g.GraphClient(meta, allow_writes=True, sleep=lambda s: None, min_interval_s=0.0, max_calls=0)
    with pytest.raises(g.CallBudget):
        client.upload_form_photo(PAGE, PICTURE, mime="image/png")
    assert meta.uploads == []
