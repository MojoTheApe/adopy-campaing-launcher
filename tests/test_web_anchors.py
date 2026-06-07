from __future__ import annotations

from src.web.app import _anchor_slug


def test_anchor_slug_normalizes_country_names() -> None:
    assert _anchor_slug("Mexico") == "mexico"
    assert _anchor_slug("United Arab Emirates") == "united-arab-emirates"
    assert _anchor_slug("  Guatemala  ") == "guatemala"
