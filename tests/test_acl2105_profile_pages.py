"""ACL-2105: Страницы профиля как запасной источник. Никаких обращений к Meta: только заглушки."""
from types import SimpleNamespace

import pytest

from src.fb_api import creator as cr, graph as g, pages as pg
from tests.test_acl1761_api_create import ACT, _draft
from tests.test_acl1819_preflight import AuditMeta, _by


class ProfileMeta(AuditMeta):
    """AuditMeta + `/me/accounts` и чтение самой Страницы."""

    def __init__(self, *, profile=(), page_reply=None, **kw):
        super().__init__(**kw)
        self.profile = list(profile)
        self.page_reply = page_reply or {}
        self.me_accounts_calls = 0

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
    # проверка пригодности была: чтение самой Страницы, ни одной записи
    assert any(p == "/777" for p, _ in meta.gets) and meta.posts == []


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
