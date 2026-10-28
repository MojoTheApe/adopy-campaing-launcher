"""ACL-2231: bounded creative receipts and positive content verification.

Only the creative stage is journaled here. No Graph writes, tokens or global
operation lifecycle; unknown dispatches never expire into permission to create.
"""
from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from pathlib import Path

from config.paths import get_local_config_dir, machine_file_lock, _atomic_write

FIELDS = ("id,name,account_id,adlabels{name},object_story_spec,asset_feed_spec,"
          "degrees_of_freedom_spec,contextual_multi_ads,instagram_user_id")


def material(params):
    """Exclude only metadata and expiring video thumbnail URLs from identity."""
    def clean(value):
        if isinstance(value, dict):
            return {k: clean(v) for k, v in value.items() if k != "thumbnail_url"}
        if isinstance(value, list):
            return [clean(v) for v in value]
        return value
    return clean({k: v for k, v in params.items() if k not in {"name", "adlabels"}})


def fingerprint(params):
    return hashlib.sha256(json.dumps(material(params), sort_keys=True,
                                     ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _contains(expected, actual):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(_contains(v, actual.get(k)) for k, v in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(expected) == len(actual) and all(
            _contains(e, a) for e, a in zip(expected, actual))
    if expected == "":
        return actual in ("", None)
    return type(expected) is type(actual) and expected == actual


def complete(params, row):
    """Missing material is unknown, rather than evidence of a different creative."""
    def readable(want, got):
        if isinstance(want, dict):
            return isinstance(got, dict) and all(readable(v, got.get(k)) for k, v in want.items())
        if isinstance(want, list):
            return isinstance(got, list) and all(
                readable(want[min(i, len(want)-1)], v) for i, v in enumerate(got)) if want else isinstance(got, list)
        return got is None if want == "" and got is None else type(want) is type(got)
    actual = dict(row)
    story = actual.get("object_story_spec")
    if isinstance(story, dict) and actual.get("instagram_user_id") and "instagram_user_id" not in story:
        actual["object_story_spec"] = dict(story, instagram_user_id=actual["instagram_user_id"])
    return readable(material(params), actual)


def matches(act, params, row):
    """Require actual account and all requested material, never just a name/tag."""
    if not isinstance(row, dict) or not re.fullmatch(r"[0-9]{1,30}", str(row.get("id") or "")):
        return False
    if str(row.get("account_id") or "") != str(act).removeprefix("act_"):
        return False
    want = material(params)
    actual = dict(row)
    story = dict(actual.get("object_story_spec") or {})
    if "instagram_user_id" not in story and actual.get("instagram_user_id"):
        story["instagram_user_id"] = actual["instagram_user_id"]
    actual["object_story_spec"] = story
    return _contains(want, actual)


def same_ad(name, stem):
    # Meta may append its date/hash suffix. Preserve the ad index identity (M2).
    return bool(re.fullmatch(re.escape(stem) +
                r"(?: [0-9a-f]{12})? \[acl-[0-9a-f]{12}\](?: [0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9a-f]{32})?", str(name or "")))


class Receipt:
    def __init__(self, path, act, identity, digest):
        self.path, self.act, self.identity, self.digest = path, act, identity, digest
        self.data = None
        if path.exists():
            self.data = json.loads(path.read_text(encoding="utf-8"))
            if (not isinstance(self.data, dict) or self.data.get("schema") != 1
                    or self.data.get("act") != act or self.data.get("identity") != identity
                    or self.data.get("fingerprint") != digest
                    or self.data.get("state") not in {"pending", "confirmed"}
                    or (self.data.get("state") == "pending" and self.data.get("id") != "")
                    or (self.data.get("state") == "confirmed" and
                        not re.fullmatch(r"[0-9]{1,30}", str(self.data.get("id") or "")))):
                raise ValueError("Creative receipt is unreadable or belongs to a different request")

    @property
    def object_id(self):
        return str((self.data or {}).get("id") or "")

    @property
    def accepted_identical(self):
        """ACL-2249: our own write was answered with an identical creative of an earlier run."""
        return (self.data or {}).get("accepted_identical") is True

    def save(self, object_id="", *, accepted_identical=False):
        if object_id and not re.fullmatch(r"[0-9]{1,30}", str(object_id)):
            raise ValueError("Creative id was not confirmed")
        data = {"schema": 1, "act": self.act, "identity": self.identity,
                "fingerprint": self.digest, "state": "confirmed" if object_id else "pending",
                "id": str(object_id)}
        if accepted_identical and object_id:
            data["accepted_identical"] = True
        _atomic_write(self.path, json.dumps(data, sort_keys=True).encode())
        self.data = data


@contextmanager
def transaction(act, identity, params):
    if not re.fullmatch(r"act_[0-9]+", str(act)):
        raise ValueError("Creative account is invalid")
    key = hashlib.sha256((act + "\n" + identity).encode()).hexdigest()
    path = Path(get_local_config_dir()) / "creative_receipts" / (key + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    with machine_file_lock(path.with_suffix(".lock")):
        yield Receipt(path, act, identity, fingerprint(params))
