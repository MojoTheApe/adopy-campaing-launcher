"""ACL-1776: Instant Forms - робот создаёт форму из шаблона (бэкенд API).

Ни Meta, ни Multilogin: Graph - `FakeMeta` в памяти (формы Страницы, создание формы),
страница-помощник - Node. Проверяется:
* создание ровно один раз, до креатива, с чтением формы обратно;
* повторный запуск (и другая кампания с тем же шаблоном) находит форму по точному имени;
* таймаут записи сверяется чтением списка форм, слепого повтора нет;
* `tos_accepted` и условия отвергаются в ОБЕИХ линиях политики (Python и помощник страницы);
* другие рёбра Страницы, чужие ключи и DELETE отвергаются в обеих линиях;
* «Check plan» не пишет ничего и говорит, какая форма будет создана;
* Страница без принятых условий - понятная остановка до первой записи;
* проверка шаблона; RPA-бэкенд.
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
from src.fb_api import form_create as fc  # noqa: E402
from src.fb_api import form_templates as ft  # noqa: E402
from src.fb_api import graph as g  # noqa: E402
from src.fb_api import token_capture as tc  # noqa: E402
from src.fb_api import write_policy as wp  # noqa: E402
from tests.test_acl1761_api_create import ACT, _draft  # noqa: E402
from tests.test_acl1775_instant_forms import PAGE, FormMeta, _fields  # noqa: E402

TEMPLATE = {
    "name": "Demo form", "headline": "Get more information", "intro": "",
    "contact_fields": ["name", "email"],
    "questions": [{"label": "Which product interests you?", "type": "short_answer"},
                  {"label": "Budget", "type": "multiple_choice", "options": ["Low", "High"]}],
    "privacy_url": "https://example.com/privacy", "thank_you_text": "Thank you. We will contact you.",
    "website_url": "", "locale": "EN_US",
}


@pytest.fixture(autouse=True)
def _library(tmp_path, monkeypatch):
    """Библиотека шаблонов - во временной машинной папке, не в настоящей."""
    monkeypatch.setenv("ACL_CONFIG_DIR", str(tmp_path / "acl"))


class FormsMeta(FormMeta):
    """FormMeta + создание формы Страницы (`POST /{page}/leadgen_forms`) и чтение формы по id."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.form_posts: list[tuple[str, dict]] = []
        self.details: dict[str, dict] = {}
        self.permissions: list[dict] | None = None
        self.refuse: dict | None = None
        self.tamper_questions = False

    def write(self, path, params, files=None, filename="", on_ad=False, form_page="", **_k):
        if form_page:
            wp.check_form_create(form_page, "POST", path, params, files, PAGE)      # линия политики Python
            self.posts.append((path, dict(params)))
            self.form_posts.append((path, copy.deepcopy(params)))
            mode = self.fail_next.pop("leadgen_forms", None)
            if mode == "unsent":
                return {"error": "call_unknown"}
            if self.refuse:
                return {"status": 400, "body": {"error": self.refuse}}
            fid = str(7100000 + len(self.form_posts))
            row = {"id": fid, "name": params["name"], "status": "ACTIVE", "locale": params["locale"],
                   "created_time": "2026-10-12T10:00:00+0000"}
            self.rows.append(row)
            qs = copy.deepcopy(params["questions"])
            if self.tamper_questions:
                qs = qs[:-1]
            self.details[fid] = {**row, "page": {"id": PAGE}, "questions": {"data": qs}}
            if mode == "lost":
                return {"error": "call_unknown"}
            return {"status": 200, "body": {"id": fid}}
        return super().write(path, params, files, filename, on_ad)

    def _get(self, path, params):
        parts = path.strip("/").split("/")
        if parts == ["me", "permissions"]:
            if self.permissions is None:
                return {"error": {"code": 100, "message": "(#100) not available"}}
            return {"data": self.permissions}
        if len(parts) == 1 and parts[0] in self.details:
            return self.details[parts[0]]
        return super()._get(path, params)


def _seeded(**kw):
    """FormsMeta, где форма шаблона уже есть на Странице (создана прежним запуском)."""
    clean, _ = ft.validate(TEMPLATE)
    meta = FormsMeta(rows=[{"id": "7000050", "name": ft.form_name(clean), "status": "ACTIVE", "locale": "en_US"}], **kw)
    meta.details["7000050"] = {**meta.rows[0], "page": {"id": PAGE},
                               "questions": {"data": ft.expected_questions(clean)}}
    return meta


def _saved(**over):
    return ft.save({**TEMPLATE, **over})


def _tpl_draft(tmp_path, template_ids, name=None):
    d = _draft(tmp_path, ads=len(template_ids))
    if name:
        d.name = name
        d.ad_sets[0].name = name
    d.link = ""
    d.ad_sets[0].destination = "instant_form"
    for ad, tid in zip(d.ad_sets[0].ads, template_ids):
        ad.lead_form_id = ""
        ad.lead_form_template = tid
    return d


def _creator(meta, *, writes=True, **kw):
    client = g.GraphClient(meta, allow_writes=writes, sleep=lambda s: None, min_interval_s=0.0, max_calls=200)
    facts = cr.preflight_account(client, ACT)
    return cr.CampaignCreator(client, facts, cr.TargetingResolver(client), sleep=lambda s: None, **kw), client


def _run(meta, draft, **kw):
    creator, client = _creator(meta, **kw)
    return creator.run(cr.plan_from_draft(draft)), client


def _edges(meta):
    return [p.rsplit("/", 1)[1] for p, _ in meta.posts]


# ───────────────────────── шаблон: проверка, имя, хранение ─────────────────────────

def test_a_valid_template_makes_a_narrow_graph_request():
    clean, errors = ft.validate(TEMPLATE)
    assert errors == []
    params = ft.form_params(clean)
    assert set(params) <= wp.FORM_KEYS and "tos_accepted" not in json.dumps(params)
    assert params["name"].startswith("Demo form · ACL ") and ft.ROBOT_NAME_RE.search(params["name"])
    assert [q["type"] for q in params["questions"]] == ["FULL_NAME", "EMAIL", "CUSTOM", "CUSTOM"]
    assert params["questions"][3]["options"] == [{"key": "q2_option_1", "value": "Low"},
                                                 {"key": "q2_option_2", "value": "High"}]
    assert params["privacy_policy"] == {"url": "https://example.com/privacy", "link_text": "Privacy Policy"}
    assert params["thank_you_page"] == {"title": "Thank you", "body": "Thank you. We will contact you.",
                                        "button_type": "NONE"}
    assert "context_card" not in params
    assert wp.check_form_create(PAGE, "POST", f"/{PAGE}/leadgen_forms", params, pinned_page=PAGE) == "leadgen_forms"
    full, _ = ft.validate({**TEMPLATE, "intro": "Hello", "website_url": "https://example.com/x"})
    p2 = ft.form_params(full)
    assert p2["context_card"]["content"] == ["Hello"]
    assert p2["thank_you_page"]["button_type"] == "VIEW_WEBSITE"
    wp.check_form_create(PAGE, "POST", f"/{PAGE}/leadgen_forms", p2, pinned_page=PAGE)


BAD_TEMPLATES = [
    ("no_name", {"name": " "}, "Template name is required"),
    ("long_name", {"name": "x" * 61}, "Template name is longer than 60"),
    ("no_headline", {"headline": ""}, "Headline is required"),
    ("no_contact", {"contact_fields": []}, "at least one contact field"),
    ("unknown_contact", {"contact_fields": ["ssn"]}, "Unknown contact field"),
    ("no_privacy", {"privacy_url": ""}, "Privacy policy URL is required"),
    ("http_privacy", {"privacy_url": "http://example.com/p"}, "must start with https://"),
    ("junk_privacy", {"privacy_url": "https://nodot"}, "must start with https://"),
    ("no_thanks", {"thank_you_text": ""}, "Thank-you screen text is required"),
    ("http_site", {"website_url": "http://example.com"}, "Website link"),
    ("bad_locale", {"locale": "english"}, "Unknown locale"),
    ("six_questions", {"questions": [{"label": f"q{i}", "type": "short_answer"} for i in range(6)]}, "At most 5"),
    ("empty_question", {"questions": [{"label": " ", "type": "short_answer"}]}, "Question 1: text is required"),
    ("one_option", {"questions": [{"label": "q", "type": "multiple_choice", "options": ["a"]}]}, "at least 2 options"),
    ("same_options", {"questions": [{"label": "q", "type": "multiple_choice", "options": ["a", "A"]}]}, "options must differ"),
]


@pytest.mark.parametrize("name,over,text", BAD_TEMPLATES, ids=[b[0] for b in BAD_TEMPLATES])
def test_template_validation(name, over, text):
    _clean, errors = ft.validate({**TEMPLATE, **over})
    assert any(text in e for e in errors), errors
    with pytest.raises(ft.TemplateError):
        ft.save({**TEMPLATE, **over})
    assert ft.load() == []


def test_the_name_is_stable_per_content_and_changes_with_content():
    a, _ = ft.validate(TEMPLATE)
    b, _ = ft.validate(copy.deepcopy(TEMPLATE))
    c, _ = ft.validate({**TEMPLATE, "headline": "Another headline"})
    assert ft.form_name(a) == ft.form_name(b)
    assert ft.form_name(a) != ft.form_name(c)
    renamed, _ = ft.validate({**TEMPLATE, "id": "0123456789ab"})
    assert ft.form_name(renamed) == ft.form_name(a), "id шаблона не входит в имя формы"


def test_the_library_saves_loads_replaces_and_deletes_locally(tmp_path):
    saved = _saved()
    assert ft.library_path().parent == tmp_path / "acl" and ft.load() == [saved]
    row = ft.listing()["rows"][0]
    again = ft.save({**TEMPLATE, "id": saved["id"], "headline": "Changed", "_expected_revision": row["revision"]})
    assert again["id"] == saved["id"] and [t["headline"] for t in ft.load()] == ["Changed"]
    with pytest.raises(ft.TemplateError, match="already exists"):
        ft.save(TEMPLATE)
    assert ft.get(saved["id"])["headline"] == "Changed"
    assert ft.delete(saved["id"]) is True and ft.delete(saved["id"]) is False and ft.load() == []


def test_a_broken_library_file_does_not_break_the_page():
    ft.library_path().parent.mkdir(parents=True, exist_ok=True)
    ft.library_path().write_text("{not json", encoding="utf-8")
    assert ft.load() == []


# ───────────────────────── политика: Python ─────────────────────────

BASE = ft.form_params(ft.validate(TEMPLATE)[0])


def _p(**over):
    out = copy.deepcopy(BASE)
    for k, v in over.items():
        if v is None:
            out.pop(k, None)
        else:
            out[k] = v
    return out


def _custom_q(**extra):
    return {"type": "CUSTOM", "key": "q_custom", "label": "Which product?", **extra}


def _custom_with_option_key(key):
    return _custom_q(options=[{"key": "o1", "value": "Low", key: True}])


#: Условия Lead Ads/статус внутри `questions`: вопрос CUSTOM, вариант ответа, строка JSON.
NESTED_TERMS = []
for _key in ("tos_accepted", "terms", "status"):
    NESTED_TERMS += [
        (f"nested_{_key}_custom_question", _p(questions=[_custom_q(**{_key: True})])),
        (f"nested_{_key}_option", _p(questions=[_custom_with_option_key(_key)])),
        (f"nested_{_key}_questions_json_string",
         {**BASE, "questions": json.dumps([_custom_q(**{_key: True})])}),
        (f"nested_{_key}_option_json_string",
         {**BASE, "questions": json.dumps([_custom_with_option_key(_key)])}),
    ]


def _deep(path, key, value):
    out = copy.deepcopy(BASE)
    target = out
    for step in path:
        target = target[step]
    target[key] = value
    return out


#: Одна таблица на ОБЕ линии: (имя, страница пути, ребро пути, параметры, разрешено).
FORM_MATRIX = [
    ("ok", PAGE, "leadgen_forms", BASE, True),
    ("ok_json_strings", PAGE, "leadgen_forms", {**BASE, "questions": json.dumps(BASE["questions"]),
                                                "privacy_policy": json.dumps(BASE["privacy_policy"])}, True),
    ("ok_follow_up_https", PAGE, "leadgen_forms", _p(follow_up_action_url="https://example.com/after"), True),
    ("ok_settings_bool", PAGE, "leadgen_forms",
     _p(is_optimized_for_quality=True, block_display_for_non_targeted_viewer=True), True),
    ("ok_first_last_name", PAGE, "leadgen_forms",
     _p(questions=[{"type": "FIRST_NAME"}, {"type": "LAST_NAME"}, {"type": "PHONE"}]), True),
    ("setting_string_bool", PAGE, "leadgen_forms", _p(is_optimized_for_quality="false"), False),
    ("setting_number_bool", PAGE, "leadgen_forms", _p(block_display_for_non_targeted_viewer=1), False),
    ("follow_up_too_long", PAGE, "leadgen_forms", _p(follow_up_action_url="https://example.com/" + "a" * 1100), False),
    ("first_name_with_label", PAGE, "leadgen_forms", _p(questions=[{"type": "FIRST_NAME", "label": "x"}]), False),
    ("tos_top_level", PAGE, "leadgen_forms", _p(tos_accepted=True), False),
    ("tos_false_too", PAGE, "leadgen_forms", _p(tos_accepted=False), False),
    ("tos_upper", PAGE, "leadgen_forms", _p(TOS_ACCEPTED=True), False),
    ("leadgen_tos_key", PAGE, "leadgen_forms", _p(leadgen_tos_accepted=True), False),
    ("tos_nested_privacy", PAGE, "leadgen_forms", _deep(["privacy_policy"], "tos_accepted", True), False),
    ("tos_nested_question", PAGE, "leadgen_forms", _deep(["questions", 0], "tos_accepted", True), False),
    ("tos_in_json_string", PAGE, "leadgen_forms",
     {**BASE, "privacy_policy": json.dumps({**BASE["privacy_policy"], "tos_accepted": True})}, False),
    ("terms_key", PAGE, "leadgen_forms", _p(terms_accepted=True), False),
    ("extra_key_status", PAGE, "leadgen_forms", _p(status="ACTIVE"), False),
    ("extra_key_token", PAGE, "leadgen_forms", _p(access_token="EAAB" + "x" * 30), False),
    ("extra_key_organic", PAGE, "leadgen_forms", _p(allow_organic_lead_retrieval=True), False),
    ("extra_key_cover", PAGE, "leadgen_forms", _p(cover_photo="123"), False),
    ("follow_up_http", PAGE, "leadgen_forms", _p(follow_up_action_url="http://example.com"), False),
    ("privacy_http", PAGE, "leadgen_forms", _deep(["privacy_policy"], "url", "http://example.com/p"), False),
    ("privacy_extra_key", PAGE, "leadgen_forms", _deep(["privacy_policy"], "note", "x"), False),
    ("name_without_label", PAGE, "leadgen_forms", _p(name="Demo form"), False),
    ("name_wrong_label", PAGE, "leadgen_forms", _p(name="Demo form · ACL XYZ123"), False),
    ("no_questions", PAGE, "leadgen_forms", _p(questions=[]), False),
    ("question_type_other", PAGE, "leadgen_forms", _p(questions=[{"type": "DATE_TIME"}]), False),
    ("standard_question_with_label", PAGE, "leadgen_forms", _p(questions=[{"type": "EMAIL", "label": "x"}]), False),
    ("custom_without_label", PAGE, "leadgen_forms", _p(questions=[{"type": "CUSTOM", "key": "k"}]), False),
    ("thank_you_button_other", PAGE, "leadgen_forms",
     _deep(["thank_you_page"], "button_type", "CALL_BUSINESS"), False),
    ("thank_you_site_http", PAGE, "leadgen_forms", _deep(["thank_you_page"], "website_url", "http://x.example.com"), False),
    ("locale_lower", PAGE, "leadgen_forms", _p(locale="en_us"), False),
    ("no_locale", PAGE, "leadgen_forms", _p(locale=None), False),
    *[(n, PAGE, "leadgen_forms", prm, False) for n, prm in NESTED_TERMS],
    ("ok_thank_you_no_link_none", PAGE, "leadgen_forms", BASE, True),
    # другие рёбра и чужие Страницы
    ("page_feed", PAGE, "feed", BASE, False),
    ("page_tos_edge", PAGE, "leadgen_tos", BASE, False),
    ("page_forms_sub", PAGE, "leadgen_forms/7000001", BASE, False),
    ("page_subscribed_apps", PAGE, "subscribed_apps", BASE, False),
    ("other_page", "999999", "leadgen_forms", BASE, False),
    ("act_edge", ACT, "leadgen_forms", BASE, False),
]
FORM_IDS = [m[0] for m in FORM_MATRIX]


@pytest.mark.parametrize("name,page,edge,params,allowed", FORM_MATRIX, ids=FORM_IDS)
def test_form_policy_python(name, page, edge, params, allowed):
    path = f"/{page}/{edge}"
    if allowed:
        assert wp.check_form_create(PAGE, "POST", path, params, pinned_page=PAGE) == "leadgen_forms"
    else:
        with pytest.raises(wp.WriteRefused):
            wp.check_form_create(PAGE, "POST", path, params, pinned_page=PAGE)


@pytest.mark.parametrize("name,params", NESTED_TERMS, ids=[n for n, _ in NESTED_TERMS])
def test_terms_nested_in_questions_are_refused_by_the_terms_guard_not_by_the_allowlist(name, params):
    """Сообщение именно про условия: вложенный список ключей не должен маскировать результат."""
    with pytest.raises(wp.WriteRefused, match="never accepts Lead Ads terms"):
        wp.check_form_create(PAGE, "POST", f"/{PAGE}/leadgen_forms", params, pinned_page=PAGE)


def test_the_page_must_be_the_pinned_page_and_unpinned_means_no_form_writes():
    path = f"/{PAGE}/leadgen_forms"
    with pytest.raises(wp.WriteRefused, match="not the Page of the plan"):
        wp.check_form_create(PAGE, "POST", path, BASE, pinned_page="999999")
    with pytest.raises(wp.WriteRefused, match="no Page is pinned"):
        wp.check_form_create(PAGE, "POST", path, BASE)
    with pytest.raises(wp.WriteRefused, match="no Page is pinned"):
        wp.check_form_create(PAGE, "POST", path, BASE, pinned_page="")
    assert wp.policy_for_form_create(PAGE)["page"] == PAGE and wp.policy_for_form_create()["page"] == ""
    assert wp.policy_for_page(ACT, PAGE)["form_create"]["page"] == PAGE


def test_form_policy_never_deletes_never_sends_files_and_needs_the_page():
    with pytest.raises(wp.WriteRefused, match="DELETE"):
        wp.check_form_create(PAGE, "DELETE", f"/{PAGE}/leadgen_forms", BASE, pinned_page=PAGE)
    with pytest.raises(wp.WriteRefused, match="files"):
        wp.check_form_create(PAGE, "POST", f"/{PAGE}/leadgen_forms", BASE, {"source": b"x"}, pinned_page=PAGE)
    with pytest.raises(wp.WriteRefused, match="Page"):
        wp.check_form_create("", "POST", "//leadgen_forms", BASE, pinned_page=PAGE)


def test_the_two_policy_chains_do_not_overlap():
    """Путь формы не годится цепочке act_X, а путь act_X - цепочке форм."""
    with pytest.raises(wp.WriteRefused):
        wp.check(ACT, "POST", f"/{PAGE}/leadgen_forms", BASE)
    with pytest.raises(wp.WriteRefused):
        wp.check(ACT, "POST", f"/{ACT}/leadgen_forms", BASE)
    with pytest.raises(wp.WriteRefused):
        wp.check_form_create(PAGE, "POST", f"/{ACT}/campaigns", {"name": "x · ACL abcdef"}, pinned_page=PAGE)
    assert "tos_accepted" in wp.FORBIDDEN_ANYWHERE and "tos_accepted" not in wp.FORM_KEYS
    assert wp.FORM_KEYS == frozenset({"name", "questions", "privacy_policy", "thank_you_page", "context_card",
                                      "question_page_custom_headline", "locale", "follow_up_action_url",
                                      "is_optimized_for_quality", "block_display_for_non_targeted_viewer"})


def test_the_policy_does_not_mutate_the_callers_params():
    params = {**BASE, "questions": json.dumps(BASE["questions"])}
    before = copy.deepcopy(params)
    wp.check_form_create(PAGE, "POST", f"/{PAGE}/leadgen_forms", params, pinned_page=PAGE)
    assert params == before


# ───────────────────────── политика: помощник страницы (Node) ─────────────────────────

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
    const spec = { method: 'POST', path: '/' + m.page + '/' + m.edge, params: m.params, form_page: PAGEV };
    out[m.name] = await h.call(spec);
  }
  out.delete = await h.call({ method: 'DELETE', path: '/' + PAGEV + '/leadgen_forms', params: {}, form_page: PAGEV });
  out.no_form_page = await h.call({ method: 'POST', path: '/' + PAGEV + '/leadgen_forms', params: MATRIX[0].params });
  out.files = await h.call({ method: 'POST', path: '/' + PAGEV + '/leadgen_forms', params: MATRIX[0].params,
                             form_page: PAGEV, files: { source: 'x' } });
  out.calls = calls.length;
  process.stdout.write(JSON.stringify(out));
})();
"""


def _node_form_matrix(policy=None) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    name = "__acl_graph_form_create"
    matrix = [{"name": n, "page": p, "edge": e, "params": prm} for n, p, e, prm, _a in FORM_MATRIX]
    script = (_NODE.replace("TOKENV", json.dumps("EAAB" + "Zx9" * 20)).replace("NAME", json.dumps(name))
              .replace("PAGEV", json.dumps(PAGE)).replace("MATRIX", "(" + json.dumps(matrix) + ")")
              .replace("HELPER", json.dumps(tc.helper_source(name, policy or wp.policy_for_page(ACT, PAGE)))))
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(script)
        path = fh.name
    try:
        done = subprocess.run([node, path], capture_output=True, text=True, encoding="utf-8", timeout=60)
    finally:
        Path(path).unlink(missing_ok=True)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_form_policy_page_helper_matches_python_on_the_whole_matrix():
    out = _node_form_matrix()
    for name, _p_, _e, _params, allowed in FORM_MATRIX:
        got = out[name]
        if allowed:
            assert got.get("body") == {"id": "5"}, (name, got)
        else:
            assert got.get("error") == "write_refused", (name, got)
    assert out["calls"] == sum(1 for m in FORM_MATRIX if m[4]), "в Meta ушло не то число записей"
    assert out["delete"] == {"error": "read_only"}
    assert out["no_form_page"].get("error") == "write_refused"
    assert out["files"].get("error") == "write_refused"


def test_the_page_helper_names_the_nested_terms_refusal():
    out = _node_form_matrix()
    for name, _params in NESTED_TERMS:
        assert out[name] == {"error": "write_refused", "why": "form_forbidden"}, name


def test_the_page_helper_compares_with_the_pinned_page_not_with_the_callers_claim():
    other = _node_form_matrix(wp.policy_for_page(ACT, "999999"))
    assert other["ok"] == {"error": "write_refused", "why": "form_page"} and other["calls"] == 0
    unpinned = _node_form_matrix(wp.policy_for_page(ACT))
    assert unpinned["ok"] == {"error": "write_refused", "why": "form_no_page"} and unpinned["calls"] == 0


def test_the_page_helper_names_the_terms_refusal_and_only_when_it_has_a_policy():
    out = _node_form_matrix()
    assert out["tos_top_level"] == {"error": "write_refused", "why": "form_forbidden"}
    assert out["tos_in_json_string"] == {"error": "write_refused", "why": "form_forbidden"}
    # Помощник без цепочки форм (прежние установки) ничего Странице не пишет.
    old = wp.policy_for_page(ACT)
    old.pop("form_create")
    refused = _node_form_matrix(old)
    assert refused["ok"] == {"error": "write_refused", "why": "form_policy"} and refused["calls"] == 0


def test_the_write_path_runs_both_layers_for_a_form():
    """InPageGraph.write: Python-проверка и спецификация для помощника несут Страницу плана."""
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
    t = tc.InPageGraph(tab, allow_writes=True, write_act=ACT, write_page=PAGE)
    with pytest.raises(wp.WriteRefused):                      # без Страницы плана путь формы - не act_X
        t.write(f"/{PAGE}/leadgen_forms", BASE)
    with pytest.raises(wp.WriteRefused):
        t.write(f"/{PAGE}/leadgen_forms", _p(tos_accepted=True), form_page=PAGE)
    assert not [e for e in tab.exprs if ".call(" in e], "отвергнутое дошло до страницы"
    t.write(f"/{PAGE}/leadgen_forms", BASE, form_page=PAGE)
    sent = [e for e in tab.exprs if ".call(" in e]
    assert len(sent) == 1 and f'"form_page": "{PAGE}"' in sent[0]


def test_the_session_pins_the_page_once_and_refuses_form_writes_without_it():
    tab = type("T", (), {"create_isolated_world": lambda self, n: 7,
                         "evaluate_in": lambda self, ctx, expr, timeout=60.0: (
                             {"kind": "ads_manager", "has_token": True} if ".state()" in expr else None)})()
    t = tc.InPageGraph(tab, allow_writes=True, write_act=ACT)
    assert t.write_page is None
    with pytest.raises(wp.WriteRefused, match="no Page is pinned"):
        t.write(f"/{PAGE}/leadgen_forms", BASE, form_page=PAGE)
    t.pin_page(PAGE)
    t.pin_page(PAGE)                                          # то же значение - можно
    with pytest.raises(ValueError, match="different Page"):
        t.pin_page("999999")
    with pytest.raises(wp.WriteRefused, match="not the Page of the plan"):
        t.write("/999999/leadgen_forms", BASE, form_page="999999")
    assert t._write_policy["form_create"]["page"] == PAGE
    with pytest.raises(ValueError):
        tc.InPageGraph(tab, allow_writes=True, write_act=ACT, write_page="abc")


def test_pinning_waits_for_the_token_in_the_fresh_world():
    """После сброса мира токен мог выпасть из буфера Resource Timing: ждём state(), затем probe()."""
    class Tab:
        def __init__(self):
            self.calls = []
            self.worlds = 0

        def create_isolated_world(self, name):
            self.worlds += 1
            return 7 + self.worlds

        def evaluate_in(self, ctx, expr, timeout=60.0):
            if expr.lstrip().startswith("(() =>"):
                return None                                     # установка помощника
            self.calls.append(expr)
            if ".probe()" in expr:
                return {"kind": "ads_manager", "has_token": True}
            return {"kind": "ads_manager", "has_token": False}  # state(): токена в новом мире нет

    tab = Tab()
    ticks = iter(range(0, 10000, 20))
    t = tc.InPageGraph(tab, allow_writes=True, write_act=ACT, sleep=lambda s: None,
                       clock=lambda: float(next(ticks)))
    state = t.pin_page(PAGE, timeout=30.0)
    assert state["has_token"] is True
    assert any(".state()" in c for c in tab.calls) and any(".probe()" in c for c in tab.calls)
    assert tab.worlds >= 1 and t.write_page == PAGE


def test_pinning_reports_when_the_token_never_comes_back():
    class Tab:
        def create_isolated_world(self, name):
            return 7

        def evaluate_in(self, ctx, expr, timeout=60.0):
            return None if expr.lstrip().startswith("(() =>") else {"kind": "other", "has_token": False}

    ticks = iter(range(0, 10000, 20))
    t = tc.InPageGraph(Tab(), allow_writes=True, write_act=ACT, sleep=lambda s: None,
                       clock=lambda: float(next(ticks)))
    assert t.pin_page(PAGE, timeout=30.0)["has_token"] is False


def test_the_pending_marker_is_written_atomically_and_joined_with_a_pipe(tmp_path, monkeypatch):
    path = fc.pending_path()
    calls = []
    real = fc.os.replace
    monkeypatch.setattr(fc.os, "replace", lambda a, b: (calls.append((Path(a).name, Path(b).name)), real(a, b))[1])
    fc.record_pending(ACT, PAGE, "Demo · ACL abcdef")
    assert calls == [("pending_forms.json.tmp", "pending_forms.json")], "запись идёт через os.replace"
    assert not list(path.parent.glob("*.tmp"))
    assert list(json.loads(path.read_text(encoding="utf-8"))) == [f"{ACT}|{PAGE}|Demo · ACL abcdef"]
    # Сбой замены: прежний файл цел, временного файла не осталось, ошибка видна.
    before = path.read_text(encoding="utf-8")

    def boom(a, b):
        raise OSError("disk")
    monkeypatch.setattr(fc.os, "replace", boom)
    with pytest.raises(OSError):
        fc.record_pending(ACT, PAGE, "Other · ACL abcdef")
    assert path.read_text(encoding="utf-8") == before and not list(path.parent.glob("*.tmp"))


def test_no_form_is_created_when_the_local_marker_cannot_be_saved(tmp_path, monkeypatch):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a folder", encoding="utf-8")
    monkeypatch.setattr(fc, "pending_path", lambda: blocker / "pending_forms.json")
    tid = _saved()["id"]
    meta = FormsMeta()
    out, _c = _run(meta, _tpl_draft(tmp_path, [tid]))
    assert out.stop_kind == "form_state" and "Couldn't record the form state locally" in out.error
    assert "not creating a form" in out.error and not meta.form_posts


def test_a_no_link_thank_you_page_sends_button_type_none_and_the_readback_accepts_it():
    clean, _ = ft.validate(TEMPLATE)
    assert ft.form_params(clean)["thank_you_page"]["button_type"] == "NONE"
    linked, _ = ft.validate({**TEMPLATE, "website_url": "https://example.com/x"})
    assert ft.form_params(linked)["thank_you_page"]["button_type"] == "VIEW_WEBSITE"
    row = {"id": "1", "name": ft.form_name(clean), "status": "ACTIVE", "page": {"id": PAGE},
           "thank_you_page": {"button_type": "NONE"},
           "questions": {"data": ft.expected_questions(clean)}}
    assert fc.compare_form(clean, PAGE, row) == []
    bad = copy.deepcopy(BASE)
    bad["thank_you_page"]["button_type"] = "VIEW_ON_FACEBOOK"
    with pytest.raises(wp.WriteRefused, match="button_type"):
        wp.check_form_create(PAGE, "POST", f"/{PAGE}/leadgen_forms", bad, pinned_page=PAGE)


# ───────────────────────── создание: один раз, до креатива, со сверкой ─────────────────────────

def test_the_form_is_created_once_before_its_creative_and_read_back(tmp_path):
    tid = _saved()["id"]
    meta = FormsMeta()
    out, client = _run(meta, _tpl_draft(tmp_path, [tid, tid]))
    assert out.error == "", out.error
    assert len(meta.form_posts) == 1, "две ad с одним шаблоном - одна форма"
    path, params = meta.form_posts[0]
    assert path == f"/{PAGE}/leadgen_forms"
    assert set(params) <= wp.FORM_KEYS and "tos" not in json.dumps(params).lower()
    edges = _edges(meta)
    assert edges.index("leadgen_forms") < edges.index("adcreatives"), "форма создаётся до креатива, которому нужна"
    assert edges.count("leadgen_forms") == 1
    fid = out.created["forms"][0]
    for creative in [o for o in meta.objects.values() if o["_kind"] == "creative"]:
        spec = creative["object_story_spec"]
        data = spec.get("link_data") or spec.get("video_data")
        assert data["call_to_action"]["value"] == {"lead_gen_form_id": fid}
    assert out.readback["ok"] is True, out.mismatches
    assert any("read back, ACTIVE, on the Page" in s for s in out.steps)
    assert "forms" not in out.reused


def test_a_rerun_reuses_the_form_and_never_creates_a_duplicate(tmp_path):
    tid = _saved()["id"]
    meta = FormsMeta()
    first, _ = _run(meta, _tpl_draft(tmp_path, [tid]))
    assert first.error == "" and len(meta.form_posts) == 1
    forms_before = len(meta.rows)
    # Тот же запуск ещё раз: объекты найдены, форм не прибавилось.
    again, _ = _run(meta, _tpl_draft(tmp_path, [tid]))
    assert again.error == "" and len(meta.form_posts) == 1 and len(meta.rows) == forms_before
    assert again.readback["ok"] is True, again.mismatches
    # Другая кампания с тем же шаблоном: форма найдена по точному имени и использована.
    other, _ = _run(meta, _tpl_draft(tmp_path, [tid], name="MX Leads 2"))
    assert other.error == "" and len(meta.form_posts) == 1 and len(meta.rows) == forms_before
    assert other.reused["forms"] == [first.created["forms"][0]]
    assert "forms" not in other.created
    assert other.readback["ok"] is True, other.mismatches


def test_a_changed_template_gets_a_new_form_and_the_old_one_stays():
    # Содержание поменялось -> другой хэш -> другое имя -> новая форма; старую никто не трогает.
    a, _ = ft.validate(TEMPLATE)
    b, _ = ft.validate({**TEMPLATE, "headline": "Another headline"})
    assert ft.form_name(a) != ft.form_name(b)


def test_a_timeout_is_reconciled_by_reading_the_forms_list_without_a_retry(tmp_path):
    tid = _saved()["id"]
    meta = FormsMeta()
    meta.fail_next["leadgen_forms"] = "lost"            # форма создана, ответ потерян
    out, client = _run(meta, _tpl_draft(tmp_path, [tid]))
    assert out.error == "", out.error
    assert len(meta.form_posts) == 1, "слепого повтора нет"
    assert any("result unknown — reconciling by readback" in s for s in out.steps)
    assert any("reconciled — found by readback" in s for s in out.steps)
    assert out.readback["ok"] is True, out.mismatches
    assert client.unreconciled is None


def test_a_timeout_with_nothing_found_stops_and_blocks_any_blind_retry(tmp_path):
    tid = _saved()["id"]
    meta = FormsMeta()
    meta.fail_next["leadgen_forms"] = "unsent"          # формы нет, ответ потерян
    creator, client = _creator(meta)
    out = creator.run(cr.plan_from_draft(_tpl_draft(tmp_path, [tid])))
    assert out.stop_kind == "unknown" and "nothing was found by readback" in out.error
    assert len(meta.form_posts) == 1 and "adcreatives" not in _edges(meta)
    assert client.unreconciled == f"/{PAGE}/leadgen_forms"
    with pytest.raises(g.WritesBlocked):                # клиент не пишет, пока не сверено
        client.post(f"/{PAGE}/leadgen_forms", BASE, form_page=PAGE)
    assert len(meta.form_posts) == 1


def test_a_rerun_after_an_unknown_write_waits_instead_of_creating_a_second_form(tmp_path, monkeypatch):
    tid = _saved()["id"]
    meta = FormsMeta()
    meta.fail_next["leadgen_forms"] = "unsent"
    first, _ = _run(meta, _tpl_draft(tmp_path, [tid]))
    assert first.stop_kind == "unknown" and len(meta.form_posts) == 1
    name = ft.form_name(ft.validate(TEMPLATE)[0])
    assert fc.is_pending(ACT, PAGE, name)
    # Список всё ещё без формы, прошло мало времени: повторный запуск стоит.
    again, _ = _run(meta, _tpl_draft(tmp_path, [tid]))
    assert "may still be appearing at Meta" in again.error and "wait a few minutes" in again.error
    assert len(meta.form_posts) == 1, "вторая форма не создана"
    # Check plan говорит то же и ничего не пишет.
    creator, _c = _creator(meta, writes=False)
    plan_out = creator.plan_only(cr.plan_from_draft(_tpl_draft(tmp_path, [tid])))
    assert "may still be appearing at Meta" in plan_out.error and len(meta.form_posts) == 1
    # Через 15 минут метка устарела: форму можно создать.
    real = fc.time.time()
    monkeypatch.setattr(fc.time, "time", lambda: real + fc.PENDING_WINDOW_S + 5)
    late, _ = _run(meta, _tpl_draft(tmp_path, [tid]))
    assert late.error == "" and len(meta.form_posts) == 2
    assert not fc.is_pending(ACT, PAGE, name)


def test_the_pending_marker_is_dropped_when_the_form_shows_up(tmp_path):
    tid = _saved()["id"]
    meta = FormsMeta()
    meta.fail_next["leadgen_forms"] = "unsent"
    _run(meta, _tpl_draft(tmp_path, [tid]))
    name = ft.form_name(ft.validate(TEMPLATE)[0])
    assert fc.is_pending(ACT, PAGE, name)
    seeded = _seeded()
    out, _ = _run(seeded, _tpl_draft(tmp_path, [tid]))
    assert out.error == "" and not seeded.form_posts and not fc.is_pending(ACT, PAGE, name)


def test_a_form_that_reads_back_wrong_stops_before_the_creative(tmp_path):
    tid = _saved()["id"]
    meta = FormsMeta()
    meta.tamper_questions = True
    out, _ = _run(meta, _tpl_draft(tmp_path, [tid]))
    assert out.stop_kind == "form_mismatch" and "read back with differences" in out.error
    assert "adcreatives" not in _edges(meta)


def test_a_form_of_another_page_or_not_active_fails_the_readback():
    clean, _ = ft.validate(TEMPLATE)
    good = {"id": "1", "name": ft.form_name(clean), "status": "ACTIVE", "page": {"id": PAGE},
            "questions": {"data": ft.expected_questions(clean)}}
    assert fc.compare_form(clean, PAGE, good) == []
    assert fc.compare_form(clean, PAGE, {**good, "page": {"id": "999"}}) == ["the form does not belong to this Page"]
    assert any("status" in x for x in fc.compare_form(clean, PAGE, {**good, "status": "ARCHIVED"}))
    wrong = copy.deepcopy(good)
    wrong["questions"]["data"][2]["label"] = "Other"
    assert fc.compare_form(clean, PAGE, wrong) == ["question 3: text"]


def test_an_archived_form_with_our_name_is_not_reused_and_not_duplicated(tmp_path):
    tid = _saved()["id"]
    clean, _ = ft.validate(TEMPLATE)
    meta = FormsMeta(rows=[{"id": "7000050", "name": ft.form_name(clean), "status": "ARCHIVED", "locale": "en_US"}])
    out, client = _run(meta, _tpl_draft(tmp_path, [tid]))
    assert out.stop_kind == "plan" and "archived" in out.error and "Restore it in Meta" in out.error
    assert meta.posts == [] and client.writes == 0


# ───────────────────────── Check plan: ни одной записи ─────────────────────────

def test_check_plan_writes_nothing_and_says_which_form_will_be_created(tmp_path):
    tid = _saved()["id"]
    meta = FormsMeta()
    creator, client = _creator(meta, writes=False)
    out = creator.plan_only(cr.plan_from_draft(_tpl_draft(tmp_path, [tid, tid])))
    assert out.error == "", out.error
    assert meta.posts == [] and client.writes == 0
    summary = out.plan["summary"]
    clean, _ = ft.validate(TEMPLATE)
    name = ft.form_name(clean)
    assert summary["forms"] == [{"template": "Demo form", "form_name": name, "action": "create", "form_id": ""}]
    notes = [ad["form_note"] for s in summary["ad_sets"] for ad in s["ads"]]
    assert notes == [f"form will be created: {name}"] * 2
    assert any(f"form will be created: {name}" in s for s in out.steps)
    # Креатив в плане не несёт выдуманного id.
    creative = out.plan["ad_sets"][0]["ads"][0]["creative"]
    assert "<form to be created>" in json.dumps(creative)


def test_check_plan_says_when_the_form_already_exists(tmp_path):
    tid = _saved()["id"]
    meta = _seeded()
    creator, client = _creator(meta, writes=False)
    out = creator.plan_only(cr.plan_from_draft(_tpl_draft(tmp_path, [tid])))
    assert out.error == "" and meta.posts == []
    item = out.plan["summary"]["ad_sets"][0]["ads"][0]
    assert item["form_action"] == "reuse" and item["form_id"] == "7000050"
    assert item["form_note"].startswith("form found on the Page, reused:")


# ───────────────────────── условия Lead Ads и права токена ─────────────────────────

def test_a_page_without_accepted_terms_stops_before_any_write_with_a_clear_text(tmp_path):
    tid = _saved()["id"]
    meta = FormsMeta(rows=[], tos=False)
    out, client = _run(meta, _tpl_draft(tmp_path, [tid]))
    assert out.stop_kind == "plan"
    assert "has not accepted the Lead Ads terms" in out.error and "Accept them in Meta" in out.error
    assert "the robot never accepts" in out.error
    assert meta.posts == [] and client.writes == 0


def test_terms_unaccepted_with_other_forms_present_also_stops_before_any_write(tmp_path):
    tid = _saved()["id"]
    meta = FormsMeta(tos=False)                          # на Странице есть чужие формы, условия не приняты
    out, client = _run(meta, _tpl_draft(tmp_path, [tid]))
    assert out.stop_kind == "plan" and "Accept the terms for this Page in Meta" in out.error
    assert meta.posts == [] and client.writes == 0


def test_a_reused_form_does_not_need_the_terms_check(tmp_path):
    tid = _saved()["id"]
    meta = _seeded(tos=False)
    out, _ = _run(meta, _tpl_draft(tmp_path, [tid]))
    assert out.error == "" and meta.form_posts == [] and out.reused["forms"] == ["7000050"]
    assert out.readback["ok"] is True, out.mismatches


def test_meta_refusing_the_form_gives_a_clear_text_and_never_asks_for_other_tokens(tmp_path):
    tid = _saved()["id"]
    meta = FormsMeta()
    meta.refuse = {"code": 200, "message": "(#200) Requires pages_manage_ads permission to manage the object"}
    out, _ = _run(meta, _tpl_draft(tmp_path, [tid]))
    assert out.stop_kind == "form_refused"
    assert "Use existing form" in out.error and "does not ask for or store other tokens" in out.error
    assert "adcreatives" not in _edges(meta) and len(meta.form_posts) == 1


def test_meta_refusing_for_the_terms_names_the_terms(tmp_path):
    tid = _saved()["id"]
    meta = FormsMeta()
    meta.refuse = {"code": 100, "message": "You must accept the Lead Ads Terms of Service for this Page"}
    out, _ = _run(meta, _tpl_draft(tmp_path, [tid]))
    assert out.stop_kind == "form_refused" and "has not accepted the Lead Ads terms" in out.error
    assert "never accepts" in out.error


def test_the_missing_permission_is_only_a_warning_because_the_token_list_may_lie(tmp_path):
    tid = _saved()["id"]
    meta = FormsMeta()
    meta.permissions = [{"permission": "ads_management", "status": "granted"}]
    out, _ = _run(meta, _tpl_draft(tmp_path, [tid]))
    assert out.error == ""
    assert any("pages_manage_ads" in w for w in out.warnings)
    meta2 = FormsMeta()
    meta2.permissions = [{"permission": "pages_manage_ads", "status": "granted"}]
    out2, _ = _run(meta2, _tpl_draft(tmp_path, [tid]))
    assert out2.error == "" and not any("pages_manage_ads" in w for w in out2.warnings)


# ───────────────────────── план из черновика ─────────────────────────

def test_a_missing_or_invalid_template_and_a_double_choice_stop_the_plan(tmp_path):
    d = _tpl_draft(tmp_path, ["0123456789ab"])
    with pytest.raises(cr.PlanError, match="not found in the team Forms library"):
        cr.plan_from_draft(d)
    tid = _saved()["id"]
    d2 = _tpl_draft(tmp_path, [tid])
    d2.ad_sets[0].ads[0].lead_form_id = "7000001"
    with pytest.raises(cr.PlanError, match="either an existing form or a template"):
        cr.plan_from_draft(d2)
    bad = copy.deepcopy(ft.validate(TEMPLATE)[0])
    bad.update(id="aaaaaaaaaaaa", privacy_url="http://x.example.com")
    with pytest.raises(cr.PlanError, match="Privacy policy URL must start with https://"):
        cr.plan_from_draft(_tpl_draft(tmp_path, ["aaaaaaaaaaaa"]), template_loader=lambda _i: bad)


def test_a_saved_template_with_a_legacy_locale_still_builds_a_plan(tmp_path):
    # ACL-2053: язык старого шаблона вне закрытого списка не должен ломать публикацию.
    legacy = {**copy.deepcopy(ft.validate(TEMPLATE)[0]), "id": "bbbbbbbbbbbb", "locale": "ES_MX"}
    plan = cr.plan_from_draft(_tpl_draft(tmp_path, ["bbbbbbbbbbbb"]), template_loader=lambda _i: legacy)
    assert plan.ad_sets[0].ads[0].form_template["locale"] == "ES_MX"


def test_a_website_ad_set_ignores_a_stray_template(tmp_path):
    tid = _saved()["id"]
    d = _draft(tmp_path, ads=1)
    d.ad_sets[0].ads[0].lead_form_template = tid
    plan = cr.plan_from_draft(d)
    assert plan.ad_sets[0].ads[0].form_template is None


def test_the_template_ids_travel_from_the_web_form_to_the_draft():
    from src.fb_ads_rpa.build_draft import slots_to_campaign_drafts
    from src.web import app as web

    _c, _a, slots = web._parse_build_upload_form(_fields(
        camp_0_destination="instant_form", camp_0_ad_0_form_template=" 0123456789ab ",
        camp_0_ad_1_form_id="7000001"))
    assert slots[0]["lead_form_template"] == "0123456789ab" and "lead_form_template" not in slots[1]
    d, = slots_to_campaign_drafts(slots, account_label="Acc", country="MX")
    assert [a.lead_form_template for a in d.ad_sets[0].ads] == ["0123456789ab", ""]
    assert d.ad_sets[0].ads[0].to_preview_dict()["lead_form_template"] == "0123456789ab"
    _c, _a, plain = web._parse_build_upload_form(_fields())
    assert all("lead_form_template" not in s for s in plain)
    stray = [{**plain[0], "lead_form_template": "0123456789ab"}]
    d3, = slots_to_campaign_drafts(stray, account_label="Acc", country="MX")
    assert d3.ad_sets[0].ads[0].lead_form_template == ""


# ───────────────────────── RPA-бэкенд и библиотека ─────────────────────────

class _Req:
    def __init__(self, fields=None, body=None):
        self._f, self._b = dict(fields or {}), body

    async def form(self, **_limits):  # read_form passes parse limits
        return self._f

    async def json(self):
        if self._b is None:
            raise ValueError("no body")
        return self._b


def test_the_rpa_backend_refuses_a_template_slot_out_loud(monkeypatch):
    from src.web import app as web

    monkeypatch.setenv("ACL_FB_BACKEND", "rpa")
    fields = _fields(camp_0_destination="instant_form", camp_0_ad_0_form_template="0123456789ab",
                     camp_0_ad_1_form_template="0123456789ab")
    slots = web._parse_build_upload_form(fields)[2]
    assert web._instant_form_refusal(slots).status_code == 409
    resp = asyncio.run(web.preview_publish(_Req(fields)))
    assert resp.status_code == 409 and json.loads(resp.body)["error"] == web.INSTANT_FORM_RPA_ERROR
    monkeypatch.setenv("ACL_FB_BACKEND", "api")
    assert web._instant_form_refusal(slots) is None


def test_the_library_endpoints_save_list_and_delete_locally():
    from src.web import app as web

    resp = asyncio.run(web.api_form_templates_save(_Req(body={**TEMPLATE, "privacy_url": "http://x.example.com"})))
    assert resp.status_code == 400 and "must start with https://" in json.loads(resp.body)["errors"][0]
    assert asyncio.run(web.api_form_templates_save(_Req())).status_code == 400
    resp = asyncio.run(web.api_form_templates_save(_Req(body=TEMPLATE)))
    saved = json.loads(resp.body)["template"]
    listed = json.loads(web.api_form_templates_list().body)
    assert [t["id"] for t in listed["templates"]] == [saved["id"]]
    assert json.loads(asyncio.run(web.api_form_templates_delete(_Req(body={"id": saved["id"]}))).body) == {"ok": True}
    assert json.loads(web.api_form_templates_list().body)["templates"] == []


def test_the_builder_page_has_both_paths_the_dialog_and_the_rpa_reason():
    page = (ROOT / "src" / "web" / "templates" / "report.html").read_text(encoding="utf-8")
    for text in ("Use existing form", "Create from template", "Create and save form", "Save and select",
                 "Back to ad", "API backend only: the robot creates forms through the Graph API",
                 "_form_template", "Privacy policy URL", "Completion screen"):
        assert text in page, text
    assert "tos_accepted" not in page and "leadgen_forms" not in page


def test_the_page_validates_a_template_exactly_like_the_server():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    cases = {"valid": TEMPLATE}
    for name, over, _text in BAD_TEMPLATES:
        if name != "unknown_contact":                   # страница молча отбрасывает неизвестное поле
            cases[name] = {**TEMPLATE, **over}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
        json.dump(cases, fh)
        path = fh.name
    try:
        done = subprocess.run([node, str(ROOT / "tests" / "form_templates_harness.cjs"),
                               str(ROOT / "src" / "web" / "templates" / "report.html"), path],
                              capture_output=True, text=True, encoding="utf-8", timeout=60)
    finally:
        Path(path).unlink(missing_ok=True)
    assert done.returncode == 0, done.stderr
    js = json.loads(done.stdout)
    for name, tpl in cases.items():
        assert js[name] == ft.validate(tpl)[1], name


def test_the_terms_walk_alone_refuses_a_nested_terms_key():
    """Вторая линия внутри цепочки: даже там, где вложенный список ключей пропустил бы, условия - отказ."""
    for bad in ({"a": {"tos_accepted": True}}, [{"x": [{"leadgen_tos_accepted": 1}]}], {"terms": 1}):
        with pytest.raises(wp.WriteRefused, match="never accepts Lead Ads terms"):
            wp._form_forbidden_walk(bad, "t")
    wp._form_forbidden_walk({"label": "Do you accept our offer?"}, "t")      # слова в значениях не ключи


def test_only_the_exact_name_is_reused_a_similar_name_is_not(tmp_path):
    tid = _saved()["id"]
    meta = FormsMeta(rows=[
        {"id": "7000060", "name": "Demo form · ACL aaaaaa", "status": "ACTIVE", "locale": "en_US"},     # другой хэш
        {"id": "7000061", "name": "Demo form", "status": "ACTIVE", "locale": "en_US"},                    # без метки
    ])
    out, _ = _run(meta, _tpl_draft(tmp_path, [tid]))
    assert out.error == "" and len(meta.form_posts) == 1
    assert out.created["forms"] and "forms" not in out.reused
