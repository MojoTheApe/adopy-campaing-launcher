"""ACL-1771: резолвер детального таргетинга (интересы, поведения, должности, работодатели).

Только фейки, ни одного живого вызова Meta. Проверяется:
* точное имя без учёта регистра/разделителей, но НЕ слитые символы и диакритика (C++ != C);
* запасной поиск adinterest, список поведений постранично, adworkposition/adworkemployer,
  учебные типы только при близком кандидате;
* «A or B» - кандидат с разовым подтверждением, а не автоматический выбор;
* близкое совпадение никогда не берётся само;
* подтверждение -> кэш (ключ: тип + термин) -> повторное использование; перепроверка id;
* бюджет считается в ЧТЕНИЯХ (каждая страница), нехватка даёт unknown;
* правило pre-flight, блок публикации и эндпоинты (шлюз API, только показанные кандидаты).
"""
from __future__ import annotations

import asyncio
import json as _j
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_api import creator as cr  # noqa: E402
from src.fb_api import graph as g  # noqa: E402
from src.fb_api import preflight_rules as pf  # noqa: E402
from src.fb_api import targeting_resolver as tr  # noqa: E402
from tests.test_acl1761_api_create import ACT, _draft  # noqa: E402
from tests.test_acl1775_instant_forms import FormMeta  # noqa: E402

PAGE_SIZE = 3


class FakeSearch:
    """Фейк GraphClient.get_all: `/search` по таблице; страницы по 3 строки.

    `calls` растёт на число прочитанных страниц, как у настоящего клиента.
    """

    def __init__(self, *, valid=None, interests=None, behaviors=None, typed=None, max_calls=None):
        #: норм. имя -> (каноническое имя, id) для adinterestvalid
        self.valid = {tr.norm(k): v for k, v in (valid or {}).items()}
        self.interests = interests or {}          # q -> [{id,name}]
        self.behaviors = behaviors or []          # полный список поведений
        self.typed = typed or {}                  # (type, q) -> [{id,name}]
        self.log: list[tuple[str, int, dict]] = []
        self.calls = 0
        if max_calls is not None:
            self.max_calls = max_calls

    def types(self) -> list[str]:
        return [t for t, _, _ in self.log]

    def get_all(self, path, params=None, *, max_pages=5):
        assert path == "/search"
        t = params["type"]
        if t == "adinterestvalid":
            rows = []
            for n in params.get("interest_list", []):
                hit = self.valid.get(tr.norm(n))
                rows.append({"name": hit[0], "valid": True, "id": hit[1]} if hit else {"name": n, "valid": False})
            known = {i for _, i in self.valid.values()}
            for i in params.get("interest_fbid_list", []):
                rows.append({"id": i, "valid": True, "name": f"id {i}"} if i in known else {"id": i, "valid": False})
            data = rows
        elif t == "adinterest":
            data = self.interests.get(params["q"], self.interests.get(tr.norm(params["q"]), []))
        elif t == "adTargetingCategory":
            assert params["class"] == "behaviors", "обзор классов - только поведения"
            data = self.behaviors
        else:
            data = self.typed.get((t, params["q"]), [])
        pages = max(1, min(max_pages, -(-len(data) // PAGE_SIZE)))
        self.calls += pages
        self.log.append((t, pages, dict(params)))
        return data[:max_pages * PAGE_SIZE]


def _store(tmp_path):
    return tr.ConfirmedStore(tmp_path / "confirmed.json")


def _resolver(client, tmp_path, **kw):
    return tr.TermResolver(client, _store(tmp_path), **kw)


def _one(r, term):
    r.prefetch([term])
    return r.resolve(term)


# ───────────────────────────── сравнение имён ─────────────────────────────

@pytest.mark.parametrize("a,b", [("C++", "C"), ("C#", "C"), ("C++", "C#"), (".NET", "NET"), ("AT&T", "AT T"),
                                 ("AT&T", "ATT"), ("R&B", "R B"), ("año", "ano"), ("Йога", "Иога"),
                                 ("Pokémon", "Pokemon")])
def test_norm_keeps_symbols_and_diacritics(a, b):
    assert tr.norm(a) != tr.norm(b)


@pytest.mark.parametrize("a,b", [("Forex Trading", "forex  trading"), ("Hip-Hop", "hip hop"), ("a_b", "A/B"),
                                 ("YOGA", "yoga"), ("Año", "año")])
def test_norm_ignores_case_whitespace_and_dash_underscore_slash(a, b):
    assert tr.norm(a) == tr.norm(b)


def test_a_symbol_collision_is_never_an_exact_hit(tmp_path):
    c = FakeSearch(interests={"C++": [{"id": "1", "name": "C"}, {"id": "2", "name": "C#"}]})
    res = _one(_resolver(c, tmp_path), "C++")
    assert res["state"] != "exact" and res["picks"] == []
    c2 = FakeSearch(interests={"AT&T": [{"id": "3", "name": "AT T"}]})
    assert _one(_resolver(c2, tmp_path), "AT&T")["state"] != "exact"
    c3 = FakeSearch(interests={"año": [{"id": "4", "name": "ano"}]})
    assert _one(_resolver(c3, tmp_path), "año")["state"] != "exact"
    c4 = FakeSearch(interests={"Йога": [{"id": "5", "name": "Иога"}]})
    assert _one(_resolver(c4, tmp_path), "Йога")["state"] != "exact"


# ───────────────────────────── порядок поиска ─────────────────────────────

def test_exact_hit_in_any_case_is_taken_with_one_read(tmp_path):
    c = FakeSearch(valid={"yoga": ("Yoga", "11")})
    r = _resolver(c, tmp_path)
    for spelling in ("YOGA", "yoga", "Yoga"):
        res = _one(r, spelling)
        assert res["state"] == "exact" and res["picks"][0] == {"id": "11", "type": "interests", "name": "Yoga"}
    assert c.types() == ["adinterestvalid"]


def test_search_fallback_finds_exact_name(tmp_path):
    c = FakeSearch(interests={"forex trading": [{"id": "5", "name": "Forex Trading"},
                                                {"id": "6", "name": "Forex Trading Tips"}]})
    res = _one(_resolver(c, tmp_path), "forex  trading")
    assert res["state"] == "exact" and res["picks"] == [{"id": "5", "type": "interests", "name": "Forex Trading"}]
    assert c.types() == ["adinterestvalid", "adinterest"]


def test_behaviors_paged_and_work_lookups_use_documented_search_types(tmp_path):
    behaviors = [{"id": str(i), "name": f"Filler {i}"} for i in range(1, 8)] + [{"id": "99", "name": "Frequent Travelers"}]
    c = FakeSearch(behaviors=behaviors,
                   typed={("adworkposition", "Marketing Manager"): [{"id": "71", "name": "Marketing Manager"}],
                          ("adworkemployer", "Plus500"): [{"id": "72", "name": "Plus500"}]})
    r = _resolver(c, tmp_path)
    assert _one(r, "frequent travelers")["picks"] == [{"id": "99", "type": "behaviors", "name": "Frequent Travelers"}]
    assert _one(r, "Marketing Manager")["picks"][0]["type"] == "work_positions"
    assert _one(r, "Plus500")["picks"][0]["type"] == "work_employers"
    beh = [pages for t, pages, p in c.log if t == "adTargetingCategory"]
    assert beh == [3], "список поведений читается один раз и постранично"
    assert "adTargetingCategory" not in [t for t, _, p in c.log if p.get("class") != "behaviors"]


def test_education_searches_run_only_when_something_is_near(tmp_path):
    c = FakeSearch(typed={("adeducationmajor", "Computer Science"): [{"id": "81", "name": "Computer Science"}]})
    res = _one(_resolver(c, tmp_path), "Computer Science")
    assert res["state"] != "exact" and "adeducationschool" not in c.types(), "нет близкого - учебные типы пропущены"
    near = FakeSearch(interests={"Computer Science": [{"id": "1", "name": "Computer Sciences"}]},
                      typed={("adeducationmajor", "Computer Science"): [{"id": "81", "name": "Computer Science"}]})
    res = _one(_resolver(near, tmp_path), "Computer Science")
    assert res["state"] == "exact" and res["picks"][0]["type"] == "education_majors"


def test_interests_builds_flexible_spec_with_all_buckets(tmp_path):
    c = FakeSearch(valid={"plus500": ("Plus500", "31")}, behaviors=[{"id": "9", "name": "Expats"}])
    spec = cr.TargetingResolver(c, _store(tmp_path)).interests([["Plus500", "expats"]])
    assert spec == [{"interests": [{"id": "31", "name": "Plus500"}], "behaviors": [{"id": "9", "name": "Expats"}]}]


# ───────────────────────────── «A or B» - только с подтверждением ─────────────────────────────

def _alpari():
    return FakeSearch(typed={("adworkemployer", "Alpari Group"): [{"id": "31", "name": "Alpari Group"}]},
                      valid={"plus500": ("Plus500", "32")})


def test_split_of_exact_parts_is_a_candidate_needing_one_time_confirm(tmp_path):
    r = _resolver(_alpari(), tmp_path)
    res = _one(r, "Alpari Group or Plus500")
    assert res["state"] == "pending" and res["picks"] == [], "само не берётся"
    cand = res["suggested"]
    assert cand["type"] == tr.SPLIT and cand["name"] == "Alpari Group + Plus500" and cand["id"] == "31+32"
    assert {(p["type"], p["name"]) for p in cand["picks"]} == {("work_employers", "Alpari Group"),
                                                              ("interests", "Plus500")}


def test_split_blocks_the_spec_until_confirmed_then_reuses_the_cache(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(cr.TargetingUnresolved, match=r"picked 'Alpari Group \+ Plus500' \[split\] for 'Alpari Group or Plus500'\?"):
        cr.TargetingResolver(_alpari(), store).interests([["Alpari Group or Plus500"]])
    cand = _one(_resolver(_alpari(), tmp_path), "Alpari Group or Plus500")["suggested"]
    store.put("Alpari Group or Plus500", id=cand["id"], type=cand["type"], name=cand["name"], picks=cand["picks"])
    later = FakeSearch(valid={"plus500": ("Plus500", "32")})
    spec = cr.TargetingResolver(later, store).interests([["alpari group OR plus500"]])
    assert spec == [{"work_employers": [{"id": "31", "name": "Alpari Group"}],
                     "interests": [{"id": "32", "name": "Plus500"}]}]
    assert later.types() == ["adinterestvalid"], "один пакет перепроверки, поисков нет"


def test_one_bad_part_of_a_split_is_reported_alone(tmp_path):
    c = FakeSearch(valid={"plus500": ("Plus500", "32")}, interests={"Alpari Group": [{"id": "5", "name": "Alpari Grp Ltd"}]})
    with pytest.raises(cr.TargetingUnresolved) as ei:
        cr.TargetingResolver(c, _store(tmp_path)).interests([["Alpari Group or Plus500"]])
    assert [i["term"] for i in ei.value.items] == ["Alpari Group"]


# ───────────────────────────── никакой молчаливой подмены ─────────────────────────────

NEAR = {"forex trading": [{"id": "6", "name": "Forex Trading Tips"}, {"id": "7", "name": "Forex Trader"}]}


def test_a_near_match_is_never_auto_picked(tmp_path):
    res = _one(_resolver(FakeSearch(interests=NEAR), tmp_path), "forex trading")
    assert res["state"] == "pending" and res["picks"] == []
    assert {x["name"] for x in res["candidates"]} == {"Forex Trading Tips", "Forex Trader"}


def test_a_near_match_blocks_the_flexible_spec_with_picked_x_for_y(tmp_path):
    with pytest.raises(cr.TargetingUnresolved) as ei:
        cr.TargetingResolver(FakeSearch(interests=NEAR), _store(tmp_path)).interests([["forex trading"]])
    assert "picked 'Forex Trad" in str(ei.value) and "for 'forex trading'?" in str(ei.value)


def test_nothing_close_is_unresolved_and_named(tmp_path):
    with pytest.raises(cr.TargetingUnresolved, match="Detailed targeting not found by Meta: Zzzz; no candidates"):
        cr.TargetingResolver(FakeSearch(), _store(tmp_path)).interests([["Zzzz"]])


# ───────────────────────────── подтверждение, кэш, ключ, перепроверка ─────────────────────────────

def test_confirm_then_cache_then_reuse_with_one_revalidation_read(tmp_path):
    store = _store(tmp_path)
    store.put("forex trading", id="6", type="interests", name="Forex Trading Tips")
    c = FakeSearch(valid={"x": ("Forex Trading Tips", "6")})
    spec = cr.TargetingResolver(c, store).interests([["Forex  Trading"]])
    assert spec == [{"interests": [{"id": "6", "name": "Forex Trading Tips"}]}]
    assert c.types() == ["adinterestvalid"], "перепроверка в том же пакете, поисков нет"
    assert c.log[0][2]["interest_fbid_list"] == ["6"]
    assert c.calls == 1


def test_a_cached_id_that_meta_no_longer_returns_drops_to_needs_confirm(tmp_path):
    store = _store(tmp_path)
    store.put("forex trading", id="6", type="interests", name="Forex Trading Tips")
    c = FakeSearch(interests=NEAR)          # id 6 Meta не знает (valid пуст)
    with pytest.raises(cr.TargetingUnresolved) as ei:
        cr.TargetingResolver(c, store).interests([["forex trading"]])
    assert ei.value.items[0]["state"] == "pending"
    assert "no longer returned" in ei.value.items[0]["reason"]


def test_cache_key_has_the_type_so_other_type_does_not_overwrite(tmp_path):
    s = _store(tmp_path)
    s.put("Marketing", id="1", type="interests", name="Marketing")
    s.put("marketing", id="2", type="work_positions", name="Marketing")
    assert {(e["type"], e["id"]) for e in s.get_all("MARKETING")} == {("interests", "1"), ("work_positions", "2")}
    s.put("Marketing", id="3", type="interests", name="Marketing 2")        # тот же тип - заменяет
    assert {(e["type"], e["id"]) for e in s.get_all("marketing")} == {("interests", "3"), ("work_positions", "2")}
    assert len(s.entries()) == 2
    assert s.remove("marketing") is True and s.entries() == []


def test_store_rejects_bad_ids_and_types_and_clears_all(tmp_path):
    s = _store(tmp_path)
    with pytest.raises(ValueError):
        s.put("x", id="abc", type="interests", name="X")
    with pytest.raises(ValueError):
        s.put("x", id="1", type="countries", name="X")
    with pytest.raises(ValueError):
        s.put("x", id="1+2", type="split", name="X", picks=[{"id": "q", "type": "interests"}])
    s.put("a", id="1", type="behaviors", name="A")
    s.put("b", id="2", type="work_positions", name="B")
    assert s.clear() == 2 and s.entries() == []


def test_corrupt_cache_file_is_treated_as_empty(tmp_path):
    p = tmp_path / "confirmed.json"
    p.write_text("{not json", encoding="utf-8")
    assert tr.ConfirmedStore(p).get("anything") is None


def test_default_store_lives_in_the_local_config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ACL_CONFIG_DIR", str(tmp_path))
    tr.ConfirmedStore().put("t", id="1", type="interests", name="T")
    assert (tmp_path / tr.CACHE_FILE).is_file()


# ───────────────────────────── бюджет в чтениях ─────────────────────────────

def test_reads_are_counted_per_page_not_per_get_all(tmp_path):
    rows = [{"id": str(i), "name": f"Other {i}"} for i in range(7)]       # 3 страницы по 3
    c = FakeSearch(interests={"Nothing": rows})
    r = _resolver(c, tmp_path)
    _one(r, "Nothing")
    assert r.reads == c.calls, "счётчик резолвера = настоящие чтения клиента"
    assert any(pages == 2 for t, pages, _ in c.log if t == "adinterest"), "две страницы - два чтения, не одно"
    assert r.reads > len(c.log)


def test_reads_match_a_real_client_page_by_page(tmp_path):
    class Meta:
        def __init__(self):
            self.gets = 0

        def request(self, method, path, params=None):
            self.gets += 1
            if params["type"] == "adinterest" and not params.get("after"):
                return {"status": 200, "body": {"data": [{"id": "1", "name": "Nope One"}],
                                                "paging": {"cursors": {"after": "p2"}, "next": "u"}}}
            return {"status": 200, "body": {"data": []}}

    meta = Meta()
    client = g.GraphClient(meta, allow_writes=False, sleep=lambda s: None, min_interval_s=0.0, max_calls=60)
    r = _resolver(client, tmp_path)
    _one(r, "Zzz")
    assert r.reads == client.calls == meta.gets and meta.gets >= 5


def test_per_term_cap_is_eight_reads_and_the_result_is_unknown(tmp_path):
    rows = [{"id": str(i), "name": f"Other {i}"} for i in range(6)]
    c = FakeSearch(interests={"Nothing": rows}, behaviors=rows, typed={("adworkposition", "Nothing"): rows})
    r = _resolver(c, tmp_path, term_cap=3)
    res = _one(r, "Nothing")
    assert res["state"] == "unknown" and res["reason"] == tr.UNKNOWN_MESSAGE
    assert r.reads <= 1 + 3 + 3, "пакет + не больше потолка термина + общий список поведений"


def test_unknown_blocks_with_the_press_check_plan_again_message(tmp_path):
    c = FakeSearch()
    r = cr.TargetingResolver(c, _store(tmp_path))
    r.terms.term_cap = 1
    with pytest.raises(cr.TargetingUnresolved) as ei:
        r.interests([["Nothing close"]])
    assert str(ei.value) == "Couldn't check all interests — press Check plan again"


def test_client_reserve_and_job_cap_stop_the_search(tmp_path):
    c = FakeSearch(max_calls=5)
    r = _resolver(c, tmp_path)
    res = _one(r, "Nothing here")
    assert c.calls <= 5 - tr.CLIENT_RESERVE and res["state"] == "unknown"
    j = _resolver(FakeSearch(), tmp_path, job_cap=2)
    assert _one(j, "One")["state"] == "unknown" or j.reads <= 2
    assert j.reads <= 2


def test_each_term_is_resolved_once_per_job(tmp_path):
    c = FakeSearch(valid={"yoga": ("Yoga", "11")})
    r = _resolver(c, tmp_path)
    for _ in range(3):
        _one(r, "yoga")
    assert c.calls == 1


def test_behaviors_list_is_shared_between_terms(tmp_path):
    c = FakeSearch(behaviors=[{"id": "9", "name": "Expats"}])
    r = _resolver(c, tmp_path)
    _one(r, "nope one")
    _one(r, "nope two")
    assert sum(1 for t, _, _ in c.log if t == "adTargetingCategory") == 1


# ───────────────────────────── pre-flight и блок публикации ─────────────────────────────

class TargetMeta(FormMeta):
    def __init__(self, *, interests=None, valid=None, typed=None, **kw):
        super().__init__(**kw)
        self.interests, self.typed = interests or {}, typed or {}
        self.valid = {tr.norm(k): v for k, v in (valid or {}).items()}

    def _get(self, path, params):
        if path == "/search":
            t = params["type"]
            if t == "adinterestvalid":
                rows = [{"name": self.valid[tr.norm(n)][0], "valid": True, "id": self.valid[tr.norm(n)][1]}
                        if tr.norm(n) in self.valid else {"name": n, "valid": False}
                        for n in params.get("interest_list", [])]
                known = {i for _, i in self.valid.values()}
                rows += [{"id": i, "valid": i in known} for i in params.get("interest_fbid_list", [])]
                return {"data": rows}
            if t == "adinterest":
                return {"data": self.interests.get(params["q"], [])}
            if t == "adTargetingCategory":
                return {"data": []}
            if t in ("adworkposition", "adworkemployer", "adeducationschool", "adeducationmajor"):
                return {"data": self.typed.get((t, params["q"]), [])}
        return super()._get(path, params)


def _audit(meta, tmp_path, label, store, max_calls=80):
    d = _draft(tmp_path, ads=1)
    d.ad_sets[0].targeting_label = label
    client = g.GraphClient(meta, allow_writes=False, sleep=lambda s: None, min_interval_s=0.0, max_calls=max_calls)
    facts = cr.preflight_account(client, ACT)
    creator = cr.CampaignCreator(client, facts, cr.TargetingResolver(client, store), sleep=lambda s: None)
    out = creator.plan_only(cr.plan_from_draft(d))
    assert client.writes == 0 and meta.posts == []
    return out, {v["rule"]: v for v in out.audit}


def test_preflight_unresolved_term_is_an_error_naming_term_and_candidates(tmp_path):
    meta = TargetMeta(interests={"Qwerty": [{"id": "5", "name": "Qwertz Keyboards"}]})
    out, got = _audit(meta, tmp_path, "Qwerty", _store(tmp_path))
    assert out.stop_kind == "plan" and "Qwerty" in out.error, "публикация не пойдёт"
    v = got["interests_resolve"]
    assert v["status"] == "error" and "'Qwerty'" in v["detail"]


def test_preflight_pending_confirmation_is_an_error_with_a_confirm_action(tmp_path):
    meta = TargetMeta(interests={"forex trading": [{"id": "6", "name": "Forex Trading Tips"}]})
    out, got = _audit(meta, tmp_path, "forex trading", _store(tmp_path))
    v = got["interests_confirmed"]
    assert v["status"] == "error" and v["action"] == "confirm"
    assert "picked 'Forex Trading Tips'" in v["detail"] and v["items"][0]["candidates"]
    assert got["interests_resolve"]["status"] == "ok" and pf.blocks(out.audit)
    assert out.targeting and out.targeting[0]["state"] == "pending"


def test_preflight_split_needs_confirm_then_passes(tmp_path):
    store = _store(tmp_path)
    meta = TargetMeta(valid={"plus500": ("Plus500", "32")},
                      typed={("adworkemployer", "Alpari Group"): [{"id": "31", "name": "Alpari Group"}]})
    out, got = _audit(meta, tmp_path, "Alpari Group or Plus500", store)
    assert got["interests_confirmed"]["status"] == "error" and got["interests_confirmed"]["action"] == "confirm"
    cand = out.targeting[0]["suggested"]
    store.put("Alpari Group or Plus500", id=cand["id"], type=cand["type"], name=cand["name"], picks=cand["picks"])
    out, got = _audit(meta, tmp_path, "Alpari Group or Plus500", store)
    assert out.error == "" and got["interests_confirmed"]["status"] == "ok"
    flex = out.plan["ad_sets"][0]["targeting"]["flexible_spec"] if "targeting" in out.plan["ad_sets"][0] else None
    assert flex is None or flex[0].keys() == {"work_employers", "interests"}


def test_preflight_passes_after_confirmation(tmp_path):
    store = _store(tmp_path)
    store.put("forex trading", id="6", type="interests", name="Forex Trading Tips")
    meta = TargetMeta(valid={"x": ("Forex Trading Tips", "6")})
    out, got = _audit(meta, tmp_path, "forex trading", store)
    assert out.error == "" and got["interests_confirmed"]["status"] == "ok" and got["interests_resolve"]["status"] == "ok"
    assert out.targeting[0]["state"] == "confirmed"


def test_preflight_budget_ran_out_is_unknown_and_the_plan_still_blocks(tmp_path):
    meta = TargetMeta()
    out, got = _audit(meta, tmp_path, "Nothing close", _store(tmp_path), max_calls=9)
    assert got["interests_resolve"]["status"] == "unknown"
    assert "press Check plan again" in got["interests_resolve"]["detail"]
    assert out.stop_kind == "plan" and "press Check plan again" in out.error, "план с ошибкой: Publish не пройдёт"


def test_the_two_new_rules_are_in_the_table_and_are_server_errors():
    for rid in ("interests_resolve", "interests_confirmed"):
        assert pf.RULE_BY_ID[rid]["side"] == "server" and pf.RULE_BY_ID[rid]["severity"] == "error"


# ───────────────────────────── эндпоинты ─────────────────────────────

class _Req:
    def __init__(self, body):
        self._b = body

    async def json(self):
        return self._b


def _call(fn, body=None):
    r = asyncio.run(fn(_Req(body)) if body is not None else fn())
    return r.status_code, _j.loads(r.body)


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.setenv("ACL_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("ACL_FB_BACKEND", "api")
    tr.forget_candidates()
    from src.web import app as web
    return web


def test_endpoints_need_the_api_backend_like_publish_plan(web, monkeypatch):
    monkeypatch.setenv("ACL_FB_BACKEND", "rpa")
    for fn, body in ((web.targeting_resolver_cache, None), (web.targeting_resolver_confirm, {"term": "x"}),
                     (web.targeting_resolver_clear, {"all": True})):
        assert _call(fn, body)[0] == 409


def test_confirm_accepts_only_candidates_of_the_last_check_plan(web):
    # Кандидатов ещё не показывали.
    assert _call(web.targeting_resolver_confirm, {"term": "Forex Trading", "id": "6", "type": "interests",
                                                   "name": "Evil"})[0] == 400
    tr.register_candidates([{"term": "Forex Trading", "state": "pending",
                             "candidates": [{"id": "6", "type": "interests", "name": "Forex Trading Tips", "score": 0.9}],
                             "suggested": {"id": "6", "type": "interests", "name": "Forex Trading Tips", "score": 0.9}}])
    assert _call(web.targeting_resolver_confirm, {"term": "Forex Trading", "id": "7", "type": "interests",
                                                   "name": "x"})[0] == 400, "другой id"
    assert _call(web.targeting_resolver_confirm, {"term": "Forex Trading", "id": "6", "type": "behaviors",
                                                   "name": "x"})[0] == 400, "другой тип"
    assert _call(web.targeting_resolver_confirm, {"term": "Other", "id": "6", "type": "interests",
                                                   "name": "x"})[0] == 400, "другой термин"
    code, body = _call(web.targeting_resolver_confirm, {"term": "forex  trading", "id": "6", "type": "interests",
                                                         "name": "Spoofed name"})
    assert code == 200 and body["entry"]["name"] == "Forex Trading Tips", "имя берётся у кандидата, не из запроса"
    assert [e["id"] for e in _call(web.targeting_resolver_cache)[1]["entries"]] == ["6"]


def test_a_new_check_plan_replaces_the_candidates_of_a_term(web):
    tr.register_candidates([{"term": "T", "candidates": [{"id": "1", "type": "interests", "name": "A"}]}])
    tr.register_candidates([{"term": "T", "candidates": [{"id": "2", "type": "interests", "name": "B"}]}])
    assert _call(web.targeting_resolver_confirm, {"term": "T", "id": "1", "type": "interests"})[0] == 400
    assert _call(web.targeting_resolver_confirm, {"term": "T", "id": "2", "type": "interests"})[0] == 200


def test_split_candidate_is_confirmable_and_clear_works(web):
    cand = tr.split_candidate([{"id": "31", "type": "work_employers", "name": "Alpari Group"},
                               {"id": "32", "type": "interests", "name": "Plus500"}])
    tr.register_candidates([{"term": "Alpari Group or Plus500", "state": "pending", "candidates": [cand],
                             "suggested": cand}])
    assert _call(web.targeting_resolver_confirm, {"term": "Alpari Group or Plus500", "id": "31+32",
                                                   "type": "split"})[0] == 200
    entry = _call(web.targeting_resolver_cache)[1]["entries"][0]
    assert entry["type"] == "split" and len(entry["picks"]) == 2
    assert _call(web.targeting_resolver_clear, {})[0] == 400, "без термина и без all - отказ"
    assert _call(web.targeting_resolver_clear, {"term": "alpari group or plus500"})[1]["removed"] == 1
    assert _call(web.targeting_resolver_cache)[1]["entries"] == []
