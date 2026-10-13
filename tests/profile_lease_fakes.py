"""Stable valid identities for synthetic profiles; never real account data."""
from uuid import NAMESPACE_URL, uuid5


def fake_profile(label: str) -> str:
    return str(uuid5(NAMESPACE_URL, "https://example.invalid/acl-test-profile/" + label))
