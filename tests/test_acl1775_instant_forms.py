"""ACL-1775: Instant Forms - выбрать существующую форму Страницы (бэкенд API).

Ни Meta, ни Multilogin здесь не трогаются: Graph - `FakeMeta` в памяти (с
формами Страницы), страница помощника - Node. Проверяется:
* узкое изменение политики записи в ОБЕИХ линиях (Python и помощник страницы):
  `lead_gen_form_id` - только в креативе, в call_to_action.value, и только
  когда набор плана ON_AD; везде в другом месте запрещён;
* чтение форм Страницы и понятные причины, почему прочитать нельзя;
* набор ON_AD/LEAD_GENERATION/Страница без пикселя, креатив с формой, PAUSED;
* чужая форма и архивная форма - отказ до первой записи (0 записей);
* сверка: форма в креативе и принадлежность Странице;
* сайтовый путь не изменился ни байтом (золотой файл из прежнего создателя);
* RPA-бэкенд: слот с Instant Form отвергается вслух.
"""
from __future__ import annotations

import asyncio
import copy
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_api import creator as cr  # noqa: E402
from src.fb_api import forms as fm  # noqa: E402
from src.fb_api import graph as g  # noqa: E402
from src.fb_api import token_capture as tc  # noqa: E402
from src.fb_api import write_policy as wp  # noqa: E402
from tests.test_acl1761_api_create import ACT, FakeMeta, _draft  # noqa: E402

PAGE = "111"
FORM = "7000001"
FORM2 = "7000003"
ARCHIVED = "7000002"
FOREIGN = "7000009"          # форма другой Страницы: в списке нашей её нет
GOLDEN = ROOT / "tests" / "fixtures" / "acl1775_website_golden.json"

ROWS = [
    {"id": FORM, "name": "Lead form A", "status": "ACTIVE", "locale": "en_US", "created_time": "2026-09-01T10:00:00+0000"},
    {"id": ARCHIVED, "name": "Old form", "status": "ARCHIVED", "locale": "en_US", "created_time": "2026-01-01T10:00:00+0000"},
    {"id": FORM2, "name": "Lead form B", "status": "ACTIVE", "locale": "es_LA", "created_time": "2026-09-02T10:00:00+0000"},
]


class FormMeta(FakeMeta):
    """FakeMeta + формы Страницы 111 (`/{page}/leadgen_forms`) и флаг условий Lead Ads."""

    def __init__(self, *, rows=None, forms_error=None, tos=True, **kw):
        super().__init__(**kw)
        self.rows = copy.deepcopy(ROWS if rows is None else rows)
        self.forms_error = forms_error
        self.tos = tos
        #: Пока True, форма пропадает из списка Страницы, как только создан креатив.
        self.drop_forms_after_creative = False
        self.form_gets: list[dict] = []
        self.pixels: list[dict] | None = None

    def _get(self, path, params):
        parts = path.strip("/").split("/")
        if parts == [PAGE, "leadgen_forms"]:
            self.form_gets.append(dict(params))
            if self.forms_error:
                return {"error": self.forms_error}
            rows = self.rows
            if self.drop_forms_after_creative and self._of("creative"):
                rows = []
            return self._page(rows, params)
        if parts == [PAGE] and "connected_instagram_account" not in str(params.get("fields")):
            return {"id": PAGE, "leadgen_tos_accepted": self.tos}
        if parts == [ACT, "adspixels"] and self.pixels is not None:
            return {"data": self.pixels}
        return super()._get(path, params)


def _instant(tmp_path, *, forms=(FORM, FORM2), video=False, link="https://example.invalid/l"):
    d = _draft(tmp_path, video=video, ads=len(forms))
    d.link = link
    d.ad_sets[0].destination = "instant_form"
    for ad, fid in zip(d.ad_sets[0].ads, forms):
        ad.lead_form_id = fid
    return d


def _creator(meta, **kw):
    client = g.GraphClient(meta, allow_writes=True, sleep=lambda s: None, min_interval_s=0.0, max_calls=200)
    facts = cr.preflight_account(client, ACT)
    return cr.CampaignCreator(client, facts, cr.TargetingResolver(client), sleep=lambda s: None, **kw), client


def _run(meta, draft, **kw):
    creator, client = _creator(meta, **kw)
    return creator.run(cr.plan_from_draft(draft)), client


def _objs(meta, kind):
    return [o for o in meta.objects.values() if o["_kind"] == kind]


# ───────────────────────── политика записи: Python ─────────────────────────

def _cta_story(fmt="link_data", value=None, **cta_extra):
    value = {"lead_gen_form_id": FORM} if value is None else value
    data = {"message": "m", "call_to_action": {"type": "LEARN_MORE", "value": value, **cta_extra}}
    if fmt == "link_data":
        data["link"] = cr.INSTANT_FORM_LINK
    return {"page_id": PAGE, fmt: data}


def _creative(story):
    return {"name": "x", "object_story_spec": story}


#: Одна таблица на ОБЕ линии: (имя, ребро, параметры, on_ad, разрешено).
MATRIX = [
    ("link_data_ok", "adcreatives", _creative(_cta_story("link_data")), True, True),
    ("video_data_ok", "adcreatives", _creative(_cta_story("video_data")), True, True),
    # Ревью m2: годная форма в CTA + вторая lead_gen_form_id в том же spec - отказ в обеих линиях.
    ("valid_plus_link_data_level", "adcreatives", _creative({"page_id": PAGE, "link_data": {
        **_cta_story("link_data")["link_data"], "lead_gen_form_id": FORM}}), True, False),
    ("valid_plus_story_level", "adcreatives", _creative({**_cta_story("link_data"), "lead_gen_form_id": FORM}), True, False),
    ("valid_plus_second_format", "adcreatives", _creative({"page_id": PAGE,
        "link_data": _cta_story("link_data")["link_data"],
        "video_data": {"call_to_action": {"type": "LEARN_MORE", "value": {"lead_gen_form_id": FORM2}}}}), True, False),
    ("valid_plus_cta_level", "adcreatives", _creative({"page_id": PAGE, "link_data": {
        "call_to_action": {"type": "LEARN_MORE", "lead_gen_form_id": FORM,
                           "value": {"lead_gen_form_id": FORM}}}}), True, False),
    ("link_data_ok_as_string", "adcreatives", {"name": "x", "object_story_spec": json.dumps(_cta_story())}, True, True),
    ("no_on_ad_link", "adcreatives", _creative(_cta_story("link_data")), False, False),
    ("no_on_ad_video", "adcreatives", _creative(_cta_story("video_data")), False, False),
    ("top_level_key", "adcreatives", {"name": "x", "lead_gen_form_id": FORM}, True, False),
    ("story_level", "adcreatives", _creative({"page_id": PAGE, "lead_gen_form_id": FORM}), True, False),
    ("link_data_level", "adcreatives", _creative({"page_id": PAGE, "link_data": {"lead_gen_form_id": FORM}}), True, False),
    ("cta_level", "adcreatives", _creative({"page_id": PAGE, "link_data": {
        "call_to_action": {"type": "LEARN_MORE", "lead_gen_form_id": FORM}}}), True, False),
    ("value_extra_key", "adcreatives", _creative(_cta_story(value={"lead_gen_form_id": FORM, "link": "https://x.invalid"})), True, False),
    ("both_formats", "adcreatives", _creative({"page_id": PAGE, "link_data": _cta_story("link_data")["link_data"],
                                               "video_data": _cta_story("video_data")["video_data"]}), True, False),
    ("deeper_child", "adcreatives", _creative({"page_id": PAGE, "link_data": {"child_attachments": [{
        "call_to_action": {"type": "LEARN_MORE", "value": {"lead_gen_form_id": FORM}}}]}}), True, False),
    ("other_format", "adcreatives", _creative({"page_id": PAGE, "photo_data": {
        "call_to_action": {"type": "LEARN_MORE", "value": {"lead_gen_form_id": FORM}}}}), True, False),
    ("upper_case_key", "adcreatives", _creative(_cta_story(value={"LEAD_GEN_FORM_ID": FORM})), True, False),
    ("not_digits", "adcreatives", _creative(_cta_story(value={"lead_gen_form_id": "abc123"})), True, False),
    ("too_short", "adcreatives", _creative(_cta_story(value={"lead_gen_form_id": "123"})), True, False),
    ("as_number", "adcreatives", _creative(_cta_story(value={"lead_gen_form_id": 7000001})), True, False),
    ("empty", "adcreatives", _creative(_cta_story(value={"lead_gen_form_id": ""})), True, False),
    ("with_space", "adcreatives", _creative(_cta_story(value={"lead_gen_form_id": "7000001 "})), True, False),
    ("in_adset_promoted_object", "adsets", {"name": "s", "status": "PAUSED",
                                           "promoted_object": {"page_id": PAGE, "lead_gen_form_id": FORM}}, True, False),
    ("in_adset_targeting", "adsets", {"name": "s", "status": "PAUSED", "targeting": {"lead_gen_form_id": FORM}}, True, False),
    ("in_ad_creative", "ads", {"name": "a", "status": "PAUSED", "creative": {"creative_id": "1", "lead_gen_form_id": FORM}}, True, False),
    ("in_campaign_label", "campaigns", {"name": "c", "status": "PAUSED", "adlabels": [{"name": "l", "lead_gen_form_id": FORM}]}, True, False),
]
MATRIX_IDS = [m[0] for m in MATRIX]


@pytest.mark.parametrize("name,edge,params,on_ad,allowed", MATRIX, ids=MATRIX_IDS)
def test_policy_python_lead_form_matrix(name, edge, params, on_ad, allowed):
    path = f"/{ACT}/{edge}"
    if allowed:
        assert wp.check(ACT, "POST", path, params, on_ad=on_ad) == edge
    else:
        with pytest.raises(wp.WriteRefused):
            wp.check(ACT, "POST", path, params, on_ad=on_ad)


def test_policy_default_is_not_on_ad():
    """Без явного `on_ad` форма не проходит: старые вызовы по-прежнему ничего не разрешают."""
    with pytest.raises(wp.WriteRefused, match="ON_AD"):
        wp.check(ACT, "POST", f"/{ACT}/adcreatives", _creative(_cta_story()))


def test_policy_does_not_mutate_the_callers_params():
    params = _creative(_cta_story())
    before = copy.deepcopy(params)
    wp.check(ACT, "POST", f"/{ACT}/adcreatives", params, on_ad=True)
    assert params == before, "проверка вырезала форму из настоящих параметров"


@pytest.mark.parametrize("path", [
    f"/{PAGE}/leadgen_forms",                  # создание формы - ACL-1776, не здесь
    f"/{ACT}/leadgen_forms",
    f"/{PAGE}/leadgen_tos",                    # условия не принимаем
    f"/{ACT}/adcreatives/extra",
])
def test_policy_still_never_writes_forms_or_terms(path):
    with pytest.raises(wp.WriteRefused):
        wp.check(ACT, "POST", path, _creative(_cta_story()), on_ad=True)


def test_policy_still_forbids_terms_acceptance_anywhere():
    with pytest.raises(wp.WriteRefused):
        wp.check(ACT, "POST", f"/{ACT}/adcreatives",
                 _creative(_cta_story(value={"lead_gen_form_id": FORM, "tos_accepted": True})), on_ad=True)
    assert "tos_accepted" in wp.FORBIDDEN_ANYWHERE and wp.LEAD_FORM_KEY in wp.FORBIDDEN_ANYWHERE


def test_page_policy_describes_the_only_allowed_place():
    pol = wp.policy_for_page(ACT)
    assert pol["lead_form"] == {"key": "lead_gen_form_id", "edge": "adcreatives",
                                "formats": ["link_data", "video_data"], "id_re": "^[0-9]{5,25}$"}
    assert "lead_gen_form_id" in pol["forbidden"]


# ───────────────────────── политика записи: помощник страницы (Node) ─────────────────────────

_NODE = r"""
const calls = [];
const fakeFetch = async (url, init) => { calls.push(String(url)); return { status: 200,
  text: async () => JSON.stringify({ id: '5' }), headers: { get: () => null } }; };
const loc = { href: 'https://adsmanager.facebook.com/adsmanager/manage/campaigns',
              hostname: 'adsmanager.facebook.com', pathname: '/adsmanager/manage/campaigns' };
const doc = { readyState: 'complete', querySelector: () => null, scripts: [],
              documentElement: { innerHTML: '"accessToken":"' + TOKENV + '"' } };
const G = {};
new Function('globalThis','location','document','performance','fetch','URL','URLSearchParams', HELPER)(
  G, loc, doc, { getEntriesByType: () => [] }, fakeFetch, URL, URLSearchParams);
const h = G[NAME];
(async () => {
  const out = {};
  for (const m of MATRIX) {
    const spec = { method: 'POST', path: '/' + ACTV + '/' + m.edge, params: m.params };
    if (m.on_ad) spec.on_ad = true;
    if (m.edge !== 'adcreatives') spec.params = Object.assign({}, m.params);
    out[m.name] = await h.call(spec);
  }
  out.calls = calls.length;
  process.stdout.write(JSON.stringify(out));
})();
"""


def _node_matrix() -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    name = "__acl_graph_forms"
    matrix = [{"name": n, "edge": e, "params": p, "on_ad": o} for n, e, p, o, _a in MATRIX]
    script = (_NODE.replace("TOKENV", json.dumps("EAAB" + "Zx9" * 20)).replace("NAME", json.dumps(name))
              .replace("ACTV", json.dumps(ACT)).replace("MATRIX", "(" + json.dumps(matrix) + ")")
              .replace("HELPER", json.dumps(tc.helper_source(name, wp.policy_for_page(ACT)))))
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(script)
        path = fh.name
    try:
        done = subprocess.run([node, path], capture_output=True, text=True, encoding="utf-8", timeout=60)
    finally:
        Path(path).unlink(missing_ok=True)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_policy_page_helper_matches_python_on_the_whole_matrix():
    out = _node_matrix()
    for name, _edge, _params, _on_ad, allowed in MATRIX:
        got = out[name]
        if allowed:
            assert got.get("body") == {"id": "5"}, (name, got)
        else:
            assert got.get("error") == "write_refused", (name, got)
    assert out["calls"] == sum(1 for m in MATRIX if m[4]), "в Meta ушло не то число записей"


def test_policy_page_helper_without_the_flag_refuses_the_form():
    """Помощник не доверяет Python: без `on_ad` форма - отказ `lead_form`."""
    out = _node_matrix()
    assert out["no_on_ad_link"] == {"error": "write_refused", "why": "lead_form"}
    assert out["not_digits"] == {"error": "write_refused", "why": "lead_form"}
    assert out["value_extra_key"] == {"error": "write_refused", "why": "lead_form"}


def test_the_write_path_passes_on_ad_to_both_layers():
    """InPageGraph.write: Python-проверка и спецификация для помощника несут один и тот же `on_ad`."""
    class Tab:
        def __init__(self):
            self.exprs = []

        def create_isolated_world(self, name):
            return 7

        def evaluate_in(self, ctx, expr, timeout=60.0):
            self.exprs.append(expr)
            if expr.lstrip().startswith("(() =>"):
                return None
            if ".state()" in expr:
                return {"kind": "ads_manager", "has_token": True}
            return {"status": 200, "body": {"id": "1"}}

    tab = Tab()
    t = tc.InPageGraph(tab, allow_writes=True, write_act=ACT)
    with pytest.raises(wp.WriteRefused):
        t.write(f"/{ACT}/adcreatives", _creative(_cta_story()))
    assert not [e for e in tab.exprs if ".call(" in e], "форма без on_ad дошла до страницы"
    t.write(f"/{ACT}/adcreatives", _creative(_cta_story()), on_ad=True)
    spec = json.loads([e for e in tab.exprs if ".call(" in e][0].split(".call(", 1)[1].rsplit(")", 1)[0])
    assert spec["on_ad"] is True
    t.write(f"/{ACT}/campaigns", {"name": "c", "objective": "OUTCOME_LEADS", "status": "PAUSED",
                                  "special_ad_categories": []})
    spec = json.loads([e for e in tab.exprs if ".call(" in e][1].split(".call(", 1)[1].rsplit(")", 1)[0])
    assert "on_ad" not in spec


# ───────────────────────── чтение форм Страницы ─────────────────────────

def test_capability_probe_is_pure_and_names_each_state():
    ok = fm.forms_capability(PAGE, ROWS)
    assert ok.state == fm.OK and ok.ok and [f.id for f in ok.usable] == [FORM, FORM2]
    assert [f["id"] for f in ok.as_dict()["forms"]] == [FORM, FORM2], "архивная форма попала в выбор"
    assert fm.forms_capability(PAGE, []).state == fm.EMPTY
    assert fm.forms_capability("", []).state == fm.NO_PAGE
    assert fm.forms_capability("../etc", []).state == fm.NO_PAGE
    perm = fm.forms_capability(PAGE, None, g.GraphError("Graph API error 200: (#200) Requires leads_retrieval", code=200))
    assert perm.state == fm.UNREADABLE and perm.message == "This Page's forms can't be read with the current session"
    assert perm.code == 200 and not perm.ok
    terms = fm.forms_capability(PAGE, None, g.GraphError("The Page has not accepted the Lead Ads terms", code=100))
    assert terms.state == fm.TERMS and "Lead Ads terms" in terms.message and "never accepts" in terms.message
    assert fm.forms_capability(PAGE, [], terms_accepted=False).state == fm.TERMS
    assert fm.forms_capability(PAGE, None, g.GraphError("x", code=200), terms_accepted=False).state == fm.TERMS
    assert fm.forms_capability(PAGE, [], terms_accepted=True).state == fm.EMPTY


def test_reading_forms_is_one_get_with_the_documented_fields():
    meta = FormMeta()
    client = g.GraphClient(meta, sleep=lambda s: None, min_interval_s=0.0, max_calls=20)
    read = fm.read_page_forms(client, PAGE)
    assert read.ok and read.by_id()[FORM].name == "Lead form A"
    assert meta.form_gets == [{"fields": "id,name,status,locale,created_time", "limit": 100}]
    assert not meta.posts and client.writes == 0 and not client.allow_writes


def test_unreadable_forms_give_the_clear_message_not_a_raw_error():
    meta = FormMeta(forms_error={"code": 200, "type": "OAuthException",
                                 "message": "(#200) Requires leads_retrieval permission"})
    client = g.GraphClient(meta, sleep=lambda s: None, min_interval_s=0.0, max_calls=20)
    read = fm.read_page_forms(client, PAGE)
    assert read.state == fm.UNREADABLE
    assert read.message == "This Page's forms can't be read with the current session"
    assert "leads_retrieval" not in read.message


def test_a_page_without_accepted_terms_is_reported():
    meta = FormMeta(rows=[], tos=False)
    client = g.GraphClient(meta, sleep=lambda s: None, min_interval_s=0.0, max_calls=20)
    read = fm.read_page_forms(client, PAGE)
    assert read.state == fm.TERMS and "has not accepted the Lead Ads terms" in read.message
    assert not meta.posts, "робот не принимает условия"


def test_a_stop_of_the_client_is_not_swallowed_by_the_forms_read():
    class Stopping:
        def request(self, method, path, params=None):
            return {"status": 400, "body": {"error": {"code": 4, "message": "limit"}}}

        def state(self):
            return {"kind": "ads_manager", "has_token": True}

    client = g.GraphClient(Stopping(), sleep=lambda s: None, min_interval_s=0.0, max_calls=20)
    with pytest.raises(g.RateLimited):
        fm.read_page_forms(client, PAGE)


def test_a_bad_page_id_makes_no_call():
    meta = FormMeta()
    client = g.GraphClient(meta, sleep=lambda s: None, min_interval_s=0.0, max_calls=20)
    assert fm.read_page_forms(client, "12/../x").state == fm.NO_PAGE
    assert meta.gets == []


# ───────────────────────── план из черновика ─────────────────────────

def test_plan_marks_the_ad_set_and_the_form_of_each_ad(tmp_path):
    plan = cr.plan_from_draft(_instant(tmp_path))
    assert plan.ad_sets[0].instant and plan.ad_sets[0].destination == "instant_form"
    assert [a.form_id for a in plan.ad_sets[0].ads] == [FORM, FORM2]


def test_instant_form_needs_no_tracking_link(tmp_path):
    plan = cr.plan_from_draft(_instant(tmp_path, link=""))
    assert plan.link == ""
    with pytest.raises(cr.PlanError, match="Tracking link is empty"):
        d = _draft(tmp_path)
        d.link = ""
        cr.plan_from_draft(d)


def test_an_ad_without_a_form_is_a_plan_error(tmp_path):
    with pytest.raises(cr.PlanError, match="Ad 2: no Instant Form selected"):
        cr.plan_from_draft(_instant(tmp_path, forms=(FORM, "")))


def test_unknown_conversion_location_is_a_plan_error(tmp_path):
    d = _instant(tmp_path)
    d.ad_sets[0].destination = "messenger"
    with pytest.raises(cr.PlanError, match="Unknown conversion location"):
        cr.plan_from_draft(d)


def test_website_values_are_dormant_for_an_instant_form_ad(tmp_path):
    """Форма в Website-наборе не идёт никуда: серверу важно назначение, а не набранное."""
    d = _draft(tmp_path)
    for ad in d.ad_sets[0].ads:
        ad.lead_form_id = FORM
    plan = cr.plan_from_draft(d)
    assert all(a.form_id == "" for a in plan.ad_sets[0].ads)


# ───────────────────────── создание Instant Form ─────────────────────────

def test_instant_form_ad_set_and_creative_are_created_paused_and_read_back(tmp_path):
    meta = FormMeta()
    out, client = _run(meta, _instant(tmp_path))
    assert out.error == "" and out.readback["ok"] is True, out.mismatches
    camp, = _objs(meta, "campaign")
    adset, = _objs(meta, "adset")
    assert camp["status"] == "PAUSED" and adset["status"] == "PAUSED"
    assert adset["destination_type"] == "ON_AD" and adset["optimization_goal"] == "LEAD_GENERATION"
    assert adset["promoted_object"] == {"page_id": PAGE}, "в promoted_object только Страница, без пикселя"
    assert "pixel" not in json.dumps(adset["promoted_object"]) and "custom_event_type" not in json.dumps(adset)
    creatives = _objs(meta, "creative")
    assert len(creatives) == 2
    forms_seen = []
    for c in creatives:
        cta = c["object_story_spec"]["link_data"]["call_to_action"]
        assert cta["type"] == "LEARN_MORE" and set(cta["value"]) == {"lead_gen_form_id"}
        forms_seen.append(cta["value"]["lead_gen_form_id"])
        assert c["object_story_spec"]["page_id"] == PAGE
        assert c["object_story_spec"]["link_data"]["link"] == cr.INSTANT_FORM_LINK
    assert sorted(forms_seen) == [FORM, FORM2]
    assert all(o["status"] == "PAUSED" for o in _objs(meta, "ad"))
    assert len(out.ad_ids) == 2


def test_the_flag_reaches_only_the_creative_writes_and_nothing_else_is_written(tmp_path):
    meta = FormMeta()
    out, _ = _run(meta, _instant(tmp_path))
    assert out.error == ""
    flagged = {path.rsplit("/", 1)[1] for path, flag in meta.on_ad_posts if flag}
    assert flagged == {"adcreatives"}
    assert {p.rsplit("/", 1)[1] for p, _ in meta.posts} <= {"campaigns", "adsets", "adcreatives", "ads", "adimages"}
    assert not any("leadgen" in p or "tos" in p for p, _ in meta.posts), "формы не создаём, условия не принимаем"
    assert all(p.startswith(f"/{ACT}/") for p, _ in meta.posts), "запись вне кабинета"


def test_instant_form_video_ad_carries_the_form_too(tmp_path):
    meta = FormMeta()
    out, _ = _run(meta, _instant(tmp_path, video=True))
    assert out.error == "" and out.readback["ok"] is True, out.mismatches
    vid = [c for c in _objs(meta, "creative") if "video_data" in c["object_story_spec"]]
    assert vid and vid[0]["object_story_spec"]["video_data"]["call_to_action"]["value"] == {"lead_gen_form_id": FORM2}


def test_instant_form_needs_no_pixel_on_the_account(tmp_path):
    meta = FormMeta()
    meta.pixels = []
    out, _ = _run(meta, _instant(tmp_path))
    assert out.error == "" and out.readback["ok"] is True
    meta2 = FormMeta()
    meta2.pixels = []
    web_dir = tmp_path / "w"
    web_dir.mkdir()
    out2, _ = _run(meta2, _draft(web_dir))
    assert "pixels/datasets" in out2.error, "сайтовому пути пиксель по-прежнему нужен"


def test_readback_checks_the_form_ownership_and_the_ad_set_destination(tmp_path):
    meta = FormMeta()
    out, _ = _run(meta, _instant(tmp_path))
    by = {(c["object"], c["field"]): c for c in out.readback["checks"]}
    assert all(by[(o, "lead_gen_form_id")]["ok"] and by[(o, "form_on_page")]["ok"]
               for o in {k[0] for k in by if k[1] == "form_on_page"})
    assert by[("ad set MX Leads 1", "destination_type")]["expected"] == "ON_AD"
    assert by[("ad set MX Leads 1", "optimization_goal")]["expected"] == "LEAD_GENERATION"
    assert by[("ad set MX Leads 1", "promoted_page")]["expected"] == PAGE
    assert ("ad set MX Leads 1", "pixel") not in by


def test_readback_flags_a_form_that_is_no_longer_the_pages(tmp_path):
    meta = FormMeta()
    meta.drop_forms_after_creative = True
    out, _ = _run(meta, _instant(tmp_path))
    assert "form_on_page" in {m["field"] for m in out.mismatches}


def test_readback_flags_a_form_meta_changed_in_the_creative(tmp_path):
    meta = FormMeta()
    meta.mutate_ads["creative.object_story_spec.link_data.call_to_action.value.lead_gen_form_id"] = FOREIGN
    out, _ = _run(meta, _instant(tmp_path, forms=(FORM,)))
    assert "lead_gen_form_id" in {m["field"] for m in out.mismatches}


@pytest.mark.parametrize("key,value,field", [
    ("destination_type", "WEBSITE", "destination_type"),
    ("optimization_goal", "OFFSITE_CONVERSIONS", "optimization_goal"),
    ("promoted_object", {"page_id": "999"}, "promoted_page"),
])
def test_readback_flags_an_ad_set_meta_changed(tmp_path, key, value, field):
    meta = FormMeta()
    meta.mutate_on_read[("adset", key)] = value
    out, _ = _run(meta, _instant(tmp_path, forms=(FORM,)))
    assert field in {m["field"] for m in out.mismatches}


# ───────────────────────── отказы до записи ─────────────────────────

def test_a_form_of_another_page_is_refused_before_any_write(tmp_path):
    meta = FormMeta()
    out, client = _run(meta, _instant(tmp_path, forms=(FORM, FOREIGN)))
    assert out.stop_kind == "plan" and "Ad 2" in out.error and FOREIGN in out.error
    assert "not a form of this Page" in out.error
    assert meta.posts == [] and client.writes == 0 and not out.campaign_id, "до отказа что-то записано"


def test_an_archived_form_is_refused_before_any_write(tmp_path):
    meta = FormMeta()
    out, client = _run(meta, _instant(tmp_path, forms=(ARCHIVED,)))
    assert out.stop_kind == "plan" and "archived" in out.error
    assert meta.posts == [] and client.writes == 0


@pytest.mark.parametrize("status", ["", None, "PENDING", "ARCHIVED", "DRAFT", "DELETED"])
def test_only_an_active_form_is_usable_and_the_message_is_clear(tmp_path, status):
    row = {"id": "7000010", "name": "Odd form", "locale": "en_US"}
    if status is not None:
        row["status"] = status
    assert fm.parse_forms([row])[0].usable is False
    meta = FormMeta(rows=[row])
    out, client = _run(meta, _instant(tmp_path, forms=("7000010",)))
    assert out.stop_kind == "plan" and "only ACTIVE forms can be used" in out.error
    assert (status or "unknown status").lower() in out.error
    assert meta.posts == [] and client.writes == 0
    assert fm.parse_forms([{**row, "status": "active"}])[0].usable is True


def test_unknown_destination_from_the_web_form_is_refused_out_loud(tmp_path, monkeypatch):
    from src.fb_ads_rpa.build_draft import slots_to_campaign_drafts
    from src.web import app as web

    _c, _a, slots = web._parse_build_upload_form(_fields(camp_0_destination="messenger"))
    assert all(s["destination"] == "messenger" for s in slots)
    d, = slots_to_campaign_drafts(slots, account_label="Acc", country="MX")
    with pytest.raises(cr.PlanError, match="Unknown conversion location"):
        d.ad_sets[0].ads[0].creative_path = str(tmp_path / "x.png")
        cr.plan_from_draft(d)
    monkeypatch.setenv("ACL_FB_BACKEND", "rpa")
    assert web._instant_form_refusal(slots).status_code == 409


def test_the_call_budget_covers_the_extra_form_reads():
    from src.web import publish_api

    assert publish_api.DEFAULT_CREATE_MAX_CALLS >= 150


def test_unsupported_objective_message_mentions_instant_form(tmp_path):
    d = _draft(tmp_path)
    d.objective = "Traffic"
    with pytest.raises(cr.PlanError, match="website or Instant Form"):
        cr.plan_from_draft(d)


def test_unreadable_forms_stop_the_plan_with_the_clear_message_and_no_write(tmp_path):
    meta = FormMeta(forms_error={"code": 200, "message": "(#200) Requires leads_retrieval"})
    out, client = _run(meta, _instant(tmp_path))
    assert out.stop_kind == "plan"
    assert "This Page's forms can't be read with the current session" in out.error
    assert meta.posts == [] and client.writes == 0


def test_a_page_without_accepted_terms_stops_the_plan_and_nothing_is_accepted(tmp_path):
    meta = FormMeta(rows=[], tos=False)
    out, client = _run(meta, _instant(tmp_path, forms=(FORM,)))
    assert out.stop_kind == "plan" and "has not accepted the Lead Ads terms" in out.error
    assert meta.posts == [] and client.writes == 0


def test_dry_run_refuses_a_foreign_form_too_and_still_writes_nothing(tmp_path):
    meta = FormMeta()
    creator, client = _creator(meta)
    out = creator.plan_only(cr.plan_from_draft(_instant(tmp_path, forms=(FOREIGN,))))
    assert out.stop_kind == "plan" and "not a form of this Page" in out.error
    assert meta.posts == [] and not out.plan


def test_the_creator_runs_the_policy_on_the_form_creative_in_the_second_line(tmp_path, monkeypatch):
    """Креатив с формой при сайтовом наборе (искажённый план) - отказ политики, ни одной записи."""
    meta = FormMeta()
    d = _draft(tmp_path, ads=1)
    orig = cr.CampaignCreator.story_spec

    def with_form(plan, ad, media, **kw):
        spec = orig(plan, ad, media)
        spec["link_data"]["call_to_action"]["value"] = {"lead_gen_form_id": FORM}
        return spec

    monkeypatch.setattr(cr.CampaignCreator, "story_spec", staticmethod(with_form))
    out, client = _run(meta, d)
    assert out.stop_kind == "plan" and "ON_AD" in out.error
    assert meta.posts == [] and client.writes == 0


def test_the_creator_never_passes_the_flag_for_a_website_ad_set(tmp_path):
    meta = FormMeta()
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert out.error == "" and not any(flag for _p, flag in meta.on_ad_posts)


# ───────────────────────── «Check plan» показывает форму ─────────────────────────

def test_check_plan_shows_the_form_name_and_id_and_no_website_fields(tmp_path):
    meta = FormMeta()
    creator, _ = _creator(meta)
    out = creator.plan_only(cr.plan_from_draft(_instant(tmp_path, link="")))
    assert out.error == "" and meta.posts == []
    s = out.plan["summary"]["ad_sets"][0]
    assert s["destination"] == "Instant Form" and "LEAD_GENERATION" in s["optimization"]
    assert [(a["form_id"], a["form_name"]) for a in s["ads"]] == [(FORM, "Lead form A"), (FORM2, "Lead form B")]
    assert all("link" not in a for a in s["ads"])
    ad = out.plan["ad_sets"][0]
    assert ad["destination_type"] == "ON_AD" and ad["promoted_object"] == {"page_id": PAGE}
    assert ad["ads"][0]["creative"]["link_data"]["call_to_action"]["value"] == {"lead_gen_form_id": FORM}


# ───────────────────────── Website: ни байтом иначе ─────────────────────────

def _golden_run(cr_mod, tmp):
    out = {}
    for mode in ("plan", "run"):
        meta = FakeMeta()
        client = g.GraphClient(meta, allow_writes=True, sleep=lambda s: None, min_interval_s=0.0, max_calls=200)
        facts = cr_mod.preflight_account(client, ACT)
        creator = cr_mod.CampaignCreator(client, facts, cr_mod.TargetingResolver(client), sleep=lambda s: None)
        plan = cr_mod.plan_from_draft(_draft(Path(tmp), video=True, ads=2))
        if mode == "plan":
            o = creator.plan_only(plan)
            out["plan"], out["plan_error"] = o.plan, o.error
        else:
            o = creator.run(plan)
            out["posts"], out["error"] = meta.posts, o.error
            out["readback_checks"], out["steps"] = o.readback["checks"], o.steps
    return json.loads(json.dumps(out))


def _without_instagram(value):
    """ACL-1896 добавляет ровно Instagram-идентичность: поле креатива, шаг предпроверки и строку сверки."""
    if isinstance(value, dict):
        if value.get("field") == "instagram_user_id":
            return None
        return {k: _without_instagram(v) for k, v in value.items() if k != "instagram_user_id"}
    if isinstance(value, (list, tuple)):
        items = [_without_instagram(v) for v in value
                 if not (isinstance(v, str) and v.startswith("preflight: Instagram account of the Page"))]
        return type(value)(v for v in items if v is not None) if isinstance(value, list) else tuple(items)
    return value


def test_website_plan_posts_and_readback_are_identical_to_the_pre_forms_creator(tmp_path):
    """Золотой файл снят прежним создателем (до ACL-1775) на том же черновике.

    ACL-1896: кроме Instagram-идентичности (поле, шаг, строка сверки) не меняется ни байта.
    """
    want = json.loads(GOLDEN.read_text(encoding="utf-8"))
    got = _golden_run(cr, tmp_path)
    assert got != want, "креатив должен нести Instagram-идентичность Страницы"
    assert _without_instagram(got) == want


def test_website_draft_does_not_grow_new_keys(tmp_path):
    meta = FakeMeta()
    creator, _ = _creator(meta)
    out = creator.plan_only(cr.plan_from_draft(_draft(tmp_path, ads=1)))
    assert "destination" not in out.plan["summary"]["ad_sets"][0]
    assert "form_id" not in out.plan["summary"]["ad_sets"][0]["ads"][0]
    assert out.plan["ad_sets"][0]["destination_type"] == "WEBSITE"


# ───────────────────────── слоты формы и черновик ─────────────────────────

def _fields(**over):
    f = {"country": "Mexico", "account_label": "Acc", "tracking_link": "https://t.invalid/x", "camp_count": "1",
         "camp_0_name": "MX_test", "camp_0_ad_count": "2", "camp_0_budget": "20",
         "camp_0_ad_0_creative_file": "a.png", "camp_0_ad_0_copy": "T1",
         "camp_0_ad_1_creative_file": "b.png", "camp_0_ad_1_copy": "T2"}
    f.update(over)
    return f


def test_website_slots_are_byte_identical_and_ignore_a_dormant_form():
    from src.web import app as web

    plain = web._parse_build_upload_form(_fields())
    dormant = web._parse_build_upload_form(_fields(camp_0_destination="website", camp_0_ad_0_form_id=FORM,
                                                   camp_0_ad_1_form_id=FORM2))
    assert plain == dormant, "значение формы у сайтового набора уехало на сервер"
    assert all("destination" not in s and "lead_form_id" not in s for s in plain[2])


def test_instant_form_slots_carry_the_destination_and_each_ads_form():
    from src.web import app as web

    _c, _a, slots = web._parse_build_upload_form(_fields(
        camp_0_destination="instant_form", camp_0_ad_0_form_id=FORM, camp_0_ad_1_form_id=f" {FORM2} "))
    assert [s["destination"] for s in slots] == ["instant_form", "instant_form"]
    assert [s["lead_form_id"] for s in slots] == [FORM, FORM2]


def test_slots_become_a_draft_with_the_destination_and_forms():
    from src.fb_ads_rpa.build_draft import slots_to_campaign_drafts
    from src.web import app as web

    _c, _a, slots = web._parse_build_upload_form(_fields(
        camp_0_destination="instant_form", camp_0_ad_0_form_id=FORM, camp_0_ad_1_form_id=FORM2))
    d, = slots_to_campaign_drafts(slots, account_label="Acc", country="MX")
    assert d.ad_sets[0].destination == "instant_form"
    assert [a.lead_form_id for a in d.ad_sets[0].ads] == [FORM, FORM2]
    _c, _a, plain = web._parse_build_upload_form(_fields())
    d2, = slots_to_campaign_drafts(plain, account_label="Acc", country="MX")
    assert d2.ad_sets[0].destination == "website" and all(a.lead_form_id == "" for a in d2.ad_sets[0].ads)
    assert "destination" not in d2.ad_sets[0].to_preview_dict()

    # Слот без назначения, но с набранной формой: форма у сайтового набора не живёт.
    stray = [{**plain[0], "lead_form_id": FORM}]
    d3, = slots_to_campaign_drafts(stray, account_label="Acc", country="MX")
    assert d3.ad_sets[0].destination == "website" and d3.ad_sets[0].ads[0].lead_form_id == ""


# ───────────────────────── RPA-бэкенд ─────────────────────────

class _Req:
    def __init__(self, fields):
        self._f = dict(fields)

    async def form(self, **_limits):  # read_form передаёт пределы разбора
        return self._f


def test_the_rpa_backend_refuses_an_instant_form_slot_out_loud(monkeypatch):
    from src.web import app as web

    monkeypatch.setenv("ACL_FB_BACKEND", "rpa")
    fields = _fields(camp_0_destination="instant_form", camp_0_ad_0_form_id=FORM, camp_0_ad_1_form_id=FORM2)
    resp = asyncio.run(web.preview_publish(_Req(fields)))
    assert resp.status_code == 409 and json.loads(resp.body)["error"] == web.INSTANT_FORM_RPA_ERROR
    fields["user_confirmed"] = "1"
    resp = asyncio.run(web.publish_jobs_start(_Req(fields)))
    assert resp.status_code == 409 and "Instant Forms" in json.loads(resp.body)["error"]


def test_the_rpa_backend_still_takes_a_website_slot(monkeypatch):
    from src.web import app as web

    monkeypatch.setenv("ACL_FB_BACKEND", "rpa")
    assert web._instant_form_refusal(web._parse_build_upload_form(_fields())[2]) is None
    monkeypatch.setenv("ACL_FB_BACKEND", "api")
    assert web._instant_form_refusal(web._parse_build_upload_form(
        _fields(camp_0_destination="instant_form", camp_0_ad_0_form_id=FORM, camp_0_ad_1_form_id=FORM2))[2]) is None


def test_the_forms_endpoint_is_api_only_and_needs_an_account(monkeypatch):
    from src.web import app as web
    from src.web import page_forms

    calls = []
    monkeypatch.setattr(page_forms, "read_forms", lambda account: calls.append(account) or {
        "ok": True, "state": "ok", "forms": [], "message": ""})
    monkeypatch.setenv("ACL_FB_BACKEND", "rpa")
    resp = asyncio.run(web.api_page_forms(_Req({"account_label": "Acc"})))
    assert resp.status_code == 409 and calls == []
    monkeypatch.setenv("ACL_FB_BACKEND", "api")
    assert asyncio.run(web.api_page_forms(_Req({}))).status_code == 400
    resp = asyncio.run(web.api_page_forms(_Req({"account_label": "Acc"})))
    assert resp.status_code == 200 and calls == ["Acc"]


def test_the_forms_read_uses_a_client_that_cannot_write(monkeypatch):
    """Чтение для выбора идёт сессией чтения: клиент без права записи, один вызов на нажатие."""
    from contextlib import contextmanager
    from types import SimpleNamespace

    from src.analysis_agent import account_spend as _as
    from src.analysis_agent import account_spend_api as _api
    from src.web import page_forms

    meta = FormMeta()
    client = g.GraphClient(meta, sleep=lambda s: None, min_interval_s=0.0, max_calls=20)

    @contextmanager
    def fake_session(account):
        yield SimpleNamespace(client=client, act=ACT, account=account, notes={})

    monkeypatch.setattr(_api, "_session", fake_session)
    monkeypatch.setattr(_as, "_entry_for", lambda account: SimpleNamespace(page_id=PAGE))
    monkeypatch.setattr(_as, "serialized_browser", lambda **kw: (lambda fn: fn))
    out = page_forms.read_forms("Acc")
    assert out["ok"] is True and [f["id"] for f in out["forms"]] == [FORM, FORM2]
    assert out["account"] == "Acc" and out["page_id"] == PAGE
    assert meta.posts == [] and client.writes == 0 and not client.allow_writes
    assert len(meta.form_gets) == 1


def test_the_forms_read_failure_comes_back_as_text(monkeypatch):
    from contextlib import contextmanager

    from src.analysis_agent import account_spend as _as
    from src.analysis_agent import account_spend_api as _api
    from src.web import page_forms

    @contextmanager
    def broken(account):
        raise _api.ApiReadError(f"{account} is not in the account map")
        yield  # pragma: no cover

    monkeypatch.setattr(_api, "_session", broken)
    monkeypatch.setattr(_as, "_entry_for", lambda account: None)
    monkeypatch.setattr(_as, "serialized_browser", lambda **kw: (lambda fn: fn))
    out = page_forms.read_forms("Nope")
    assert out["ok"] is False and out["state"] == "failed" and "not in the account map" in out["message"]
