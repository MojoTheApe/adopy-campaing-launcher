"""ACL-2206: поздняя метка проверяется через весь create/readback, без повторной записи."""
import pytest

from src.fb_api import creator as cr
from src.fb_api import graph as g
from tests.test_acl1761_api_create import ACT, FakeMeta, _draft


class LateCreativeLabelMeta(FakeMeta):
    def __init__(self, hidden_reads):
        super().__init__()
        self.hidden_reads = hidden_reads
        self.label_reads = 0

    def _get(self, path, params):
        row = super()._get(path, params)
        obj = self.objects.get(path.strip('/'), {})
        if obj.get('_kind') == 'creative' and params.get('fields') == 'adlabels{name}':
            self.label_reads += 1
            if self.hidden_reads is None or self.label_reads <= self.hidden_reads:
                row = dict(row)
                row.pop('adlabels', None)
        return row


def run(meta, draft):
    client = g.GraphClient(meta, allow_writes=True, sleep=lambda _: None, min_interval_s=0, max_calls=200)
    facts = cr.preflight_account(client, ACT)
    waited = []
    creator = cr.CampaignCreator(client, facts, cr.TargetingResolver(client), sleep=waited.append)
    return creator.run(cr.plan_from_draft(draft)), client, waited


@pytest.mark.parametrize('hidden_reads', [1, 2, 3])
def test_delayed_creative_label_reaches_the_ad_and_readback_without_duplicate_writes(tmp_path, hidden_reads):
    meta = LateCreativeLabelMeta(hidden_reads)
    out, client, waited = run(meta, _draft(tmp_path, ads=1))
    assert not out.error and out.readback['ok'], out.error
    assert [path.rsplit('/', 1)[-1] for path, _ in meta.posts] == [
        'campaigns', 'adsets', 'adimages', 'adcreatives', 'ads']
    assert meta.label_reads == hidden_reads + 1
    assert waited == list(cr.LABEL_RECHECK_WAITS[:hidden_reads])
    assert client.calls == len(meta.gets) + len(meta.posts)
    assert len(out.ad_ids) == 1


def test_missing_label_endpoint_uses_confirmed_content_without_repeating_creative(tmp_path):
    meta = LateCreativeLabelMeta(None)
    out, client, waited = run(meta, _draft(tmp_path, ads=1))
    assert not out.error and out.readback['ok']
    assert meta.label_reads == 4 and waited == list(cr.LABEL_RECHECK_WAITS)
    assert [path.rsplit('/', 1)[-1] for path, _ in meta.posts] == [
        'campaigns', 'adsets', 'adimages', 'adcreatives', 'ads']
    assert len(out.ad_ids) == 1 and out.created.get('creatives')
    assert client.calls == len(meta.gets) + len(meta.posts)
