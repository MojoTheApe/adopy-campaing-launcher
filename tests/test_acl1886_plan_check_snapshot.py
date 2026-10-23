"""ACL-1886, критерий 5: снимок настоящего «Check plan» при отказе Graph-чтения.

Настоящий `publish_api.check_plan` → GraphClient → Creator над поддельной Meta в
памяти; чтение `/search` или `/campaigns` получает обезличенный ответ error10 из
`fb_api_permission_denied_2026_10_13.json`. Снимок: операция доходит до
публичного результата, success/publishable остаются false, записей ноль, сокетов
ноль, синтетический id кабинета в результат не попадает.
"""
from __future__ import annotations

import json
import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.test_acl1761_api_create import ACT, FakeMeta, _draft  # noqa: E402
from tests.test_acl1761_plan_check import _read_session  # noqa: E402
from tests.test_acl1886_graph_error_context import denied  # noqa: E402

_DENIED = ("Graph API error 10: (#10) Permission Denied [type=OAuthException, "
           "host=adsmanager-graph.facebook.com, credentials=include, http_status=400, method=GET, ")

_CATALOG_10 = ("Permission was not granted or has been removed. Check that this account has the needed "
               "permission for the ad account or Page.")


class DeniedMeta(FakeMeta):
    """Поддельная Meta, у которой одно чтение (по окончанию пути) отказано."""

    def __init__(self, denied_suffix):
        super().__init__()
        self.denied_suffix = denied_suffix
        self.denied = []

    def request(self, method, path, params=None):
        if path.endswith(self.denied_suffix):
            assert method == "GET"
            self.gets.append((path, params or {}))
            self.denied.append(path)
            return denied()
        return super().request(method, path, params)


@pytest.fixture
def no_network(monkeypatch):
    """Любая попытка открыть сокет — падение теста, а не тихий сетевой вызов."""
    attempts = []

    def refuse(self, *args, **kwargs):
        attempts.append(args)
        raise AssertionError("Plan check snapshot must not touch the network")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    return attempts


def _check(tmp_path, monkeypatch, denied_suffix):
    from src.web import publish_api

    meta = DeniedMeta(denied_suffix)
    monkeypatch.setattr(publish_api, "build_drafts", lambda slots, **kw: [_draft(tmp_path, ads=1)])
    opened = _read_session(monkeypatch, meta)
    out = publish_api.check_plan([{"name": "x"}], account_label="Test", country="MX")
    return out, meta, opened


def _assert_read_only(out, meta, opened, no_network):
    assert out["dry_run"] is True and out["error"] == "" and out["account_audit"] is None
    (res,) = out["results"]
    assert res["dry_run"] is True and res["success"] is False and res["publishable"] is False
    assert res["created"] == {} and res["plan"] == {}
    assert meta.posts == [] and meta.on_ad_posts == []
    (client,) = opened
    assert client.allow_writes is False and client.writes == 0
    assert no_network == []
    assert ACT.removeprefix("act_") not in json.dumps(out)
    return res


def test_denied_search_reaches_the_public_plan_result(tmp_path, monkeypatch, no_network):
    out, meta, opened = _check(tmp_path, monkeypatch, "/search")
    res = _assert_read_only(out, meta, opened, no_network)
    assert meta.denied and set(meta.denied) == {"/search"}
    assert res["error"] == "Meta refused: " + _CATALOG_10  # ACL-1805: the catalog text; the raw line stays in problems[].raw
    assert res["problems"][0]["raw"].endswith("operation=search, search_type=adgeolocation]")
    audit = {row["rule"]: row for row in res["audit"]}
    assert audit["country_resolves"]["status"] == "unknown"
    assert audit["country_resolves"]["detail"] == (
        "Meta did not answer: " + _DENIED + "operation=search, search_type=adgeolocation]")
    assert audit["language_resolves"]["detail"] == (
        "Meta did not answer: " + _DENIED + "operation=search, search_type=adlocale]")
    assert audit["interests_resolve"]["detail"] == "Meta did not answer: " + _DENIED + "operation=search]"
    assert not any(p.endswith("/campaigns") for p, _ in meta.gets)


def test_denied_campaigns_reaches_the_public_plan_result(tmp_path, monkeypatch, no_network):
    out, meta, opened = _check(tmp_path, monkeypatch, "/campaigns")
    res = _assert_read_only(out, meta, opened, no_network)
    assert meta.denied == [f"/{ACT}/campaigns"]
    assert res["error"] == "Meta refused: " + _CATALOG_10
    assert res["problems"][0]["raw"].endswith("operation=campaigns]")
    assert "operation=search" not in res["error"]
