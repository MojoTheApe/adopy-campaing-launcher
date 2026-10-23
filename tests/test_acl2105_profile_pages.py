"""ACL-2105: Страницы профиля как запасной источник. Никаких обращений к Meta: только заглушки."""
from types import SimpleNamespace

import pytest

from src.fb_api import creator as cr, graph as g, pages as pg
from tests.test_acl1761_api_create import ACT, ACT2, _draft
from tests.test_acl1819_preflight import AuditMeta, _by


class ProfileMeta(AuditMeta):
    """AuditMeta + `/me/accounts` и чтение самой Страницы."""

    def __init__(self, *, profile=(), page_reply=None, **kw):
        super().__init__(**kw)
        self.profile = list(profile)
        self.page_reply = page_reply or {}
        self.me_accounts_calls = 0
        self.validations = []

    def validate_page(self, act, page_id, link):
        self.validations.append((act, page_id, link))
        assert act == ACT
        if self.page_reply.get("is_published") is False:
            return {"status": 400, "body": {"error": {"code": 100, "message": "the Page is not published"}}}
        if self.page_reply.get("promotion_eligible") is False:
            return {"status": 400, "body": {"error": {"code": 100,
                    "message": self.page_reply.get("promotion_ineligible_reason", "Page refused")}}}
        return {"status": 200, "body": {"success": True}}

    def _get(self, path, params):
        if path == "/me/accounts":
            self.me_accounts_calls += 1
            return {"data": self.profile}
        pid = path.strip("/")
        if pid.isdigit() and pid in {str(r["id"]) for r in self.profile} and "is_published" in str(params.get("fields")):
            return {"id": pid, "name": "P", "is_published": True, **self.page_reply}
        return super()._get(path, params)


def _client(meta, writes=False):
    return g.GraphClient(meta, allow_writes=writes, sleep=lambda s: None, min_interval_s=0, max_calls=100)


def _audit(meta, draft):
    client = _client(meta)
    creator = cr.CampaignCreator(client, cr.preflight_account(client, ACT), cr.TargetingResolver(client),
                                 sleep=lambda s: None)
    out = creator.plan_only(cr.plan_from_draft(draft))
    assert client.writes == 0 and meta.posts == [], "проверка не пишет в Meta"
    return out


def _advertise(pid, name="Profile Page", tasks=("ADVERTISE", "ANALYZE")):
    return {"id": pid, "name": name, "tasks": list(tasks)}


def test_empty_promote_pages_uses_profile_page_with_source(tmp_path):
    meta = ProfileMeta(pages=(), profile=[_advertise("777")])
    client = _client(meta)
    facts = cr.preflight_account(client, ACT)
    plan = SimpleNamespace(page_id="", page_source="", ad_sets=[])
    notes = cr.resolve_page(plan, facts)
    assert plan.page_id == "777" and plan.page_source == "account default"
    assert facts.page_source_of("777") == "profile"
    assert "profile" in notes[0]


def test_ad_account_pages_come_first_and_profile_is_not_read_needlessly():
    meta = ProfileMeta(pages=("111",), profile=[_advertise("777")])
    facts = cr.preflight_account(_client(meta), ACT)
    assert facts.pages == ["111"] and meta.me_accounts_calls == 0
    merged = pg.read_merged(_client(meta), ACT, always_profile=True)
    assert [(r["id"], r["source"]) for r in merged.rows] == [("111", "ad account"), ("777", "profile")]


def test_pages_without_advertise_task_are_ignored():
    meta = ProfileMeta(pages=(), profile=[_advertise("777", tasks=("ANALYZE", "MODERATE")),
                                          {"id": "888", "name": "No tasks"},
                                          _advertise("999", tasks=("MANAGE",))])
    facts = cr.preflight_account(_client(meta), ACT)
    assert facts.pages == ["999"]


def test_both_empty_keeps_manual_page_path(tmp_path):
    meta = ProfileMeta(pages=(), profile=[])
    draft = _draft(tmp_path, ads=1)
    draft.page_id = ""
    out = _audit(meta, draft)
    assert not out.error
    assert _by(out)["page_ok"]["status"] == "warning"
    assert out.plan["summary"]["manual_required"] and "page_origin" not in out.plan["summary"]


def test_draft_page_only_in_profile_is_accepted_after_usability_check(tmp_path):
    meta = ProfileMeta(pages=("111",), profile=[_advertise("777")])
    draft = _draft(tmp_path, ads=1)
    draft.page_id = "777"
    out = _audit(meta, draft)
    assert _by(out)["page_ok"]["status"] == "ok", _by(out)["page_ok"]
    assert out.plan["summary"]["page_origin"] == "profile"
    # The actual account/Page proof happened once; no creative or other object was created.
    assert len(meta.validations) == 1 and meta.validations[0][:2] == (ACT, "777") and meta.posts == []


@pytest.mark.parametrize("reply, reason", [
    ({"is_published": False}, "not published"),
    ({"promotion_eligible": False, "promotion_ineligible_reason": "POLICY"}, "POLICY"),
])
def test_unusable_profile_page_stops_with_page_and_reason(tmp_path, reply, reason):
    meta = ProfileMeta(pages=(), profile=[_advertise("777", name="Obtain markets")], page_reply=reply)
    draft = _draft(tmp_path, ads=1)
    draft.page_id = "777"
    out = _audit(meta, draft)
    detail = _by(out)["page_ok"]
    assert detail["status"] == "error"
    assert "Obtain markets" in detail["detail"] and "777" in detail["detail"] and reason in detail["detail"]
    assert "Nothing was created" in detail["detail"]


def test_creation_with_unusable_profile_page_writes_nothing(tmp_path):
    meta = ProfileMeta(pages=(), profile=[_advertise("777")], page_reply={"is_published": False})
    client = _client(meta, writes=True)
    creator = cr.CampaignCreator(client, cr.preflight_account(client, ACT), cr.TargetingResolver(client),
                                 sleep=lambda s: None)
    draft = _draft(tmp_path, ads=1)
    draft.page_id = ""
    out = creator.run(cr.plan_from_draft(draft))
    assert out.error and "777" in out.error and not out.campaign_id
    assert meta.posts == []


def test_foreign_page_still_refused_when_not_in_either_source(tmp_path):
    meta = ProfileMeta(pages=("111",), profile=[_advertise("777")])
    draft = _draft(tmp_path, ads=1)
    draft.page_id = "999"
    out = _audit(meta, draft)
    assert _by(out)["page_ok"]["status"] == "error"
    assert "not among the pages" in _by(out)["page_ok"]["detail"]


def test_refresh_pages_merges_sources_and_reports_counts():
    from src.web.account_pages import pages_from_client
    meta = ProfileMeta(pages=("111",), profile=[_advertise("777")])
    counts = {}
    rows = pages_from_client(_client(meta), ACT, counts=counts)
    assert [(r["id"], r["source"]) for r in rows] == [("111", "ad account"), ("777", "profile")]
    assert counts["account"] == 1 and counts["profile"] == 1


def test_instant_forms_page_for_falls_back_to_profile():
    from src.web.page_forms import page_for
    meta = ProfileMeta(pages=(), profile=[_advertise("777")])
    assert page_for("", _client(meta), ACT) == "777"
    assert page_for("555", _client(meta), ACT) == "555"


def test_telemetry_counts_per_source(monkeypatch):
    seen = []
    from src.fb_ads_rpa import run_telemetry as rt
    monkeypatch.setattr(rt, "record", lambda step, ok=None, **kw: seen.append((step, kw["diag"])))
    meta = ProfileMeta(pages=(), profile=[_advertise("777")])
    cr.preflight_account(_client(meta), ACT)
    step, diag = seen[-1]
    assert step == "api_pages_sources" and diag["account_pages"] == 0 and diag["profile_pages"] == 1


def test_profile_read_path_has_safe_error_context():
    class T:
        def request(self, method, path, params=None):
            return {"status": 400, "body": {"error": {"code": 10, "type": "OAuthException", "message": "denied"}}}

        def state(self):
            return {"kind": "ads_manager", "has_token": True}

        def reacquire(self, timeout=60.0):
            return self.state()
    with pytest.raises(g.GraphError) as caught:
        g.GraphClient(T(), min_interval_s=0).get("/me/accounts", {"fields": "id,name,tasks"})
    assert caught.value.details["operation"] == "profile_pages"


@pytest.mark.parametrize("action", ["plan", "run"])
def test_account_creative_refusal_precedes_every_created_object(tmp_path, action):
    class RefusesForAccount(ProfileMeta):
        def validate_page(self, act, page_id, link):
            self.validated = (act, page_id, link)
            return {"status": 400, "body": {"error": {"code": 100,
                    "message": "Synthetic Page is not usable for this ad account"}}}
    meta = RefusesForAccount(pages=(), profile=[_advertise("777")],
                             page_reply={"is_published": True, "promotion_eligible": True})
    draft = _draft(tmp_path, ads=1)
    draft.page_id = "777"
    client = _client(meta, writes=action == "run")
    creator = cr.CampaignCreator(client, cr.preflight_account(client, ACT), cr.TargetingResolver(client),
                                 sleep=lambda _: None)
    out = creator.plan_only(cr.plan_from_draft(draft)) if action == "plan" else creator.run(cr.plan_from_draft(draft))
    if action == "plan":
        assert _by(out)["page_ok"]["status"] == "error", _by(out)["page_ok"]
    else:
        assert out.error and not out.campaign_id, out
    assert meta.validated[:2] == (ACT, "777")
    assert not meta.posts and not meta.objects and client.writes == 0


@pytest.mark.parametrize("reply", [{}, {"id": "123"}, {"success": False}, {"success": "true"},
                                  {"success": True, "error": None}, {"success": True, "id": "123"}])
def test_missing_explicit_validation_success_never_proves_page(tmp_path, reply):
    class UnknownProof(ProfileMeta):
        def validate_page(self, act, page_id, link):
            return {"status": 200, "body": reply}
    meta = UnknownProof(pages=(), profile=[_advertise("777")])
    draft = _draft(tmp_path, ads=1)
    draft.page_id = "777"
    out = _audit(meta, draft)
    assert _by(out)["page_ok"]["status"] == "error", _by(out)["page_ok"]
    assert not meta.posts and not meta.objects


def test_successful_profile_proof_precedes_the_first_created_object(tmp_path):
    class OrderedMeta(ProfileMeta):
        def write(self, *args, **kwargs):
            assert len(self.validations) == 1 and self.validations[0][:2] == (ACT, "777")
            return super().write(*args, **kwargs)
    meta = OrderedMeta(pages=(), profile=[_advertise("777")])
    draft = _draft(tmp_path, ads=1)
    draft.page_id = "777"
    client = _client(meta, writes=True)
    creator = cr.CampaignCreator(client, cr.preflight_account(client, ACT), cr.TargetingResolver(client),
                                 sleep=lambda _: None)
    out = creator.run(cr.plan_from_draft(draft))
    assert not out.error and out.campaign_id and out.ad_ids, out
    assert meta.posts and len(meta.validations) == 1


def test_run_obtains_fresh_page_proof_after_check_plan(tmp_path):
    meta = ProfileMeta(pages=(), profile=[_advertise("777")])
    draft = _draft(tmp_path, ads=1)
    draft.page_id = "777"
    client = _client(meta, writes=True)
    creator = cr.CampaignCreator(client, cr.preflight_account(client, ACT), cr.TargetingResolver(client),
                                 sleep=lambda _: None)
    assert not creator.plan_only(cr.plan_from_draft(draft)).error
    meta.page_reply = {"promotion_eligible": False, "promotion_ineligible_reason": "account permission changed"}
    out = creator.run(cr.plan_from_draft(draft))
    assert out.error and "account permission changed" in out.error and not out.campaign_id
    assert len(meta.validations) == 2 and not meta.posts


def test_unavailable_validation_support_does_not_fall_back_to_generic_flags(tmp_path):
    meta = ProfileMeta(pages=(), profile=[_advertise("777")],
                       page_reply={"is_published": True, "promotion_eligible": True})
    meta.validate_page = None
    draft = _draft(tmp_path, ads=1)
    draft.page_id = "777"
    out = _audit(meta, draft)
    assert _by(out)["page_ok"]["status"] == "error"
    assert "unavailable" in _by(out)["page_ok"]["detail"]
    assert not meta.posts and not meta.objects


def test_plan_cannot_substitute_an_unverified_ad_account(tmp_path):
    meta = ProfileMeta(pages=(), profile=[_advertise("777")])
    draft = _draft(tmp_path, ads=1)
    draft.page_id = "777"
    client = _client(meta, writes=True)
    creator = cr.CampaignCreator(client, cr.preflight_account(client, ACT), cr.TargetingResolver(client),
                                 sleep=lambda _: None)
    plan = cr.plan_from_draft(draft)
    plan.act = ACT2
    out = creator.run(plan)
    assert out.error and "777" in out.error and "differs" in out.error
    assert not out.campaign_id and not meta.validations and not meta.posts


class _Rows:
    truncated: list = []

    def __init__(self, rows):
        self._rows = rows

    def get_all(self, path, params=None, max_pages=1):
        return self._rows


def _kept(rows):
    from src.fb_api import pages as pg
    return [r["name"] for r in pg.profile_rows(_Rows(rows))[0]]


def test_new_page_experience_tasks_count_as_advertise():
    """ACL-2110: Страница нового формата (PROFILE_PLUS_*) с ADVERTISE принимается как profile."""
    from src.fb_api import pages as pg

    rows = [{"id": "1", "name": "Obtain markets",
             "tasks": ["PROFILE_PLUS_MANAGE_LEADS", "profile_plus_advertise", "PROFILE_PLUS_CREATE_CONTENT"]}]
    assert _kept(rows) == ["Obtain markets"]
    merged = pg.merge([], pg.profile_rows(_Rows(rows))[0])
    assert merged == [{"id": "1", "name": "Obtain markets", "source": "profile"}]


def test_new_page_experience_without_advertise_is_ignored():
    rows = [{"id": "2", "name": "Leads only", "tasks": ["PROFILE_PLUS_MANAGE_LEADS"]},
            {"id": "3", "name": "Unknown", "tasks": ["PROFILE_PLUS_SOMETHING", "WHATEVER"]},
            {"id": "4", "name": "No tasks"}, {"id": "5", "name": "Bad", "tasks": "ADVERTISE"}]
    assert _kept(rows) == []


def test_full_control_and_manage_imply_advertise_both_namings():
    rows = [{"id": "1", "name": "A", "tasks": ["FULL_CONTROL"]},
            {"id": "2", "name": "B", "tasks": ["PROFILE_PLUS_FULL_CONTROL"]},
            {"id": "3", "name": "C", "tasks": ["PROFILE_PLUS_MANAGE"]},
            {"id": "4", "name": "D", "tasks": ["MANAGE"]},
            {"id": "5", "name": "E", "tasks": ["ADVERTISE"]},
            {"id": "6", "name": "F", "tasks": ["ANALYZE", "CREATE_CONTENT"]}]
    assert _kept(rows) == ["A", "B", "C", "D", "E"]
