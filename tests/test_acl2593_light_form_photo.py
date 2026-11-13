"""ACL-2593: a form-background upload needs the Ads Manager page; the light work tab has none.

Decided before the lease from the job's planned forms; a late upload on a light tab switches the lease first.
Fakes only: no Meta, no Multilogin, no CDP. Light mode is switched on explicitly (the suite default is off).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.fb_api import form_cover_store as store
from src.fb_api import form_templates as ft
from src.fb_api import graph as g
from src.fb_api import light_tab
from src.web import publish_api as pa
from tests.test_acl1775_instant_forms import PAGE
from tests.test_acl1776_create_forms import _tpl_draft
from tests.test_acl2344_warm_light import LightGraph, rig  # noqa: F401  (fixture)
from tests.test_acl2344_warm_light import _light_rows
from tests.test_acl2571_shared_form_background import (  # noqa: F401
    PICTURE, SHA, _config, save_with_picture, tpl)
from tests.test_acl2054_forms_library import local  # noqa: F401
from tests.test_acl1812_warm_sessions import ACT, PID


# --------------------------- the decision, from the planned forms ---------------------------

def test_a_form_with_a_new_background_needs_the_upload(tmp_path):
    tid = save_with_picture()["id"]
    d = _tpl_draft(tmp_path, [tid])
    d.page_id = PAGE
    assert pa.drafts_need_form_photo([d]) is True


def test_no_background_keeps_the_light_mode(tmp_path):
    tid = ft.save(tpl(name="Plain form"))["id"]
    d = _tpl_draft(tmp_path, [tid])
    d.page_id = PAGE
    assert pa.drafts_need_form_photo([d]) is False


def test_a_stored_photo_id_for_the_page_is_reused_without_an_upload(tmp_path):
    tid = save_with_picture()["id"]
    store.remember_photo(PAGE, SHA, "9001")
    d = _tpl_draft(tmp_path, [tid])
    d.page_id = PAGE
    assert pa.drafts_need_form_photo([d]) is False
    d.page_id = "777"                      # another Page: a photo id works only on its own Page
    assert pa.drafts_need_form_photo([d]) is True


def test_a_draft_without_a_page_yet_cannot_prove_reuse(tmp_path):
    tid = save_with_picture()["id"]
    d = _tpl_draft(tmp_path, [tid])
    d.page_id = ""
    assert pa.drafts_need_form_photo([d]) is True


def test_an_existing_form_choice_needs_no_upload(tmp_path):
    tid = save_with_picture()["id"]
    d = _tpl_draft(tmp_path, [tid])
    d.page_id = PAGE
    d.ad_sets[0].ads[0].lead_form_id = "555"
    assert pa.drafts_need_form_photo([d]) is False


def test_the_picture_on_another_computer_needs_no_upload(tmp_path, _config):
    tid = save_with_picture()["id"]
    for f in (_config / "form_covers").glob("*.bin"):
        f.unlink()
    d = _tpl_draft(tmp_path, [tid])
    d.page_id = PAGE
    assert pa.drafts_need_form_photo([d]) is False


# --------------------------- the lease ---------------------------

def test_a_job_with_an_upload_runs_the_lease_in_ads_manager_mode(rig):
    with light_tab.force_ads_manager("form_photo_upload"):
        lease = rig.checkout(mode=f"create:{ACT}")
    sess = rig.reg.session(PID)
    assert sess.mode_kind == "ads_manager" and len(rig.tabs) == 1 and rig.tabs[0].navigations == 1
    assert sess.light_fallbacks == 0, "a planned Ads Manager lease is not a failure of the light page"
    row = _light_rows(rig)[-1]
    assert row["mode"] == "ads_manager" and row["fallback_reason"] == "form_photo_upload"
    assert not any(isinstance(v, str) and ("://" in v or "act_" in v) for v in row.values())
    lease.release()


def test_a_job_without_an_upload_keeps_the_light_mode(rig):
    lease = rig.checkout(mode=f"create:{ACT}")
    assert rig.reg.session(PID).mode_kind == "light"
    assert _light_rows(rig)[-1]["mode"] == "light"
    lease.release()


def test_the_reason_is_a_known_code():
    assert "form_photo_upload" in light_tab.REASONS
    with pytest.raises(ValueError):
        with light_tab.force_ads_manager("anything else"):
            pass
    assert light_tab.forced_reason() == ""


def test_a_light_lease_can_be_switched_to_ads_manager_and_keeps_its_lock(rig):
    lease = rig.checkout(mode=f"create:{ACT}")
    first_tab = lease.tab
    new = rig.reg.lease_to_ads_manager(lease, ACT, f"create:{ACT}", rig.make, "form_photo_upload")
    sess = rig.reg.session(PID)
    assert new is lease.graph and first_tab.closed and lease.tab is sess.tab and sess.tab is not first_tab
    assert sess.mode_kind == "ads_manager" and sess.owner != 0, "the same lease goes on (profile still held)"
    assert sess.light_fallbacks == 0
    assert rig.reg.lease_to_ads_manager(lease, ACT, f"create:{ACT}", rig.make, "form_photo_upload") is new
    lease.release()


# --------------------------- an upload that reaches a light tab ---------------------------

class Transport:
    def __init__(self, *, light, reply=None):
        self.light = light
        self.reply = reply or {"ok": True, "photo_id": "9001", "width": 600, "height": 400}
        self.uploads = 0
        self.write_page = None
        self.pinned = []
        self.leave_light = None

    def upload_form_photo(self, page_id, data, *, mime):
        self.uploads += 1
        if self.light:
            return {"error": "no_session"}       # what a light tab answers: no fb_dtsg / LSD
        return self.reply

    def pin_page(self, page):
        self.pinned.append(page)
        self.write_page = page


def _client(t):
    return g.GraphClient(t, allow_writes=True, sleep=lambda s: None, min_interval_s=0.0, max_calls=10)


def test_a_late_upload_switches_the_lease_and_uploads_exactly_once():
    light, heavy = Transport(light=True), Transport(light=False)
    light.write_page = PAGE
    light.leave_light = lambda: heavy
    client = _client(light)
    got = client.upload_form_photo(PAGE, PICTURE, mime="image/png")
    assert got["photo_id"] == "9001"
    assert light.uploads == 0 and heavy.uploads == 1
    assert client.transport is heavy and heavy.pinned == [PAGE]
    assert client.writes == 1 and client.calls == 1


def test_a_light_tab_never_answers_with_the_missing_session_values():
    light = Transport(light=True)
    with pytest.raises(g.FormPhotoRefused) as exc:
        _client(light).upload_form_photo(PAGE, PICTURE, mime="image/png")
    assert "session values" not in str(exc.value) and exc.value.code == "light_tab"
    assert light.uploads == 0, "nothing was sent"


def test_a_failed_switch_refuses_before_sending():
    light = Transport(light=True)

    def boom():
        raise RuntimeError("no tab")
    light.leave_light = boom
    client = _client(light)
    with pytest.raises(g.FormPhotoRefused) as exc:
        client.upload_form_photo(PAGE, PICTURE, mime="image/png")
    assert exc.value.code == "light_switch" and light.uploads == 0 and client.writes == 0


def test_a_switch_that_stays_light_is_a_refusal_not_a_send():
    light = Transport(light=True)
    light.leave_light = lambda: Transport(light=True)
    with pytest.raises(g.FormPhotoRefused):
        _client(light).upload_form_photo(PAGE, PICTURE, mime="image/png")


def test_a_second_upload_does_not_switch_again():
    light, heavy = Transport(light=True), Transport(light=False)
    calls = []
    light.leave_light = lambda: calls.append(1) or heavy
    client = _client(light)
    client.upload_form_photo(PAGE, PICTURE, mime="image/png")
    client.upload_form_photo(PAGE, PICTURE, mime="image/png")
    assert calls == [1] and heavy.uploads == 2


# --------------------------- the publisher ---------------------------

def _open_warm(monkeypatch, *, needs, dry=False):
    from src.fb_api import connection, warm

    seen = {}

    class Reg:
        def load_snapshot(self, pid):
            return None

        def checkout(self, pid, **kw):
            seen["forced"] = light_tab.forced_reason()
            seen["mode"] = kw["mode"]
            return SimpleNamespace(graph=LightGraph(SimpleNamespace()))

    monkeypatch.setattr(warm, "registry", lambda: Reg())
    monkeypatch.setattr(connection, "prepare_existing", lambda *a, **k: True)
    pub = pa.ApiPublisher(dry_run=dry)
    pub.needs_form_photo = needs
    pub.act = ACT
    pub._entry = SimpleNamespace()
    monkeypatch.setattr(pub, "_finish_open", lambda *a: None)
    pub._open_warm("Acc", PID)
    return pub, seen


def test_the_publisher_asks_for_ads_manager_only_when_the_job_uploads(monkeypatch):
    monkeypatch.setenv("ACL_LIGHT_WORK_TAB", "1")
    pub, seen = _open_warm(monkeypatch, needs=True)
    assert seen["forced"] == "form_photo_upload"
    assert light_tab.forced_reason() == "", "the decision does not leak past the checkout"
    pub, seen = _open_warm(monkeypatch, needs=False)
    assert seen["forced"] == "" and pub.light_fallback_reason == ""


def test_a_dry_run_never_forces_the_heavy_tab(monkeypatch):
    monkeypatch.setenv("ACL_LIGHT_WORK_TAB", "1")
    _pub, seen = _open_warm(monkeypatch, needs=True, dry=True)
    assert seen["forced"] == ""


def test_plan_form_photo_decides_from_the_drafts(tmp_path):
    pub = pa.ApiPublisher()
    d = _tpl_draft(tmp_path, [save_with_picture()["id"]])
    d.page_id = PAGE
    assert pub.plan_form_photo([d]) is True and pub.needs_form_photo
    store.remember_photo(PAGE, SHA, "9001")
    assert pub.plan_form_photo([d]) is False


def test_an_unreadable_draft_takes_the_safe_side():
    pub = pa.ApiPublisher()
    assert pub.plan_form_photo([SimpleNamespace(errors=[], ad_sets=5)]) is True
