"""ACL-2787: «Use Facebook Page» нажимается только для `none`, один раз на Страницу; отказ - прежнее сообщение."""
import pytest

from src.fb_api import instagram as ig, page_backed_ui as pbu
from tests.test_acl1761_api_create import FakeMeta, _draft, _run
from tests.test_acl1819_preflight import AuditMeta, _audit, _by
from tests.test_acl2221_page_backed import IG, NO_IDENTITY


class Presser:
    """Поддельное нажатие: при успехе у Страницы появляется Page-backed аккаунт."""

    def __init__(self, meta, *, fail="", raises=None):
        self.meta, self.fail, self.raises, self.calls = meta, fail, raises, []

    def __call__(self, act, page_id):
        self.calls.append((act, page_id))
        if self.raises:
            raise self.raises
        if not self.fail:
            self.meta.instagram = "page_backed_edge"
        return self.fail


def test_none_then_press_then_found_check_plan_passes(tmp_path):
    meta = AuditMeta(instagram="none")
    press = Presser(meta)
    row = _by(_audit(meta, _draft(tmp_path, ads=1), creator_kw={"use_facebook_page": press}))["instagram_identity"]
    assert row["status"] == "ok" and IG in row["detail"] and "Page-backed" in row["detail"]
    assert len(press.calls) == 1 and meta.posts == []


def test_publish_continues_with_the_new_identity(tmp_path):
    meta = FakeMeta(instagram="none")
    press = Presser(meta)
    out, _ = _run(meta, _draft(tmp_path, ads=1), use_facebook_page=press)
    assert out.error == "" and len(press.calls) == 1
    creatives = [o for o in meta.objects.values() if o["_kind"] == "creative"]
    assert creatives[0]["object_story_spec"]["instagram_user_id"] == IG


@pytest.mark.parametrize("kind", ["refused", "raises", "pressed_but_still_none"])
def test_failure_keeps_the_manual_message_on_this_account(tmp_path, kind):
    meta = AuditMeta(instagram="none")
    if kind == "refused":
        press = Presser(meta, fail="control not found")
    elif kind == "raises":
        press = Presser(meta, raises=RuntimeError("cdp lost"))
    else:
        press = lambda act, page: ""      # noqa: E731 - "нажато", но Meta так и не показала аккаунт
    out = _audit(meta, _draft(tmp_path, ads=1), creator_kw={"use_facebook_page": press})
    row = _by(out)["instagram_identity"]
    assert row["status"] == "error" and row["detail"] == NO_IDENTITY
    assert meta.posts == []


def test_unknown_identity_does_not_trigger_the_action(tmp_path):
    meta = AuditMeta(instagram="edge_error")
    press = Presser(meta)
    row = _by(_audit(meta, _draft(tmp_path, ads=1), creator_kw={"use_facebook_page": press}))["instagram_identity"]
    assert press.calls == [] and row["status"] == "error" and "did not confirm" in row["detail"]


def test_found_identity_does_not_trigger_the_action(tmp_path):
    meta = AuditMeta(instagram="one")
    press = Presser(meta)
    _audit(meta, _draft(tmp_path, ads=1), creator_kw={"use_facebook_page": press})
    assert press.calls == []


def test_the_press_is_tried_once_per_page_per_run(tmp_path):
    meta = FakeMeta(instagram="none")
    press = Presser(meta, fail="control not found")
    out, _ = _run(meta, _draft(tmp_path, ads=1), use_facebook_page=press)
    assert out.stop_kind == "plan" and len(press.calls) == 1


def test_default_press_without_a_profile_tab_keeps_the_manual_message(tmp_path):
    # у поддельного клиента нет вкладки профиля: драйвер ничего не открывает и отказывает
    assert pbu.press("act_1", "111", page_name="Sample Page") == "the account's profile tab is not available"
    out = _audit(AuditMeta(instagram="none"), _draft(tmp_path, ads=1))
    assert _by(out)["instagram_identity"]["detail"] == ig.MSG_NO_INSTAGRAM


@pytest.mark.parametrize("fail", ["control not found", ""])
def test_n_drafts_on_one_page_press_once_per_run(tmp_path, fail):
    # по одному создателю на объявление, но множество «уже пробовали» общее на прогон (ApiPublisher.ig_pressed)
    shared: set = set()
    press = Presser(FakeMeta(instagram="none"), fail=fail)
    for _ in range(3):
        meta = FakeMeta(instagram="none")
        press.meta = meta
        _run(meta, _draft(tmp_path, ads=1), use_facebook_page=press, pressed=shared)
    assert len(press.calls) == 1 and len(shared) == 1


def test_publisher_hands_one_pressed_set_to_every_creator():
    from src.web import publish_api
    pub = publish_api.ApiPublisher(dry_run=True)
    assert isinstance(pub.ig_pressed, set)
