"""ACL-2267: долгий кэш разобранных терминов таргетинга (60 дней) и его сверка с ACL-2258.

Всё офлайн: фейковый `/search`. id интересов - 13 цифр, как у Meta.
"""
from __future__ import annotations

import json

import pytest

from src.fb_api import creator as cr
from src.fb_api import facts_cache as fc
from src.fb_api import graph as g
from src.fb_api import targeting_resolver as tr

YOGA, YOGA2, GOLF, CHESS = "6003000000001", "6003000000009", "6003000000002", "6003000000003"
BEHAVIOR = "6002714895372"


class Meta:
    """Фейк `GraphClient.get_all` для `/search`.

    `valid`: имя -> id для adinterestvalid (по имени и по id); `search`: q -> строки adinterest;
    `reject`: id, на которые Meta отвечает `Invalid id` (`named` - называя id в тексте).
    """

    def __init__(self, *, valid=None, search=None, behaviors=None, reject=(), named=False, cut=False):
        self.valid = dict(valid or {})
        self.search = dict(search or {})
        self.behaviors = list(behaviors or [])
        self.reject, self.named, self.cut = set(reject), named, cut
        self.log: list[dict] = []
        self.calls = 0
        self.truncated: list[str] = []

    def types(self) -> list[str]:
        return [p["type"] for p in self.log]

    def get_all(self, path, params=None, *, max_pages=5):
        params = dict(params or {})
        self.log.append(params)
        self.calls += 1
        t = params["type"]
        if t == "adinterestvalid":
            ids = [str(i) for i in params.get("interest_fbid_list") or []]
            bad = [i for i in ids if i in self.reject]
            if bad:
                raise g.GraphError(f"(#100) Invalid id: {bad[0]}" if self.named else "(#100) Invalid id: " + bad[0],
                                   code=100)
            if self.cut:
                self.truncated.append(path)
                return []
            rows = []
            by_id = {v: k for k, v in self.valid.items()}
            for n in params.get("interest_list") or []:
                if n in self.valid:
                    rows.append({"id": self.valid[n], "name": n, "valid": True})
            for i in ids:
                if i in by_id:
                    rows.append({"id": i, "name": by_id[i], "valid": True})
            return rows
        if t == "adinterest":
            return list(self.search.get(params.get("q"), []))
        if t == "adTargetingCategory":
            return list(self.behaviors)
        if t == "adgeolocation":
            return [{"country_code": "MX", "name": "Mexico"}]
        if t == "adlocale":
            return [{"key": 23, "name": "Spanish (All)"}, {"key": 24, "name": "Spanish"}]
        return []


@pytest.fixture
def facts(tmp_path):
    return fc.FactsCache(path=tmp_path / "facts.json")


@pytest.fixture
def store(tmp_path):
    return tr.ConfirmedStore(path=tmp_path / "confirmed.json")


def run(meta, store, facts, *terms):
    r = tr.TermResolver(meta, store, facts=facts)
    r.prefetch(list(terms))
    return r, {t: r.resolve(t) for t in terms}


def warm(facts, term, pid, kind="interests"):
    facts.put_term(facts.term_key("term", tr.norm(term)), {"picks": [{"id": pid, "type": kind, "name": term}]})


def warm_id(facts, term):
    v = facts.get_term(facts.term_key("term", tr.norm(term)))
    return v["picks"][0]["id"] if v else None


# ── повторная проверка не ищет заново ──

def test_second_check_asks_meta_one_batch_and_searches_nothing(store, facts):
    first = Meta(valid={"Golf": GOLF}, search={"Yoga Lovers": [{"id": YOGA, "name": "Yoga Lovers"}]})
    _, res = run(first, store, facts, "Golf", "Yoga Lovers")
    assert {r["state"] for r in res.values()} == {"exact"}
    assert "adinterest" in first.types()

    again = Meta(valid={"Golf": GOLF, "Yoga Lovers": YOGA})
    _, res = run(again, store, facts, "Golf", "Yoga Lovers")
    assert res["Yoga Lovers"]["picks"][0]["id"] == YOGA and res["Golf"]["picks"][0]["id"] == GOLF
    assert again.types() == ["adinterestvalid"]
    assert sorted(again.log[0]["interest_fbid_list"]) == sorted([GOLF, YOGA])
    assert "interest_list" not in again.log[0]


def test_warm_behavior_needs_no_call_at_all(store, facts):
    warm(facts, "Frequent Travelers", BEHAVIOR, kind="behaviors")
    meta = Meta()
    _, res = run(meta, store, facts, "Frequent Travelers")
    assert res["Frequent Travelers"]["state"] == "exact" and meta.calls == 0


def test_country_and_language_come_from_the_cache(facts):
    one = cr.TargetingResolver(Meta(), facts=facts)
    assert one.country("MX") == "MX" and one.locale("Spanish") == 23
    meta = Meta()
    two = cr.TargetingResolver(meta, facts=facts)
    assert two.country("MX") == "MX" and two.locale("Spanish") == 23
    assert meta.calls == 0 and two.locale_names["Spanish"] == "Spanish (All)"
    assert two.locale_groups == one.locale_groups
    assert two.facts_stats()["warm_hits"] == 2


# ── отвергнутый id ──

def test_warm_interest_missing_from_a_full_answer_is_dropped_and_searched_again(store, facts):
    warm(facts, "Yoga", YOGA)
    meta = Meta(valid={"Yoga": YOGA2})
    r, res = run(meta, store, facts, "Yoga")
    assert res["Yoga"]["picks"][0]["id"] == YOGA2
    assert warm_id(facts, "Yoga") == YOGA2
    assert r.facts_stats["warm_dropped"] == 1


def test_named_invalid_id_drops_the_entry_and_is_sent_once(store, facts):
    facts.put_term(facts.term_key("term", "yoga"), {"picks": [{"id": YOGA, "type": "interests", "name": "Yoga"}]})
    meta = Meta(reject={YOGA}, named=True)
    r, res = run(meta, store, facts, "Yoga")
    sent = [p for p in meta.log if YOGA in (p.get("interest_fbid_list") or [])]
    assert len(sent) == 1
    assert res["Yoga"]["state"] == "unresolved" and res["Yoga"]["reason"].startswith(tr.WARM_GONE_REASON)
    assert warm_id(facts, "Yoga") is None


def test_scrubbed_invalid_id_does_not_fail_the_check_and_leaves_no_entry(store, facts):
    # GraphError вычищает длинные числа: Meta назвала id, а в тексте "<id>" (регрессия ветки prep-speed)
    warm(facts, "Yoga", YOGA)
    meta = Meta(reject={YOGA}, valid={"Yoga": YOGA2})
    assert YOGA not in str(g.GraphError(f"(#100) Invalid id: {YOGA}", code=100))
    r, res = run(meta, store, facts, "Yoga")
    assert res["Yoga"]["picks"][0]["id"] == YOGA2
    assert warm_id(facts, "Yoga") == YOGA2
    assert r.facts_stats["warm_dropped_unattributed"] == 1
    again = Meta(reject={YOGA}, valid={"Yoga": YOGA2})
    run(again, store, facts, "Yoga")
    assert again.types() == ["adinterestvalid"]       # без провала и без повтора


def test_scrubbed_invalid_id_with_only_confirmed_ids_still_fails(store, facts):
    store.put("Yoga", id=YOGA, type="interests", name="Yoga")
    with pytest.raises(g.GraphError):
        run(Meta(reject={YOGA}), store, facts, "Yoga")
    assert len(store.entries()) == 1


def test_mixed_batch_drops_warm_and_keeps_the_confirmed_pick(store, facts):
    store.put("Golf", id=GOLF, type="interests", name="Golf")
    warm(facts, "Yoga", YOGA)
    meta = Meta(reject={YOGA}, valid={"Golf": GOLF})
    _, res = run(meta, store, facts, "Golf", "Yoga")
    assert res["Golf"]["state"] == "confirmed"
    assert len(store.entries()) == 1 and warm_id(facts, "Yoga") is None
    retry = [p for p in meta.log if p["type"] == "adinterestvalid"][1]
    assert retry.get("interest_fbid_list") == [GOLF]


@pytest.mark.parametrize("cut", [True, False], ids=["cut", "refused"])
def test_cut_or_refused_answer_drops_nothing(store, facts, cut):
    meta = Meta(cut=cut)
    warm(facts, "Yoga", YOGA)
    if not cut:
        orig = meta.get_all

        def refuse(path, params=None, *, max_pages=5):
            if params["type"] != "adinterestvalid":
                return orig(path, params, max_pages=max_pages)
            meta.calls += 1
            raise g.GraphError("(#10) Permission Denied", code=10)
        meta.get_all = refuse
    run(meta, store, facts, "Yoga")
    assert warm_id(facts, "Yoga") == YOGA
    if cut:      # обрезанный ответ не переспрашивается в задании
        assert sum(YOGA in (p.get("interest_fbid_list") or []) for p in meta.log) == 1


def test_spent_budget_drops_nothing(store, facts):
    warm(facts, "Yoga", YOGA)
    r = tr.TermResolver(Meta(), store, facts=facts, job_cap=0)
    r.prefetch(["Yoga"])
    assert r.resolve("Yoga")["state"] == "unknown"
    assert warm_id(facts, "Yoga") == YOGA


def test_refused_batch_with_only_warm_ids_gives_no_confirmed_warning(store, facts):
    warm(facts, "Yoga", YOGA)
    meta = Meta(search={"Yoga": [{"id": YOGA, "name": "Yoga"}]})
    orig = meta.get_all

    def get_all(path, params=None, *, max_pages=5):
        if params["type"] == "adinterestvalid":
            meta.calls += 1
            raise g.GraphError("(#10) Permission Denied", code=10)
        return orig(path, params, max_pages=max_pages)

    meta.get_all = get_all
    r, res = run(meta, store, facts, "Yoga")
    assert res["Yoga"]["state"] == "exact"
    assert not any("Confirmed interest picks" in w for w in r.warnings())


def test_rejected_warm_term_has_budget_for_the_search(store, facts):
    # 8 тёплых терминов, все отвергнуты: бюджет считается по всем ожидающим терминам, а не по `every`
    names = [f"Term{i}" for i in range(8)]
    for i, t in enumerate(names):
        warm(facts, t, f"6003000001{i:03d}")
    meta = Meta(search={t: [{"id": f"6003000002{i:03d}", "name": t}] for i, t in enumerate(names)})
    r, res = run(meta, store, facts, *names)
    assert {x["state"] for x in res.values()} == {"exact"}
    assert r.job_cap >= tr.JOB_READ_BASE + tr.JOB_READ_PER_TERM * len(names)
    assert meta.types().count("adinterest") == len(names)


def test_corrupt_picks_shape_is_a_miss_not_a_crash(store, facts):
    key = facts.term_key("term", tr.norm("Yoga"))
    facts.put_term(key, {"picks": 5})
    meta = Meta(search={"Yoga": [{"id": YOGA, "name": "Yoga"}]})
    _, res = run(meta, store, facts, "Yoga")
    assert res["Yoga"]["state"] == "exact" and "adinterest" in meta.types()
    facts.put_term(key, {"picks": 5})
    assert facts.drop_interest_ids([YOGA]) == []


def test_corrupt_language_id_is_a_miss_not_a_crash(facts):
    facts.put_term(facts.term_key("locale", tr.norm("Spanish")), {"id": "²", "name": "Spanish (All)"})
    meta = Meta()
    r = cr.TargetingResolver(meta, facts=facts)
    assert r.locale("Spanish") == 23 and meta.calls == 1


def test_short_interest_id_is_never_written_and_reads_as_a_miss(store, facts):
    warm(facts, "Swing trading", "61")
    meta = Meta(search={"Swing trading": [{"id": "61", "name": "Swing trading"}]})
    run(meta, store, facts, "Swing trading")
    assert "adinterest" in meta.types()
    assert warm_id(facts, "Swing trading") == "61"      # не перезаписан: 61 в кэш не пишется
    assert not any("61" in (p.get("interest_fbid_list") or []) for p in meta.log)


def test_candidates_are_never_cached(store, facts):
    run(Meta(search={"Forex": [{"id": YOGA, "name": "Forex Market"}]}), store, facts, "Forex")
    assert warm_id(facts, "Forex") is None


def test_confirmed_pick_beats_the_warm_one(store, facts):
    store.put("Yoga", id=GOLF, type="interests", name="Golf")
    warm(facts, "Yoga", YOGA)
    _, res = run(Meta(valid={"Golf": GOLF, "Yoga": YOGA}), store, facts, "Yoga")
    assert res["Yoga"]["state"] == "confirmed" and res["Yoga"]["picks"][0]["id"] == GOLF


# ── сброс по отказу Meta ──

def test_only_graph_targeting_codes_point_at_the_cache():
    assert fc.kinds_for_error(g.GraphError("x", code=100, subcode=1487694)) == {"targeting"}
    assert fc.kinds_for_error(g.GraphError("x", code=100, subcode=2446394)) == {"targeting"}
    assert fc.kinds_for_error(g.GraphError("x", code=2446394)) == {"targeting"}
    assert fc.kinds_for_error(g.GraphError("targeting option is invalid", code=100)) == set()
    assert fc.kinds_for_error(cr.PlanError("Country 'XX' was not found by Meta targeting search")) == set()
    assert fc.kinds_for_error(cr.TargetingUnresolved([{"term": "a", "state": "unresolved", "candidates": [],
                                                       "reason": "no longer returned"}])) == set()


def test_meta_refusing_targeting_drops_what_the_job_used(store, facts):
    resolver = cr.TargetingResolver(Meta(valid={"Yoga": YOGA}), store=store, facts=facts)
    resolver.country("MX")
    resolver.interests([["Yoga"]])
    assert warm_id(facts, "Yoga") == YOGA
    creator = cr.CampaignCreator.__new__(cr.CampaignCreator)
    creator.resolver = resolver
    creator._reset_stale_facts(g.GraphError("gone", code=100, subcode=1487694))
    assert warm_id(facts, "Yoga") is None
    assert facts.get_term(facts.term_key("country", "mx")) is None


# ── файл и срок ──

def test_ttl_is_fixed_from_writing_and_zero_switches_off(tmp_path, monkeypatch):
    now = [1000.0]
    f = fc.FactsCache(path=tmp_path / "f.json", clock=lambda: now[0])
    f.put_term("k", {"a": 1})
    now[0] += 60 * 86400
    assert f.get_term("k") == {"a": 1}
    now[0] += 1
    assert f.get_term("k") is None
    monkeypatch.setenv(fc.DAYS_ENV, "0")
    f.put_term("k2", {"a": 1})
    assert f.get_term("k2") is None


def test_clear_and_drop_work_when_switched_off(tmp_path, monkeypatch):
    f = fc.FactsCache(path=tmp_path / "f.json")
    f.put_term("k", {"a": 1})
    monkeypatch.setenv(fc.DAYS_ENV, "0")
    f.clear()
    monkeypatch.delenv(fc.DAYS_ENV)
    assert f.get_term("k") is None


def test_revalidation_does_not_extend_the_life(store, tmp_path):
    now = [1000.0]
    f = fc.FactsCache(path=tmp_path / "f.json", clock=lambda: now[0])
    warm(f, "Yoga", YOGA)
    now[0] += 59 * 86400
    run(Meta(valid={"Yoga": YOGA}), store, f, "Yoga")
    now[0] += 2 * 86400
    assert warm_id(f, "Yoga") is None


def test_corrupt_file_is_an_empty_cache_and_holds_only_terms(tmp_path):
    p = tmp_path / "f.json"
    p.write_text("{not json", encoding="utf-8")
    f = fc.FactsCache(path=p)
    assert f.get_term("k") is None
    f.put_term("k", {"a": 1})
    assert set(json.loads(p.read_text(encoding="utf-8"))) == {"version", "terms"}


# ── кабинет всегда свежий ──

class Account:
    def __init__(self, account_id):
        self.account_id, self.calls = account_id, []

    def ad_accounts(self):
        return [{"account_id": "1234567890", "account_status": 1}]

    def account(self, act):
        self.calls.append("account")
        return {"account_id": self.account_id, "currency": "USD", "timezone_name": "UTC"}


def test_empty_account_id_in_the_answer_is_a_mismatch():
    with pytest.raises(cr.PlanError, match="different ad account"):
        cr.preflight_account(Account(""), "act_1234567890")


def test_no_account_fact_is_ever_written(store, facts):
    cr.TargetingResolver(Meta(valid={"Yoga": YOGA}), store=store, facts=facts).interests([["Yoga"]])
    data = json.loads(facts.path.read_text(encoding="utf-8"))
    assert all(k.split("|")[0] in ("term", "country", "locale") for k in data["terms"])


# ── «Clear» в Check plan ──

def test_clear_forgets_the_term_in_both_caches(tmp_path, monkeypatch):
    import asyncio

    monkeypatch.setenv("ACL_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("ACL_FB_BACKEND", "api")
    from src.web import app as web

    class Req:
        def __init__(self, body):
            self.body = body

        async def json(self):
            return self.body

    f = fc.default()
    warm(f, "Yoga", YOGA)
    warm(f, "Golf", GOLF)
    assert asyncio.run(web.targeting_resolver_clear(Req({"term": "yoga"}))).status_code == 200
    assert warm_id(f, "Yoga") is None and warm_id(f, "Golf") == GOLF
    asyncio.run(web.targeting_resolver_clear(Req({"all": True})))
    assert warm_id(f, "Golf") is None


def test_cached_all_group_without_members_stays_a_group(facts):
    # ACL-1907: «Spanish (All)» без известных членов должна оставаться группой и после чтения из кэша
    class OnlyAll(Meta):
        def get_all(self, path, params=None, *, max_pages=5):
            if (params or {}).get("type") == "adlocale":
                self.calls += 1
                return [{"key": 1002, "name": "Spanish (All)"}]
            return super().get_all(path, params, max_pages=max_pages)

    one = cr.TargetingResolver(OnlyAll(), facts=facts)
    assert one.locale("Spanish") == 1002 and one.locale_groups == {1002: {}}
    meta = OnlyAll()
    two = cr.TargetingResolver(meta, facts=facts)
    assert two.locale("Spanish") == 1002 and meta.calls == 0
    assert two.locale_groups == {1002: {}}


def test_future_dated_entry_is_stale(tmp_path):
    now = [1_000_000.0]
    f = fc.FactsCache(path=tmp_path / "f.json", clock=lambda: now[0])
    f.put_term("term|a", {"picks": []})
    assert f.get_term("term|a") is not None
    now[0] -= 2 * 3600            # часы сдвинули назад: запись «из будущего»
    assert f.get_term("term|a") is None
    now[0] += 3600                # в пределах допуска на рассинхрон
    assert f.get_term("term|a") is not None
