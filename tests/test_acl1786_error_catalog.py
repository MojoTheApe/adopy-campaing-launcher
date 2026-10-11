"""ACL-1786: каталог ошибок Meta."""
from src.fb_api import error_catalog as ec
from src.fb_api.graph import GraphError, POLICY_CODES, RATE_CODES, TOKEN_CODES

LEAD = "Param call_to_action[value][lead_gen_form_id] must be a valid Lead Gen Data id."


def test_schema():
    assert ec.CATALOG
    for (code, sub), e in ec.CATALOG.items():
        assert isinstance(code, int) and (sub is None or isinstance(sub, int))
        assert e.message_en and e.action_en and e.source_url
        assert isinstance(e.retryable, bool)
        assert e.category in ec.CATEGORIES
        if e.documented:
            assert e.source_url.startswith("https://developers.facebook.com/")


def test_every_graph_code_covered():
    for code in TOKEN_CODES | RATE_CODES | POLICY_CODES:
        assert (code, None) in ec.CATALOG, code


def test_categories_match_graph_sets():
    for c in TOKEN_CODES:
        assert ec.explain(c).category == "token"
    for c in RATE_CODES:
        x = ec.explain(c)
        assert x.category == "rate_limit" and x.retryable
    for c in POLICY_CODES:
        x = ec.explain(c)
        assert x.category == "policy" and not x.retryable


def test_known_codes():
    assert ec.explain(1885272).message_en == "The budget is too low."
    assert ec.explain(80004).known
    assert ec.explain(190).category == "token"


def test_subcode_precedence():
    assert ec.explain(190, 459).category == "account"
    assert ec.explain(613, 1487632).message_en.startswith("An ad set budget")
    assert ec.explain(613, 999).message_en == ec.explain(613).message_en  # неизвестный подкод -> код


def test_unknown_falls_back_to_raw():
    x = ec.explain(987654, None, "Something odd happened")
    assert not x.known and x.line() == "Something odd happened"
    assert ec.explain(None, None, "").line()


def test_observed_lead_form_text():
    x = ec.explain(100, None, LEAD)
    assert "instant form" in x.message_en  # текст точнее общего кода 100
    assert ec.explain(None, None, LEAD).known
    assert "instant form" in ec.explain(None, None, LEAD).message_en


def test_problem_line_from_grapherror():
    err = GraphError("Budget too low", code=1885272)
    assert ec.problem_line(err) == "The budget is too low. Raise the budget and publish again."
    assert ec.problem_line(GraphError("raw text", code=424242)) == "raw text"


def test_code_2238055_two_meanings():
    ig = ec.explain(2238055, None, "You cannot pass both instagram_user_id and instagram_actor_id")
    assert "instagram_user_id" in ig.message_en
    bud = ec.explain(2238055, None, "Your campaign budget must be at least $5.00")
    assert "budget" in bud.message_en and "instagram" not in bud.message_en.lower()
    amb = ec.explain(2238055, None, "Something else entirely")
    assert "two different problems" in amb.message_en
    assert "Something else entirely" in amb.line()


def test_code_104_neutral_action():
    x = ec.explain(104)
    assert "ACL team" in x.action_en and "sign in" not in x.action_en.lower()
