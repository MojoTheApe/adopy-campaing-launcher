"""Exercise actual public browser entry points without opening a browser."""
import threading
from types import SimpleNamespace

import pytest

from src.analysis_agent import account_spend
from src.fb_ads_rpa.operations import browser_operation, profile_lock
from src.fb_ads_rpa.rpa_publisher import RpaPublisher


class StopBeforeBrowser(Exception):
    pass


def publisher(entered, release):
    obj = object.__new__(RpaPublisher)
    obj.hand_off = False
    obj.dry_run = True
    draft = SimpleNamespace(status='DRAFT', errors=[], account_label='synthetic')
    obj.build_drafts = lambda *a, **k: [draft]
    obj.close = lambda *a, **k: None
    def attach(account):
        entered.set()
        release.wait(5)
        raise StopBeforeBrowser()
    obj._ensure_browser = attach
    return obj, draft


def invoke(obj, draft, method):
    try:
        if method == 'publish_draft':
            obj.publish_draft(draft)
        else:
            obj.publish_slots([], account_label='synthetic', country='Test')
    except StopBeforeBrowser:
        pass


@pytest.mark.parametrize('method', ['publish_draft', 'publish_slots'])
def test_direct_publisher_waits_for_readers(method):
    # С acl-1210 публикация занимает профиль, а не процесс: ждёт она
    # чтение СВОЕГО профиля — оба ярлыка разрешены в один физический UUID,
    # и берётся тот же `profile_lock`, что и у чтений.
    entered, release = threading.Event(), threading.Event()
    obj, draft = publisher(entered, release)
    thread = threading.Thread(target=invoke, args=(obj, draft, method))
    try:
        with browser_operation.reading(), profile_lock('550e8400-e29b-41d4-a716-446655440000'):
            thread.start()
            assert not entered.wait(.2)
        assert entered.wait(3)
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()


@pytest.mark.parametrize('method', ['publish_draft', 'publish_slots'])
def test_direct_publisher_does_not_wait_for_a_reader_of_another_profile(method):
    """Свойство acl-1210: чтение чужого профиля публикации не мешает."""
    entered, release = threading.Event(), threading.Event()
    obj, draft = publisher(entered, release)
    thread = threading.Thread(target=invoke, args=(obj, draft, method))
    try:
        with browser_operation.reading(), profile_lock('6ba7b810-9dad-11d1-80b4-00c04fd430c8'):
            thread.start()
            assert entered.wait(2), "публикация ждёт чтение чужого профиля"
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()


def test_separate_publishers_are_exclusive():
    first, second, release = threading.Event(), threading.Event(), threading.Event()
    a, da = publisher(first, release)
    b, db = publisher(second, release)
    threads = [threading.Thread(target=invoke, args=(a, da, 'publish_draft')),
               threading.Thread(target=invoke, args=(b, db, 'publish_draft'))]
    try:
        threads[0].start()
        assert first.wait(3)
        threads[1].start()
        assert not second.wait(.2)
    finally:
        release.set()
        for t in threads:
            if t.ident is not None: t.join(5)
    assert second.is_set()


@pytest.mark.parametrize('first_id', ['550e8400-e29b-41d4-a716-446655440000', ' 550e8400-e29b-41d4-a716-446655440000 '])
def test_profile_probe_serializes_positional_and_keyword_calls(monkeypatch, first_id):
    first, second, release = threading.Event(), threading.Event(), threading.Event()
    monkeypatch.setattr(account_spend, '_probe_entry', lambda *a, **k: object())
    def attach(*a, **k):
        if not first.is_set():
            first.set(); release.wait(5)
        else:
            second.set()
        raise RuntimeError('synthetic: no browser was opened')
    monkeypatch.setattr(account_spend, '_attach_only', attach)
    threads = [threading.Thread(target=account_spend.read_ad_account, args=(first_id,)),
               threading.Thread(target=account_spend.read_ad_account, kwargs={'profile_id':'550e8400-e29b-41d4-a716-446655440000'})]
    try:
        threads[0].start(); assert first.wait(3)
        threads[1].start(); assert not second.wait(.2)
    finally:
        release.set()
        for t in threads:
            if t.ident is not None: t.join(5)
    assert second.is_set()


@pytest.fixture(autouse=True)
def physical_mapping(monkeypatch):
    from src.fb_ads_rpa import rpa_publisher as rp
    monkeypatch.setattr(rp, "load_ad_account_map", lambda *_: {})
    monkeypatch.setattr(rp, "resolve_account_entry", lambda *_: SimpleNamespace(
        profile_id="550e8400-e29b-41d4-a716-446655440000"))
