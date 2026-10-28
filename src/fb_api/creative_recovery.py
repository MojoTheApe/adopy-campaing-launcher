"""ACL-2231: bounded creative receipts and positive content verification.

Only the creative stage is journaled here. No Graph writes, tokens or global
operation lifecycle; unknown dispatches never expire into permission to create.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import time
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


def _short(value):
    text = str(value)
    return text if len(text) <= 60 else text[:57] + "..."


def first_diff(act, params, row):
    """Explain a refusal: the first path where the earlier creative differs from the requested one.

    Diagnostic only. It never decides which creative is accepted (that stays with matches()); values are
    trimmed to a short form."""
    if not isinstance(row, dict):
        return "the creative could not be read"
    if not re.fullmatch(r"[0-9]{1,30}", str(row.get("id") or "")):
        return "id: unreadable"
    if str(row.get("account_id") or "") != str(act).removeprefix("act_"):
        return (f"account_id: expected {_short(str(act).removeprefix('act_'))!r} "
                f"got {_short(row.get('account_id') or '')!r}")
    actual = dict(row)
    story = dict(actual.get("object_story_spec") or {})
    if "instagram_user_id" not in story and actual.get("instagram_user_id"):
        story["instagram_user_id"] = actual["instagram_user_id"]
    actual["object_story_spec"] = story

    def walk(want, got, where):
        if _contains(want, got):
            return ""
        if isinstance(want, dict):
            if not isinstance(got, dict):
                return f"{where}: expected an object got {_short(got)!r}"
            for key, value in want.items():
                found = walk(value, got.get(key), f"{where}.{key}" if where else key)
                if found:
                    return found
            return ""
        if isinstance(want, list):
            if not isinstance(got, list):
                return f"{where}: expected a list got {_short(got)!r}"
            if len(want) != len(got):
                return f"{where}: expected {len(want)} items got {len(got)}"
            for i, (x, y) in enumerate(zip(want, got)):
                found = walk(x, y, f"{where}[{i}]")
                if found:
                    return found
            return ""
        return f"{where}: expected {_short(want)!r} got {_short('' if got is None else got)!r}"
    return walk(material(params), actual, "") or "no content difference (the tag or id did not match)"


# ACL-2284: where a creative carries its media. A difference ONLY in these places means "another file",
# not "another creative we failed to confirm".
_MEDIA_LISTS = (("asset_feed_spec", "images"), ("asset_feed_spec", "videos"))
_MEDIA_SCALARS = (("object_story_spec", "link_data", "image_hash"), ("object_story_spec", "video_data", "video_id"),
                  ("object_story_spec", "video_data", "image_url"))


def _dig(obj, path):
    for key in path:
        if not isinstance(obj, dict) or key not in obj:
            return None, False
        obj = obj[key]
    return obj, True


def _put(obj, path, value):
    for key in path[:-1]:
        obj = obj[key]
    obj[path[-1]] = value


def _pair(item):
    labels = tuple(sorted(str(l.get("name")) for l in (item.get("adlabels") or []) if isinstance(l, dict)))
    return (str(item.get("hash") or item.get("video_id") or ""), labels)


def media_diff(act, params, row):
    """ACL-2284: how a found creative of this account differs from the requested one.

    "same"  - it matches (matches());
    "order" - same files with the same labels, only listed in another order (main vs Stories/Reels);
    "stale" - the ONLY difference is the media (image hash / video id): another file, not reusable;
    "other" - anything else (foreign account, unreadable, text/CTA/link/Page/identity): the caller stops.
    """
    if matches(act, params, row):
        return "same"
    if not isinstance(row, dict) or not re.fullmatch(r"[0-9]{1,30}", str(row.get("id") or "")):
        return "other"
    if str(row.get("account_id") or "") != str(act).removeprefix("act_"):
        return "other"
    want = material(params)
    actual = dict(row)
    story = dict(actual.get("object_story_spec") or {})
    if "instagram_user_id" not in story and actual.get("instagram_user_id"):
        story["instagram_user_id"] = actual["instagram_user_id"]
    actual["object_story_spec"] = story
    fixed = copy.deepcopy(want)
    same_set, touched = True, False
    for path in _MEDIA_LISTS:
        w, has_w = _dig(want, path)
        g, has_g = _dig(actual, path)
        if has_w != has_g:
            return "other"
        if not has_w:
            continue
        if not (isinstance(w, list) and isinstance(g, list) and all(isinstance(i, dict) for i in w + g)
                and all(i.get("hash") or i.get("video_id") for i in g)):
            return "other"
        if sorted(_pair(i) for i in w) != sorted(_pair(i) for i in g):
            same_set = False
        _put(fixed, path, copy.deepcopy(g))
        touched = True
    for path in _MEDIA_SCALARS:
        w, has_w = _dig(want, path)
        g, has_g = _dig(actual, path)
        if has_w != has_g:
            return "other"
        if not has_w:
            continue
        if not isinstance(g, str) or not g:
            return "other"
        if w != g:
            same_set = False
        _put(fixed, path, g)
        touched = True
    if not touched or not _contains(fixed, actual):
        return "other"
    return "order" if same_set else "stale"


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
            if (isinstance(self.data, dict) and self.data.get("schema") == 1
                    and self.data.get("act") == act and self.data.get("identity") == identity
                    and self.data.get("fingerprint") != digest):
                # A repeat of the same ad with other content (new media ids, edited text) is a different
                # creative: the earlier receipt describes another object and must not block this one.
                self.data = None
            elif (not isinstance(self.data, dict) or self.data.get("schema") != 1
                    or self.data.get("act") != act or self.data.get("identity") != identity
                    or self.data.get("fingerprint") != digest
                    or self.data.get("state") not in {"pending", "confirmed"}
                    or (self.data.get("state") == "pending" and self.data.get("id") != "")
                    or (self.data.get("state") == "confirmed" and
                        not re.fullmatch(r"[0-9]{1,30}", str(self.data.get("id") or "")))):
                raise ValueError("Creative receipt is unreadable or belongs to a different request")

    def age_s(self):
        try:
            return max(0.0, time.time() - self.path.stat().st_mtime)
        except OSError:
            return 0.0

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


def video_pairs(act, params, row):
    """(requested id, returned id) of videos that differ, when that is the ONLY difference; else None.

    A repeated upload of the same file gives a new video id, while Meta answers an identical creative
    write with the earlier creative that still points at the earlier id."""
    if not isinstance(row, dict):
        return None
    want = material(params)
    feed = (want.get("asset_feed_spec") or {}) if isinstance(want, dict) else {}
    got_feed = row.get("asset_feed_spec") or {}
    wv, gv = feed.get("videos"), got_feed.get("videos")
    if not isinstance(wv, list) or not isinstance(gv, list) or len(wv) != len(gv) or not wv:
        return None
    pairs = []
    for w, g in zip(wv, gv):
        if not isinstance(w, dict) or not isinstance(g, dict) or not g.get("video_id"):
            return None
        if str(w.get("video_id")) != str(g.get("video_id")):
            pairs.append((str(w.get("video_id")), str(g["video_id"])))
    if not pairs:
        return None
    fixed = dict(params)
    fixed["asset_feed_spec"] = dict(params["asset_feed_spec"], videos=[
        dict(v, video_id=g["video_id"]) for v, g in zip(params["asset_feed_spec"]["videos"], gv)])
    return pairs if matches(act, fixed, row) else None
