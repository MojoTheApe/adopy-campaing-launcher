"""ACL-2178: lead form templates with conditions on each answer (offline only).

Meta's create shape comes from the sanitized fixture (Doc120 probe of 03.11 and the owner's HAR of 11.11):
`conditional_logic.logic_data` is an object keyed `customized_N`, the lead end page lives in
`end_pages.thank_you_pages[0]`, the non-lead page in `end_pages.disqualified_end_components[0]` (no
`button_type`). No Meta call is made here: the live check on a paused write is a separate criterion.
"""
from __future__ import annotations

import copy
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from src.fb_api import form_create as fc
from src.fb_api import form_templates as ft
from src.fb_api import graph as g
from src.fb_api import write_policy as wp
from tests import test_acl1776_create_forms as t1776
from tests.test_acl1775_instant_forms import PAGE

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "meta"
HTML = ROOT / "src" / "web" / "templates" / "report.html"
LOGIC = json.loads((FIX / "acl2178_form_logic_sanitized.json").read_text(encoding="utf-8"))
GOLD = json.loads((FIX / "acl2178_no_conditions_golden.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _library(tmp_path, monkeypatch):
    monkeypatch.setenv("ACL_CONFIG_DIR", str(tmp_path / "acl"))


def tpl(**over):
    out = copy.deepcopy(LOGIC["create"]["template"])
    out.update(over)
    return out


def errors_of(**over):
    return ft.validate(tpl(**over))[1]


def check(params):
    return wp.check_form_create(PAGE, "POST", f"/{PAGE}/leadgen_forms", params, pinned_page=PAGE)


def with_questions(*qs, **over):
    return tpl(questions=list(qs), **over)


def mc(label, options, logic):
    return {"label": label, "type": "multiple_choice_logic", "options": options, "logic": logic}


# ───────────────────── templates without conditions are untouched ─────────────────────

@pytest.mark.parametrize("name", sorted(GOLD))
def test_a_template_without_conditions_keeps_hash_content_and_request_byte_for_byte(name):
    g_ = GOLD[name]
    clean, errors = ft.validate(g_["input"])
    assert errors == []
    assert clean == g_["clean"]
    assert ft.content_hash(clean) == g_["hash"]
    params = ft.form_params(clean)
    assert params == g_["params"]
    assert json.dumps(params, sort_keys=True) == json.dumps(g_["params"], sort_keys=True)
    assert "conditional_logic" not in params and "end_pages" not in params
    assert not ft.has_logic(clean)


def test_a_plain_template_still_reads_back_with_the_unchanged_field_list():
    clean = ft.validate(GOLD["plain_default"]["input"])[0]
    assert fc.readback_fields(clean) == fc.READBACK_FIELDS
    assert fc.compare_logic(clean, {"id": "1"}) == []


def test_the_unused_keys_do_not_appear_in_a_clean_template():
    clean, errors = ft.validate(tpl())
    assert errors == []
    assert [q["type"] for q in clean["questions"]] == ["multiple_choice_logic"] * 2
    plain = ft.validate(t1776.TEMPLATE)[0]
    assert all("logic" not in q and "after_answer" not in q for q in plain["questions"])
    assert "non_lead_end" not in plain


# ───────────────────── request shape equals the recorded fixture ─────────────────────

def test_the_request_shape_equals_the_recorded_fixture():
    clean, errors = ft.validate(tpl())
    assert errors == []
    params = ft.form_params(clean)
    assert params["conditional_logic"] == LOGIC["create"]["conditional_logic"]
    assert params["end_pages"] == LOGIC["create"]["end_pages"]
    assert "thank_you_page" not in params
    # Custom questions first, then the contact fields, keys by position.
    kinds = [q["type"] for q in params["questions"]]
    assert kinds == ["CUSTOM", "CUSTOM", "EMAIL", "PHONE"]
    assert params["questions"][0]["options"][1] == {"key": "q1_option_2", "value": "No"}
    check(params)                       # the write policy lets exactly this through


def test_logic_is_built_from_the_answer_text_not_from_the_order_of_a_dict():
    flipped = tpl()
    flipped["questions"][0]["logic"] = {"No": "close", "Yes": "q2"}          # same answers, other key order
    assert ft.form_params(ft.validate(flipped)[0]) == ft.form_params(ft.validate(tpl())[0])
    swapped = tpl()
    swapped["questions"][0]["options"] = ["No", "Yes"]                        # the options moved, the branches follow
    data = ft.form_params(ft.validate(swapped)[0])["conditional_logic"]["logic_data"]["customized_0"]
    assert data[0]["destination_type"] == "DISQUALIFIED_END_COMPONENT" and data[1]["destination_type"] == "QUESTION"


def test_next_continues_to_the_next_question_that_is_not_after_an_answer_and_a_plain_question_submits_last():
    t = with_questions(
        mc("Q1", ["A", "B"], {"A": "next", "B": "submit"}),
        {"label": "Only after A", "type": "short_answer", "after_answer": True},
        {"label": "Last", "type": "short_answer"})
    t["questions"][0]["logic"] = {"A": "next", "B": "submit"}
    data = ft.logic_data(ft.validate(t)[0])
    assert data["customized_0"][0] == {"destination_id": "customized_2", "destination_type": "QUESTION"}
    assert data["customized_0"][1]["destination_type"] == "THANK_YOU_PAGE"
    assert data["customized_1"] == [{"destination_id": "customized_2", "destination_type": "QUESTION"}]
    assert data["customized_2"] == [{"destination_id": "thank_you_page_0", "destination_type": "THANK_YOU_PAGE"}]


def test_the_lead_end_page_has_no_button_without_a_link_and_the_non_lead_page_never_sends_button_type():
    clean = ft.validate(tpl(website_url=""))[0]
    pages = ft.form_params(clean)["end_pages"]
    assert pages["thank_you_pages"][0]["button_type"] == "NONE"
    assert "button_type" not in pages["disqualified_end_components"][0]
    check(ft.form_params(clean))


def test_a_form_with_conditions_and_no_closing_answer_has_no_non_lead_page():
    t = with_questions(mc("Q1", ["A", "B"], {"A": "submit", "B": "next"}), {"label": "Q2", "type": "short_answer"},
                       non_lead_end={"title": "x"})
    clean, errors = ft.validate(t)
    assert errors == []
    assert "non_lead_end" not in clean, "a stale non-lead page must not change the hash"
    assert "disqualified_end_components" not in ft.form_params(clean)["end_pages"]


# ───────────────────── round trip, duplicate, old builds ─────────────────────

def test_save_load_and_duplicate_keep_the_conditions():
    saved = ft.save(tpl())
    loaded = ft.get(saved["id"])
    assert loaded["questions"] == saved["questions"] and loaded["non_lead_end"] == saved["non_lead_end"]
    copy_ = ft.duplicate(saved["id"])
    assert copy_["questions"] == saved["questions"] and copy_["non_lead_end"] == saved["non_lead_end"]
    assert "conditions" in ft.summary(loaded)
    assert ft.content_hash(copy_) == ft.content_hash(loaded)


def test_turning_conditions_off_drops_the_stored_non_lead_page():
    saved = ft.save(tpl())
    editor = {**ft.get(saved["id"]), "_expected_revision": next(
        r["revision"] for r in ft.listing()["rows"] if r["id"] == saved["id"])}
    editor["questions"] = [{"label": q["label"], "type": "multiple_choice", "options": q["options"]}
                           for q in editor["questions"]]
    editor.pop("non_lead_end")
    ft.save(editor)
    stored = next(r for r in ft.listing()["rows"] if r["id"] == saved["id"])
    assert stored["template"].get("non_lead_end") is None
    assert "non_lead_end" not in ft._snapshot()["templates"][saved["id"]]


def test_an_old_build_refuses_the_new_question_type():
    """A frozen copy of the old kinds tuple: the distinct type makes an old `validate` fail the template."""
    old_kinds = ("short_answer", "multiple_choice")
    assert ft.CHOICE_LOGIC not in old_kinds and ft.CHOICE_LOGIC in ft.QUESTION_KINDS
    assert ft.CHOICE_LOGIC == "multiple_choice_logic"


# ───────────────────── validation, one test per error ─────────────────────

ERROR_CASES = [
    ("not_choice", dict(questions=[{"label": "Q", "type": "short_answer", "logic": {"A": "submit"}},
                                    mc("Q2", ["A", "B"], {"A": "submit", "B": "close"})]),
     "Question 1: conditions need a multiple choice question"),
    ("unchosen", dict(questions=[mc("Q1", ["Yes", "No"], {"Yes": "submit"})]),
     'Question 1: choose what happens after the answer "No"'),
    ("renamed", dict(questions=[mc("Q1", ["Yes", "Maybe"], {"Yes": "submit", "No": "close", "Maybe": "submit"})]),
     'Question 1: the condition for "No" does not match any answer'),
    ("missing_target", dict(questions=[mc("Q1", ["Yes", "No"], {"Yes": "q9", "No": "submit"})]),
     'Question 1: the answer "Yes" goes to question 9, which does not exist'),
    ("loop", dict(questions=[mc("Q1", ["Yes", "No"], {"Yes": "q1", "No": "submit"}),
                              {"label": "Q2", "type": "short_answer"}]),
     'Question 1: the answer "Yes" can only go to a later question'),
    ("backwards", dict(questions=[{"label": "Q1", "type": "short_answer"},
                                   mc("Q2", ["Yes", "No"], {"Yes": "q1", "No": "submit"})]),
     'Question 2: the answer "Yes" can only go to a later question'),
    ("bad_action", dict(questions=[mc("Q1", ["Yes", "No"], {"Yes": "jump", "No": "submit"})]),
     'Question 1: "jump" is not a valid action'),
    ("unreachable", dict(questions=[mc("Q1", ["Yes", "No"], {"Yes": "submit", "No": "submit"}),
                                     {"label": "Q2", "type": "short_answer", "after_answer": True}]),
     "Question 2: only shown after an answer, but no answer leads to it"),
    ("first_after", dict(questions=[{"label": "Q1", "type": "short_answer", "after_answer": True},
                                    mc("Q2", ["Yes", "No"], {"Yes": "submit", "No": "next"})]),
     "The first question can't be shown only after an answer"),
    ("no_submit", dict(questions=[mc("Q1", ["Yes", "No"], {"Yes": "close", "No": "close"})]),
     "At least one answer must submit the form"),
    ("nl_title", dict(non_lead_end={"title": "", "text": "t", "website_url": "https://example.com/"}),
     "Non-lead end page: title is required"),
    ("nl_text", dict(non_lead_end={"title": "t", "text": "", "website_url": "https://example.com/"}),
     "Non-lead end page: text is required"),
    ("nl_long", dict(non_lead_end={"title": "t", "text": "x" * 301, "website_url": "https://example.com/"}),
     "Non-lead end page: text is longer than 300 characters"),
    ("nl_link", dict(non_lead_end={"title": "t", "text": "t", "website_url": "http://example.com/"}),
     "Non-lead end page: website link must start with https://"),
]


@pytest.mark.parametrize("name,over,message", ERROR_CASES, ids=[c[0] for c in ERROR_CASES])
def test_each_validation_error(name, over, message):
    assert message in errors_of(**over)


def test_the_limit_of_five_questions_stays():
    qs = [mc("Q1", ["Yes", "No"], {"Yes": "q2", "No": "close"})] + [
        {"label": f"Q{i}", "type": "short_answer"} for i in range(2, 7)]
    assert "At most 5 custom questions" in errors_of(questions=qs)


def test_a_valid_template_has_no_errors_and_forces_the_custom_questions_first():
    clean, errors = ft.validate(tpl(questions_first=False))
    assert errors == [] and clean["questions_first"] is True


# ───────────────────── write policy and the page helper ─────────────────────

def good():
    return ft.form_params(ft.validate(tpl())[0])


def test_the_policy_accepts_the_new_shape_also_as_json_strings():
    params = good()
    check({**params, "conditional_logic": json.dumps(params["conditional_logic"]),
           "end_pages": json.dumps(params["end_pages"]), "questions": json.dumps(params["questions"])})


def _mut(fn):
    p = copy.deepcopy(good())
    fn(p)
    return p


POLICY_BAD = {
    "logic_without_end_pages": lambda p: p.pop("end_pages"),
    "end_pages_without_logic": lambda p: p.pop("conditional_logic"),
    "both_thank_you_places": lambda p: p.update(thank_you_page={"title": "x"}),
    "version_2": lambda p: p["conditional_logic"].update(version=2),
    "version_true": lambda p: p["conditional_logic"].update(version=True),
    "extra_logic_key": lambda p: p["conditional_logic"].update(id="1"),
    "list_logic_data": lambda p: p["conditional_logic"].update(logic_data=[]),
    "missing_question_entry": lambda p: p["conditional_logic"]["logic_data"].pop("customized_1"),
    "extra_question_entry": lambda p: p["conditional_logic"]["logic_data"].update(customized_5=[]),
    "wrong_length": lambda p: p["conditional_logic"]["logic_data"]["customized_0"].pop(),
    "bad_type": lambda p: p["conditional_logic"]["logic_data"]["customized_0"][0].update(destination_type="URL"),
    "extra_dest_key": lambda p: p["conditional_logic"]["logic_data"]["customized_0"][0].update(status="x"),
    "backward_jump": lambda p: p["conditional_logic"]["logic_data"]["customized_1"][0].update(
        destination_id="customized_0", destination_type="QUESTION"),
    "jump_past_the_end": lambda p: p["conditional_logic"]["logic_data"]["customized_0"][0].update(destination_id="customized_7"),
    "wrong_thank_id": lambda p: p["conditional_logic"]["logic_data"]["customized_1"][0].update(destination_id="thank_you_page_1"),
    "wrong_disq_id": lambda p: p["conditional_logic"]["logic_data"]["customized_0"][1].update(destination_id="x"),
    "button_type_on_non_lead": lambda p: p["end_pages"]["disqualified_end_components"][0].update(button_type="NONE"),
    "two_lead_pages": lambda p: p["end_pages"]["thank_you_pages"].append(dict(p["end_pages"]["thank_you_pages"][0])),
    "no_non_lead_page": lambda p: p["end_pages"].pop("disqualified_end_components"),
    "non_lead_without_close": lambda p: p["conditional_logic"]["logic_data"]["customized_0"].__setitem__(
        1, {"destination_id": "thank_you_page_0", "destination_type": "THANK_YOU_PAGE"}),
    "http_link": lambda p: p["end_pages"]["thank_you_pages"][0].update(website_url="http://example.com/"),
    "tos_in_end_page": lambda p: p["end_pages"]["thank_you_pages"][0].update(tos_accepted=True),
    "tos_in_logic": lambda p: p["conditional_logic"].update(tos_accepted=True),
    "extra_end_pages_key": lambda p: p["end_pages"].update(status="ACTIVE"),
}


@pytest.mark.parametrize("name", sorted(POLICY_BAD))
def test_the_policy_refuses_each_bad_shape(name):
    with pytest.raises(wp.WriteRefused):
        check(_mut(POLICY_BAD[name]))


def test_every_key_form_params_emits_is_allowed_by_the_policy():
    params = good()
    assert set(params) <= wp.FORM_KEYS
    assert set(params["conditional_logic"]) <= set(wp.FORM_NESTED["conditional_logic"])
    assert set(params["end_pages"]) <= set(wp.FORM_NESTED["end_pages"])
    assert set(params["end_pages"]["thank_you_pages"][0]) <= set(wp.FORM_NESTED["thank_you_end_page"])
    assert set(params["end_pages"]["disqualified_end_components"][0]) <= set(wp.FORM_NESTED["non_lead_end_page"])
    assert "status" not in json.dumps(params) and not (set(wp.FORM_FORBIDDEN_ANYWHERE) & {"conditional_logic", "end_pages"})


def _matrix():
    rows = [("logic_ok", PAGE, "leadgen_forms", good(), True),
            ("logic_ok_strings", PAGE, "leadgen_forms",
             {**good(), "conditional_logic": json.dumps(good()["conditional_logic"]),
              "end_pages": json.dumps(good()["end_pages"])}, True)]
    for name in sorted(POLICY_BAD):
        rows.append((name, PAGE, "leadgen_forms", _mut(POLICY_BAD[name]), False))
    return rows


def test_the_page_helper_agrees_with_the_policy_on_every_shape(monkeypatch):
    matrix = _matrix()
    monkeypatch.setattr(t1776, "FORM_MATRIX", matrix)
    out = t1776._node_form_matrix()
    for name, _p, _e, params, allowed in matrix:
        got = out[name]
        if allowed:
            assert got.get("body") == {"id": "5"}, (name, got)
        else:
            assert got == {"error": "write_refused", "why": "form_logic"} or got.get("error") == "write_refused", (name, got)
        try:
            check(params)
            python_allows = True
        except wp.WriteRefused:
            python_allows = False
        assert python_allows == allowed, name


def test_the_page_helper_refuses_when_its_table_has_no_logic_rules(monkeypatch):
    """Fail closed: an installation whose policy table predates this feature refuses logic requests."""
    policy = wp.policy_for_page(t1776.ACT, PAGE)
    policy["form_create"] = {k: v for k, v in policy["form_create"].items() if k != "logic"}
    monkeypatch.setattr(t1776, "FORM_MATRIX", [("logic_ok", PAGE, "leadgen_forms", good(), True)])
    out = t1776._node_form_matrix(policy)
    assert out["logic_ok"] == {"error": "write_refused", "why": "form_logic"}


# ───────────────────── readback ─────────────────────

def test_readback_asks_for_the_logic_fields_only_with_conditions():
    clean = ft.validate(tpl())[0]
    assert fc.readback_fields(clean).endswith(",conditional_logic,end_pages")


def test_the_recorded_readback_matches_the_template():
    clean = ft.validate(tpl())[0]
    assert fc.compare_logic(clean, LOGIC["readback"]) == []


def test_readback_names_a_changed_condition_by_answer_text():
    clean = ft.validate(tpl())[0]
    row = copy.deepcopy(LOGIC["readback"])
    row["conditional_logic"]["logic_data"][0]["value"][1] = {"destination_id": "thank_you_page_0",
                                                             "destination_type": "THANK_YOU_PAGE"}
    assert fc.compare_logic(clean, row) == ['question 1: condition for "No"']


def test_readback_reports_missing_logic_and_a_changed_end_page():
    clean = ft.validate(tpl())[0]
    assert fc.compare_logic(clean, {"id": "1"}) == ["conditions are missing on the Page form"]
    row = copy.deepcopy(LOGIC["readback"])
    row["end_pages"]["disqualified_end_components"][0]["website_url"] = "https://example.com/other"
    row["end_pages"]["thank_you_pages"][0]["body"] = "Different"
    assert fc.compare_logic(clean, row) == ["lead end page: body", "non-lead end page: website url"]


def test_readback_ignores_the_fields_meta_adds():
    clean = ft.validate(tpl())[0]
    row = copy.deepcopy(LOGIC["readback"])
    assert row["end_pages"]["thank_you_pages"][0]["enable_messenger"] is False      # the fixture carries Meta's extras
    assert fc.compare_logic(clean, row) == []


def test_a_refusal_shows_meta_code_and_subcode_for_a_form_with_conditions():
    clean = ft.validate(tpl())[0]
    exc = g.GraphError("(#100) Unexpected key", code=100, subcode=1892112)
    assert "(code 100, subcode 1892112)" in fc.refused_message(exc, clean)
    plain = fc.refused_message(exc, ft.validate(t1776.TEMPLATE)[0])
    assert "code 100" not in plain, "forms without conditions keep today's text"


# ───────────────────── the page: validation parity and the builder panel ─────────────────────

def _node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    return node


def test_the_page_validates_conditions_exactly_like_the_server():
    cases = {"valid": tpl()}
    for name, over, _m in ERROR_CASES:
        cases[name] = tpl(**over)
    cases["plain"] = t1776.TEMPLATE
    cases["bad_type"] = tpl(questions=[{"label": "Q", "type": "radio"}])
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
    for name, t in cases.items():
        assert js[name] == ft.validate(t)[1], name


def test_the_forms_tab_keeps_edits_renumbers_and_flags_conditions():
    done = subprocess.run([_node(), str(ROOT / "tests" / "forms_tab_harness.cjs"), str(HTML), "logic"],
                          capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert done.returncode == 0, done.stdout + done.stderr


def test_the_builder_panel_carries_conditions_into_the_template():
    done = subprocess.run([_node(), str(ROOT / "tests" / "builder_form_logic_harness.cjs"), str(HTML)],
                          capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert done.returncode == 0, done.stdout + done.stderr


def test_the_english_labels_are_on_the_page():
    page = HTML.read_text(encoding="utf-8")
    for text in ("Conditional logic", "Then:", "Continue to next question", "Submit the form", "Close the form",
                 "Only shown after an answer", "Non-lead end page", "This answer no longer exists",
                 "Remove all conditions from this template?"):
        assert text in page, text


def test_a_form_with_conditions_and_a_cover_sends_both_and_the_name_marks_both():
    """ACL-2579 + ACL-2178 together: UPLOAD background, intro-less card, logic, and one name hash for both."""
    over = {"cover_photo_id": "111222333", "cover_page_id": "444555666", "intro": "Welcome"}
    clean, errors = ft.validate(tpl(**over))
    assert errors == []
    params = ft.form_params(clean)
    assert params["background_image_option"] == "UPLOAD"
    assert params["context_card"]["cover_photo_id"] == "111222333"
    assert params["conditional_logic"] == LOGIC["create"]["conditional_logic"]
    assert params["end_pages"] == LOGIC["create"]["end_pages"]
    check(params)
    plain, _ = ft.validate(tpl())
    assert ft.content_hash(clean) != ft.content_hash(plain)
    fields = fc.readback_fields(clean)
    assert "background_image_option" in fields and "conditional_logic,end_pages" in fields
