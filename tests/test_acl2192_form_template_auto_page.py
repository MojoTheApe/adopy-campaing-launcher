"""ACL-2192: публикация формы из шаблона при Странице «Automatic» (пустой page_id).

Раньше `pin_page("")` падал («the Page to pin must be a numeric Page id») ещё до первой записи,
потому что Страницу кабинета выбирал creator позже. Теперь Страница выбирается ДО закрепления.
Только подделки: Graph - `FormsMeta` в памяти, закрепление - запись вызова.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.test_acl1761_api_create import ACT, _publisher  # noqa: E402
from tests.test_acl1775_instant_forms import PAGE  # noqa: E402
from tests.test_acl1776_create_forms import FormsMeta, _saved, _tpl_draft  # noqa: E402,F401
from tests.test_acl1776_create_forms import _library  # noqa: E402,F401  (autouse-фикстура)


class PinMeta(FormsMeta):
    """FormsMeta, который принимает закрепление Страницы и запоминает значение."""

    def __init__(self, pages=("111",), **kw):
        super().__init__(**kw)
        self.pages = list(pages)
        self.pinned: list[str] = []

    def pin_page(self, page, *, timeout=30.0):
        if not str(page).isdigit():
            raise ValueError("the Page to pin must be a numeric Page id")
        self.pinned.append(page)
        return {"kind": "ads_manager", "has_token": True, "pinned": True}

    def _get(self, path, params):
        parts = path.strip("/").split("/")
        if parts[0] == ACT and len(parts) > 1 and parts[1] == "promote_pages":
            return {"data": [{"id": p} for p in self.pages]}
        return super()._get(path, params)


def _draft_for(tmp_path, page_id):
    tid = _saved()["id"]
    d = _tpl_draft(tmp_path, [tid])
    d.page_id = page_id
    return d


def test_automatic_page_is_resolved_before_pinning(tmp_path):
    meta = PinMeta()
    res = _publisher(meta).publish_draft(_draft_for(tmp_path, None))
    assert not res.get("error"), res
    assert meta.pinned == [PAGE]                       # числовой id Страницы кабинета
    assert meta.form_posts and meta.form_posts[0][0] == f"/{PAGE}/leadgen_forms"
    ads = [p for p in meta.posts if p[0].endswith("/ads")]
    assert ads and any(PAGE in str(v) for _p, v in meta.posts if _p.endswith("/adcreatives"))


def test_no_pages_available_is_a_clear_refusal_and_nothing_is_created(tmp_path):
    meta = PinMeta(pages=())
    res = _publisher(meta).publish_draft(_draft_for(tmp_path, None))
    assert "Nothing was created." in res["error"] and "Page" in res["error"]
    assert meta.pinned == [] and meta.posts == []


def test_explicit_page_id_path_is_unchanged(tmp_path):
    meta = PinMeta(pages=("111", "222"))
    res = _publisher(meta).publish_draft(_draft_for(tmp_path, "111"))
    assert not res.get("error"), res
    assert meta.pinned == ["111"]


def test_explicit_page_not_on_account_still_refused_by_the_creator_path(tmp_path):
    meta = PinMeta(pages=("222",))
    res = _publisher(meta).publish_draft(_draft_for(tmp_path, "111"))
    assert res["error"]                                  # как раньше: чужая Страница не подменяется
    assert not [p for p in meta.posts if p[0].endswith("/adsets")]


def test_check_plan_is_unchanged_and_does_not_pin(tmp_path):
    meta = PinMeta()
    res = _publisher(meta, dry_run=True).publish_draft(_draft_for(tmp_path, None))
    assert not res.get("error"), res
    assert meta.pinned == [] and meta.posts == []
    assert res["plan"]["summary"]["page_source"] == "account default"
