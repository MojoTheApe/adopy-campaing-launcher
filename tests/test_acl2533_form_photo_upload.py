"""ACL-2533: фон формы лидов - загрузка картинки из билдера, с предпросмотром.

Загрузка - НЕ Graph: тот же запрос, что шлёт сам Ads Manager (`/ads/leadgen/upload/photo/`), с вкладки
профиля, от имени закреплённой Страницы, по сессии пользователя. Здесь проверяется всё, что можно
проверить без живого аккаунта: запрос помощника (JS в Node на поддельной странице), отказы политики,
путь предпросмотра, публикация с загруженным id, отказ с именем формы и неизменность пути без картинки.
Живая загрузка на тестовой Странице (O1-O3 из исследования) - отдельный шаг с владельцем.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.fb_api import form_templates as ft
from src.fb_api import graph as g
from src.fb_api import token_capture as tc
from src.fb_api import write_policy as wp
from tests.test_acl1761_api_create import StubTab
from tests.test_acl1776_create_forms import PAGE, TEMPLATE
from tests.test_acl1776_create_forms import FormsMeta, _run, _tpl_draft  # noqa: F401
from tests.test_acl2054_forms_library import local, seeded, team  # noqa: F401,E402
from tests.test_acl2188_form_cover import PHOTO, CoverMeta, tpl  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "src" / "web" / "templates" / "report.html"

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 60 + b"jpeg-bytes"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 60 + b"png-bytes"
SHA = hashlib.sha256(JPEG).hexdigest()
NEW_PHOTO = "122118799999999999"
DTSG = "AQHabc123_XYZ:45"
LSD = "AVq-lsd_9"
USER = "100012345678901"
JAZOEST = "2" + str(sum(ord(c) for c in DTSG))

# ───────────────────────── политика записи ─────────────────────────


def test_the_policy_allows_only_the_fixed_host_the_pinned_page_and_jpeg_png():
    url = wp.check_form_photo_upload(wp.FORM_PHOTO_HOST, PAGE, PAGE, "image/jpeg", 1000)
    assert url == "https://adsmanager.facebook.com/ads/leadgen/upload/photo/"
    assert wp.check_form_photo_upload("adsmanager.facebook.com", PAGE, PAGE, "image/png", wp.FORM_PHOTO_MAX_BYTES)


@pytest.mark.parametrize("host", ["graph.facebook.com", "www.facebook.com", "evil.example.com",
                                  "adsmanager.facebook.com.evil.com", ""])
def test_the_policy_refuses_any_other_host(host):
    with pytest.raises(wp.WriteRefused, match="only from adsmanager.facebook.com"):
        wp.check_form_photo_upload(host, PAGE, PAGE, "image/jpeg", 10)


def test_the_policy_refuses_a_page_other_than_the_pinned_one():
    with pytest.raises(wp.WriteRefused, match="not the Page of the plan"):
        wp.check_form_photo_upload(wp.FORM_PHOTO_HOST, "999", PAGE, "image/jpeg", 10)
    with pytest.raises(wp.WriteRefused, match="no Page is pinned"):
        wp.check_form_photo_upload(wp.FORM_PHOTO_HOST, PAGE, "", "image/jpeg", 10)
    with pytest.raises(wp.WriteRefused, match="not set"):
        wp.check_form_photo_upload(wp.FORM_PHOTO_HOST, "12ab", PAGE, "image/jpeg", 10)


@pytest.mark.parametrize("mime", ["image/gif", "image/webp", "application/pdf", "image/jpeg; x=1", ""])
def test_the_policy_refuses_other_file_types(mime):
    with pytest.raises(wp.WriteRefused, match="JPEG or PNG"):
        wp.check_form_photo_upload(wp.FORM_PHOTO_HOST, PAGE, PAGE, mime, 10)


@pytest.mark.parametrize("size", [0, -1, True, 1.5, None])
def test_the_policy_refuses_an_empty_or_odd_size(size):
    with pytest.raises(wp.WriteRefused, match="empty"):
        wp.check_form_photo_upload(wp.FORM_PHOTO_HOST, PAGE, PAGE, "image/png", size)


def test_the_policy_refuses_a_file_over_the_cap():
    with pytest.raises(wp.WriteRefused, match="larger than 8 MB"):
        wp.check_form_photo_upload(wp.FORM_PHOTO_HOST, PAGE, PAGE, "image/png", wp.FORM_PHOTO_MAX_BYTES + 1)


def test_the_policy_table_for_the_page_helper_and_the_upload_budget():
    table = wp.policy_for_form_photo(PAGE)
    assert table == {"host": "adsmanager.facebook.com", "url": wp.FORM_PHOTO_URL, "page": PAGE,
                     "mimes": ["image/jpeg", "image/png"], "max_bytes": 8 * 1024 * 1024, "page_re": wp.FORM_PAGE_RE}
    # ACL-2571: the creation/publish helper carries the same upload block (the Page of the plan), and an
    # object-edit helper still does not.
    assert wp.policy_for_page("act_1234567890", PAGE)["form_photo"] == table
    assert "form_photo" not in wp.policy_for_commands("act_1234567890")
    assert wp.policy_for_form_photo_only(PAGE) == {"form_photo_only": True, "form_photo": table}
    assert wp.FORM_PHOTO_UPLOAD_CALLS == 1


# ───────────────────────── помощник страницы (JS в Node) ─────────────────────────

_HARNESS = r"""
const DTSG = __DTSG__, LSD = __LSD__, USER = __USER__;
const OK_REPLY = 'for (;;);' + JSON.stringify({__ar: 1, payload: {photo: {id: '__NEW__',
  src: 'https://scontent.xx.fbcdn.net/v/t39.30808-6/x_n.jpg?oe=1', width: 1024, height: 1536,
  originalWidth: 1024, originalHeight: 1536, primaryColor: '#195EA9'}}, lid: '1'});
function b64(bytes) { return Buffer.from(bytes).toString('base64'); }
async function sha(bytes) {
  return Buffer.from(await crypto.subtle.digest('SHA-256', bytes)).toString('hex');
}
function scripts(parts) {
  const out = [];
  if (parts.dtsg !== false) out.push({textContent: 'x;["DTSGInitialData",[],{"token":"' + DTSG + '"},258]'});
  if (parts.lsd !== false) out.push({textContent: 'y;["LSD",[],{"token":"' + LSD + '"},323]'});
  if (parts.user !== false) out.push({textContent: 'z;"USER_ID":"' + USER + '","ACCOUNT_ID":"' + USER + '"'});
  return out;
}
async function scenario(o) {
  const calls = [];
  const fakeFetch = async (url, init) => {
    calls.push({url: String(url), init: init || {}});
    if (o.net === 'fail') throw new TypeError('Failed to fetch');
    const text = o.reply === undefined ? OK_REPLY : o.reply;
    return {status: o.status || 200, text: async () => text, headers: {get: () => null}};
  };
  const loc = {href: 'https://' + (o.host || 'adsmanager.facebook.com') + '/adsmanager/manage/campaigns?act=1',
               hostname: o.host || 'adsmanager.facebook.com', protocol: 'https:',
               pathname: '/adsmanager/manage/campaigns'};
  const doc = {readyState: 'complete', querySelector: () => null, scripts: scripts(o.page || {}),
               documentElement: {innerHTML: ''}};
  const perf = {getEntriesByType: () => []};
  const G = {};
  new Function('globalThis', 'location', 'document', 'performance', 'fetch', 'URL', 'URLSearchParams', HELPER[o.helper || 'only'])(
    G, loc, doc, perf, fakeFetch, URL, URLSearchParams);
  const h = G[NAME];
  const bytes = o.bytes || Buffer.from(__JPEG__, 'base64');
  const staged = h.stage('f1', b64(bytes));
  const spec = Object.assign({av: __PAGE__, mime: 'image/jpeg', stage: 'f1',
                              expect: {bytes: bytes.length, sha256: o.sha || await sha(bytes)}}, o.spec || {});
  const result = await h.uploadFormPhoto(spec);
  const again = o.twice ? await h.uploadFormPhoto(spec) : null;
  const graphPost = await h.call({method: 'POST', path: '/act_1234567890/campaigns', params: {name: 'x'}});
  return {result, again, staged, graphPost, calls: calls.map((c) => {
    const body = c.init.body;
    return {url: c.url, method: c.init.method, credentials: c.init.credentials, redirect: c.init.redirect,
            headers: c.init.headers, body_dtsg: body && body.get ? body.get('fb_dtsg') : null,
            file_type: body && body.get && body.get('file') ? body.get('file').type : null,
            file_name: body && body.get && body.get('file') ? body.get('file').name : null,
            file_size: body && body.get && body.get('file') ? body.get('file').size : null};
  })};
}
(async () => {
  const out = {};
  for (const [name, o] of Object.entries(SCENARIOS)) out[name] = await scenario(o);
  process.stdout.write(JSON.stringify(out));
})();
"""

SCENARIOS = {
    "ok": {},
    "png": {"bytes": "PNG", "spec": {"mime": "image/png"}},
    "twice": {"twice": True},
    "wrong_host": {"host": "www.facebook.com"},
    "lookalike_host": {"host": "adsmanager.facebook.com.evil.com"},
    "av_mismatch": {"spec": {"av": "999"}},
    "av_bad": {"spec": {"av": "12ab"}},
    "bad_type": {"spec": {"mime": "image/gif"}},
    "too_big": {"spec": {"expect": {"bytes": 8 * 1024 * 1024 + 1, "sha256": "x"}}},
    "size_lie": {"spec": {"expect": {"bytes": 5, "sha256": "x"}}},
    "bad_sha": {"sha": "0" * 64},
    "no_dtsg": {"page": {"dtsg": False}},
    "no_lsd": {"page": {"lsd": False}},
    "no_user": {"page": {"user": False}},
    "refused": {"reply": 'for (;;);{"__ar":1,"error":1357031,"errorSummary":"Photo Upload Failed",'
                         '"errorDescription":"Image is too small. fb_dtsg=' + DTSG + ' lsd ' + LSD + '"}'},
    "html_reply": {"reply": "<html>login</html>", "status": 200},
    "network": {"net": "fail"},
    "no_policy": {"helper": "none"},
    "with_graph_policy": {"helper": "graph"},
}


def _node_run() -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    name = "__acl_graph_photo"
    helpers = {"only": tc.helper_source(name, wp.policy_for_form_photo_only(PAGE)),
               "graph": tc.helper_source(name, wp.policy_for_page("act_1234567890", PAGE)),
               "none": tc.helper_source(name, None)}
    src = (_HARNESS.replace("__DTSG__", json.dumps(DTSG)).replace("__LSD__", json.dumps(LSD))
           .replace("__USER__", json.dumps(USER)).replace("__NEW__", NEW_PHOTO)
           .replace("__PAGE__", json.dumps(PAGE)).replace("__JPEG__", json.dumps(base64.b64encode(JPEG).decode()))
           .replace("NAME", json.dumps(name)))
    # `bytes: "PNG"` in a scenario is a marker: the real PNG bytes are put in.
    src = ("const HELPER = " + json.dumps(helpers) + ";\nconst SCENARIOS = " + json.dumps(SCENARIOS) + ";\n"
           "const PNG = Buffer.from(" + json.dumps(base64.b64encode(PNG).decode()) + ", 'base64');\n"
           "for (const o of Object.values(SCENARIOS)) if (o.bytes === 'PNG') o.bytes = PNG;\n" + src)
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as fh:
        fh.write(src)
        script = fh.name
    try:
        done = subprocess.run([node, script], capture_output=True, encoding="utf-8", timeout=90)
    finally:
        Path(script).unlink(missing_ok=True)
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout)


@pytest.fixture(scope="module")
def js():
    return _node_run()


def test_the_upload_request_is_the_one_ads_manager_sends(js):
    out = js["ok"]
    assert out["result"] == {"ok": True, "photo_id": NEW_PHOTO, "width": 1024, "height": 1536}
    assert len(out["calls"]) == 1, "one request, no retry"
    call = out["calls"][0]
    head, query = call["url"].split("?", 1)
    assert head == "https://adsmanager.facebook.com/ads/leadgen/upload/photo/"
    params = dict(p.split("=", 1) for p in query.split("&"))
    assert params == {"av": PAGE, "__user": USER, "__a": "1", "fb_dtsg": DTSG.replace(":", "%3A"),
                      "jazoest": JAZOEST, "lsd": LSD}
    assert call["method"] == "POST" and call["credentials"] == "same-origin" and call["redirect"] == "error"
    assert call["headers"] == {"x-fb-lsd": LSD}
    assert call["body_dtsg"] == DTSG
    assert call["file_type"] == "image/jpeg" and re.fullmatch(r"[0-9a-f]{16}\.jpg", call["file_name"])
    assert call["file_size"] == len(JPEG)


def test_the_reply_carries_only_id_and_size_never_the_cdn_address_or_secrets(js):
    text = json.dumps(js["ok"]["result"])
    assert "fbcdn" not in text and "src" not in text and DTSG not in text and LSD not in text
    assert set(js["ok"]["result"]) == {"ok", "photo_id", "width", "height"}


def test_a_png_goes_with_a_png_name_and_type(js):
    call = js["png"]["calls"][0]
    assert js["png"]["result"]["photo_id"] == NEW_PHOTO
    assert call["file_type"] == "image/png" and call["file_name"].endswith(".png")


def test_the_staged_file_is_dropped_after_one_attempt(js):
    assert js["twice"]["result"]["ok"] is True
    assert js["twice"]["again"] == {"error": "no_file"} and len(js["twice"]["calls"]) == 1


@pytest.mark.parametrize("name,why", [
    ("wrong_host", "photo_host"), ("lookalike_host", "photo_host"), ("av_mismatch", "photo_page"),
    ("av_bad", "photo_av"), ("bad_type", "photo_type"), ("too_big", "photo_size"), ("size_lie", "file_size"),
    ("bad_sha", "file_hash"), ("no_policy", "photo_policy")])
def test_the_page_helper_refuses_before_any_request(js, name, why):
    assert js[name]["result"] == {"error": "write_refused", "why": why}
    assert js[name]["calls"] == []


@pytest.mark.parametrize("name", ["no_dtsg", "no_lsd", "no_user"])
def test_missing_page_secrets_stop_before_any_request(js, name):
    assert js[name]["result"] == {"error": "no_session"} and js[name]["calls"] == []


def test_a_refusal_of_meta_gives_its_text_and_hides_the_secrets(js):
    out = js["refused"]["result"]
    assert out["error"] == "upload_refused" and out["status"] == 200
    assert out["text"].startswith("Photo Upload Failed: Image is too small.")
    assert DTSG not in out["text"] and LSD not in out["text"]
    assert len(js["refused"]["calls"]) == 1


def test_an_unexpected_reply_is_a_refusal_with_a_plain_reason_and_no_retry(js):
    out = js["html_reply"]["result"]
    assert out["error"] == "upload_refused" and "unexpected reply" in out["text"]
    assert len(js["html_reply"]["calls"]) == 1


def test_a_network_failure_is_one_request_and_is_not_retried(js):
    assert js["network"]["result"] == {"error": "network"} and len(js["network"]["calls"]) == 1


def test_the_upload_only_helper_has_no_graph_write_chain(js):
    assert js["ok"]["graphPost"] == {"error": "read_only"}
    # ACL-2571: the creation/publish policy carries the upload block of its Page, so it uploads (one request)
    assert js["with_graph_policy"]["result"]["ok"] is True
    assert len(js["with_graph_policy"]["calls"]) == 1


# ───────────────────────── Python: транспорт ─────────────────────────


class PhotoTab(StubTab):
    def __init__(self, reply=None, **kw):
        super().__init__(**kw)
        self.photo_reply = reply if reply is not None else {"ok": True, "photo_id": NEW_PHOTO, "width": 10, "height": 20}

    def evaluate_in(self, ctx, expr, timeout=60.0):
        if ".uploadFormPhoto(" in expr:
            self.exprs.append(expr)
            if self.call_exc:
                raise self.call_exc
            return self.photo_reply
        return super().evaluate_in(ctx, expr, timeout)

    def uploads(self):
        return [e for e in self.exprs if ".uploadFormPhoto(" in e]


def _photo_graph(tab=None, pin=PAGE):
    t = tc.InPageGraph(tab or PhotoTab(), form_photo=True)
    if pin:
        t.pin_photo_page(pin)
    return t


def test_the_form_photo_mode_is_separate_and_cannot_write_to_graph():
    with pytest.raises(ValueError):
        tc.InPageGraph(PhotoTab(), form_photo=True, allow_writes=True, write_act="act_1234567890")
    t = _photo_graph()
    assert t.allow_writes is False
    with pytest.raises(ValueError):
        t.write("/act_1234567890/campaigns", {"name": "x"})
    assert t._write_policy == wp.policy_for_form_photo_only(PAGE)
    assert "form_photo_only" in tc.helper_source("n", t._write_policy)


def test_pinning_the_photo_page_is_once_and_the_same_value_only():
    t = _photo_graph(pin="")
    assert t._write_policy["form_photo"]["page"] == ""
    t.pin_photo_page(PAGE)
    t.pin_photo_page(PAGE)
    with pytest.raises(ValueError, match="different Page"):
        t.pin_photo_page("999")
    with pytest.raises(ValueError):
        t.pin_photo_page("12ab")
    with pytest.raises(ValueError):
        tc.InPageGraph(PhotoTab()).pin_photo_page(PAGE)


def test_the_upload_stages_the_file_and_sends_one_spec_with_size_and_sha():
    tab = PhotoTab()
    out = _photo_graph(tab).upload_form_photo(PAGE, JPEG, mime="image/jpeg")
    assert out["photo_id"] == NEW_PHOTO
    (expr,) = tab.uploads()
    spec = json.loads(expr.split(".uploadFormPhoto(", 1)[1].rsplit(")", 1)[0])
    assert spec["av"] == PAGE and spec["mime"] == "image/jpeg"
    assert spec["expect"] == {"bytes": len(JPEG), "sha256": SHA}
    assert re.fullmatch(r"f[0-9a-f]{16}", spec["stage"]) and tab.staged[spec["stage"]] == len(JPEG)
    assert "access_token" not in expr and DTSG not in expr


@pytest.mark.parametrize("kw,exc", [
    ({"page": "999", "mime": "image/jpeg", "data": JPEG}, wp.WriteRefused),
    ({"page": PAGE, "mime": "image/gif", "data": JPEG}, wp.WriteRefused),
    ({"page": PAGE, "mime": "image/jpeg", "data": b""}, wp.WriteRefused),
    ({"page": PAGE, "mime": "image/jpeg", "data": b"x" * (wp.FORM_PHOTO_MAX_BYTES + 1)}, wp.WriteRefused),
    ({"page": PAGE, "mime": "image/jpeg", "data": "text"}, ValueError)])
def test_python_refuses_before_the_page_sees_anything(kw, exc):
    tab = PhotoTab()
    with pytest.raises(exc):
        _photo_graph(tab).upload_form_photo(kw["page"], kw["data"], mime=kw["mime"])
    assert tab.stage_calls == 0 and tab.uploads() == []


def test_an_unpinned_session_does_not_upload():
    tab = PhotoTab()
    with pytest.raises(wp.WriteRefused, match="no Page is pinned"):
        _photo_graph(tab, pin="").upload_form_photo(PAGE, JPEG, mime="image/jpeg")
    assert tab.uploads() == []
    with pytest.raises(ValueError):
        tc.InPageGraph(PhotoTab()).upload_form_photo(PAGE, JPEG, mime="image/jpeg")


def test_a_page_that_goes_away_after_sending_is_unknown_not_a_retry():
    tab = PhotoTab(call_exc=tc.CdpTimeout("slow"))
    out = _photo_graph(tab).upload_form_photo(PAGE, JPEG, mime="image/jpeg")
    assert out == {"error": "call_unknown", "reason": "cdp_timeout"} and len(tab.uploads()) == 1


def test_the_creation_helper_uploads_only_to_its_own_page_and_the_object_edit_helper_never():
    tab = PhotoTab()
    t = tc.InPageGraph(tab, allow_writes=True, write_act="act_1234567890", write_page=PAGE)
    assert t.upload_form_photo(PAGE, JPEG, mime="image/jpeg")["photo_id"] == NEW_PHOTO
    assert len(tab.uploads()) == 1
    other = PhotoTab()
    t2 = tc.InPageGraph(other, allow_writes=True, write_act="act_1234567890", write_page=PAGE)
    with pytest.raises(wp.WriteRefused):                    # a Page that is not the plan's
        t2.upload_form_photo("999", JPEG, mime="image/jpeg")
    assert other.stage_calls == 0 and other.uploads() == []
    no_page = PhotoTab()
    with pytest.raises(ValueError, match="not enabled"):    # the preflight helper has no Page: no upload
        tc.InPageGraph(no_page, allow_writes=True, write_act="act_1234567890").upload_form_photo(
            PAGE, JPEG, mime="image/jpeg")
    with pytest.raises(ValueError):                         # an object-edit helper never uploads
        tc.InPageGraph(PhotoTab(), allow_object_updates=True, write_act="act_1234567890").upload_form_photo(
            PAGE, JPEG, mime="image/jpeg")


# ───────────────────────── Python: клиент ─────────────────────────


def _client(reply, tab=None, max_calls=9):
    class T:
        form_photo_page = PAGE

        def upload_form_photo(self, page_id, data, *, mime):
            self.args = (page_id, len(data), mime)
            return reply if not isinstance(reply, Exception) else (_ for _ in ()).throw(reply)

    t = T()
    return g.GraphClient(t, sleep=lambda s: None, min_interval_s=0.0, max_calls=max_calls), t


def test_the_client_returns_id_and_size_and_counts_one_call():
    c, t = _client({"ok": True, "photo_id": NEW_PHOTO, "width": 1024, "height": 1536, "src": "https://cdn/x"})
    assert c.upload_form_photo(PAGE, JPEG, mime="image/jpeg") == {"photo_id": NEW_PHOTO, "width": 1024, "height": 1536}
    assert c.calls == 1 and c.writes == 1 and t.args == (PAGE, len(JPEG), "image/jpeg")


def test_meta_refusal_is_a_graph_error_with_meta_text():
    c, _ = _client({"error": "upload_refused", "text": "Photo Upload Failed: too small", "status": 200})
    with pytest.raises(g.GraphError, match="Meta refused the background image: Photo Upload Failed: too small"):
        c.upload_form_photo(PAGE, JPEG, mime="image/jpeg")


@pytest.mark.parametrize("err", ["write_refused", "no_session", "no_file", "read_only"])
def test_a_refusal_before_sending_is_a_plain_refusal_and_does_not_stop_the_client(err):
    c, _ = _client({"error": err, "why": "photo_host"})
    with pytest.raises(g.FormPhotoRefused, match="nothing was sent") as got:
        c.upload_form_photo(PAGE, JPEG, mime="image/jpeg")
    assert not isinstance(got.value, g.GraphStop) and c.stopped is None     # no stop -> no profile cooldown


def test_a_missing_fb_dtsg_says_what_to_do():
    c, _ = _client({"error": "no_session"})
    with pytest.raises(g.FormPhotoRefused, match="Reload Ads Manager") as got:
        c.upload_form_photo(PAGE, JPEG, mime="image/jpeg")
    assert got.value.code == "no_session" and c.stopped is None


@pytest.mark.parametrize("reply", [{"error": "network"}, {"error": "call_unknown", "reason": "page_closed"},
                                   {"ok": True}, {"ok": True, "photo_id": "x1"}])
def test_an_unknown_result_is_write_unknown(reply):
    c, _ = _client(reply)
    with pytest.raises(g.WriteUnknown):
        c.upload_form_photo(PAGE, JPEG, mime="image/jpeg")


def test_the_client_respects_the_call_budget():
    c, t = _client({"ok": True, "photo_id": NEW_PHOTO}, max_calls=0)
    with pytest.raises(g.CallBudget):
        c.upload_form_photo(PAGE, JPEG, mime="image/jpeg")
    assert not hasattr(t, "args")


# ───────────────────────── дешборд: файл, сессия, журнал ─────────────────────────

from src.web import page_forms  # noqa: E402


def test_the_file_type_comes_from_the_bytes_not_the_name():
    assert page_forms.check_photo_file(JPEG) == ("image/jpeg", "")
    assert page_forms.check_photo_file(PNG) == ("image/png", "")
    assert page_forms.check_photo_file(b"GIF89a....")[1] == "The background image must be a JPEG or PNG file."
    assert page_forms.check_photo_file(b"%PDF-1.4")[1].startswith("The background image must be")
    assert page_forms.check_photo_file(b"")[1] == "The image file is empty."
    big = JPEG + b"\x00" * wp.FORM_PHOTO_MAX_BYTES
    assert page_forms.check_photo_file(big)[1] == "The background image is larger than 8 MB."


class UploadMeta:
    """Transport of the photo-only session: Graph reads (Pages) + the upload; records both."""

    def __init__(self, reply=None):
        self.reply = reply or {"ok": True, "photo_id": NEW_PHOTO, "width": 1024, "height": 1536}
        self.uploads = []
        self.posts = []
        self.form_photo_page = ""

    def pin_photo_page(self, page):
        self.form_photo_page = page

    def state(self):
        return {"kind": "ads_manager", "has_token": True}

    def request(self, method, path, params=None):
        if method != "GET":
            self.posts.append(path)
        return {"status": 200, "body": {"data": []}}

    def upload_form_photo(self, page_id, data, *, mime):
        self.uploads.append((page_id, hashlib.sha256(data).hexdigest(), mime))
        return self.reply


def _patch_upload(monkeypatch, meta, pages=(PAGE,)):
    from src.analysis_agent import account_spend as _as
    from src.analysis_agent import account_spend_api as _api
    from src.web import account_pages

    client = g.GraphClient(meta, sleep=lambda s: None, min_interval_s=0.0, max_calls=20)
    opened = []

    @contextmanager
    def fake_session(account, photo=False, commands=False):
        opened.append({"account": account, "photo": photo, "commands": commands})
        yield SimpleNamespace(client=client, act="act_1", account=account, notes={})

    records = []
    monkeypatch.setattr(_api, "_session", fake_session)
    monkeypatch.setattr(_as, "serialized_browser", lambda **kw: (lambda fn: fn))
    monkeypatch.setattr(_as, "_record", lambda step, ok, dur, diag: records.append((step, ok, diag)))
    monkeypatch.setattr(account_pages, "pages_from_client", lambda c, act, **k: [{"id": p, "name": p} for p in pages])
    return client, opened, records


def test_the_session_uploads_once_for_a_page_of_the_account_and_reports_id_and_sha(monkeypatch):
    meta = UploadMeta()
    client, opened, records = _patch_upload(monkeypatch, meta)
    out = page_forms.upload_photo("Acc", PAGE, JPEG)
    assert out["ok"] is True and out["photo_id"] == NEW_PHOTO and out["page_id"] == PAGE
    assert out["sha256"] == SHA and out["width"] == 1024 and out["height"] == 1536 and out["account"] == "Acc"
    assert meta.uploads == [(PAGE, SHA, "image/jpeg")] and meta.form_photo_page == PAGE
    assert opened == [{"account": "Acc", "photo": True, "commands": False}]
    assert not client.allow_writes and not meta.posts
    assert client.max_calls == page_forms.READ_MAX_CALLS + wp.FORM_PHOTO_UPLOAD_CALLS    # +1 in the call estimate


def test_the_audit_event_has_no_photo_or_page_id_and_no_token(monkeypatch):
    meta = UploadMeta()
    _, _, records = _patch_upload(monkeypatch, meta)
    page_forms.upload_photo("Acc", PAGE, JPEG)
    ((step, ok, diag),) = records
    assert step == "api_form_photo_upload" and ok is True
    assert diag == {"account": "Acc", "source": "web_session", "state": "uploaded", "bytes": len(JPEG),
                    "width": 1024, "height": 1536}
    blob = json.dumps(records)
    assert NEW_PHOTO not in blob and PAGE not in blob and "EAA" not in blob and SHA not in blob


def test_a_page_outside_the_account_makes_no_upload(monkeypatch):
    meta = UploadMeta()
    _patch_upload(monkeypatch, meta)
    out = page_forms.upload_photo("Acc", "999", JPEG)
    assert out["ok"] is False and "not available for this account" in out["message"] and meta.uploads == []


def test_the_upload_runs_as_a_write_not_a_read(monkeypatch):
    from src.analysis_agent import account_spend as _as

    meta = UploadMeta()
    _patch_upload(monkeypatch, meta)
    seen = []
    monkeypatch.setattr(_as, "serialized_browser", lambda **kw: (seen.append(kw), (lambda fn: fn))[1])
    page_forms.upload_photo("Acc", PAGE, JPEG)
    assert len(seen) == 1 and not seen[0].get("reading") and seen[0]["key"] is _as._profile_key


def test_a_not_sent_refusal_is_shown_as_a_refusal_and_starts_no_cooldown(monkeypatch):
    from src.analysis_agent import account_spend_api as _api

    meta = UploadMeta({"error": "no_session"})
    client, _, records = _patch_upload(monkeypatch, meta)
    started = []
    monkeypatch.setattr(_api, "_start_cooldown", lambda key, stop: started.append(stop))
    out = page_forms.upload_photo("Acc", PAGE, JPEG)
    assert out["ok"] is False and out["state"] == "refused" and "Reload Ads Manager" in out["message"]
    assert client.stopped is None and started == []
    assert records[0][2]["state"] == "refused"


def test_a_bad_file_is_refused_before_a_profile_is_opened(monkeypatch):
    meta = UploadMeta()
    _, opened, records = _patch_upload(monkeypatch, meta)
    for data in (b"", b"GIF89a", JPEG + b"\x00" * wp.FORM_PHOTO_MAX_BYTES):
        out = page_forms.upload_photo("Acc", PAGE, data)
        assert out["ok"] is False and out["state"] == "refused" and out["photo_id"] == ""
    assert opened == [] and meta.uploads == [] and records == []


def test_meta_refusal_comes_back_as_text_and_is_recorded_as_a_failure(monkeypatch):
    meta = UploadMeta({"error": "upload_refused", "text": "Photo Upload Failed: Image is too small", "status": 200})
    _, _, records = _patch_upload(monkeypatch, meta)
    out = page_forms.upload_photo("Acc", PAGE, JPEG)
    assert out["ok"] is False and out["state"] == "refused" and out["photo_id"] == ""
    assert "Meta refused the background image: Photo Upload Failed: Image is too small" in out["message"]
    assert records[0][1] is False and records[0][2]["state"] == "refused"


def test_an_unknown_result_says_so_and_does_not_retry(monkeypatch):
    meta = UploadMeta({"error": "network"})
    _patch_upload(monkeypatch, meta)
    out = page_forms.upload_photo("Acc", PAGE, JPEG)
    assert out["state"] == "unknown" and "unknown" in out["message"] and len(meta.uploads) == 1


class _Upload:
    def __init__(self, data):
        self.data = data

    async def read(self):
        return self.data


def test_the_endpoint_takes_one_file_and_needs_account_page_and_the_api_backend(monkeypatch):
    from src.web import app as web
    from src.web import request_limits
    from tests.test_acl1775_instant_forms import _Req

    calls = []
    monkeypatch.setattr(page_forms, "upload_photo", lambda a, p, d: calls.append((a, p, d)) or {"ok": True})
    monkeypatch.setenv("ACL_FB_BACKEND", "rpa")
    form = {"account_label": "A", "page_id": PAGE, "file": _Upload(JPEG)}
    assert asyncio.run(web.api_form_photo_upload(_Req(form))).status_code == 409
    monkeypatch.setenv("ACL_FB_BACKEND", "api")
    assert asyncio.run(web.api_form_photo_upload(_Req({**form, "account_label": ""}))).status_code == 400
    assert asyncio.run(web.api_form_photo_upload(_Req({**form, "page_id": ""}))).status_code == 400
    assert asyncio.run(web.api_form_photo_upload(_Req({"account_label": "A", "page_id": PAGE}))).status_code == 400
    assert asyncio.run(web.api_form_photo_upload(_Req({**form, "file": "text"}))).status_code == 400
    assert calls == []
    assert asyncio.run(web.api_form_photo_upload(_Req(form))).status_code == 200
    assert calls == [("A", PAGE, JPEG)]
    # The only route that accepts a file has its own limits; every other form still takes none.
    assert request_limits.ROUTE_MAX_BODY[request_limits.FORM_PHOTO_ROUTE] > wp.FORM_PHOTO_MAX_BYTES
    assert request_limits.FORM_PHOTO_FORM.max_files == 1 and request_limits.FORM_PHOTO_FORM.max_part_size > wp.FORM_PHOTO_MAX_BYTES
    assert request_limits.BUILDER_FORM.max_files == 0 and request_limits.SMALL_FORM.max_files == 0


# ───────────────────────── шаблон: id, Страница, sha256 ─────────────────────────

UPLOADED = {"cover_photo_id": NEW_PHOTO, "cover_page_id": PAGE, "cover_sha256": SHA}


def test_the_template_keeps_the_uploaded_pair_and_the_checksum_but_never_bytes():
    clean, errors = ft.validate(tpl(**UPLOADED, cover_bytes="AAAA", cover_data="data:image/png;base64,AAAA"))
    assert errors == []
    assert clean["cover_photo_id"] == NEW_PHOTO and clean["cover_sha256"] == SHA
    assert "cover_bytes" not in clean and "cover_data" not in clean
    assert "AAAA" not in json.dumps(clean)


def test_a_checksum_must_be_64_hex_and_belongs_to_a_pair():
    _, errors = ft.validate(tpl(**{**UPLOADED, "cover_sha256": "abc"}))
    assert "Background image checksum must be 64 hex characters" in errors
    clean, errors = ft.validate(tpl(cover_sha256=SHA))           # no photo: nothing is kept, nothing to say
    assert "cover_sha256" not in clean
    clean, errors = ft.validate(tpl(**{**UPLOADED, "cover_sha256": SHA.upper()}))
    assert errors == [] and clean["cover_sha256"] == SHA


def test_the_checksum_is_not_sent_to_meta_and_the_form_name_follows_the_picture():
    clean = ft.validate(tpl(**UPLOADED))[0]
    params = ft.form_params(clean)
    assert params["context_card"]["cover_photo_id"] == NEW_PHOTO and SHA not in json.dumps(params)
    wp.check_form_create(PAGE, "POST", f"/{PAGE}/leadgen_forms", params, pinned_page=PAGE)
    other = ft.validate(tpl(**{**UPLOADED, "cover_photo_id": "122118700000000001"}))[0]
    assert ft.form_name(other) != ft.form_name(clean)
    # The same picture on the same Page: same name, so a republish finds and reuses the form.
    assert ft.form_name(ft.validate(tpl(**UPLOADED))[0]) == ft.form_name(clean)


def test_remove_drops_the_checksum_with_the_pair(local):
    saved = ft.save(tpl(**UPLOADED))
    row = next(r for r in ft.listing()["rows"] if r["id"] == saved["id"])
    assert ft.get(saved["id"])["cover_sha256"] == SHA
    ft.save({**tpl(), "id": saved["id"], "_expected_revision": row["revision"]})
    again = ft.get(saved["id"])
    assert not {"cover_photo_id", "cover_page_id", "cover_sha256"} & set(again)


def test_duplicate_and_the_library_keep_the_uploaded_pair(local):
    saved = ft.save(tpl(**UPLOADED))
    copy = ft.duplicate(saved["id"])
    assert copy["cover_photo_id"] == NEW_PHOTO and copy["cover_sha256"] == SHA


# ───────────────────────── публикация ─────────────────────────


def test_publish_sends_the_uploaded_id_and_reads_it_back(tmp_path):
    tid = ft.save(tpl(**UPLOADED))["id"]
    meta = CoverMeta()
    out, _ = _run(meta, _tpl_draft(tmp_path, [tid]))
    assert out.error == "", out.error
    (path, params) = meta.form_posts[0][:2]
    assert params["context_card"]["cover_photo_id"] == NEW_PHOTO and SHA not in json.dumps(params)
    assert any("read back, ACTIVE, on the Page" in s for s in out.steps)


def test_a_republish_on_the_same_page_finds_the_form_and_uploads_nothing(tmp_path):
    saved = ft.save(tpl(**UPLOADED))
    t = ft.get(saved["id"])
    meta = CoverMeta(rows=[{"id": "7000050", "name": ft.form_name(t), "status": "ACTIVE", "locale": "en_US"}])
    meta.details["7000050"] = {**meta.rows[0], "page": {"id": PAGE}, "questions": {"data": ft.expected_questions(t)},
                               "context_card": {"cover_photo": {"id": NEW_PHOTO}}}
    out, _ = _run(meta, _tpl_draft(tmp_path, [saved["id"]]))
    assert out.error == "" and not meta.form_posts
    assert any("found on the Page, reused" in s for s in out.steps)


def test_a_refusal_at_publish_shows_the_form_name_and_metas_text(tmp_path):
    tid = ft.save(tpl(**UPLOADED))["id"]
    meta = CoverMeta()
    meta.refuse = {"code": 100, "message": "Invalid photo id"}
    out, _ = _run(meta, _tpl_draft(tmp_path, [tid]))
    assert out.stop_kind == "form_refused" and "Invalid photo id" in out.error
    assert ft.form_name(ft.get(tid)) in out.error


def test_the_picture_of_another_page_still_stops_before_any_write(tmp_path):
    tid = ft.save(tpl(**{**UPLOADED, "cover_page_id": "777"}))["id"]
    meta = CoverMeta()
    out, _ = _run(meta, _tpl_draft(tmp_path, [tid]))
    assert "belongs to another Page" in out.error and not meta.posts


def test_a_template_without_a_background_is_unchanged(tmp_path):
    plain = ft.validate(tpl())[0]
    assert not {"cover_photo_id", "cover_page_id", "cover_sha256"} & set(plain)
    assert "cover_photo_id" not in ft.form_params(plain).get("context_card", {})
    tid = ft.save(tpl())["id"]
    meta = CoverMeta()
    out, _ = _run(meta, _tpl_draft(tmp_path, [tid]))
    assert out.error == "", out.error
    assert "cover_photo_id" not in meta.form_posts[0][1]["context_card"]


# ───────────────────────── страница: предпросмотр только локальный ─────────────────────────


def _picker_source():
    page = HTML.read_text(encoding="utf-8")
    start = page.index("window.formCoverPicker = function")
    return page[start:page.index("function coverContext", start)]


def test_the_preview_is_a_blob_of_the_local_file_and_never_an_address_from_meta():
    picker = _picker_source()
    assert "https://" not in picker and "innerHTML" not in picker and "data:" not in picker
    assigned = re.findall(r"\.src\s*=\s*([^;]+);", picker)
    # ACL-2571: the chosen file (blob:) or the stored file served by THIS dashboard; never a Meta address.
    assert assigned == ["localUrl", "'/api/form-templates/cover-file?id=' + encodeURIComponent(st.localTid) + '&sha=' + encodeURIComponent(st.local)"], assigned
    assert "localUrl = URL.createObjectURL(f)" in picker
    app = (ROOT / "src" / "web" / "app.py").read_text(encoding="utf-8")
    assert "img-src 'self' data: blob:;" in app


def test_the_picker_keeps_the_file_with_one_fetch_to_this_dashboard_and_never_calls_meta():
    page = HTML.read_text(encoding="utf-8")
    assert "fetch('/api/form-photo-upload'" not in page, "ACL-2571: the Page upload happens at publish, not in the editor"
    picker = _picker_source()
    assert "fileInput.addEventListener('change'" in picker
    change = picker[picker.index("fileInput.addEventListener('change'"):picker.index("function store()")]
    assert "fetch(" not in change and "store()" not in change, "choosing a file must not send anything"
    assert picker.count("fetch('/api/form-templates/cover-file'") == 1, "one function keeps the file; every save shares it"
    assert "gateNote" not in picker and "pageReady" not in picker, "no Page-first gate"


def test_the_page_validates_the_checksum_like_the_server():
    page = HTML.read_text(encoding="utf-8")
    assert "Background image checksum must be 64 hex characters" in page
    assert "Background picture checksum must be 64 hex characters" in page
    assert "cover.set(tpl.cover_photo_id, tpl.cover_page_id, tpl.cover_sha256, tpl.cover_local_sha256, tpl.id)" in page


# ───────────────────────── тёплая вкладка: две Страницы подряд ─────────────────────────


def test_two_uploads_for_two_pages_through_the_warm_registry_each_get_a_fresh_pinned_graph(tmp_path, monkeypatch):
    """The registry must not hand the second upload the graph pinned to the first Page."""
    from src.analysis_agent import account_spend_api as api
    from tests.test_acl1812_warm_sessions import ACT, PID, FakeGraph, Rig

    class PhotoGraph(FakeGraph):
        form_photo_page = None

        def pin_photo_page(self, page):             # the same rule as InPageGraph.pin_photo_page
            if self.form_photo_page and self.form_photo_page != page:
                raise ValueError("a different Page is already pinned for this session")
            self.form_photo_page = page

    made = []

    def make(tab, commands_act=None, photo=False):
        graph = PhotoGraph(tab)
        made.append((graph, photo))
        return graph

    rig = Rig(tmp_path)
    rig.warm_up()
    monkeypatch.setattr(api, "_warm_registry", lambda: rig.reg)
    monkeypatch.setattr(api, "_make_graph", make)
    api._COOLDOWN.clear()
    graphs = []
    for page in ("111", "222"):
        with api._warm_session_held("Acc", ACT, "k", PID, False, photo=True) as sess:
            sess.client.transport.pin_photo_page(page)        # the second Page must not be refused
            graphs.append(sess.client.transport)
    assert graphs[0] is not graphs[1] and [g_.form_photo_page for g_ in graphs] == ["111", "222"]
    assert all(photo for _, photo in made[-2:])
    # a plain read still reuses its own graph (the fresh-graph rule is for the photo mode only)
    reads = []
    for _ in range(2):
        with api._warm_session_held("Acc", ACT, "k", PID, False) as sess:
            reads.append(sess.client.transport)
    assert reads[0] is reads[1]
