"""ACL-1761: кампании через Graph API в профиле — только PAUSED, без повторов записей.

Ни Meta, ни Multilogin здесь не трогаются: Graph — это `FakeMeta` в памяти
(транспорт `InPageGraph` подменён), страница помощника — поддельная.
Проверяется: PAUSED на обеих линиях, закрытый список записей, нет DELETE,
каждая запись — один раз, неизвестный результат — сверка чтением, повторный
запуск ничего не создаёт, сверка ловит расхождения, ожидание видео ограничено,
и задание дешборда идёт в API только при ACL_FB_BACKEND=api.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_api import creator as cr  # noqa: E402
from src.fb_api import graph as g  # noqa: E402
from src.fb_api import token_capture as tc  # noqa: E402
from src.fb_api import write_policy as wp  # noqa: E402
from src.fb_api.cdp import CdpTimeout  # noqa: E402

ACT = "act_1234567890"          # заглушки из acl_scan_secrets._PLACEHOLDERS
ACT2 = "act_1234567890123456"
TOKEN = "EAAB" + "Zx9" * 20


# ───────────────────────── политика записи ─────────────────────────

def _camp(**over):
    p = {"name": "C", "objective": "OUTCOME_LEADS", "status": "PAUSED", "special_ad_categories": []}
    p.update(over)
    return p


def test_policy_accepts_a_paused_campaign():
    assert wp.check(ACT, "POST", f"/{ACT}/campaigns", _camp()) == "campaigns"


@pytest.mark.parametrize("status", ["ACTIVE", "active", "", None, "ARCHIVED", "DELETED"])
def test_policy_requires_paused(status):
    with pytest.raises(wp.WriteRefused):
        wp.check(ACT, "POST", f"/{ACT}/campaigns", _camp(status=status))


@pytest.mark.parametrize("edge", ["campaigns", "adsets", "ads"])
def test_policy_requires_status_on_objects_that_can_run(edge):
    with pytest.raises(wp.WriteRefused):
        wp.check(ACT, "POST", f"/{ACT}/{edge}", {"name": "x"})


@pytest.mark.parametrize("key", ["effective_status", "configured_status", "execution_options"])
def test_policy_rejects_status_side_doors(key):
    with pytest.raises(wp.WriteRefused):
        wp.check(ACT, "POST", f"/{ACT}/campaigns", _camp(**{key: "PAUSED"}))


@pytest.mark.parametrize("edge,params", [
    ("adcreatives", {"name": "x", "object_story_spec": {"link_data": {"lead_gen_form_id": "1"}}}),
    ("adsets", {"name": "x", "status": "PAUSED", "targeting": {"effective_status": "PAUSED"}}),
    ("ads", {"name": "x", "status": "PAUSED", "creative": {"configured_status": "PAUSED"}}),
])
def test_policy_forbidden_keys_are_refused_at_any_depth_even_with_paused(edge, params):
    with pytest.raises(wp.WriteRefused):
        wp.check(ACT, "POST", f"/{ACT}/{edge}", params)


@pytest.mark.parametrize("value", [
    '{"link_data": {"status": "ACTIVE"}}',
    '{"link_data": {"effective_status": "PAUSED"}}',
    "not json", "42", 42,
])
def test_policy_json_typed_values_are_parsed_before_the_checks(value):
    with pytest.raises(wp.WriteRefused):
        wp.check(ACT, "POST", f"/{ACT}/adcreatives", {"name": "x", "object_story_spec": value})


def test_policy_accepts_a_clean_json_string():
    assert wp.check(ACT, "POST", f"/{ACT}/adcreatives",
                    {"name": "x", "object_story_spec": '{"page_id": "1"}'}) == "adcreatives"


def test_policy_rejects_active_deep_inside_a_value():
    with pytest.raises(wp.WriteRefused):
        wp.check(ACT, "POST", f"/{ACT}/adcreatives",
                 {"name": "x", "object_story_spec": {"link_data": {"status": "ACTIVE"}}})


@pytest.mark.parametrize("method", ["DELETE", "GET", "PUT", "post"])
def test_policy_is_post_only_and_never_delete(method):
    with pytest.raises(wp.WriteRefused):
        wp.check(ACT, method, f"/{ACT}/campaigns", _camp())


@pytest.mark.parametrize("path", [
    f"/{ACT2}/campaigns",                      # не проверенный кабинет
    f"/{ACT}/leadgen_forms",                   # формы не создаём
    f"/{ACT}/adlabels",
    "/123456/leadgen_tos",                     # условия не принимаем
    f"/{ACT}/campaigns/extra",
    "/me/adaccounts",
])
def test_policy_closed_list_of_paths(path):
    with pytest.raises(wp.WriteRefused):
        wp.check(ACT, "POST", path, _camp())


def test_policy_rejects_unknown_keys_and_wrong_file_fields():
    with pytest.raises(wp.WriteRefused):
        wp.check(ACT, "POST", f"/{ACT}/campaigns", _camp(spend_cap=1))
    with pytest.raises(wp.WriteRefused):
        wp.check(ACT, "POST", f"/{ACT}/adimages", {}, {"source": "f1"})
    with pytest.raises(wp.WriteRefused):
        wp.check(ACT, "POST", f"/{ACT}/adimages", {})
    with pytest.raises(wp.WriteRefused):
        wp.check(ACT, "POST", f"/{ACT}/campaigns", _camp(), {"filename": "f1"})
    assert wp.check(ACT, "POST", f"/{ACT}/adimages", {}, {"filename": "f1"}) == "adimages"


# ───────────────────────── InPageGraph: запись в странице ─────────────────────────

class StubTab:
    """Вкладка: помощник ставится молча, вызовы записываются."""

    def __init__(self, call_reply=None, call_exc=None, stale_at_stage: int | None = None,
                 short_by: int = 0):
        self.exprs: list[str] = []
        self.call_reply = call_reply if call_reply is not None else {"status": 200, "body": {"id": "1"}}
        self.call_exc = call_exc
        self.stale_at_stage = stale_at_stage
        self.short_by = short_by
        self.staged: dict[str, int] = {}
        self.stage_calls = 0

    def create_isolated_world(self, name):
        return 7

    def evaluate_in(self, ctx, expr, timeout=60.0):
        self.exprs.append(expr)
        if expr.lstrip().startswith("(() =>"):
            return None
        if ".state()" in expr:
            return {"kind": "ads_manager", "has_token": True}
        if ".stage(" in expr:
            import base64

            self.stage_calls += 1
            if self.stale_at_stage is not None and self.stage_calls == self.stale_at_stage:
                self.staged.clear()                  # новый документ — пустой помощник
                raise tc.StaleContextError("Cannot find context with specified id")
            sid, chunk = json.loads("[" + expr.split(".stage(", 1)[1].rsplit(")", 1)[0] + "]")
            n = len(base64.b64decode(chunk)) - self.short_by
            self.staged[sid] = self.staged.get(sid, 0) + n
            return {"ok": True, "bytes": self.staged[sid]}
        if ".call(" in expr:
            if self.call_exc:
                raise self.call_exc
            return self.call_reply
        if ".startCall(" in expr:
            # ACL-1899: запись с файлом - задание в помощнике, опрос прогресса.
            if self.call_exc:
                raise self.call_exc
            return {"job": "j1"}
        if ".poll(" in expr:
            return {"state": "done", "sent": 10, "total": 10, "result": self.call_reply}
        raise AssertionError(expr[:60])

    def calls(self):
        return [e for e in self.exprs if ".call(" in e or ".startCall(" in e]


def test_read_mode_page_cannot_write():
    t = tc.InPageGraph(StubTab())
    with pytest.raises(ValueError):
        t.write(f"/{ACT}/campaigns", _camp())
    with pytest.raises(ValueError):
        t.request("POST", f"/{ACT}/campaigns", _camp())
    # и в помощник политика записи не вшита
    assert "const WRITE = null;" in tc.helper_source("n")


def test_write_mode_needs_the_verified_act():
    with pytest.raises(ValueError):
        tc.InPageGraph(StubTab(), allow_writes=True)
    with pytest.raises(ValueError):
        tc.InPageGraph(StubTab(), allow_writes=True, write_act="1234567890")


def test_policy_is_checked_in_python_before_the_page_sees_anything():
    tab = StubTab()
    t = tc.InPageGraph(tab, allow_writes=True, write_act=ACT)
    with pytest.raises(wp.WriteRefused):
        t.write(f"/{ACT}/campaigns", _camp(status="ACTIVE"))
    assert tab.calls() == []


def test_the_page_helper_gets_the_same_policy():
    src = tc.helper_source("n", wp.policy_for_page(ACT))
    assert json.dumps(ACT) in src and '"paused_required"' in src


@pytest.mark.parametrize("exc", [CdpTimeout("x"), tc.PageError("y"), tc.CdpError("z")])
def test_a_failure_after_sending_is_unknown_and_sent_once(exc):
    tab = StubTab(call_exc=exc)
    t = tc.InPageGraph(tab, allow_writes=True, write_act=ACT)
    assert t.write(f"/{ACT}/campaigns", _camp()) == {"error": "call_unknown"}
    assert len(tab.calls()) == 1


def test_files_are_staged_in_chunks_without_calling_meta():
    tab = StubTab()
    t = tc.InPageGraph(tab, allow_writes=True, write_act=ACT)
    t.STAGE_CHUNK = 4
    _ctx, sid = t.stage_file(b"0123456789")
    assert sid.startswith("f") and len([e for e in tab.exprs if ".stage(" in e]) == 3
    assert tab.staged[sid] == 10 and tab.calls() == []


def test_a_stale_world_mid_staging_restarts_from_zero_never_resends_one_chunk():
    tab = StubTab(stale_at_stage=2)
    t = tc.InPageGraph(tab, allow_writes=True, write_act=ACT)
    t.STAGE_CHUNK = 4
    _ctx, sid = t.stage_file(b"0123456789")
    # 1 кусок, сбой на 2-м, затем все 3 заново под НОВЫМ id
    assert tab.stage_calls == 5 and tab.staged == {sid: 10}


def test_a_short_staging_is_refused_and_nothing_is_sent():
    tab = StubTab(short_by=1)
    t = tc.InPageGraph(tab, allow_writes=True, write_act=ACT)
    got = t.write(f"/{ACT}/adimages", {}, files={"filename": b"0123456789"}, filename="a.png")
    assert got["error"] == "no_file" and tab.calls() == []


def test_the_write_carries_the_expected_size_and_sha256():
    tab = StubTab(call_reply={"status": 200, "body": {"images": {"a.png": {"hash": "h"}}}})
    t = tc.InPageGraph(tab, allow_writes=True, write_act=ACT)
    t.write(f"/{ACT}/adimages", {}, files={"filename": b"0123456789"}, filename="a.png")
    spec = json.loads(tab.calls()[0].split("(", 1)[1].rsplit(")", 1)[0])
    assert spec["expect"] == {"bytes": 10, "sha256": hashlib.sha256(b"0123456789").hexdigest()}
    assert list(spec["files"]) == ["filename"] and spec["files"]["filename"].startswith("f")

# ───────────────────────── GraphClient.post ─────────────────────────

class ScriptedTransport:
    def __init__(self, replies):
        self.replies = list(replies)
        self.writes: list[tuple] = []
        self.gets: list[tuple] = []
        self.reacquired = 0

    def write(self, path, params, files=None, filename="", **_k):
        self.writes.append((path, params))
        return self.replies.pop(0)

    def request(self, method, path, params=None):
        assert method == "GET"
        self.gets.append((path, params))
        return {"status": 200, "body": {"data": []}}

    def state(self):
        return {"kind": "ads_manager", "has_token": True}

    def reacquire(self, timeout=60.0):
        self.reacquired += 1
        return {"kind": "ads_manager", "has_token": True}


def _client(replies, **kw):
    return g.GraphClient(ScriptedTransport(replies), allow_writes=True, sleep=lambda s: None,
                         min_interval_s=0.0, **kw)


def test_read_only_client_refuses_post():
    c = g.GraphClient(ScriptedTransport([]), sleep=lambda s: None)
    with pytest.raises(ValueError):
        c.post(f"/{ACT}/campaigns", _camp())


@pytest.mark.parametrize("reply", [
    {"error": "network"}, {"error": "call_unknown"}, {"error": "bad_reply"},
    {"status": 500, "body": {"error": {"code": 2, "message": "Service temporarily unavailable"}}},
    {"status": 200, "body": {"error": {"code": 1, "message": "An unknown error occurred"}}},
    {"status": 502, "body": {"non_json": "<html>"}},
])
def test_unknown_results_are_never_retried_and_block_further_writes(reply):
    c = _client([reply, {"status": 200, "body": {"id": "9"}}])
    with pytest.raises(g.WriteUnknown):
        c.post(f"/{ACT}/campaigns", _camp())
    assert len(c.transport.writes) == 1
    with pytest.raises(g.WritesBlocked) as blocked:
        c.post(f"/{ACT}/adsets", _camp())             # не сверено — записей нет
    assert not isinstance(blocked.value, g.WriteUnknown)
    assert len(c.transport.writes) == 1
    c.get("/x")                                       # чтения для сверки — можно
    assert c.reconciled(f"/{ACT}/adsets") is False    # чужая запись флаг не снимает
    assert c.unreconciled == f"/{ACT}/campaigns"
    assert c.reconciled(f"/{ACT}/campaigns") is True
    assert c.post(f"/{ACT}/campaigns", _camp())["id"] == "9"


def test_own_call_cap_ends_only_this_campaign_meta_stops_stay():
    c = _client([{"status": 200, "body": {"id": "1"}}] * 3, max_calls=1)
    c.post(f"/{ACT}/campaigns", _camp())
    with pytest.raises(g.CallBudget):
        c.post(f"/{ACT}/campaigns", _camp())
    c.reset_budget(5)
    assert c.stopped is None and c.post(f"/{ACT}/campaigns", _camp())["id"] == "1"
    c2 = _client([{"status": 400, "body": {"error": {"code": 17, "message": "slow"}}}])
    with pytest.raises(g.RateLimited):
        c2.post(f"/{ACT}/campaigns", _camp())
    c2.reset_budget(5)
    assert isinstance(c2.stopped, g.RateLimited)


def test_token_error_on_a_write_stops_without_recapture_or_retry():
    c = _client([{"status": 400, "body": {"error": {"code": 190, "message": "expired"}}}])
    with pytest.raises(g.LoggedOut):
        c.post(f"/{ACT}/campaigns", _camp())
    assert len(c.transport.writes) == 1 and c.transport.reacquired == 0
    with pytest.raises(g.LoggedOut):
        c.get("/x")


@pytest.mark.parametrize("code,exc", [(368, g.PolicyBlocked), (17, g.RateLimited), (4, g.RateLimited),
                                      (80004, g.RateLimited), (613, g.RateLimited)])
def test_policy_and_rate_codes_stop_the_client(code, exc):
    c = _client([{"status": 400, "body": {"error": {"code": code, "message": "no"}}}])
    with pytest.raises(exc):
        c.post(f"/{ACT}/campaigns", _camp())
    assert c.stopped is not None and len(c.transport.writes) == 1


def test_checkpoint_subcode_stops():
    c = _client([{"status": 400, "body": {"error": {"code": 190, "error_subcode": 459, "message": "cp"}}}])
    with pytest.raises(g.Checkpoint):
        c.post(f"/{ACT}/campaigns", _camp())


def test_meta_validation_refusal_is_a_known_failure():
    c = _client([{"status": 400, "body": {"error": {"code": 100, "message": "Invalid parameter"}}}])
    with pytest.raises(g.GraphError):
        c.post(f"/{ACT}/campaigns", _camp())
    assert c.unreconciled is None and c.stopped is None


def test_page_refusal_before_sending_stops():
    c = _client([{"error": "write_refused", "why": "status"}])
    with pytest.raises(g.PageFailed):
        c.post(f"/{ACT}/campaigns", _camp())


def test_call_cap_counts_writes():
    c = _client([{"status": 200, "body": {"id": str(i)}} for i in range(5)], max_calls=2)
    c.post(f"/{ACT}/campaigns", _camp())
    c.post(f"/{ACT}/campaigns", _camp())
    with pytest.raises(g.CallBudget):
        c.post(f"/{ACT}/campaigns", _camp())


# ───────────────────────── поддельная Meta ─────────────────────────

class FakeMeta:
    """Graph API в памяти: тот же интерфейс, что `InPageGraph` (request/write/stage_file)."""

    def __init__(self, *, video_states=("processing", "ready"), hash_fn=None, drop_labels=()):
        self.objects: dict[str, dict] = {}
        self.images: dict[str, str] = {}
        self.posts: list[tuple[str, dict]] = []
        #: (путь, передан ли on_ad) по каждой записи - ACL-1775.
        self.on_ad_posts: list[tuple[str, bool]] = []
        self.gets: list[tuple[str, dict]] = []
        self.video_states = list(video_states)
        self.next_id = 100
        #: путь записи → режим: "lost" (создано, ответ потерян), "unsent" (не создано, ответ потерян)
        self.fail_next: dict[str, str] = {}
        self.mutate_on_read: dict = {}
        #: Правки строк `/{adset}/ads` при чтении: "a.b.c" → значение.
        self.mutate_ads: dict = {}
        #: Хэш картинки у «Meta» (по умолчанию md5; бывает и не md5).
        self.hash_fn = hash_fn or (lambda d: hashlib.md5(d).hexdigest())
        #: Виды объектов, на которых «Meta» не сохраняет adlabels.
        self.drop_labels = set(drop_labels)

    # -- служебное
    def _new(self, kind, **fields):
        self.next_id += 1
        oid = str(self.next_id)
        self.objects[oid] = {"id": oid, "_kind": kind, **fields}
        return oid

    def _out(self, obj, fields=None):
        row = {k: v for k, v in obj.items() if not k.startswith("_")}
        if "adlabels" in row:
            row["adlabels"] = {"data": [{"id": "9", "name": x["name"]} for x in row["adlabels"]]}
        return row

    def _page(self, rows, params):
        """Постранично, как Graph: `limit`, курсор `after` — номер строки."""
        limit = int(params.get("limit") or 25)
        start = int(params.get("after") or 0)
        chunk = rows[start:start + limit]
        body = {"data": chunk}
        if start + limit < len(rows):
            body["paging"] = {"cursors": {"after": str(start + limit)}, "next": "more"}
        return body

    def _of(self, kind, parent_key=None, parent=None):
        return [o for o in self.objects.values() if o["_kind"] == kind
                and (parent_key is None or o.get(parent_key) == parent)]

    # -- транспорт
    def state(self):
        return {"kind": "ads_manager", "has_token": True}

    def reacquire(self, timeout=60.0):
        return self.state()

    def _store(self, kind, params):
        p = dict(params)
        if kind in self.drop_labels:
            p.pop("adlabels", None)
        return self._new(kind, **p)

    def write(self, path, params, files=None, filename="", on_ad=False, **_k):
        wp.check(ACT, "POST", path, params, files, on_ad=on_ad)       # та же политика, что у страницы
        self.posts.append((path, dict(params)))
        self.on_ad_posts.append((path, bool(on_ad)))
        edge = path.rsplit("/", 1)[1]
        mode = self.fail_next.pop(edge, None)
        if mode == "unsent":
            return {"error": "call_unknown"}
        if edge == "adimages":
            data = files["filename"]
            assert isinstance(data, bytes)
            h = self.hash_fn(data)
            self.images[h] = filename
            body = {"images": {filename: {"hash": h}}}
        elif edge == "advideos":
            oid = self._new("video", title=params.get("title"), _polls=0)
            body = {"id": oid}
        elif edge == "campaigns":
            p = dict(params)
            p["daily_budget"] = str(p["daily_budget"])
            body = {"id": self._store("campaign", p)}
        elif edge == "adsets":
            body = {"id": self._store("adset", params)}
        elif edge == "adcreatives":
            body = {"id": self._store("creative", params)}
        elif edge == "ads":
            body = {"id": self._store("ad", params)}
        else:
            raise AssertionError(path)
        if mode == "lost":
            return {"error": "call_unknown"}
        return {"status": 200, "body": body}

    def request(self, method, path, params=None):
        assert method == "GET", "no DELETE, no POST through request()"
        params = params or {}
        self.gets.append((path, params))
        body = self._get(path, params)
        return {"status": 400 if "error" in body else 200, "body": body}

    def _get(self, path, params):
        parts = path.strip("/").split("/")
        if path == "/me/adaccounts":
            return {"data": [{"account_id": ACT[4:], "account_status": 1}]}
        if path == "/search":
            t = params["type"]
            if t == "adgeolocation":
                return {"data": [{"key": "MX", "country_code": "MX", "name": "Mexico"}]}
            if t == "adlocale":
                return {"data": [{"key": 23, "name": "Spanish (All)"}, {"key": 7, "name": "Spanish"}]}
            if t == "adinterestvalid":
                return {"data": [{"name": n, "valid": True, "id": f"6{i}"}
                                 for i, n in enumerate(params["interest_list"])]}
            return {"data": []}
        if parts[0] == ACT and len(parts) == 1:
            return {"account_id": ACT[4:], "currency": "USD", "account_status": 1}
        if parts[0] == ACT:
            edge = parts[1]
            if edge == "promote_pages":
                return {"data": [{"id": "111"}]}
            if edge == "adspixels":
                return {"data": [{"id": "222"}]}
            if edge == "adimages":
                return {"data": [{"hash": h, "name": self.images[h]}
                                 for h in params.get("hashes", []) if h in self.images]}
            if edge == "advideos":
                return {"data": [self._out(o) for o in self._of("video")]}
            if edge == "adcreatives":
                # Как живой Meta (5414, job 2fb409d1): фильтр по имени здесь — ошибка #100.
                if "filtering" in params:
                    return {"error": {"code": 100, "type": "OAuthException", "message":
                            "(#100) Filtering field 'name' with operation 'contain' is not supported"}}
                rows = [self._out(o) for o in reversed(self._of("creative"))]      # новые первыми
                return self._page(rows, params)
            want = params["filtering"][0]["value"]
            live = params.get("effective_status")      # как Meta: удалённые/архивные в выдачу не попадают
            equal = params["filtering"][0].get("operator") == "EQUAL"
            return {"data": [self._out(o) for o in self._of("campaign")
                             if (o.get("name", "") == want if equal else want in o.get("name", ""))
                             and (not live or o.get("status") in live)]}
        oid = parts[0]
        if len(parts) == 2:
            if parts[1] == "adsets":
                return {"data": [self._out(o) for o in self._of("adset", "campaign_id", oid)]}
            if parts[1] == "ads":
                rows = []
                parent = self.objects.get(oid, {})
                if parent.get("_kind") == "campaign":      # /{campaign}/ads - объявления всех его групп
                    sets = {a["id"] for a in self._of("adset", "campaign_id", oid)}
                    return {"data": [self._out(o) for o in self._of("ad") if o.get("adset_id") in sets]}
                for o in self._of("ad", "adset_id", oid):
                    row = self._out(o)
                    cre = self._out(self.objects[o["creative"]["creative_id"]])
                    row["creative"] = {k: cre[k] for k in ("id", "object_story_spec", "adlabels",
                                                          "degrees_of_freedom_spec", "contextual_multi_ads", "asset_feed_spec") if k in cre}
                    row = json.loads(json.dumps(row))
                    for key, value in self.mutate_ads.items():
                        target = row
                        *head, last = key.split(".")
                        for k in head:
                            target = target[k]
                        target[last] = value
                    rows.append(row)
                return {"data": rows}
            if parts[1] == "thumbnails":
                return {"data": [{"uri": "https://example.invalid/t.jpg", "is_preferred": True}]}
        obj = self.objects[oid]
        if obj["_kind"] == "video" and params.get("fields") == "title":
            return {"id": oid, "title": obj.get("title")}
        if obj["_kind"] == "video":
            obj["_polls"] += 1
            state = self.video_states[min(obj["_polls"] - 1, len(self.video_states) - 1)]
            return {"id": oid, "status": {"video_status": state}}
        out = json.loads(json.dumps(self._out(obj)))
        for (kind, key), value in self.mutate_on_read.items():
            if obj["_kind"] == kind:
                target = out
                *head, last = key.split(".")
                for k in head:
                    target = target[k]
                target[last] = value
        return out


def _draft(tmp_path, *, video=False, ads=2, body2=""):
    from src.fb_ads_rpa.draft import AdDraft, AdSetDraft, CampaignDraft

    items = []
    for i in range(ads):
        is_video = video and i == ads - 1
        f = tmp_path / (f"{1000 + i}_clip.mp4" if is_video else f"{1000 + i}_img.png")
        f.write_bytes(b"creative-%d" % i)
        items.append(AdDraft(creative_type="Video" if is_video else "Image", creative_id=str(1000 + i),
                             creative_filename=f.name, creative_path=str(f), copy_name=f"Text{i}",
                             ad_title=f"Head {i}", ad_body=f"Body {i}", ad_link_description="Desc",
                             ad_body2=body2))
    s = AdSetDraft(name="MX Leads 1", targeting_label="Day trading, Swing trading", os="All",
                   age_min=25, age_max=55, daily_budget=20.0, ads=items)
    return CampaignDraft(name="MX Leads 1", country="MX", account_label="Test", link="https://example.invalid/l",
                         language="Spanish", page_id="111", ad_account_id=ACT, ad_sets=[s])


def _run(meta, draft, **kw):
    client = g.GraphClient(meta, allow_writes=True, sleep=lambda s: None, min_interval_s=0.0, max_calls=200)
    facts = cr.preflight_account(client, ACT)
    creator = cr.CampaignCreator(client, facts, cr.TargetingResolver(client), sleep=lambda s: None,
                                 **kw)
    return creator.run(cr.plan_from_draft(draft)), client


def test_full_create_is_paused_everywhere_and_the_readback_matches(tmp_path):
    meta = FakeMeta()
    out, client = _run(meta, _draft(tmp_path, video=True, ads=3))
    assert out.error == "", out.error
    assert out.readback["ok"] is True and out.readback["checked"] > 20
    edges = [p.rsplit("/", 1)[1] for p, _ in meta.posts]
    assert edges == ["campaigns", "adsets", "adimages", "adcreatives", "ads", "adimages", "adcreatives",
                     "ads", "advideos", "adcreatives", "ads"]
    for path, params in meta.posts:
        if path.rsplit("/", 1)[1] in ("campaigns", "adsets", "ads"):
            assert params["status"] == "PAUSED"
        assert "ACTIVE" not in json.dumps(params)
    camp = next(o for o in meta.objects.values() if o["_kind"] == "campaign")
    assert camp["daily_budget"] == "2000" and camp["objective"] == "OUTCOME_LEADS"
    adset = next(o for o in meta.objects.values() if o["_kind"] == "adset")
    t = adset["targeting"]
    assert t["geo_locations"] == {"countries": ["MX"]} and t["locales"] == [23]
    assert t["age_min"] == 25 and t["age_max"] == 55
    assert adset["promoted_object"] == {"pixel_id": "222", "custom_event_type": "LEAD"}
    assert len(out.ad_ids) == 3 and len(set(out.ad_ids)) == 3


def test_a_rerun_creates_nothing(tmp_path):
    meta = FakeMeta()
    first, _ = _run(meta, _draft(tmp_path, video=True, ads=3))
    n_posts, n_objects = len(meta.posts), len(meta.objects)
    second, client = _run(meta, _draft(tmp_path, video=True, ads=3))
    assert second.error == "" and second.created == {}
    assert len(meta.posts) == n_posts and len(meta.objects) == n_objects
    assert client.writes == 0
    assert second.campaign_id == first.campaign_id and sorted(second.ad_ids) == sorted(first.ad_ids)
    assert second.readback["ok"] is True


def test_a_rerun_after_a_partial_run_only_adds_what_is_missing(tmp_path):
    meta = FakeMeta()
    meta.fail_next["ads"] = "unsent"          # первое объявление не создано, ответа нет
    first, _ = _run(meta, _draft(tmp_path, ads=2))
    assert first.stop_kind == "unknown" and first.campaign_id
    camps = len([o for o in meta.objects.values() if o["_kind"] == "campaign"])
    second, _ = _run(meta, _draft(tmp_path, ads=2))
    assert second.error == "" and second.readback["ok"] is True
    assert len([o for o in meta.objects.values() if o["_kind"] == "campaign"]) == camps
    assert len([o for o in meta.objects.values() if o["_kind"] == "ad"]) == 2
    # картинка и креатив первого объявления не загружены второй раз
    assert len([p for p, _ in meta.posts if p.endswith("/adimages")]) == 2
    assert len([o for o in meta.objects.values() if o["_kind"] == "creative"]) == 2


def test_unknown_result_is_reconciled_by_readback_and_not_retried(tmp_path):
    meta = FakeMeta()
    meta.fail_next["campaigns"] = "lost"     # создано, но ответ потерян
    out, client = _run(meta, _draft(tmp_path, ads=1))
    assert out.error == ""
    assert len([p for p, _ in meta.posts if p.endswith("/campaigns")]) == 1
    assert len([o for o in meta.objects.values() if o["_kind"] == "campaign"]) == 1
    assert any("reconciled" in s for s in out.steps)


def test_unknown_result_not_found_stops_before_any_further_write(tmp_path):
    meta = FakeMeta()
    meta.fail_next["adsets"] = "unsent"
    out, client = _run(meta, _draft(tmp_path, ads=2))
    assert out.stop_kind == "unknown" and "re-run" in out.error
    edges = [p.rsplit("/", 1)[1] for p, _ in meta.posts]
    assert edges == ["campaigns", "adsets"]


def test_readback_mismatch_is_flagged(tmp_path):
    meta = FakeMeta()
    meta.mutate_on_read[("adset", "targeting.age_max")] = 65
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert out.readback["ok"] is False
    assert [(m["field"], m["expected"], m["actual"]) for m in out.mismatches] == [("age_max", 55, 65)]


def test_readback_flags_a_campaign_somebody_activated(tmp_path):
    meta = FakeMeta()
    meta.mutate_on_read[("campaign", "status")] = "ACTIVE"
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert any(m["object"] == "campaign" and m["field"] == "status" for m in out.mismatches)


def test_video_processing_wait_is_bounded(tmp_path):
    meta = FakeMeta(video_states=("processing",))
    out, client = _run(meta, _draft(tmp_path, video=True, ads=1), video_max_polls=4,
                       video_poll_s=5.0)
    assert out.stop_kind == "video_timeout"
    vid_polls = [p for p, _ in meta.gets if p.count("/") == 1 and p[1:] in meta.objects
                 and meta.objects[p[1:]]["_kind"] == "video"]
    assert len(vid_polls) == 4
    assert not [p for p, _ in meta.posts if p.endswith(("/adcreatives", "/ads"))]


def test_video_wait_respects_the_time_limit(tmp_path):
    meta = FakeMeta(video_states=("processing",))
    now = [0.0]

    def sleep(s):
        now[0] += s

    client = g.GraphClient(meta, allow_writes=True, sleep=lambda s: None, min_interval_s=0.0, max_calls=200)
    facts = cr.preflight_account(client, ACT)
    creator = cr.CampaignCreator(client, facts, cr.TargetingResolver(client), sleep=sleep,
                                 clock=lambda: now[0], video_wait_s=60.0, video_poll_s=15.0,
                                 video_max_polls=100)
    out = creator.run(cr.plan_from_draft(_draft(tmp_path, video=True, ads=1)))
    assert out.stop_kind == "video_timeout" and now[0] <= 60.0


def test_existing_campaign_with_the_same_name_but_not_ours_is_not_touched(tmp_path):
    meta = FakeMeta()
    foreign = meta._new("campaign", name="MX Leads 1", status="ACTIVE")
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    # ACL-1902: чужую не трогаем и не блокируемся - публикуем под свободным именем.
    assert out.error == "" and out.published_as == "MX Leads 1_2"
    assert meta.objects[foreign]["status"] == "ACTIVE"
    assert not any(o.get("campaign_id") == foreign for o in meta.objects.values())


def test_no_delete_and_only_allowlisted_writes(tmp_path):
    meta = FakeMeta()
    _run(meta, _draft(tmp_path, video=True, ads=2))
    for path, _ in meta.posts:
        assert path.split("/")[1] == ACT
        assert path.rsplit("/", 1)[1] in wp.EDGE_KEYS


def test_plan_errors_are_raised_before_any_call(tmp_path):
    # ACL-1774 (после живого прогона 2026-10-14): несколько текстов - отказ плана (Dynamic Creative не поддержан).
    with pytest.raises(cr.PlanError, match="Dynamic Creative"):
        cr.plan_from_draft(_draft(tmp_path, body2="second text"))
    six = _draft(tmp_path)
    six.ad_sets[0].ads[0].text_variants = [{"body": f"v{i}", "headline": "", "description": ""} for i in range(5)]
    with pytest.raises(cr.PlanError):
        cr.plan_from_draft(six)
    d = _draft(tmp_path)
    d.link = ""
    with pytest.raises(cr.PlanError):
        cr.plan_from_draft(d)


def test_unresolved_targeting_stops_before_writes(tmp_path):
    meta = FakeMeta()
    orig = meta._get

    def _get(path, params):
        if path == "/search" and params["type"] == "adinterestvalid":
            return {"data": [{"name": n, "valid": False} for n in params["interest_list"]]}
        return orig(path, params)

    meta._get = _get
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert out.stop_kind == "plan" and "Detailed targeting" in out.error and meta.posts == []


def test_preflight_refuses_an_account_the_profile_does_not_have():
    meta = FakeMeta()
    client = g.GraphClient(meta, allow_writes=True, sleep=lambda s: None, min_interval_s=0.0)
    with pytest.raises(cr.PlanError):
        cr.preflight_account(client, ACT2)


def test_tag_is_deterministic_per_account_and_name():
    assert cr.campaign_tag(ACT, "A") == cr.campaign_tag(ACT, "A ")
    assert cr.campaign_tag(ACT, "A") != cr.campaign_tag(ACT, "B")
    assert cr.campaign_tag(ACT, "A") != cr.campaign_tag(ACT2, "A")


def test_budget_minor_units():
    assert cr.budget_minor(20.0, "USD") == 2000
    assert cr.budget_minor(5000, "CLP") == 5000


# ───────────────────────── ревью #765: метки, сверка, пробный запуск ─────────────────────────

def test_label_dropped_on_the_ad_set_stops_before_any_ad_write(tmp_path):
    meta = FakeMeta(drop_labels={"adset"})
    out, _ = _run(meta, _draft(tmp_path, ads=2))
    assert out.stop_kind == "label_missing" and "ad set" in out.error.lower()
    assert [p.rsplit("/", 1)[1] for p, _ in meta.posts] == ["campaigns", "adsets"]


def test_label_dropped_on_the_campaign_stops_right_after_it(tmp_path):
    meta = FakeMeta(drop_labels={"campaign"})
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert out.stop_kind == "label_missing"
    assert [p.rsplit("/", 1)[1] for p, _ in meta.posts] == ["campaigns"]


def test_unknown_campaign_write_with_a_dropped_label_says_the_true_reason(tmp_path):
    meta = FakeMeta(drop_labels={"campaign"})
    meta.fail_next["campaigns"] = "lost"
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert out.stop_kind == "label_missing" and "not created by ACL" not in out.error
    assert len(meta.posts) == 1


@pytest.mark.parametrize("kind", ["creative", "ad"])
def test_label_dropped_on_the_first_creative_or_ad_stops(tmp_path, kind):
    meta = FakeMeta(drop_labels={kind})
    out, _ = _run(meta, _draft(tmp_path, ads=2))
    assert out.stop_kind == "label_missing"
    edges = [p.rsplit("/", 1)[1] for p, _ in meta.posts]
    assert edges.count("ads") <= 1 and edges.count("adcreatives") == 1


def test_children_of_our_campaign_are_matched_by_name_without_their_label(tmp_path):
    meta = FakeMeta()
    _run(meta, _draft(tmp_path, ads=2))
    for o in meta.objects.values():
        if o["_kind"] in ("adset", "ad"):
            o.pop("adlabels", None)            # Meta «потеряла» метки детей
    n_posts = len(meta.posts)
    out, client = _run(meta, _draft(tmp_path, ads=2))
    assert client.writes == 0 and len(meta.posts) == n_posts
    # а сверка всё равно называет потерю меток
    assert {m["object"].split(" (")[0] for m in out.mismatches if m["field"] == "tag"} >= {"ad 1", "ad 2"}


@pytest.mark.parametrize("key,value,field", [
    ("creative.object_story_spec.link_data.image_hash", "other", "image_hash"),
    ("creative.object_story_spec.link_data.call_to_action.type", "SIGN_UP", "call_to_action"),
    ("creative.id", "999", "creative_id"),
    ("adset_id", "998", "adset_id"),
    ("adlabels", {"data": []}, "tag"),
])
def test_readback_checks_media_cta_creative_adset_and_tags(tmp_path, key, value, field):
    meta = FakeMeta()
    meta.mutate_ads[key] = value
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert field in {m["field"] for m in out.mismatches}, out.mismatches


def test_readback_checks_the_video_id(tmp_path):
    meta = FakeMeta()
    meta.mutate_ads["creative.object_story_spec.video_data.video_id"] = "997"
    meta.objects["997"] = {"id": "997", "_kind": "video", "title": "someone else's", "_polls": 0}
    out, _ = _run(meta, _draft(tmp_path, video=True, ads=1))
    fields = {m["field"] for m in out.mismatches}
    assert {"video_id", "video_file"} <= fields


def test_image_hash_is_compared_with_what_meta_returned_not_with_md5(tmp_path):
    meta = FakeMeta(hash_fn=lambda d: hashlib.sha1(d).hexdigest())
    out, _ = _run(meta, _draft(tmp_path, ads=2))
    assert out.error == "" and out.readback["ok"] is True, out.mismatches


def test_duplicate_ad_names_are_refused_before_any_call(tmp_path):
    d = _draft(tmp_path, ads=2)
    a, b = d.ad_sets[0].ads
    b.creative_id, b.copy_name = a.creative_id, a.copy_name
    with pytest.raises(cr.PlanError, match="different"):
        cr.plan_from_draft(d)


def test_duplicate_ad_set_names_are_refused(tmp_path):
    import copy

    d = _draft(tmp_path, ads=1)
    d.ad_sets.append(copy.deepcopy(d.ad_sets[0]))
    with pytest.raises(cr.PlanError, match="ad sets are named"):
        cr.plan_from_draft(d)


@pytest.mark.parametrize("objective", ["Traffic", "Sales", "Awareness"])
def test_only_leads_objective_is_accepted(tmp_path, objective):
    d = _draft(tmp_path, ads=1)
    d.objective = objective
    with pytest.raises(cr.PlanError, match="Objective"):
        cr.plan_from_draft(d)


def test_draft_warnings_reach_the_result(tmp_path):
    from src.web import publish_api

    d = _draft(tmp_path, ads=1)
    d.warnings.append("Mixed budget in 'MX Leads 1' — using first slot")
    out, _ = _run(FakeMeta(), d)
    assert "Mixed budget in 'MX Leads 1' — using first slot" in publish_api.result_dict(out)["warnings"]


def test_writes_blocked_is_not_reconciled_and_does_not_clear_the_earlier_flag(tmp_path):
    meta = FakeMeta()
    client = g.GraphClient(meta, allow_writes=True, sleep=lambda s: None, min_interval_s=0.0, max_calls=200)
    facts = cr.preflight_account(client, ACT)
    client.unreconciled = f"/{ACT}/adsets"           # прошлая кампания оставила неизвестное
    looked = []
    creator = cr.CampaignCreator(client, facts, cr.TargetingResolver(client), sleep=lambda s: None)
    orig = creator._find_campaign

    def spy(plan, **kw):
        looked.append(kw)
        return orig(plan, **kw)

    creator._find_campaign = spy
    out = creator.run(cr.plan_from_draft(_draft(tmp_path, ads=1)))
    assert out.stop_kind == "blocked" and meta.posts == []
    assert client.unreconciled == f"/{ACT}/adsets"
    assert all(not kw.get("after_unknown") for kw in looked)


def _publisher(meta, *, dry_run=False):
    from src.web import publish_api

    pub = publish_api.ApiPublisher(dry_run=dry_run)
    pub.act = ACT
    pub.account_label = "Test"
    pub._client = g.GraphClient(meta, allow_writes=not dry_run, sleep=lambda s: None, min_interval_s=0.0,
                                max_calls=200)
    pub._facts = cr.preflight_account(pub._client, ACT)
    pub._resolver = cr.TargetingResolver(pub._client)
    pub.sleep = lambda _s: None
    return pub


def test_after_an_unknown_result_the_rest_of_the_job_is_refused(tmp_path):
    meta = FakeMeta()
    meta.fail_next["adsets"] = "unsent"
    pub = _publisher(meta)
    first = pub.publish_draft(_draft(tmp_path, ads=1))
    assert "unknown" in (first["error"] or "")
    n = len(meta.posts)
    d2 = _draft(tmp_path, ads=1)
    d2.name = d2.ad_sets[0].name = "MX Leads 2"
    second = pub.publish_draft(d2)
    assert second["error"].startswith("Not started: an earlier write") and len(meta.posts) == n


def test_own_call_cap_does_not_end_later_campaigns(tmp_path, monkeypatch):
    monkeypatch.setenv("ACL_FB_API_CREATE_MAX_CALLS", "20")
    meta = FakeMeta(video_states=("processing",))
    pub = _publisher(meta)
    first = pub.publish_draft(_draft(tmp_path, video=True, ads=1))
    assert "call budget" in (first["error"] or "") and pub.stop is None
    d2 = _draft(tmp_path, ads=1)
    d2.name = d2.ad_sets[0].name = "MX Leads 2"
    second = pub.publish_draft(d2)
    assert second["success"] is True, second["error"]


def test_dry_run_runs_preflight_and_returns_the_plan_with_zero_writes(tmp_path):
    meta = FakeMeta()
    pub = _publisher(meta, dry_run=True)
    res = pub.publish_draft(_draft(tmp_path, video=True, ads=2))
    assert meta.posts == [] and pub._client.writes == 0
    assert res["dry_run"] is True and res["success"] is True and res["publishable"] is False
    plan = res["plan"]
    assert plan["campaign"]["status"] == "PAUSED" and plan["existing_campaign_id"] == ""
    ads = plan["ad_sets"][0]["ads"]
    assert [a["format"] for a in ads] == ["image", "video"] and all(a["status"] == "PAUSED" for a in ads)
    assert plan["ad_sets"][0]["targeting"]["geo_locations"] == {"countries": ["MX"]}


def test_dry_run_client_cannot_write_at_all(tmp_path):
    from src.web import publish_api

    page = publish_api._make_graph(StubTab(), ACT, writes=False)
    with pytest.raises(ValueError):
        page.write(f"/{ACT}/campaigns", _camp())
    assert "const WRITE = null;" in tc.helper_source(page.name, page._write_policy)


# ───────────────────────── ревью #765, раунд 2 ─────────────────────────

FREE_TEXTS = ["[TEST] Camp", "[Offer] buy now", "{Free} gift", "[1,2] promo"]


@pytest.mark.parametrize("text", FREE_TEXTS)
def test_policy_free_text_starting_with_a_bracket_is_not_json(text):
    assert wp.check(ACT, "POST", f"/{ACT}/campaigns", _camp(name=text)) == "campaigns"
    spec = {"page_id": "1", "link_data": {"message": text, "name": text, "description": text}}
    assert wp.check(ACT, "POST", f"/{ACT}/adcreatives", {"name": text, "object_story_spec": spec})
    assert wp.check(ACT, "POST", f"/{ACT}/adcreatives",
                    {"name": text, "object_story_spec": json.dumps(spec)})
    assert wp.check(ACT, "POST", f"/{ACT}/ads", {"name": text, "status": "PAUSED",
                                                 "creative": {"creative_id": "1"}})


def test_policy_json_keys_still_must_be_json():
    with pytest.raises(wp.WriteRefused):
        wp.check(ACT, "POST", f"/{ACT}/adsets", {"name": "s", "status": "PAUSED", "targeting": "[TEST] x"})


def test_a_campaign_with_bracketed_names_and_texts_is_created(tmp_path):
    d = _draft(tmp_path, ads=2)
    d.name = d.ad_sets[0].name = "[TEST] Camp"
    a, b = d.ad_sets[0].ads
    a.ad_body, a.ad_title, a.ad_link_description = "[Offer] buy now", "{Free} gift", "[1,2] promo"
    b.copy_name = "[B] text"
    meta = FakeMeta()
    out, client = _run(meta, d)
    assert out.error == "" and out.readback["ok"] is True, (out.error, out.mismatches)
    assert client.stopped is None


def _bad_story(monkeypatch):
    orig = cr.CampaignCreator.story_spec

    def story(plan, ad, media):
        spec = orig(plan, ad, media)
        spec["link_data" if "link_data" in spec else "video_data"]["status"] = "ACTIVE"
        return spec

    monkeypatch.setattr(cr.CampaignCreator, "story_spec", staticmethod(story))


def test_dry_run_runs_the_write_policy_on_the_planned_objects(tmp_path, monkeypatch):
    _bad_story(monkeypatch)
    meta = FakeMeta()
    client = g.GraphClient(meta, sleep=lambda s: None, min_interval_s=0.0, max_calls=200)
    facts = cr.preflight_account(client, ACT)
    out = cr.CampaignCreator(client, facts, cr.TargetingResolver(client)).plan_only(
        cr.plan_from_draft(_draft(tmp_path, ads=1)))
    assert out.stop_kind == "plan" and "PAUSED" in out.error and meta.posts == []


def test_a_planned_policy_refusal_writes_nothing_and_does_not_stop_the_job(tmp_path, monkeypatch):
    _bad_story(monkeypatch)
    meta = FakeMeta()
    out, client = _run(meta, _draft(tmp_path, ads=1))
    assert out.stop_kind == "plan" and meta.posts == [] and client.stopped is None


def test_an_image_already_in_the_library_under_another_name_is_not_a_mismatch(tmp_path):
    d = _draft(tmp_path, ads=1)
    data = Path(d.ad_sets[0].ads[0].creative_path).read_bytes()
    meta = FakeMeta()
    meta.images[hashlib.md5(data).hexdigest()] = "2243_original_from_rpa.png"
    out, _ = _run(meta, d)
    assert not [p for p, _ in meta.posts if p.endswith("/adimages")]
    assert out.readback["ok"] is True, out.mismatches


def test_readback_flags_two_ads_with_the_same_name(tmp_path):
    meta = FakeMeta()
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    ad = next(o for o in meta.objects.values() if o["_kind"] == "ad")
    meta._new("ad", **{k: v for k, v in ad.items() if k not in ("id", "_kind")})
    client = g.GraphClient(meta, allow_writes=True, sleep=lambda s: None, min_interval_s=0.0, max_calls=200)
    facts = cr.preflight_account(client, ACT)
    creator = cr.CampaignCreator(client, facts, cr.TargetingResolver(client), sleep=lambda s: None)
    again = creator.run(cr.plan_from_draft(_draft(tmp_path, ads=1)))
    assert "count" in {m["field"] for m in again.mismatches}


def test_reconcile_waits_before_the_readback(tmp_path):
    meta = FakeMeta()
    meta.fail_next["campaigns"] = "lost"
    slept = []
    client = g.GraphClient(meta, allow_writes=True, sleep=lambda s: None, min_interval_s=0.0, max_calls=200)
    facts = cr.preflight_account(client, ACT)
    creator = cr.CampaignCreator(client, facts, cr.TargetingResolver(client), sleep=slept.append)
    out = creator.run(cr.plan_from_draft(_draft(tmp_path, ads=1)))
    assert out.error == "" and slept[:1] == [cr.RECONCILE_WAIT_S] and cr.RECONCILE_WAIT_S == 5.0


class _PageOverMeta:
    """Вкладка с помощником поверх FakeMeta — для `ApiPublisher.open`."""

    def __init__(self, meta, writes):
        self.meta, self.writes = meta, writes

    def install(self):
        pass

    def navigate(self, act, timeout=60.0):
        pass

    def wait_ready(self, timeout=90.0):
        return {"kind": "ads_manager", "has_token": True}

    def state(self):
        return self.meta.state()

    def request(self, *a, **k):
        return self.meta.request(*a, **k)

    def write(self, *a, **k):
        assert self.writes, "a read-only page must never be asked to write"
        return self.meta.write(*a, **k)


@pytest.mark.parametrize("dry_run", [True, False])
def test_open_builds_a_read_only_client_and_page_for_a_dry_run(monkeypatch, dry_run):
    from types import SimpleNamespace

    from src.web import publish_api

    made = []
    meta = FakeMeta()
    monkeypatch.setattr(publish_api, "_resolve_entry", lambda label: SimpleNamespace(
        ad_account_id=ACT, profile_id="550e8400-e29b-41d4-a716-446655440000", folder_id="",
        multilogin_instance="A"))
    monkeypatch.setattr(publish_api, "_cooldown", lambda label: (0.0, ""))
    monkeypatch.setattr(publish_api, "_open_profile", lambda *a: SimpleNamespace(
        cdp_url="http://127.0.0.1:1", started_now=False))
    monkeypatch.setattr(publish_api, "_open_tab", lambda url: SimpleNamespace(close=lambda: None))

    def make(tab, act, *, writes=True):
        made.append(writes)
        return _PageOverMeta(meta, writes)

    monkeypatch.setattr(publish_api, "_make_graph", make)
    pub = publish_api.ApiPublisher(dry_run=dry_run)
    pub.open("Test")
    assert made == [not dry_run] and pub._client.allow_writes is (not dry_run)


# ───────────────────────── дешборд ─────────────────────────

def test_result_dict_shape_and_publishability(tmp_path):
    from src.web import publish_api

    meta = FakeMeta()
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    res = publish_api.result_dict(out)
    assert res["backend"] == "api" and res["status"] == "PAUSED"
    assert res["success"] is True and res["publishable"] is True and res["problems"] == []
    assert res["created"] == {"campaign": 1, "adsets": 1, "images": 1, "creatives": 1, "ads": 1}

    meta2 = FakeMeta()
    meta2.mutate_on_read[("adset", "targeting.age_max")] = 65
    bad, _ = _run(meta2, _draft(tmp_path, ads=1))
    res2 = publish_api.result_dict(bad)
    assert res2["publishable"] is False and res2["blocking_failures"][0].startswith("readback mismatch")


def test_backend_switch(monkeypatch):
    from src.web import publish_api

    monkeypatch.delenv("ACL_FB_BACKEND", raising=False)
    assert publish_api.backend() == "rpa"
    monkeypatch.setenv("ACL_FB_BACKEND", "API")
    assert publish_api.backend() == "api"
    monkeypatch.setenv("ACL_FB_BACKEND", "bogus")
    assert publish_api.backend() == "rpa"


class FakeApiPublisher:
    instances: list = []

    def __init__(self, progress=lambda s: None, dry_run=False):
        import threading

        self.dry_run = dry_run
        self.cancel_event = threading.Event()
        self.opened = False
        self.finished = None
        FakeApiPublisher.instances.append(self)

    def cancel(self):
        self.cancel_event.set()

    def build_drafts(self, slots, *, account_label, country):
        from src.fb_ads_rpa.draft import CampaignDraft

        return [CampaignDraft(name=s["name"], country=country, account_label=account_label) for s in slots]

    def open(self, account_label):
        self.opened = True

    def publish_draft(self, draft):
        from src.web import publish_api

        o = cr.Outcome(campaign_name=draft.name, campaign_id="1", adset_ids=["2"], ad_ids=["3"])
        o.readback = {"ok": True, "checked": 3, "checks": [], "mismatches": []}
        return publish_api.result_dict(o)

    def finish_job(self, account_label, *, clean, other_job_waits=False):
        self.finished = clean
        return "closed" if clean else ""

    def close(self, force=False):
        pass


def test_publish_job_routes_to_the_api_only_when_switched(monkeypatch):
    from src.web import publish_api, publish_jobs

    monkeypatch.setattr(publish_api, "ApiPublisher", FakeApiPublisher)
    called = {"rpa": 0}
    monkeypatch.setattr(publish_jobs, "_run_rpa", lambda *a, **k: called.__setitem__("rpa", called["rpa"] + 1))
    monkeypatch.setattr(publish_jobs, "_write_draft_status", lambda *a, **k: None)
    monkeypatch.setattr(publish_jobs, "_start_stuck_watch", lambda *a, **k: None)
    monkeypatch.setattr(publish_jobs, "profile_key_for", lambda label: "p-" + label)
    monkeypatch.setattr(publish_jobs, "use_rpa_publish", lambda: True)

    monkeypatch.setenv("ACL_FB_BACKEND", "api")
    FakeApiPublisher.instances.clear()
    job = publish_jobs.create_job("Acc API", "MX", [{"name": "C1"}, {"name": "C2"}])
    publish_jobs._run_job(job, [{"name": "C1"}, {"name": "C2"}], account_label="Acc API", country="MX",
                          via_multilogin=True, activate=False)
    snap = publish_jobs.get_job(job)
    assert snap["fb_backend"] == "api" and called["rpa"] == 0
    assert snap["publishable"] is True and [r["status"] for r in snap["results"]] == ["PAUSED", "PAUSED"]
    assert FakeApiPublisher.instances[0].finished is True and snap.get("profile_closed") is True
    assert FakeApiPublisher.instances[0].dry_run is False

    job3 = publish_jobs.create_job("Acc Dry", "MX", [{"name": "C1"}], dry_run=True)
    publish_jobs._run_job(job3, [{"name": "C1"}], account_label="Acc Dry", country="MX",
                          via_multilogin=True, activate=False, dry_run=True)
    assert FakeApiPublisher.instances[-1].dry_run is True    # ревью M3: dry_run доходит до API

    monkeypatch.setenv("ACL_FB_BACKEND", "rpa")
    job2 = publish_jobs.create_job("Acc RPA", "MX", [{"name": "C1"}])
    publish_jobs._run_job(job2, [{"name": "C1"}], account_label="Acc RPA", country="MX",
                          via_multilogin=True, activate=False)
    assert called["rpa"] == 1


# ───────────────────────── помощник страницы (Node) ─────────────────────────

_NODE = r"""
const calls = [];
const fakeFetch = async (url, init) => {
  calls.push({ url: String(url), method: init.method,
               body: init.body instanceof FormData ? 'formdata:' + [...init.body.keys()].join(',')
                     : String(init.body || '') });
  return { status: 200, text: async () => JSON.stringify({ id: '5' }), headers: { get: () => null } };
};
const loc = { href: 'https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=1',
              hostname: 'adsmanager.facebook.com', pathname: '/adsmanager/manage/campaigns' };
const doc = { readyState: 'complete', querySelector: () => null, scripts: [],
              documentElement: { innerHTML: '"accessToken":"' + TOKENV + '"' } };
const perf = { getEntriesByType: () => [] };
const G = {};
new Function('globalThis','location','document','performance','fetch','URL','URLSearchParams',
             HELPER)(G, loc, doc, perf, fakeFetch, URL, URLSearchParams);
const h = G[NAME];
(async () => {
  const out = {};
  const camp = { name: 'C', objective: 'OUTCOME_LEADS', status: 'PAUSED', special_ad_categories: [] };
  out.ok = await h.call({ method: 'POST', path: '/' + ACT + '/campaigns', params: camp });
  out.active = await h.call({ method: 'POST', path: '/' + ACT + '/campaigns', params: { ...camp, status: 'ACTIVE' } });
  out.deep = await h.call({ method: 'POST', path: '/' + ACT + '/adcreatives',
                            params: { name: 'x', object_story_spec: { effective_status: 'PAUSED' } } });
  out.other = await h.call({ method: 'POST', path: '/act_1234567890123456/campaigns', params: camp });
  out.forms = await h.call({ method: 'POST', path: '/' + ACT + '/leadgen_forms', params: { name: 'x' } });
  out.del = await h.call({ method: 'DELETE', path: '/' + ACT + '/campaigns', params: {} });
  out.key = await h.call({ method: 'POST', path: '/' + ACT + '/campaigns', params: { ...camp, spend_cap: 1 } });
  out.nofile = await h.call({ method: 'POST', path: '/' + ACT + '/adimages', params: {}, files: { filename: 'fx' } });
  // Ревью: каждый мутант проверок в помощнике должен ронять свой случай.
  out.missing = await h.call({ method: 'POST', path: '/' + ACT + '/campaigns',
                               params: { name: 'C', objective: 'OUTCOME_LEADS' } });
  out.nested_active = await h.call({ method: 'POST', path: '/' + ACT + '/adsets',
                                     params: { name: 's', status: 'PAUSED', targeting: { geo_locations: { status: 'ACTIVE' } } } });
  out.nested_forbidden = await h.call({ method: 'POST', path: '/' + ACT + '/adsets',
                                        params: { name: 'a', status: 'PAUSED', targeting: { geo_locations: { lead_gen_form_id: '1' } } } });
  out.json_active = await h.call({ method: 'POST', path: '/' + ACT + '/adcreatives',
                                   params: { name: 'x', object_story_spec: '{"link_data":{"status":"ACTIVE"}}' } });
  out.json_bad = await h.call({ method: 'POST', path: '/' + ACT + '/adcreatives',
                                params: { name: 'x', object_story_spec: 'not json' } });
  // Ревью, раунд 3: ACTIVE, спрятанный в JSON-СТРОКЕ, — помощник разбирает её до проверки.
  out.json_str_active = await h.call({ method: 'POST', path: '/' + ACT + '/adcreatives',
    params: { name: 'x', object_story_spec: JSON.stringify({ page_id: '1',
              link_data: { message: 'hi', call_to_action: { type: 'LEARN_MORE', status: 'ACTIVE' } } }) } });
  out.json_num = await h.call({ method: 'POST', path: '/' + ACT + '/adcreatives',
                                params: { name: 'x', object_story_spec: 42 } });
  out.json_bracket = await h.call({ method: 'POST', path: '/' + ACT + '/adsets',
                                    params: { name: 's', status: 'PAUSED', targeting: '[TEST] not json' } });
  // Ревью R2-B1: обычный текст с «[» / «{» в начале — не JSON и не отказ.
  out.free_camp = await h.call({ method: 'POST', path: '/' + ACT + '/campaigns',
                                 params: { ...camp, name: '[TEST] Camp' } });
  const spec = { page_id: '1', link_data: { message: '[Offer] buy now', name: '{Free} gift',
                                            description: '[1,2] promo' } };
  out.free_obj = await h.call({ method: 'POST', path: '/' + ACT + '/adcreatives',
                                params: { name: '[TEST] ad', object_story_spec: spec } });
  out.free_str = await h.call({ method: 'POST', path: '/' + ACT + '/adcreatives',
                                params: { name: '{Free} gift', object_story_spec: JSON.stringify(spec) } });
  const hello = Buffer.from('hello');
  const sha = require('crypto').createHash('sha256').update(hello).digest('hex');
  out.stage = h.stage('fx', hello.toString('base64'));
  out.short = await h.call({ method: 'POST', path: '/' + ACT + '/adimages', params: {}, files: { filename: 'fx' },
                             filename: 'a.png', expect: { bytes: 6, sha256: sha } });
  out.badsha = await h.call({ method: 'POST', path: '/' + ACT + '/adimages', params: {}, files: { filename: 'fx' },
                              filename: 'a.png', expect: { bytes: 5, sha256: 'ff' } });
  out.img = await h.call({ method: 'POST', path: '/' + ACT + '/adimages', params: {}, files: { filename: 'fx' },
                           filename: 'a b.png', expect: { bytes: 5, sha256: sha } });
  out.calls = calls;
  process.stdout.write(JSON.stringify(out));
})();
"""


def _node_run(write_policy):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    name = "__acl_graph_test"
    # Сначала заглушки шаблона, исходник помощника — последним (его не трогаем).
    # ACT — только целым словом: «ACTIVE» в шаблоне остаётся как есть.
    template = re.sub(r"(?<![A-Za-z])ACT(?![A-Za-z])", lambda _m: json.dumps(ACT), _NODE)
    script = (template.replace("TOKENV", json.dumps(TOKEN)).replace("NAME", json.dumps(name))
              .replace("HELPER", json.dumps(tc.helper_source(name, write_policy))))
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(script)
        path = fh.name
    try:
        done = subprocess.run([node, path], capture_output=True, text=True, timeout=60)
    finally:
        Path(path).unlink(missing_ok=True)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_page_helper_write_mode_in_node():
    out = _node_run(wp.policy_for_page(ACT))
    assert out["ok"]["body"] == {"id": "5"}
    for k in ("active", "deep", "other", "forms", "key", "missing", "nested_active",
              "nested_forbidden", "json_active", "json_bad", "json_num", "json_bracket", "short",
              "badsha"):
        assert out[k]["error"] == "write_refused", k
    assert out["json_num"]["why"] == "json" and out["json_bracket"]["why"] == "json"
    assert out["json_str_active"] == {"error": "write_refused", "why": "status"}
    assert out["json_active"]["why"] == "status"
    for k in ("free_camp", "free_obj", "free_str"):
        assert out[k].get("body") == {"id": "5"}, (k, out[k])
    assert out["missing"]["why"] == "status" and out["nested_active"]["why"] == "status"
    assert out["nested_forbidden"]["why"] == "status"
    assert out["short"]["why"] == "file_size" and out["badsha"]["why"] == "file_hash"
    assert out["del"]["error"] == "read_only"
    assert out["nofile"]["error"] == "no_file"
    assert out["stage"]["ok"] is True and out["img"]["body"] == {"id": "5"}
    assert len(out["calls"]) == 5                     # только разрешённые записи ушли
    first, img = out["calls"][0], out["calls"][-1]
    assert first["method"] == "POST" and "access_token" not in first["url"]
    assert "status=PAUSED" in first["body"] and "access_token=" in first["body"]
    assert img["body"].startswith("formdata:") and "filename" in img["body"]
    assert TOKEN not in json.dumps({k: v for k, v in out.items() if k != "calls"})


def test_page_helper_without_policy_stays_read_only_in_node():
    out = _node_run(None)
    assert out["ok"]["error"] == "read_only" and out["calls"] == []
    assert out["stage"]["error"] == "read_only"
