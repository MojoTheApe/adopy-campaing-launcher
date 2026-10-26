"""ACL-1920: Meta отказала (код 10 / 200-299) в НЕОБЯЗАТЕЛЬНОМ поиске таргетинга - Check plan идёт дальше.

Правило (документируется и здесь, и в changelog):
* необязательные поиски: adinterestvalid, adTargetingCategory (поведения), adworkposition,
  adworkemployer, adeducationschool, adeducationmajor. Отказ = пропуск на всё задание (без повторов),
  английское предупреждение с названием поиска;
* обязательные: adinterest, adgeolocation, adlocale. Отказ = ошибка, в тексте тип поиска;
* термин, набранный человеком, который мог бы найтись только в отказанном поиске, НЕ теряется:
  `unresolved` с причиной, публикация заблокирована; догадки нет.
Всё поверх резолвера ACL-1771 (точное совпадение, кандидат с подтверждением, бюджет чтений, SearchCache).
Только фейки: ни одного живого вызова Meta.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_api import creator as cr  # noqa: E402
from src.fb_api import graph as g  # noqa: E402
from src.fb_api import preflight_rules as pf  # noqa: E402
from src.fb_api import targeting_resolver as tr  # noqa: E402
from tests.test_acl1771_targeting_resolver import FakeSearch, TargetMeta, _resolver, _store  # noqa: E402
from tests.test_acl1761_api_create import ACT, _draft  # noqa: E402

OPTIONAL = ("adinterestvalid", "adTargetingCategory", "adworkposition", "adworkemployer",
            "adeducationschool", "adeducationmajor")


def _denied(code: int = 10) -> g.GraphError:
    return g.GraphError(f"Graph API error {code}: (#{code}) Permission Denied [type=OAuthException, "
                        "method=GET, operation=search]", code=code, status=400)


class Refusing(FakeSearch):
    """FakeSearch, который отвечает отказом на выбранные типы поиска (а остальным - как обычно)."""

    def __init__(self, refuse=(), code: int = 10, **kw):
        super().__init__(**kw)
        self.refuse, self.code = set(refuse), code

    def get_all(self, path, params=None, *, max_pages=5):
        if params and params.get("type") in self.refuse:
            self.calls += 1
            self.log.append((params["type"], 0, dict(params)))
            raise _denied(self.code)
        return super().get_all(path, params, max_pages=max_pages)


# ───────────────────────────── резолвер ─────────────────────────────

@pytest.mark.parametrize("code", [10, 200, 294])
def test_all_optional_lookups_refused_interest_still_resolves_through_adinterest(tmp_path, code):
    c = Refusing(refuse=OPTIONAL, code=code, interests={"Forex": [{"id": "5", "name": "Forex"}]})
    r = _resolver(c, tmp_path)
    r.prefetch(["Forex"])
    res = r.resolve("Forex")
    assert res["state"] == "exact" and res["picks"] == [{"id": "5", "type": "interests", "name": "Forex"}]
    # Резолв остановился на adinterest: до поведений/работы/учёбы дело не дошло, их не трогали.
    assert r.refused.keys() == {"adinterestvalid"}
    assert r.warnings() == ["Meta refused the exact-name interest check (adinterestvalid) on this account; "
                            "interests were matched through the interest search instead."]


def test_every_optional_lookup_refused_is_named_in_its_own_warning(tmp_path):
    c = Refusing(refuse=OPTIONAL)
    r = _resolver(c, tmp_path)
    r.prefetch(["Nothing here"])
    res = r.resolve("Nothing here")
    text = " ".join(r.warnings())
    for name in ("adinterestvalid", "adTargetingCategory", "adworkposition", "adworkemployer",
                 "adeducationschool", "adeducationmajor"):
        assert f"({name})" in text
    assert "behaviors were not matched" in text and "work positions were not matched" in text
    assert res["state"] == "unresolved" and res["picks"] == []     # никакой догадки


def test_a_refused_lookup_is_not_retried_inside_the_job(tmp_path):
    c = Refusing(refuse=("adTargetingCategory", "adworkposition"))
    r = _resolver(c, tmp_path)
    r.prefetch(["Alpha", "Beta"])
    r.resolve("Alpha")
    r.resolve("Beta")
    assert c.types().count("adTargetingCategory") == 1
    assert c.types().count("adworkposition") == 1, "second term must not ask the refused lookup again"
    assert "adworkemployer" in c.types()


def test_a_user_typed_term_that_only_a_refused_lookup_could_match_blocks_with_a_clear_reason(tmp_path):
    c = Refusing(refuse=("adTargetingCategory",), behaviors=[{"id": "9", "name": "Frequent Travelers"}])
    resolver = cr.TargetingResolver(c, _store(tmp_path))
    with pytest.raises(cr.TargetingUnresolved) as ei:
        resolver.interests([["Frequent Travelers"]])
    item = ei.value.items[0]
    assert item["state"] == "unresolved" and item["term"] == "Frequent Travelers"
    assert "behaviors lookup" in item["reason"] and "could not be checked there" in item["reason"]
    assert "behaviors lookup" in str(ei.value), "the user sees which lookup Meta refused"
    assert resolver.targeting_warnings()[0].startswith("Meta refused the behaviors lookup (adTargetingCategory)")


def test_a_near_candidate_is_still_only_a_question_when_a_lookup_was_refused(tmp_path):
    c = Refusing(refuse=("adworkposition",), interests={"forex trading": [{"id": "6", "name": "Forex Trading Tips"}]})
    res = _resolver(c, tmp_path).resolve("forex trading")
    assert res["state"] == "pending" and res["picks"] == [] and res["suggested"]["id"] == "6"


def test_exact_match_in_a_working_lookup_after_a_refused_one_is_taken(tmp_path):
    c = Refusing(refuse=("adTargetingCategory",),
                 typed={("adworkemployer", "Plus500"): [{"id": "31", "name": "Plus500"}]})
    r = _resolver(c, tmp_path)
    r.prefetch(["Plus500"])
    res = r.resolve("Plus500")
    assert res["state"] == "exact" and res["picks"][0]["type"] == "work_employers"
    assert "adTargetingCategory" in r.refused


@pytest.mark.parametrize("required", ["adinterest"])
def test_refusal_of_a_required_interest_search_still_fails_and_names_the_search(tmp_path, required):
    c = Refusing(refuse=(required,))
    r = _resolver(c, tmp_path)
    r.prefetch(["Forex"])
    with pytest.raises(g.GraphError) as ei:
        r.resolve("Forex")
    assert ei.value.code == 10 and "lookup=adinterest" in str(ei.value)
    assert r.refused == {}, "a required lookup is never recorded as skipped"


def test_non_permission_graph_errors_on_optional_lookups_are_not_swallowed(tmp_path):
    class Boom(FakeSearch):
        def get_all(self, path, params=None, *, max_pages=5):
            if params["type"] == "adTargetingCategory":
                raise g.GraphError("Graph API error 1: unknown", code=1)
            return super().get_all(path, params, max_pages=max_pages)

    r = _resolver(Boom(), tmp_path)
    with pytest.raises(g.GraphError) as ei:
        r.resolve("Nothing here")
    assert "lookup=adTargetingCategory" in str(ei.value)


def test_refused_valid_check_keeps_exact_reads_and_flags_unverified_confirmed_ids(tmp_path):
    store = _store(tmp_path)
    store.put("forex trading", id="6", type="interests", name="Forex Trading Tips")
    c = Refusing(refuse=("adinterestvalid",))
    r = tr.TermResolver(c, store)
    r.prefetch(["forex trading"])
    res = r.resolve("forex trading")
    assert res["state"] == "confirmed", "the human's earlier choice is not revoked just because Meta refused the check"
    assert any("not re-checked" in w for w in r.warnings())


def test_the_refused_read_counts_in_the_budget(tmp_path):
    c = Refusing(refuse=("adinterestvalid",))
    r = _resolver(c, tmp_path)
    r.prefetch(["Forex"])
    assert r.reads == 1
    assert c.types() == ["adinterestvalid"]


def test_is_permission_refusal_classification():
    assert tr.is_permission_refusal(_denied(10)) and tr.is_permission_refusal(_denied(200))
    assert tr.is_permission_refusal(_denied(294)) and not tr.is_permission_refusal(_denied(100))
    assert not tr.is_permission_refusal(g.GraphError("x", code=None))
    assert not tr.is_permission_refusal(g.RateLimited("slow", code=4)), "stops are never swallowed"


# ───────────────────────────── Check plan целиком (транспорт-фейк) ─────────────────────────────

class RefusingMeta(TargetMeta):
    def __init__(self, refuse=(), **kw):
        super().__init__(**kw)
        self.refuse, self.search_types = set(refuse), []

    def _get(self, path, params):
        if path == "/search":
            self.search_types.append(params["type"])
            if params["type"] in self.refuse:
                return {"error": {"code": 10, "type": "OAuthException", "message": "(#10) Permission Denied"}}
        return super()._get(path, params)


def _plan_check(meta, tmp_path, label, store=None, max_calls=80):
    d = _draft(tmp_path, ads=1)
    d.ad_sets[0].targeting_label = label
    client = g.GraphClient(meta, allow_writes=False, sleep=lambda s: None, min_interval_s=0.0, max_calls=max_calls)
    facts = cr.preflight_account(client, ACT)
    creator = cr.CampaignCreator(client, facts, cr.TargetingResolver(client, store or _store(tmp_path)),
                                 sleep=lambda s: None)
    out = creator.plan_only(cr.plan_from_draft(d))
    assert client.writes == 0 and meta.posts == [], "Check plan is read-only"
    return out, {v["rule"]: v for v in out.audit}


def test_check_plan_passes_with_optional_lookups_refused_and_lists_them_as_warnings(tmp_path):
    meta = RefusingMeta(refuse=OPTIONAL, interests={"Forex": [{"id": "5", "name": "Forex"}]})
    out, got = _plan_check(meta, tmp_path, "Forex")
    assert out.error == "" and not pf.blocks(out.audit)
    assert any("exact-name interest check (adinterestvalid)" in w for w in out.warnings)
    v = got["interests_resolve"]
    assert v["status"] == "warning" and "adinterestvalid" in v["detail"]
    assert got["interests_confirmed"]["status"] == "ok"


def test_check_plan_blocks_a_typed_term_only_a_refused_lookup_could_match(tmp_path):
    meta = RefusingMeta(refuse=("adTargetingCategory", "adinterestvalid"))
    out, got = _plan_check(meta, tmp_path, "Frequent Travelers")
    assert out.stop_kind == "plan" and "behaviors lookup" in out.error
    assert got["interests_resolve"]["status"] == "error" and pf.blocks(out.audit)
    assert any("(adTargetingCategory)" in w for w in out.warnings), "the refused lookup is named as a warning too"


def test_check_plan_fails_when_adinterest_is_refused_and_says_which_search(tmp_path):
    meta = RefusingMeta(refuse=("adinterest",))
    out, got = _plan_check(meta, tmp_path, "Forex")
    assert out.stop_kind == "graph_error" and out.error.startswith("Meta refused:")
    assert "(search: adinterest)" in out.error and out.problems


@pytest.mark.parametrize("required", ["adgeolocation", "adlocale"])
def test_check_plan_fails_when_country_or_language_search_is_refused(tmp_path, required):
    meta = RefusingMeta(refuse=(required,))
    out, _ = _plan_check(meta, tmp_path, "")
    assert out.stop_kind == "graph_error" and "Meta refused" in out.error
    assert f"(search: {required})" in out.error


def test_check_plan_without_refusals_has_no_new_warnings(tmp_path):
    meta = RefusingMeta(interests={"Forex": [{"id": "5", "name": "Forex"}]})
    out, got = _plan_check(meta, tmp_path, "Forex")
    assert out.error == "" and not any("Meta refused" in w for w in out.warnings)
    assert got["interests_resolve"]["status"] == "ok"
