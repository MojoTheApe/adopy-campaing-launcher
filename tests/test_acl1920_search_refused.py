"""ACL-1920: Meta отказала (код 10/200/294) в необязательном поиске - Check plan не падает.

Только фейки. Необязательные поиски: adinterestvalid, поведения (adTargetingCategory), работа,
образование. Отказ пропускает тип на всё задание (без повторов), даёт предупреждение (English);
отказ в adinterest/adgeolocation/adlocale по-прежнему валит проверку и называет тип.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_api import creator as cr  # noqa: E402
from src.fb_api import graph as g  # noqa: E402
from src.fb_api import targeting_resolver as tr  # noqa: E402
from tests.test_acl1771_targeting_resolver import (  # noqa: E402
    FakeSearch, TargetMeta, _audit, _one, _resolver, _store)

OPTIONAL = ("adinterestvalid", "adTargetingCategory", "adworkposition", "adworkemployer",
            "adeducationschool", "adeducationmajor")


class RefusingSearch(FakeSearch):
    """FakeSearch, который отвечает ошибкой Graph с кодом на заданные типы поиска."""

    def __init__(self, refuse=(), code=10, **kw):
        super().__init__(**kw)
        self.refuse, self.code = set(refuse), code
        self.attempts: list[str] = []

    def get_all(self, path, params=None, *, max_pages=5):
        t = params["type"]
        self.attempts.append(t)
        if t in self.refuse:
            self.calls += 1
            raise g.GraphError(f"Graph API error {self.code}: (#{self.code}) Permission Denied",
                               code=self.code, status=400)
        return super().get_all(path, params, max_pages=max_pages)


def test_interests_resolve_through_adinterest_when_adinterestvalid_is_refused(tmp_path):
    c = RefusingSearch(refuse=OPTIONAL, interests={"Yoga": [{"id": "11", "name": "Yoga"}]})
    r = _resolver(c, tmp_path)
    res = _one(r, "Yoga")
    assert res["state"] == "exact" and res["picks"] == [{"id": "11", "type": "interests", "name": "Yoga"}]
    assert c.attempts == ["adinterestvalid", "adinterest"]


@pytest.mark.parametrize("code", [10, 200, 294])
def test_every_permission_code_is_a_refusal(tmp_path, code):
    c = RefusingSearch(refuse=["adinterestvalid"], code=code, interests={"Yoga": [{"id": "11", "name": "Yoga"}]})
    r = _resolver(c, tmp_path)
    assert _one(r, "Yoga")["state"] == "exact"
    assert len(r.refusals()) == 1 and "interest validation" in r.refusals()[0]


def test_a_refused_type_is_not_asked_again_in_the_job(tmp_path):
    c = RefusingSearch(refuse=OPTIONAL, interests={"A1": [{"id": "1", "name": "A1"}],
                                                  "A2": [{"id": "2", "name": "A2"}]})
    r = _resolver(c, tmp_path)
    r.prefetch(["A1", "A2"])
    r.resolve("A1")
    r.resolve("A2")
    # Неточные термины, дойдущие до всех этапов, тоже не должны повторять отказанные типы.
    r.resolve("Nothing Like It")
    r.resolve("Another Unknown")
    for t in OPTIONAL:
        assert c.attempts.count(t) <= 1, t


def test_refusals_are_reported_in_english_naming_each_lookup(tmp_path):
    c = RefusingSearch(refuse=OPTIONAL)
    r = _resolver(c, tmp_path)
    r.prefetch(["Nothing Like It"])
    r.resolve("Nothing Like It")
    text = " | ".join(r.refusals())
    for label in ("interest validation", "behaviors", "work positions", "work employers"):
        assert f"Meta refused the {label} lookup" in text


def test_a_term_that_could_only_match_through_a_refused_bucket_is_unknown_never_a_guess(tmp_path):
    c = RefusingSearch(refuse=OPTIONAL, typed={("adworkposition", "Marketing Manager"):
                                               [{"id": "71", "name": "Marketing Manager"}]})
    res = _one(_resolver(c, tmp_path), "Marketing Manager")
    assert res["state"] == "unknown" and res["picks"] == []
    assert "Meta refused" in res["reason"] and "work positions" in res["reason"]


def test_a_close_candidate_from_adinterest_still_needs_a_choice(tmp_path):
    c = RefusingSearch(refuse=OPTIONAL, interests={"forex trading": [{"id": "6", "name": "Forex Trading Tips"}]})
    res = _one(_resolver(c, tmp_path), "forex trading")
    assert res["state"] == "pending" and res["picks"] == []
    assert "Meta refused" in res["reason"]


@pytest.mark.parametrize("typ", ["adinterest"])
def test_refusal_of_adinterest_still_fails_and_names_the_type(tmp_path, typ):
    c = RefusingSearch(refuse=[typ], valid={})
    with pytest.raises(g.GraphError) as err:
        _one(_resolver(c, tmp_path), "Whatever")
    assert err.value.code == 10 and f"search_type={typ}" in str(err.value)


def test_other_errors_on_optional_searches_still_fail_with_the_type(tmp_path):
    c = RefusingSearch(refuse=["adworkposition"], code=100, interests={})
    with pytest.raises(g.GraphError) as err:
        _one(_resolver(c, tmp_path), "Whatever")
    assert "search_type=adworkposition" in str(err.value)


def test_refused_reads_count_against_the_budget(tmp_path):
    c = RefusingSearch(refuse=OPTIONAL)
    r = _resolver(c, tmp_path)
    r.prefetch(["Nothing Like It"])
    r.resolve("Nothing Like It")
    assert r.reads == c.calls and r.reads <= tr.JOB_READ_CAP


# ───────────────────────────── Check plan целиком ─────────────────────────────

DENIED = {"error": {"code": 10, "type": "OAuthException", "message": "(#10) Permission Denied"}}


class DeniedMeta(TargetMeta):
    def __init__(self, deny=OPTIONAL, **kw):
        super().__init__(**kw)
        self.deny = set(deny)
        self.search_log: list[str] = []

    def _get(self, path, params):
        if path == "/search":
            self.search_log.append(params["type"])
            if params["type"] in self.deny:
                return DENIED
        return super()._get(path, params)


def test_check_plan_passes_with_refused_optional_lookups_and_lists_them_as_warnings(tmp_path):
    meta = DeniedMeta(interests={"Yoga": [{"id": "11", "name": "Yoga"}]})
    out, got = _audit(meta, tmp_path, "Yoga", _store(tmp_path))
    assert out.error == "" and out.stop_kind == "", out.error
    assert got["interests_resolve"]["status"] == "ok" and got["interests_confirmed"]["status"] == "ok"
    assert any("interest validation" in w for w in out.warnings)
    assert meta.search_log.count("adinterestvalid") == 1, "без повторов в задании"


def test_check_plan_names_the_refused_lookup_when_a_term_needs_it(tmp_path):
    meta = DeniedMeta(deny=["adworkposition", "adworkemployer"])
    out, got = _audit(meta, tmp_path, "Marketing Manager", _store(tmp_path))
    v = got["interests_resolve"]
    assert v["status"] == "unknown" and "work positions" in v["detail"], v
    assert any("work positions" in w for w in out.warnings)
    assert out.stop_kind == "plan", "термин не проверен - публикация блокируется"


@pytest.mark.parametrize("typ", ["adinterest", "adgeolocation", "adlocale"])
def test_check_plan_still_fails_on_refusal_of_core_searches_with_the_type(tmp_path, typ):
    meta = DeniedMeta(deny=[typ], interests={"Yoga": [{"id": "11", "name": "Yoga"}]})
    out, _ = _audit(meta, tmp_path, "Yoga", _store(tmp_path))
    assert out.stop_kind == "graph_error" and "Permission was not granted" in out.error
    if typ == "adinterest":   # после ACL-1805 текст человеку - из каталога, технические детали в problems[].raw
        assert "search_type=adinterest" in str(out.problems)
    assert not any("Meta refused the" in w for w in out.warnings)


# ───────────────────────────── ревью: доработки ─────────────────────────────

def test_cached_confirmed_interest_stays_confirmed_and_the_warning_says_it_was_not_reverified(tmp_path):
    store = _store(tmp_path)
    store.put("forex trading", id="6", type="interests", name="Forex Trading Tips")
    c = RefusingSearch(refuse=["adinterestvalid"])
    r = tr.TermResolver(c, store)
    res = _one(r, "forex trading")
    assert res["state"] == "confirmed" and res["picks"][0]["id"] == "6"
    assert any("previously confirmed interests were not re-verified" in w for w in r.refusals())


@pytest.mark.parametrize("exc", [
    g.RateLimited("slow down", code=4), g.LoggedOut("logged out"), g.PolicyBlocked("policy", code=368),
    g.Checkpoint("checkpoint", code=190, subcode=459),
])
def test_graph_stops_on_an_optional_search_propagate_and_are_not_refusals(tmp_path, exc):
    class Stopping(FakeSearch):
        def get_all(self, path, params=None, *, max_pages=5):
            if params["type"] == "adTargetingCategory":
                raise exc
            return super().get_all(path, params, max_pages=max_pages)

    r = _resolver(Stopping(), tmp_path)
    with pytest.raises(type(exc)):
        _one(r, "Nothing Like It")
    assert r.refusals() == []


def test_a_token_error_190_is_not_a_permission_refusal(tmp_path):
    c = RefusingSearch(refuse=["adinterestvalid"], code=190)
    r = _resolver(c, tmp_path)
    with pytest.raises(g.GraphError) as err:
        _one(r, "Yoga")
    assert err.value.code == 190 and "search_type=adinterestvalid" in str(err.value)
    assert r.refusals() == []


def test_warnings_are_per_draft_but_the_no_retry_memory_is_shared(tmp_path):
    c = RefusingSearch(refuse=OPTIONAL, interests={"Yoga": [{"id": "11", "name": "Yoga"}]})
    r = _resolver(c, tmp_path)
    r.begin_draft()
    r.prefetch(["Marketing Manager"])
    assert _one(r, "Marketing Manager")["state"] == "unknown"
    assert any("work positions" in w for w in r.refusals())
    asked = list(c.attempts)

    r.begin_draft()                      # второй черновик: чужие отказы не показываем
    assert r.refusals() == []
    r.prefetch(["Yoga"])
    assert r.resolve("Yoga")["state"] == "exact"
    assert not any("work positions" in w for w in r.refusals())

    r.begin_draft()                      # третий: тот же термин (из кэша) снова получает предупреждение
    r.resolve("Marketing Manager")
    assert any("work positions" in w for w in r.refusals())
    new = c.attempts[len(asked):]
    for t in OPTIONAL:
        assert t not in new, f"{t} asked again"
