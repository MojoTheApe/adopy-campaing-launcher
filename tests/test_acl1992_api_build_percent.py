"""ACL-1992: процент сборки API-кампании — по созданным объектам.

Строки прогресса ниже — ровно те, что пишет `CampaignCreator._run`
(src/fb_api/creator.py); счётчик опирается только на их начала.
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.fb_api.creator import AdPlan, AdSetPlan, CampaignPlan
from src.web.api_build_progress import BuildCounter, plan_total
from src.web.publish_progress import job_progress


def _ad(i, kind='image'):
    return AdPlan(index=i, name=f'ad{i}', kind=kind, path=f'f{i}', title='t', body='b', description='d')


def _plan(*sets):
    return CampaignPlan(name='C', act='act_1', tag='x', country='MX', language='es', link='https://x',
                        page_id='1', pixel_id='2', daily_budget=10,
                        ad_sets=[AdSetPlan(name=f'S{n}', age_min=18, age_max=65, os='', interest_groups=[],
                                           ads=list(ads)) for n, ads in enumerate(sets)])


def _feed(counter, lines):
    seen = []
    for line in lines:
        counter.feed(line)
        seen.append(counter.done)
    return seen


ONE_IMAGE = [
    "preflight: targeting resolved",
    "preflight: creatives read",
    "campaign: created PAUSED, tag kept",
    "ad set 'S0': created PAUSED, tag kept",
    "ad 1: image uploaded",
    "ad 1: creative created",
    "ad 1: created PAUSED",
    "readback: all fields match",
]


def test_total_counts_every_object_of_the_plan():
    # предпроверка + кампания + сверка + группа + (медиа + креатив + объявление)
    assert plan_total(_plan([_ad(1)])) == 7
    # видео — две единицы медиа (загрузка и обработка)
    assert plan_total(_plan([_ad(1, 'video')])) == 8
    # две группы, три объявления
    assert plan_total(_plan([_ad(1), _ad(2, 'video')], [_ad(1)])) == 3 + 2 + 3 + 4 + 3


def test_one_image_ad_moves_through_every_unit_and_ends_full():
    c = BuildCounter(_plan([_ad(1)]))
    seen = _feed(c, ONE_IMAGE)
    assert seen == [0, 1, 2, 3, 4, 5, 6, 7]
    assert c.done == c.total == 7
    assert c.view() == {'done': 7, 'total': 7, 'last': 'readback: all fields match'}


def test_video_upload_and_processing_are_separate_units():
    c = BuildCounter(_plan([_ad(1, 'video')]))
    _feed(c, ONE_IMAGE[:4])
    assert c.done == 3
    c.feed("ad 1: video uploaded")
    assert c.done == 4
    c.feed("ad 1: video processed")
    assert c.done == 5
    _feed(c, ["ad 1: creative created", "ad 1: created PAUSED", "readback: 1 mismatch(es)"])
    assert c.done == c.total == 8


def test_same_ad_number_in_two_ad_sets_counts_twice():
    c = BuildCounter(_plan([_ad(1)], [_ad(1)]))
    _feed(c, ONE_IMAGE[:7])
    first = c.done
    _feed(c, ["ad set 'S1': created PAUSED, tag kept", "ad 1: image already in the ad account",
              "ad 1: creative created", "ad 1: created PAUSED", "readback: all fields match"])
    assert c.done == c.total == 11 and c.done - first == 5


def test_rerun_that_finds_everything_reaches_the_end():
    c = BuildCounter(_plan([_ad(1, 'video'), _ad(2)]))
    _feed(c, ["preflight: creatives read", "campaign: found from a previous run",
              "existing campaign and ad sets match the builder",
              "ad set 'S0': found from a previous run",
              "ad 1: found from a previous run", "ad 2: found from a previous run",
              "readback: all fields match"])
    assert c.done == c.total


def test_repeated_and_unrelated_lines_never_inflate_the_count():
    c = BuildCounter(_plan([_ad(1)]))
    _feed(c, ONE_IMAGE[:5])
    before = c.done
    _feed(c, ["ad 1: image uploaded", "ad 1: image upload: result unknown — reconciling by readback",
              "ad 1: image upload: reconciled — found by readback", "something new from a later PR"])
    assert c.done == before


def test_a_later_stage_implies_the_earlier_ones():
    # Строка предпроверки поменялась в будущем PR — кампания всё равно её засчитывает.
    c = BuildCounter(_plan([_ad(1)]))
    c.feed("campaign: created PAUSED, tag kept")
    assert c.done == 2
    c.feed("ad 1: created PAUSED")      # строки медиа/креатива не было (например, другой формат)
    assert c.done == 5


def test_forms_add_a_unit_to_both_sides():
    c = BuildCounter(_plan([_ad(1)]))
    c.feed("form 'Lead A': created")
    c.feed("form 'Lead A': read back, ACTIVE, on the Page, questions match")
    assert (c.done, c.total) == (1, 8)
    c.feed("form 'Lead A': found on the Page, reused")
    assert (c.done, c.total) == (1, 8)
    _feed(c, ONE_IMAGE)
    assert c.done == c.total == 8


def _job(**kw):
    job = {'job_id': 'j1', 'account_label': 'Demo', 'status': 'running', 'campaign_names': ['C'],
           'results': [], 'created_at': '2026-10-18T10:00:00+00:00', 'started_at': '2026-10-18T10:00:00+00:00'}
    job.update(kw)
    return job


def test_poll_returns_units_per_campaign(tmp_path):
    now = datetime(2026, 10, 18, 10, 1, tzinfo=timezone.utc)
    got = job_progress(_job(units={'C': {'done': 3, 'total': 7, 'last': 'campaign: created PAUSED, tag kept'}}),
                       path=tmp_path / 'none.jsonl', now=now)
    assert got['campaigns'][0]['units'] == {'done': 3, 'total': 7, 'last': 'campaign: created PAUSED, tag kept'}


def test_poll_without_units_or_with_garbage_gives_none(tmp_path):
    now = datetime(2026, 10, 18, 10, 1, tzinfo=timezone.utc)
    assert job_progress(_job(), path=tmp_path / 'n', now=now)['campaigns'][0]['units'] is None
    bad = _job(units={'C': {'done': 'x', 'total': 7}})
    assert job_progress(bad, path=tmp_path / 'n', now=now)['campaigns'][0]['units'] is None
    zero = _job(units={'C': {'done': 1, 'total': 0}})
    assert job_progress(zero, path=tmp_path / 'n', now=now)['campaigns'][0]['units'] is None
    over = _job(units={'C': {'done': 9, 'total': 7}})
    assert job_progress(over, path=tmp_path / 'n', now=now)['campaigns'][0]['units']['done'] == 7


def test_publisher_reports_units_from_the_creators_progress_lines(monkeypatch):
    from types import SimpleNamespace

    from src.fb_api.creator import Outcome
    from src.web import publish_api

    plan = _plan([_ad(1)])
    monkeypatch.setattr(publish_api, 'plan_from_draft', lambda draft: plan)
    monkeypatch.setattr(publish_api, '_record', lambda *a, **kw: None)
    monkeypatch.setattr(publish_api._tm, 'record', lambda *a, **kw: None)

    class FakeCreator:
        def __init__(self, *a, progress, **kw):
            self.progress = progress

        def run(self, plan_):
            for line in ONE_IMAGE:
                self.progress(line)
            return Outcome(campaign_name='C')

    monkeypatch.setattr(publish_api, 'CampaignCreator', FakeCreator)
    pub = publish_api.ApiPublisher()
    pub.act = 'act_1'
    pub._client = SimpleNamespace(unreconciled=None, reset_budget=lambda n: None, writes=0, calls=0,
                                  stopped=None)
    pub._facts = pub._resolver = object()
    got = []
    pub.units = lambda name, view: got.append((name, view['done'], view['total']))
    pub.publish_draft(SimpleNamespace(name='C'))
    assert got[0] == ('C', 0, 7)                       # до первой записи — 0 из 7
    assert [d for _, d, _ in got] == sorted(d for _, d, _ in got)
    assert got[-1] == ('C', 7, 7)


def test_dry_run_reports_no_units(monkeypatch):
    from types import SimpleNamespace

    from src.fb_api.creator import Outcome
    from src.web import publish_api

    monkeypatch.setattr(publish_api, 'plan_from_draft', lambda draft: _plan([_ad(1)]))
    monkeypatch.setattr(publish_api, '_record', lambda *a, **kw: None)

    class FakeCreator:
        def __init__(self, *a, **kw):
            pass

        def plan_only(self, plan_):
            return Outcome(campaign_name='C')

    monkeypatch.setattr(publish_api, 'CampaignCreator', FakeCreator)
    pub = publish_api.ApiPublisher(dry_run=True)
    pub.act = 'act_1'
    pub._client = SimpleNamespace(unreconciled=None, reset_budget=lambda n: None, writes=0, calls=0)
    pub._facts = pub._resolver = object()
    got = []
    pub.units = lambda name, view: got.append(view)
    pub.publish_draft(SimpleNamespace(name='C'))
    assert got == []
