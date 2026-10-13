"""No Meta calls: a missing automatic Page creates only a paused website shell."""
import pytest
from tests.test_acl1819_preflight import AuditMeta, _audit, _by
from tests.test_acl1761_api_create import ACT, _draft
from src.fb_api import creator as cr, graph as g
from src.web.publish_api import result_dict


def make(meta):
    client = g.GraphClient(meta, allow_writes=True, sleep=lambda s: None, min_interval_s=0, max_calls=100)
    return cr.CampaignCreator(client, cr.preflight_account(client, ACT), cr.TargetingResolver(client), sleep=lambda s: None)


def test_empty_automatic_page_is_a_manual_warning_in_check_plan(tmp_path):
    draft = _draft(tmp_path, ads=1)
    draft.page_id = ''
    out = _audit(AuditMeta(pages=()), draft)
    assert not out.error
    assert _by(out)['page_ok']['status'] == 'warning'
    assert out.plan['summary']['manual_required']
    assert out.plan['ad_sets'][0]['ads'] == []
    assert out.plan['summary']['ad_sets'][0]['ads'] == []
    assert out.plan['summary']['ad_sets'][0]['pending_ad_count'] == 1


def test_empty_page_creates_only_paused_shell_and_retry_reuses_it(tmp_path):
    meta = AuditMeta(pages=())
    creator = make(meta)
    draft = _draft(tmp_path, ads=2)
    draft.page_id = ''
    first = creator.run(cr.plan_from_draft(draft))
    assert not first.error
    assert first.campaign_id and first.adset_ids and not first.ad_ids
    assert first.readback['ok']
    result = result_dict(first)
    assert result['success'] and result['manual_required'] and not result['publishable']
    count = len(meta.posts)
    second = creator.run(cr.plan_from_draft(draft))
    assert second.campaign_id == first.campaign_id and not second.error
    assert len(meta.posts) == count
    # Once a Page becomes available, a retry completes the same shell.
    creator.facts.pages = ['111']
    complete = creator.run(cr.plan_from_draft(draft))
    assert complete.campaign_id == first.campaign_id and len(complete.ad_ids) == 2
    assert not complete.error and not complete.manual_required


def test_explicit_unavailable_page_still_refused(tmp_path):
    draft = _draft(tmp_path, ads=1)
    draft.page_id = '999'
    meta = AuditMeta(pages=())
    out = make(meta).run(cr.plan_from_draft(draft))
    assert out.error and not meta.posts


def test_instant_form_still_needs_a_page(tmp_path):
    draft = _draft(tmp_path, ads=1)
    draft.page_id = ''
    draft.ad_sets[0].destination = 'instant_form'
    draft.ad_sets[0].ads[0].lead_form_id = '12345'
    meta = AuditMeta(pages=())
    out = _audit(meta, draft)
    assert _by(out)['page_ok']['status'] == 'error'
    assert not meta.posts
