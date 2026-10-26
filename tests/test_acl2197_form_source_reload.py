# -*- coding: utf-8 -*-
"""ACL-2197: источник формы объявления (существующая / шаблон) переживает перезагрузку страницы.

Раньше в черновике жил выбранный шаблон, а режим (`select.cb-form-source`) — нет: после перезагрузки
режим возвращался в «Use existing form», поле шаблона отключалось, form_id был пуст, и аудит писал
«Instant Form chosen: error». Теперь источник — поле `ad_N_form_source` черновика; старый черновик
без него читается так: шаблон без form_id = режим шаблона.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

TPL = ROOT / "src" / "web" / "templates" / "report.html"


def test_the_source_select_is_a_named_draft_field_renumbered_with_its_ad():
    page = TPL.read_text(encoding="utf-8")
    assert "cb-form-source\" name=\"' + pfx + 'form_source\"" in page


pw_sync = pytest.importorskip("playwright.sync_api")

from test_acl1681_server_drafts import Profile, _local_dir  # noqa: E402,F401
from test_builder_per_account import _render  # noqa: E402
from test_drawer_review_browser import add  # noqa: E402

TID = "0123456789ab"


@pytest.fixture(scope="module")
def browser():
    with pw_sync.sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        yield b
        b.close()


@pytest.fixture
def profile(browser, _local_dir):
    prof = Profile(browser, "http://127.0.0.1:8000", _render({"Demo": [], "Other": []}))
    yield prof
    assert not prof.errors, prof.errors
    prof.close()


def _setup(p, source, form_id=""):
    """Карточка с Instant Form; два объявления: первое — как задано, второе — «существующая форма»."""
    add(p)
    p.evaluate("""([source, formId, tid]) => {
      const slot = document.querySelector('.cb-slot');
      slot.querySelector('.cb-destination').value = 'instant_form';
      cbAddAd(slot.querySelector('.cb-add-btn-sm'));
      const rows = slot.querySelectorAll('.cb-ad-row');
      const set = (row, src, fid, tpl) => {
        row.querySelector('.cb-form-source').value = src;
        row.querySelector('input[name$="_form_id"]').value = fid;
        row.querySelector('input[name$="_form_template"]').value = tpl;
      };
      set(rows[0], source, formId, tid);
      set(rows[1], 'existing', '7000001', '');
      cbSyncDestination(slot);
      slot.querySelector('.cb-form-source').dispatchEvent(new Event('change', {bubbles: true}));
      cbKeep.save();
    }""", [source, form_id, TID])


def _state(p):
    return p.evaluate("""() => Array.from(document.querySelectorAll('.cb-slot .cb-ad-row')).map(r => ({
      src: r.querySelector('.cb-form-source').value,
      tplOn: !r.querySelector('input[name$="_form_template"]').disabled,
      idOn: !r.querySelector('input[name$="_form_id"]').disabled,
      tpl: r.querySelector('input[name$="_form_template"]').value,
      fid: r.querySelector('input[name$="_form_id"]').value }))""")


def _reload(prof):
    prof.page.reload()
    prof.page.wait_for_function("window.cbSrv && cbSrv.ready()", polling=100)
    prof.page.wait_for_function("document.querySelectorAll('.cb-slot').length > 0")
    return prof.page


def test_a_template_ad_stays_in_template_mode_after_a_reload(profile):
    p = profile.open()
    _setup(p, "template")
    p = _reload(profile)
    a, b = _state(p)
    assert a["src"] == "template" and a["tpl"] == TID and a["tplOn"] and not a["idOn"]
    assert b["src"] == "existing" and b["fid"] == "7000001" and b["idOn"] and not b["tplOn"]


def test_an_ad_the_person_switched_to_existing_form_stays_existing_even_with_a_template_left_behind(profile):
    p = profile.open()
    _setup(p, "existing", form_id="")
    p = _reload(profile)
    a, _ = _state(p)
    assert a["src"] == "existing" and a["tpl"] == TID and a["idOn"] and not a["tplOn"]


def test_an_old_draft_without_the_stored_source_is_read_by_its_template(profile):
    p = profile.open()
    _setup(p, "template")
    # старый черновик: поле источника ещё не писалось
    p.evaluate("""() => {
      const d = JSON.parse(localStorage.getItem('acl.builder.v2'));
      Object.values(d.forms).forEach(f => f.campaigns.forEach(c => { Object.keys(c.fields).forEach(k => { if (/_form_source$/.test(k)) delete c.fields[k]; }); }));
      localStorage.setItem('acl.builder.v2', JSON.stringify(d));
    }""")
    p = _reload_without_server_copy(profile)
    a, b = _state(p)
    assert a["src"] == "template" and a["tplOn"] and not a["idOn"]
    assert b["src"] == "existing" and b["idOn"]


def _reload_without_server_copy(prof):
    """Старый черновик лежит только в localStorage: серверной копии не было (она появилась позже)."""
    prof.page.unroute("**/*")
    def route(r):
        path = urlparse(r.request.url).path
        if r.request.resource_type == "document":
            r.fulfill(content_type="text/html", body=prof.html)
        elif r.request.resource_type == "script":
            r.fulfill(content_type="application/javascript", body="")
        elif path == "/api/builder-state" and r.request.method == "GET":
            r.fulfill(json={"ok": True, "docs": {}})
        elif path == "/api/builder-state":
            r.fulfill(json={"ok": True, "accepted": True, "doc": {}})
        else:
            r.fulfill(json={"files": [], "texts": [], "rows": [], "profiles": [], "waiting": False})
    prof.page.route("**/*", route)
    return _reload(prof)
