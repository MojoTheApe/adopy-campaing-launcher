"""ACL-1771: резолвер детального таргетинга (интересы, поведения, должности, работодатели).

Только фейки, ни одного живого вызова Meta. Проверяется:
* точное имя в любом регистре; запасной поиск `adinterest`; постраничные классы
  behaviors / work_positions / work_employers; деление «A or B»;
* близкое совпадение НИКОГДА не берётся само (в том числе мутационными проверками
  у `tests/mutation_acl1771.py`-подобных правок в самом тесте ниже);
* подтверждение -> локальный кэш -> повторное использование без вызовов; очистка;
* бюджет вызовов: кэш на задание, потолок термина, запас клиента;
* правило pre-flight (ошибка с кандидатами, ошибка с Confirm) и блок публикации.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_api import creator as cr  # noqa: E402
from src.fb_api import graph as g  # noqa: E402
from src.fb_api import targeting_resolver as tr  # noqa: E402
from tests.test_acl1761_api_create import ACT, _draft  # noqa: E402
from tests.test_acl1775_instant_forms import FormMeta  # noqa: E402

PAGE_SIZE = 3


class FakeSearch:
    """Фейк GraphClient.get_all: `/search` по таблице; страницы по 3 строки, max_pages режет хвост."""

    def __init__(self, *, valid=None, interests=None, classes=None, class_search=None, max_calls=None):
        #: нормализованное имя -> каноническое имя (adinterestvalid)
        self.valid = {tr.norm(k): v for k, v in (valid or {}).items()}
        self.interests = interests or {}          # q -> [{id,name}]
        self.classes = classes or {}              # class -> [{id,name}] (полный список, без q)
        self.class_search = class_search or {}    # (class, q) -> [{id,name}]
        self.log: list[tuple[str, int]] = []      # (type[/class], max_pages)
        self.calls = 0                            # как у GraphClient: счётчик вызовов
        if max_calls is not None:
            self.max_calls = max_calls

    def get_all(self, path, params=None, *, max_pages=5):
        assert path == "/search"
        t = params["type"]
        self.log.append((t + ("/" + params["class"] if "class" in params else ""), max_pages))
        self.calls += 1
        if t == "adinterestvalid":
            return [{"name": self.valid[tr.norm(n)], "valid": True, "id": f"1{abs(hash(n)) % 10**6}"}
                    if tr.norm(n) in self.valid else {"name": n, "valid": False}
                    for n in params["interest_list"]]
        if t == "adinterest":
            rows = self.interests.get(params["q"], self.interests.get(tr.norm(params["q"]), []))
        elif t == "adTargetingCategory":
            cls = params["class"]
            rows = (self.classes.get(cls, []) if "q" not in params
                    else self.class_search.get((cls, params["q"]), []))
        else:
            rows = []
        return rows[:max_pages * PAGE_SIZE]


def _mk(**kw):
    c = FakeSearch(**kw)
    return c


def _store(tmp_path):
    return tr.ConfirmedStore(tmp_path / "confirmed.json")


def _resolver(client, tmp_path, **kw):
    return tr.TermResolver(client, _store(tmp_path), **kw)


# ───────────────────────────── порядок поиска ─────────────────────────────

def test_exact_hit_in_any_case_is_taken_with_one_call(tmp_path):
    c = _mk(valid={"yoga": "Yoga"})
    r = _resolver(c, tmp_path)
    for spelling in ("YOGA", "yoga", "Yoga!"):
        res = TermResolver_resolve(r, spelling)
        assert res["state"] == "exact" and res["picks"][0]["name"] == "Yoga" and res["picks"][0]["type"] == "interests"
    assert [t for t, _ in c.log] == ["adinterestvalid"], "один пакетный вызов, поиск не нужен"


def TermResolver_resolve(r, term):
    r.prefetch([term])
    return r.resolve(term)


def test_search_fallback_finds_exact_name_ignoring_case_and_punctuation(tmp_path):
    c = _mk(interests={"forex trading": [{"id": "5", "name": "Forex Trading"}, {"id": "6", "name": "Forex Trading Tips"}]})
    res = TermResolver_resolve(_resolver(c, tmp_path), "forex  trading")
    assert res["state"] == "exact" and res["picks"] == [{"id": "5", "type": "interests", "name": "Forex Trading"}]
    assert [t for t, _ in c.log] == ["adinterestvalid", "adinterest"]


def test_behaviors_work_positions_and_employers_are_searched_paged(tmp_path):
    behaviors = [{"id": str(i), "name": f"Filler behavior {i}"} for i in range(1, 7)] + \
                [{"id": "99", "name": "Frequent Travelers"}]               # на 3-й странице: одна страница не хватила бы
    c = _mk(classes={"behaviors": behaviors},
            class_search={("work_positions", "Marketing Manager"): [{"id": "71", "name": "Marketing Manager"}],
                          ("work_employers", "Plus500"): [{"id": "72", "name": "Plus500"}]})
    r = _resolver(c, tmp_path)
    assert TermResolver_resolve(r, "frequent travelers")["picks"] == [
        {"id": "99", "type": "behaviors", "name": "Frequent Travelers"}]
    assert TermResolver_resolve(r, "Marketing Manager")["picks"][0]["type"] == "work_positions"
    assert TermResolver_resolve(r, "Plus500")["picks"][0]["type"] == "work_employers"
    pages = dict(c.log)
    assert pages["adTargetingCategory/behaviors"] >= 3, "поведения читаются постранично, а не одной страницей"
    assert sum(1 for t, _ in c.log if t == "adTargetingCategory/behaviors") == 1, "список поведений читается один раз"


def test_education_classes_are_searched_last(tmp_path):
    c = _mk(class_search={("education_majors", "Computer Science"): [{"id": "81", "name": "Computer Science"}]})
    res = TermResolver_resolve(_resolver(c, tmp_path), "Computer Science")
    assert res["picks"][0]["type"] == "education_majors"


def test_a_or_b_is_split_and_each_part_lands_in_its_bucket(tmp_path):
    c = _mk(class_search={("work_employers", "Alpari Group"): [{"id": "31", "name": "Alpari Group"}]},
            valid={"plus500": "Plus500"})
    r = _resolver(c, tmp_path)
    res = TermResolver_resolve(r, "Alpari Group or Plus500")
    assert res["state"] == "split"
    assert {(p["type"], p["name"]) for p in res["picks"]} == {("work_employers", "Alpari Group"), ("interests", "Plus500")}


def test_interests_builds_flexible_spec_with_all_buckets(tmp_path):
    c = _mk(class_search={("work_employers", "Alpari Group"): [{"id": "31", "name": "Alpari Group"}]},
            valid={"plus500": "Plus500"}, classes={"behaviors": [{"id": "9", "name": "Expats"}]})
    res = cr.TargetingResolver(c, _store(tmp_path))
    spec = res.interests([["Alpari Group or Plus500", "expats"]])
    assert len(spec) == 1
    assert spec[0]["work_employers"] == [{"id": "31", "name": "Alpari Group"}]
    assert spec[0]["behaviors"] == [{"id": "9", "name": "Expats"}]
    assert spec[0]["interests"][0]["name"] == "Plus500"


# ───────────────────────────── никакой молчаливой подмены ─────────────────────────────

NEAR = {"forex trading": [{"id": "6", "name": "Forex Trading Tips"}, {"id": "7", "name": "Forex Trader"}]}


def test_a_near_match_is_never_auto_picked(tmp_path):
    c = _mk(interests=NEAR)
    res = TermResolver_resolve(_resolver(c, tmp_path), "forex trading")
    assert res["state"] == "pending", "близкое совпадение - только предложение"
    assert res["picks"] == [] and res["suggested"]["name"] in ("Forex Trading Tips", "Forex Trader")
    assert {x["name"] for x in res["candidates"]} == {"Forex Trading Tips", "Forex Trader"}


def test_a_near_match_blocks_the_flexible_spec_with_picked_x_for_y(tmp_path):
    resolver = cr.TargetingResolver(_mk(interests=NEAR), _store(tmp_path))
    with pytest.raises(cr.TargetingUnresolved) as ei:
        resolver.interests([["forex trading"]])
    assert "picked 'Forex Trad" in str(ei.value) and "for 'forex trading'?" in str(ei.value)
    assert ei.value.items[0]["state"] == "pending" and ei.value.items[0]["candidates"]


def test_nothing_close_is_unresolved_and_named(tmp_path):
    resolver = cr.TargetingResolver(_mk(), _store(tmp_path))
    with pytest.raises(cr.TargetingUnresolved, match="Detailed targeting not found by Meta: Zzzz; no candidates"):
        resolver.interests([["Zzzz"]])


def test_one_bad_part_of_a_split_is_reported_alone(tmp_path):
    c = _mk(valid={"plus500": "Plus500"}, interests={"Alpari Group": [{"id": "5", "name": "Alpari Grp Ltd"}]})
    resolver = cr.TargetingResolver(c, _store(tmp_path))
    with pytest.raises(cr.TargetingUnresolved) as ei:
        resolver.interests([["Alpari Group or Plus500"]])
    assert [i["term"] for i in ei.value.items] == ["Alpari Group"]


# ───────────────────────────── подтверждение и кэш ─────────────────────────────

def test_confirm_then_cache_then_reuse_without_any_call(tmp_path):
    store = _store(tmp_path)
    first = cr.TargetingResolver(_mk(interests=NEAR), store)
    with pytest.raises(cr.TargetingUnresolved):
        first.interests([["forex trading"]])
    store.put("forex trading", id="6", type="interests", name="Forex Trading Tips")
    silent = _mk()
    spec = cr.TargetingResolver(silent, store).interests([["Forex  Trading"]])
    assert spec == [{"interests": [{"id": "6", "name": "Forex Trading Tips"}]}]
    assert silent.log == [], "подтверждённое берётся без вопроса и без обращения к Meta"
    # Кэш лежит на диске, виден и очищается.
    assert [e["term"] for e in tr.ConfirmedStore(store.path).entries()] == ["forex trading"]
    assert store.remove("forex trading") is True and store.entries() == []
    with pytest.raises(cr.TargetingUnresolved):
        cr.TargetingResolver(_mk(interests=NEAR), store).interests([["forex trading"]])


def test_store_rejects_bad_ids_and_types_and_clears_all(tmp_path):
    s = _store(tmp_path)
    with pytest.raises(ValueError):
        s.put("x", id="abc", type="interests", name="X")
    with pytest.raises(ValueError):
        s.put("x", id="1", type="countries", name="X")
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


# ───────────────────────────── бюджет вызовов ─────────────────────────────

def test_each_term_is_resolved_once_per_job(tmp_path):
    c = _mk(valid={"yoga": "Yoga"})
    r = _resolver(c, tmp_path)
    for _ in range(3):
        TermResolver_resolve(r, "yoga")
    assert c.calls == 1


def test_per_term_cap_stops_the_search_and_says_why(tmp_path):
    c = _mk()
    r = _resolver(c, tmp_path, term_cap=3)
    res = TermResolver_resolve(r, "Nothing here")
    assert c.calls == 3 + 1 and res["state"]  # +1: пакетный adinterestvalid до поиска
    assert res["state"] == "unresolved" and "call limit" in res["reason"]


def test_split_parts_are_capped_each(tmp_path):
    c = _mk()
    r = _resolver(c, tmp_path, term_cap=4)
    TermResolver_resolve(r, "Aaa or Bbb")
    assert c.calls <= 1 + 3 * 4, "пакет + целая строка + две части, каждая не больше потолка"


def test_job_cap_and_client_reserve_are_respected(tmp_path):
    c = _mk(max_calls=5)
    r = _resolver(c, tmp_path)
    res = TermResolver_resolve(r, "Nothing here")
    assert c.calls <= 5 - tr.CLIENT_RESERVE, "своя остановка раньше потолка клиента: CallBudget не наступает"
    assert res["state"] == "unresolved" and res["reason"]
    j = _resolver(_mk(), tmp_path, job_cap=2)
    TermResolver_resolve(j, "One")
    TermResolver_resolve(j, "Two")
    assert j.calls == 2


def test_behaviors_list_is_shared_between_terms(tmp_path):
    c = _mk(classes={"behaviors": [{"id": "9", "name": "Expats"}]})
    r = _resolver(c, tmp_path)
    TermResolver_resolve(r, "nope one")
    TermResolver_resolve(r, "nope two")
    assert sum(1 for t, _ in c.log if t == "adTargetingCategory/behaviors") == 1


# ───────────────────────────── pre-flight и блок публикации ─────────────────────────────

class TargetMeta(FormMeta):
    def __init__(self, *, interests=None, valid=None, **kw):
        super().__init__(**kw)
        self.interests, self.valid = interests or {}, valid or {}

    def _get(self, path, params):
        if path == "/search":
            t = params["type"]
            if t == "adinterestvalid":
                return {"data": [{"name": self.valid[n], "valid": True, "id": "1" + str(i)}
                                 if n in self.valid else {"name": n, "valid": False}
                                 for i, n in enumerate(params["interest_list"])]}
            if t == "adinterest":
                return {"data": self.interests.get(params["q"], [])}
            if t == "adTargetingCategory":
                return {"data": []}
        return super()._get(path, params)


def _audit(meta, tmp_path, label, store):
    d = _draft(tmp_path, ads=1)
    d.ad_sets[0].targeting_label = label
    client = g.GraphClient(meta, allow_writes=False, sleep=lambda s: None, min_interval_s=0.0, max_calls=80)
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
    assert got["interests_resolve"]["status"] == "ok"
    import src.fb_api.preflight_rules as pf
    assert pf.blocks(out.audit), "ошибка блокирует Publish"
    assert out.targeting and out.targeting[0]["state"] == "pending", "Check plan получает термины"


def test_preflight_passes_after_confirmation(tmp_path):
    store = _store(tmp_path)
    store.put("forex trading", id="6", type="interests", name="Forex Trading Tips")
    meta = TargetMeta(interests={"forex trading": [{"id": "6", "name": "Forex Trading Tips"}]})
    out, got = _audit(meta, tmp_path, "forex trading", store)
    assert out.error == "" and got["interests_confirmed"]["status"] == "ok" and got["interests_resolve"]["status"] == "ok"
    assert out.targeting[0]["state"] == "confirmed"


def test_the_two_new_rules_are_in_the_table_and_are_server_errors():
    import src.fb_api.preflight_rules as pf
    for rid in ("interests_resolve", "interests_confirmed"):
        assert pf.RULE_BY_ID[rid]["side"] == "server" and pf.RULE_BY_ID[rid]["severity"] == "error"


# ───────────────────────────── эндпоинты кэша ─────────────────────────────

class _Req:
    def __init__(self, body):
        self._b = body

    async def json(self):
        return self._b


def test_endpoints_confirm_list_and_clear(tmp_path, monkeypatch):
    monkeypatch.setenv("ACL_CONFIG_DIR", str(tmp_path))
    from src.web import app as web
    import json as _j

    def call(fn, body=None):
        r = asyncio.run(fn(_Req(body)) if body is not None else fn())
        return r.status_code, _j.loads(r.body)

    assert call(web.targeting_resolver_confirm, {"term": "Forex Trading", "id": "6", "type": "interests",
                                                  "name": "Forex Trading Tips"})[0] == 200
    assert call(web.targeting_resolver_confirm, {"term": "x", "id": "no", "type": "interests", "name": "X"})[0] == 400
    code, body = call(web.targeting_resolver_cache)
    assert [e["name"] for e in body["entries"]] == ["Forex Trading Tips"]
    assert call(web.targeting_resolver_clear, {})[0] == 400, "без термина и без all - отказ"
    assert call(web.targeting_resolver_clear, {"term": "forex trading"})[1]["removed"] == 1
    assert call(web.targeting_resolver_cache)[1]["entries"] == []
