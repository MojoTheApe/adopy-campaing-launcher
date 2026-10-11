"""ACL-1761: страна из двухбуквенного кода находится, даже если «q=AR» отдаёт много чужих стран."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.fb_api import creator as cr  # noqa: E402

NOISE = [{"key": c, "country_code": c, "name": n} for c, n in
         [("QA", "Qatar"), ("BA", "Bahrain-ish"), ("CA", "Canada"), ("DA", "Dania"), ("FA", "Faria"),
          ("GA", "Gabon"), ("HA", "Hara"), ("JA", "Jara"), ("KA", "Kara"), ("LA", "Laos")]]
AR = {"key": "AR", "country_code": "AR", "name": "Argentina"}
SA = {"key": "SA", "country_code": "SA", "name": "Saudi Arabia"}


class Client:
    """Фейковый GraphClient: считает вызовы; выдача зависит от q и limit."""

    def __init__(self, mode):
        self.mode = mode
        self.calls = []

    def get_all(self, path, params=None, *, max_pages=5):
        self.calls.append((dict(params), max_pages))
        q, limit = params["q"], params["limit"]
        if q == "SA":
            return [SA]
        if q == "ZZ":
            return list(NOISE)
        if self.mode == "by_name":
            return [AR] if q == "Argentina" else list(NOISE)[:limit]
        # "later_page": на широкой выдаче Аргентина в хвосте, на limit 10 её нет
        return list(NOISE) + [AR] if limit >= 100 else list(NOISE)[:limit]


def test_ar_resolved_by_name_when_code_query_is_noise():
    c = Client("by_name")
    assert cr.TargetingResolver(c).country("AR") == "AR"


def test_ar_resolved_on_wide_page():
    c = Client("later_page")
    assert cr.TargetingResolver(c).country("AR") == "AR"
    assert c.calls[0][0]["limit"] == 100 and c.calls[0][1] == 3


def test_sa_still_resolves_and_is_cached():
    c = Client("later_page")
    r = cr.TargetingResolver(c)
    assert r.country("SA") == "SA" and r.country("sa") == "SA"
    assert len(c.calls) == 1


def test_unknown_zz_keeps_error():
    with pytest.raises(cr.PlanError, match="Country 'ZZ' was not found by Meta targeting search"):
        cr.TargetingResolver(Client("by_name")).country("ZZ")


def test_near_match_never_picked():
    class C(Client):
        def get_all(self, *a, **k):
            return [{"key": "AT", "country_code": "AT", "name": "Austria"}]
    with pytest.raises(cr.PlanError):
        cr.TargetingResolver(C("x")).country("AR")
