"""ACL-2053: шаблоны форм дотягиваются до формы, которую байер собрал руками.

Эталон - обезличенная копия живой формы D71 (`GET /{form_id}`), без реальных id.
Старые шаблоны (без новых полей) обязаны давать то же содержание запроса и тот же
хэш имени; новые поля - только по запросу и только через закрытую политику записи.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from src.fb_api import form_templates as ft
from src.fb_api import write_policy as wp
from tests.test_acl1776_create_forms import BASE, PAGE, TEMPLATE, _p  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "meta"
LIVE = json.loads((FIX / "acl2053_d71_form_sanitized.json").read_text(encoding="utf-8"))
LEGACY = json.loads((FIX / "acl2053_legacy_template_golden.json").read_text(encoding="utf-8"))
HTML = ROOT / "src" / "web" / "templates" / "report.html"


def _check(params):
    return wp.check_form_create(PAGE, "POST", f"/{PAGE}/leadgen_forms", params, pinned_page=PAGE)


def d71_template(**over):
    """Шаблон, равный форме D71: 2 обязательных вопроса с выбором ПЕРЕД контактами."""
    live_q = [q for q in LIVE["questions"] if q["type"] == "CUSTOM"]
    tpl = {
        "name": "Buyer form ARM SA",
        "headline": LIVE["question_page_custom_headline"],
        "intro_title": LIVE["context_card"]["title"],
        "intro": LIVE["context_card"]["content"][0],
        "contact_fields": ["first_name", "last_name", "phone", "email"],
        "questions": [{"label": q["label"], "type": "multiple_choice",
                       "options": [o["value"] for o in q["options"]]} for q in live_q],
        "questions_first": True,
        "privacy_url": LIVE["legal_content"]["privacy_policy"]["url"],
        "privacy_link_text": LIVE["legal_content"]["privacy_policy"]["link_text"],
        "thank_you_text": "",
        "follow_up_action_url": LIVE["follow_up_action_url"],
        "form_type": "more_volume",
        "locale": LIVE["locale"].upper(),
    }
    tpl.update(over)
    return tpl


# ───────────────────────── старые шаблоны не меняются ─────────────────────────

def test_a_legacy_template_keeps_its_content_hash_and_body():
    clean, errors = ft.validate(LEGACY["template"])
    assert errors == []
    assert clean == LEGACY["clean"], "новые поля не должны появляться у старого шаблона"
    assert ft.form_name(clean) == "Demo form · ACL 192c3c"
    params = ft.form_params(clean)
    extra = {k: params.pop(k) for k in ("is_optimized_for_quality", "block_display_for_non_targeted_viewer")}
    assert extra == {"is_optimized_for_quality": False, "block_display_for_non_targeted_viewer": True}
    assert params == LEGACY["params"], "остальное тело запроса - как было до ACL-2053"


def test_a_saved_legacy_contact_order_is_kept_as_saved():
    clean, _ = ft.validate({**TEMPLATE, "contact_fields": ["name", "email", "phone"]})
    assert clean["contact_fields"] == ["name", "email", "phone"]


# ───────────────────────── эталон D71 поле в поле ─────────────────────────

def test_the_d71_template_builds_the_same_form_field_by_field():
    clean, errors = ft.validate(d71_template())
    assert errors == []
    p = ft.form_params(clean)
    live = LIVE
    assert [q["type"] for q in p["questions"]] == [q["type"] for q in live["questions"]]
    for got, want in zip(p["questions"], live["questions"]):
        if want["type"] == "CUSTOM":
            assert got["label"] == want["label"]
            assert [o["value"] for o in got["options"]] == [o["value"] for o in want["options"]]
            assert all(o["key"] and o["value"] for o in got["options"])
        else:
            assert set(got) == {"type"}
    cc, lc = p["context_card"], live["context_card"]
    assert (cc["title"], cc["content"], cc["style"]) == (lc["title"], lc["content"], lc["style"])
    assert "cover_photo" not in cc and "cover_photo_id" not in cc
    assert p["question_page_custom_headline"] == live["question_page_custom_headline"]
    assert p["privacy_policy"] == live["legal_content"]["privacy_policy"]
    assert p["follow_up_action_url"] == live["follow_up_action_url"]
    assert p["is_optimized_for_quality"] == live["is_optimized_for_quality"] is False
    assert p["block_display_for_non_targeted_viewer"] == live["block_display_for_non_targeted_viewer"] is True
    assert p["locale"] == live["locale"].upper()
    assert "thank_you_page" not in p
    assert "allow_organic_lead" not in p            # Graph не принимает его при создании (см. CHANGELOG)
    assert _check(p) == "leadgen_forms"


def test_questions_follow_the_chosen_order():
    contact_first, _ = ft.validate(d71_template(questions_first=False))
    types = [q["type"] for q in ft.expected_questions(contact_first)]
    assert types == ["FIRST_NAME", "LAST_NAME", "PHONE", "EMAIL", "CUSTOM", "CUSTOM"]
    custom_first, _ = ft.validate(d71_template())
    assert [q["type"] for q in ft.expected_questions(custom_first)][:2] == ["CUSTOM", "CUSTOM"]
    keys = [q.get("key") for q in ft.expected_questions(custom_first) if q["type"] == "CUSTOM"]
    assert keys == ["question_1", "question_2"]


def test_the_default_order_is_contacts_first_and_default_flag_is_not_stored():
    clean, _ = ft.validate(TEMPLATE)
    assert "questions_first" not in clean
    assert [q["type"] for q in ft.expected_questions(clean)] == ["FULL_NAME", "EMAIL", "CUSTOM", "CUSTOM"]


# ───────────────────────── новые поля: проверка ─────────────────────────

NEW_BAD = [
    ("full_and_first", {"contact_fields": ["name", "first_name"]}, "full name"),
    ("full_and_last", {"contact_fields": ["name", "last_name", "email"]}, "full name"),
    ("follow_up_http", {"follow_up_action_url": "http://example.com/x"}, "Follow-up URL must start with https://"),
    ("follow_up_long", {"follow_up_action_url": "https://example.com/" + "a" * 2000}, "Follow-up URL is longer"),
    ("intro_title_long", {"intro_title": "x" * 61}, "Intro title is longer than 60"),
    ("form_type_bad", {"form_type": "fast"}, "Form type must be"),
    ("unknown_locale", {"locale": "XX_YY"}, "Unknown locale"),
    ("free_text_locale", {"locale": "english"}, "Unknown locale"),
]


@pytest.mark.parametrize("name,over,text", NEW_BAD, ids=[b[0] for b in NEW_BAD])
def test_new_field_validation(name, over, text):
    _c, errors = ft.validate(d71_template(**over))
    assert any(text in e for e in errors), errors


def test_thank_you_text_is_optional_only_with_a_follow_up_url():
    _c, errors = ft.validate(d71_template(follow_up_action_url=""))
    assert any("Thank-you screen text is required" in e for e in errors)
    clean, errors = ft.validate(d71_template(thank_you_text="Thanks"))
    assert errors == []
    assert ft.form_params(clean)["thank_you_page"]["body"] == "Thanks"


def test_form_type_higher_intent_and_locale_list():
    clean, errors = ft.validate(d71_template(form_type="higher_intent", locale="ar_ar"))
    assert errors == [] and clean["locale"] == "AR_AR"
    p = ft.form_params(clean)
    assert p["is_optimized_for_quality"] is True and p["locale"] == "AR_AR"
    assert "EN_US" in ft.LOCALES and ft.DEFAULT_LOCALE == "EN_US" and len(set(ft.LOCALES)) == len(ft.LOCALES)
    assert all(re.fullmatch(r"[A-Z]{2}_[A-Z]{2}", x) for x in ft.LOCALES)


def test_intro_title_defaults_to_the_headline_and_changes_the_name():
    base, _ = ft.validate({**TEMPLATE, "intro": "Hello"})
    assert ft.form_params(base)["context_card"]["title"] == base["headline"]
    other, _ = ft.validate({**TEMPLATE, "intro": "Hello", "intro_title": "Welcome"})
    assert ft.form_params(other)["context_card"]["title"] == "Welcome"
    assert ft.form_name(base) != ft.form_name(other)


def test_a_new_field_with_its_default_value_does_not_change_the_hash():
    a, _ = ft.validate(TEMPLATE)
    b, _ = ft.validate({**TEMPLATE, "questions_first": False, "form_type": "more_volume",
                        "intro_title": "", "follow_up_action_url": ""})
    assert a == b and ft.form_name(a) == ft.form_name(b)


# ───────────────────────── политика записи ─────────────────────────

POLICY_OK = [
    {"follow_up_action_url": "https://example.com/after"},
    {"is_optimized_for_quality": True},
    {"is_optimized_for_quality": False, "block_display_for_non_targeted_viewer": True},
    {"block_display_for_non_targeted_viewer": False},
]
POLICY_BAD = [
    {"follow_up_action_url": "http://example.com"},
    {"follow_up_action_url": ""},
    {"is_optimized_for_quality": "true"},
    {"is_optimized_for_quality": 1},
    {"block_display_for_non_targeted_viewer": "yes"},
    {"allow_organic_lead": False},
    {"cover_photo": "123"},
    {"tos_accepted": True},
    {"terms": "x"},
]


@pytest.mark.parametrize("over", POLICY_OK, ids=[json.dumps(o) for o in POLICY_OK])
def test_policy_allows_the_new_keys_with_valid_values(over):
    assert _check(_p(**over)) == "leadgen_forms"


@pytest.mark.parametrize("over", POLICY_BAD, ids=[json.dumps(o) for o in POLICY_BAD])
def test_policy_refuses_invalid_values_and_terms(over):
    with pytest.raises(wp.WriteRefused):
        _check(_p(**over))


def test_policy_allows_first_and_last_name_questions_but_nothing_else_new():
    qs = [{"type": "FIRST_NAME"}, {"type": "LAST_NAME"}, {"type": "PHONE"}]
    assert _check(_p(questions=qs)) == "leadgen_forms"
    with pytest.raises(wp.WriteRefused):
        _check(_p(questions=[{"type": "FIRST_NAME", "label": "x"}]))
    with pytest.raises(wp.WriteRefused):
        _check(_p(questions=[{"type": "ZIP"}]))
    cc = {"title": "t", "style": "PARAGRAPH_STYLE", "content": ["x"], "cover_photo": "1"}
    with pytest.raises(wp.WriteRefused):
        _check(_p(context_card=cc))


def test_policy_table_for_the_page_helper_lists_the_new_keys():
    t = wp.policy_for_form_create(PAGE)
    for k in ("follow_up_action_url", "is_optimized_for_quality", "block_display_for_non_targeted_viewer"):
        assert k in t["keys"]
    assert sorted(t["bool_keys"]) == ["block_display_for_non_targeted_viewer", "is_optimized_for_quality"]
    assert "FIRST_NAME" in t["question_types"] and "LAST_NAME" in t["question_types"]


def test_the_template_never_emits_a_key_the_policy_refuses():
    for over in ({}, {"form_type": "higher_intent"}, {"questions_first": False}):
        clean, errors = ft.validate(d71_template(thank_you_text="Thanks", **over))
        assert errors == []
        assert set(ft.form_params(clean)) <= wp.FORM_KEYS


# ───────────────────────── страница: зеркало проверки ─────────────────────────

def _node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    return node


def test_the_page_validates_the_new_fields_exactly_like_the_server():
    cases = {"valid": d71_template(), "legacy": TEMPLATE}
    for name, over, _t in NEW_BAD:
        cases[name] = d71_template(**over)
    cases["no_thanks"] = d71_template(follow_up_action_url="")
    cases["higher_intent"] = d71_template(form_type="higher_intent")
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
        json.dump(cases, fh)
        path = fh.name
    try:
        done = subprocess.run([_node(), str(ROOT / "tests" / "form_templates_harness.cjs"), str(HTML), path],
                              capture_output=True, text=True, encoding="utf-8", timeout=60)
    finally:
        Path(path).unlink(missing_ok=True)
    assert done.returncode == 0, done.stderr
    js = json.loads(done.stdout)
    for name, tpl in cases.items():
        assert js[name] == ft.validate(tpl)[1], name


def test_the_page_locale_list_is_the_server_list():
    page = HTML.read_text(encoding="utf-8")
    m = re.search(r"FORM_LOCALES\s*=\s*\[([^\]]*)\]", page)
    assert m, "список языков не найден на странице"
    assert re.findall(r"'([A-Z]{2}_[A-Z]{2})'", m.group(1)) == list(ft.LOCALES)


def test_the_builder_panel_has_the_new_controls():
    page = HTML.read_text(encoding="utf-8")
    for text in ("cb-fc-intro-title", "cb-fc-followup", "cb-fc-form-type", "cb-fc-questions-first",
                 'value="first_name"', 'value="last_name"', "More volume", "Higher intent"):
        assert text in page, text


def test_policy_refuses_a_null_setting():
    params = {**BASE, "block_display_for_non_targeted_viewer": None}
    with pytest.raises(wp.WriteRefused):
        _check(params)


# ───────────────────────── правки по ревью ─────────────────────────

def test_a_saved_legacy_locale_outside_the_list_keeps_loading_and_its_hash():
    clean, errors = ft.validate({**TEMPLATE, "locale": "ES_MX"}, lenient_locale=True)
    assert errors == [] and clean["locale"] == "ES_MX"
    _c, strict = ft.validate({**TEMPLATE, "locale": "ES_MX"})
    assert any("Unknown locale" in e for e in strict), "новый ввод - только из списка"
    ft.library_path().write_text(json.dumps({"templates": [{**TEMPLATE, "id": "0123456789ab", "locale": "ES_MX"}]}),
                                 encoding="utf-8")
    loaded = ft.load()
    assert [t["locale"] for t in loaded] == ["ES_MX"]
    name_before = ft.form_name(loaded[0])
    ft.save({**TEMPLATE, "name": "Another"})              # перезапись файла не стирает чужой шаблон
    again = {t["id"]: t for t in ft.load()}
    assert again["0123456789ab"]["locale"] == "ES_MX" and ft.form_name(again["0123456789ab"]) == name_before


def test_resaving_a_template_keeps_its_own_legacy_locale_but_rejects_it_for_a_new_one():
    ft.library_path().write_text(json.dumps({"templates": [{**TEMPLATE, "id": "0123456789ab", "locale": "UK_UA"}]}),
                                 encoding="utf-8")
    row = ft.listing()["rows"][0]
    assert ft.save({**TEMPLATE, "id": "0123456789ab", "locale": "UK_UA", "headline": "New",
                    "_expected_revision": row["revision"]})["locale"] == "UK_UA"
    with pytest.raises(ft.TemplateError):
        ft.save({**TEMPLATE, "name": "Fresh", "locale": "UK_UA"})
    with pytest.raises(ft.TemplateError):
        ft.save({**TEMPLATE, "id": "0123456789ab", "locale": "xx"})


def test_unsorted_legacy_contacts_keep_the_old_canonical_order_and_hash():
    a, _ = ft.validate(TEMPLATE)
    b, _ = ft.validate({**TEMPLATE, "contact_fields": ["phone", "email", "name"]})
    c, _ = ft.validate({**TEMPLATE, "contact_fields": ["email", "name"]})
    assert b["contact_fields"] == ["name", "email", "phone"] and c["contact_fields"] == ["name", "email"]
    assert ft.form_name(b) == ft.form_name(ft.validate({**TEMPLATE, "contact_fields": ["name", "email", "phone"]})[0])
    d, _ = ft.validate({**TEMPLATE, "contact_fields": ["name", "email"]})
    assert ft.form_name(c) == ft.form_name(d)
    e, _ = ft.validate(d71_template(contact_fields=["phone", "first_name", "email"]))
    assert e["contact_fields"] == ["phone", "first_name", "email"], "с именем/фамилией порядок задаёт автор"


def test_the_page_helper_refuses_a_policy_without_bool_keys():
    from tests.test_acl1776_create_forms import _node_form_matrix
    from tests.test_acl1776_create_forms import ACT
    policy = wp.policy_for_page(ACT, PAGE)
    del policy["form_create"]["bool_keys"]
    out = _node_form_matrix(policy)
    assert out["ok"].get("error") == "write_refused"
