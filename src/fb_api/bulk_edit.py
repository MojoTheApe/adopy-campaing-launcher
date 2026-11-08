"""Bulk edit of selected campaigns: budget, age, gender, languages, placements (ACL-2457).

One human-confirmed operation over several campaigns of ONE ad account. It rides on the
ACL-1800 command path (`src/fb_api/commands.py`): the same operation store and history, the
same preview -> confirm -> execute split, the same idempotency key, the same in-page write
policy, the same readback. What is new:

* `preview` reads every selected campaign (one Graph call each, ad sets nested) and builds an
  immutable plan: per object the fields that really change (before -> after) and, separately,
  what is skipped and why. Nothing is written. Fields the buyer did not touch are not in the plan.
* `run` (registered as `commands.RUNNERS["bulk_edit"]`) re-reads everything first and refuses to
  write if any planned object changed since the preview. Then ONE write per object field, no
  retries. A failure on one object does not stop the others; only a session-level stop (Meta
  throttle, logged out, call limit, history not writable) or an unknown outcome that the readback
  cannot settle does. Finally every written campaign is read back and compared.
* Targeting lives on ad sets and a POST replaces it whole, so the write sends the CURRENT targeting
  (read just before) with only the managed keys patched. Anything else in it travels unchanged.
"""
from __future__ import annotations

from typing import Any, Callable

from src.fb_api import commands as _c
from src.fb_api import write_policy as _wp
from src.fb_api.commands import CommandRefused, HistoryUnavailable, OperationStore
from src.fb_api.graph import GraphError, GraphStop, WritesBlocked, WriteUnknown
from src.fb_api.creator import (FACEBOOK_POSITIONS, INSTAGRAM_POSITIONS, PUBLISHER_PLATFORMS, ZERO_DECIMAL,
                                PlanError)

COMMAND = "bulk_edit"
#: Campaigns per operation (a hard cap; the Graph call ceiling may lower it).
MAX_CAMPAIGNS = 10
#: Ad sets read per campaign; more than this and the campaign's targeting is not edited.
MAX_ADSETS = 25
MAX_LANGUAGES = 5

CAMPAIGN_FIELDS = ("name,account_id,status,effective_status,daily_budget,lifetime_budget,"
                   f"adsets.limit({MAX_ADSETS}){{id,name,status,effective_status,targeting}}")

GENDER_IDS = {"men": 1, "women": 2}
GENDER_WORDS = {"all": "All", "men": "Men", "women": "Women"}
PRESETS = {
    "advantage_plus": "Advantage+ (automatic)",
    "acl_default": "ACL default (Facebook feed, Stories, Reels, in-stream; Instagram feed, Stories, Reels)",
}
CHANGE_KEYS = ("budget", "age_min", "age_max", "gender", "languages", "placements")
TARGETING_KEYS = ("age_min", "age_max", "gender", "languages", "placements")
#: Meta defaults when the ad set carries no explicit age.
DEFAULT_AGE = {"age_min": 18, "age_max": 65}
_ARCHIVED = ("ARCHIVED", "DELETED")


# ───────────────────────── the request ─────────────────────────

def parse_changes(raw: Any) -> dict:
    """The buyer's change set -> a clean dict with ONLY the fields that were asked for."""
    if not isinstance(raw, dict):
        raise CommandRefused("Choose at least one field to change.")
    unknown = sorted(set(raw) - set(CHANGE_KEYS))
    if unknown:
        raise CommandRefused(f"Field {unknown[0]!r} cannot be edited in bulk.")
    out: dict[str, Any] = {}
    if raw.get("budget") not in (None, ""):
        text = str(raw["budget"]).strip()
        if not _c._DEC_RE.fullmatch(text):
            raise CommandRefused("Enter a positive budget with at most two decimals.")
        out["budget"] = text
    for key in ("age_min", "age_max"):
        if raw.get(key) in (None, ""):
            continue
        v = raw[key]
        if isinstance(v, bool) or not (isinstance(v, int) or (isinstance(v, str) and v.strip().isdigit())):
            raise CommandRefused("Age must be a whole number from 13 to 65.")
        v = int(v)
        if not 13 <= v <= 65:
            raise CommandRefused("Age must be a whole number from 13 to 65.")
        out[key] = v
    if "age_min" in out and "age_max" in out and out["age_min"] > out["age_max"]:
        raise CommandRefused("The minimum age is above the maximum age.")
    if raw.get("gender") not in (None, ""):
        if raw["gender"] not in GENDER_WORDS:
            raise CommandRefused("Gender must be all, men or women.")
        out["gender"] = raw["gender"]
    langs = raw.get("languages")
    if langs not in (None, "", []):
        if not isinstance(langs, list) or not langs or len(langs) > MAX_LANGUAGES:
            raise CommandRefused(f"Give 1 to {MAX_LANGUAGES} languages.")
        names: list[str] = []
        for name in langs:
            text = " ".join(str(name or "").split())
            if not 2 <= len(text) <= 60:
                raise CommandRefused("A language name must be 2 to 60 characters.")
            if text.casefold() not in [n.casefold() for n in names]:
                names.append(text)
        out["languages"] = names
    if raw.get("placements") not in (None, ""):
        if raw["placements"] not in PRESETS:
            raise CommandRefused("Choose a placements preset from the list.")
        out["placements"] = raw["placements"]
    if not out:
        raise CommandRefused("Choose at least one field to change.")
    return out


def parse_campaigns(raw: Any) -> list[str]:
    if not isinstance(raw, list) or not raw:
        raise CommandRefused("Pick at least one campaign.")
    ids: list[str] = []
    for item in raw:
        oid = str(item.get("id") if isinstance(item, dict) else item or "")
        if not _c._ID_RE.fullmatch(oid):
            raise CommandRefused("Campaign id must be a numeric Meta object id.")
        if oid not in ids:
            ids.append(oid)
    if len(ids) > MAX_CAMPAIGNS:
        raise CommandRefused(f"At most {MAX_CAMPAIGNS} campaigns per bulk edit.")
    return ids


# ───────────────────────── reading ─────────────────────────

def read_campaign(client: Any, act: str, cid: str) -> dict:
    """One Graph call -> campaign with its (up to MAX_ADSETS) ad sets and their targeting."""
    raw = client.get(f"/{cid}", {"fields": CAMPAIGN_FIELDS})
    if not isinstance(raw, dict) or str(raw.get("id") or cid) != cid:
        raise CommandRefused("Meta answered for a different object; refused.", status=409)
    if str(raw.get("account_id") or "") != act.removeprefix("act_"):
        raise CommandRefused("The object belongs to a different ad account; refused.", status=409)
    edge = raw.get("adsets") if isinstance(raw.get("adsets"), dict) else {}
    sets = []
    for a in edge.get("data") or []:
        if isinstance(a, dict) and a.get("id"):
            t = a.get("targeting")
            sets.append({"id": str(a["id"]), "name": str(a.get("name") or a["id"])[:160],
                         "status": str(a.get("status") or ""),
                         "targeting": t if isinstance(t, dict) else None})
    return {"id": cid, "name": str(raw.get("name") or cid)[:160], "status": str(raw.get("status") or ""),
            "daily_budget": _c._int(raw.get("daily_budget")) or None,
            "lifetime_budget": _c._int(raw.get("lifetime_budget")) or None,
            "adsets": sets, "more_adsets": bool((edge.get("paging") or {}).get("next"))}


# ───────────────────────── targeting views ─────────────────────────

def _gender_of(t: dict) -> str:
    g = t.get("genders")
    if not g or sorted(g) == [1, 2]:
        return "all"
    return "men" if g == [1] else "women" if g == [2] else "other"


def _preset_of(t: dict) -> str:
    platforms = t.get("publisher_platforms")
    if not platforms:
        return "advantage_plus"
    default = _default_placements()
    extra = [k for k in t if k.endswith("_positions") and k not in default]
    if (set(platforms) == set(default["publisher_platforms"])
            and set(t.get("facebook_positions") or []) == set(default["facebook_positions"])
            and set(t.get("instagram_positions") or []) == set(default["instagram_positions"]) and not extra):
        return "acl_default"
    return "custom"


def _default_placements() -> dict:
    return {"publisher_platforms": list(PUBLISHER_PLATFORMS), "facebook_positions": list(FACEBOOK_POSITIONS),
            "instagram_positions": list(INSTAGRAM_POSITIONS)}


def _expand(ids: list[int], groups: dict) -> set[int]:
    out: set[int] = set()
    for i in ids:
        out |= set(groups.get(str(i)) or []) or {i}
    return out


def _locales_equal(a: list[int], b: list[int], groups: dict) -> bool:
    """Same languages; a group such as "Spanish (All)" equals the locales Meta expands it into."""
    if set(a) == set(b):
        return True
    return _expand(a, groups) == _expand(b, groups)


def current_view(t: dict) -> dict:
    """The managed part of an ad set's targeting, as the buyer thinks of it."""
    return {"age_min": t.get("age_min") or DEFAULT_AGE["age_min"],
            "age_max": t.get("age_max") or DEFAULT_AGE["age_max"],
            "gender": _gender_of(t), "languages": list(t.get("locales") or []),
            "placements": _preset_of(t)}


def _differs(key: str, cur: dict, op: dict) -> bool:
    want = op["want"][key]
    if key == "languages":
        return not _locales_equal(cur["languages"], want, op["locale_groups"])
    return cur[key] != want


def build_want(changes: dict, locales: list[int] | None) -> dict:
    want = {k: changes[k] for k in ("age_min", "age_max", "gender", "placements") if k in changes}
    if "languages" in changes:
        want["languages"] = list(locales or [])
    return want


def patch_targeting(t: dict, keys: list[str], want: dict) -> dict:
    """The current targeting with ONLY the keys the buyer changed replaced."""
    out = {k: v for k, v in t.items()}
    for key in keys:
        if key in ("age_min", "age_max"):
            out[key] = want[key]
        elif key == "gender":
            if want[key] == "all":
                out.pop("genders", None)
            else:
                out["genders"] = [GENDER_IDS[want[key]]]
        elif key == "languages":
            out["locales"] = list(want[key])
        elif key == "placements":
            for k in [k for k in out if k.endswith("_positions")] + ["publisher_platforms"]:
                out.pop(k, None)
            if want[key] == "acl_default":
                out.update(_default_placements())
    return out


# ───────────────────────── display ─────────────────────────

def _names(op: dict) -> dict:
    return op.get("locale_names") or {}


def show(key: str, value: Any, op: dict) -> str:
    if key == "age_min":
        return f"min age {value}"
    if key == "age_max":
        return f"max age {value}"
    if key == "gender":
        return f"gender {GENDER_WORDS.get(value, 'custom')}"
    if key == "languages":
        names = _names(op)
        return "languages " + (", ".join(names.get(str(i), f"#{i}") for i in value) if value else "any")
    return "placements " + {"custom": "Custom", **PRESETS}.get(value, str(value)).split(" (")[0]


def _show_all(keys: list[str], values: dict, op: dict) -> str:
    return ", ".join(show(k, values[k], op) for k in keys)


# ───────────────────────── planning ─────────────────────────

def plan_campaign(snap: dict, op: dict) -> tuple[list[dict], list[dict]]:
    """One campaign -> (items to write, skipped entries). Pure: nothing is written."""
    changes, currency = op["value"], op["currency"]
    head = {"campaign_id": snap["id"], "campaign_name": snap["name"]}
    items: list[dict] = []
    skipped: list[dict] = []

    def skip(level: str, oid: str, name: str, reason: str, field: str = "") -> None:
        skipped.append({"level": level, "id": oid, "name": name, "campaign_id": snap["id"],
                        "campaign_name": snap["name"], "reason": reason, **({"field": field} if field else {})})

    if snap["status"] in _ARCHIVED:
        skip("campaign", snap["id"], snap["name"], f"Campaign is {snap['status'].lower()}")
        return items, skipped
    if "budget" in changes:
        new = op["value_minor"]
        field = "daily_budget" if snap["daily_budget"] else "lifetime_budget" if snap["lifetime_budget"] else ""
        if not field:
            skip("campaign", snap["id"], snap["name"], "Budget is set on the ad sets, not on the campaign", "budget")
        else:
            before = snap[field]
            kind = "daily" if field == "daily_budget" else "lifetime"
            if before == new:
                skip("campaign", snap["id"], snap["name"], "Already at this budget", "budget")
            else:
                items.append({"level": "campaign", "id": snap["id"], "name": snap["name"], **head,
                              "field": field, "before": before, "after": new,
                              "before_display": f"{_c.fmt_minor(before, currency)} {kind}",
                              "after_display": f"{_c.fmt_minor(new, currency)} {kind}",
                              "constraints": [], "result": "", "saved": None, "note": ""})
    keys = [k for k in TARGETING_KEYS if k in changes]
    if not keys:
        return items, skipped
    if snap["more_adsets"]:
        skip("campaign", snap["id"], snap["name"],
             f"More than {MAX_ADSETS} ad sets; targeting not edited", "targeting")
        return items, skipped
    if not snap["adsets"]:
        skip("campaign", snap["id"], snap["name"], "No ad sets to edit", "targeting")
        return items, skipped
    for a in snap["adsets"]:
        if a["status"] in _ARCHIVED:
            skip("adset", a["id"], a["name"], f"Ad set is {a['status'].lower()}", "targeting")
            continue
        t = a["targeting"]
        if t is None:
            skip("adset", a["id"], a["name"], "Targeting could not be read", "targeting")
            continue
        advantage = (t.get("targeting_automation") or {}).get("advantage_audience")
        if advantage == 1 and ("age_min" in changes or "age_max" in changes or "gender" in changes):
            skip("adset", a["id"], a["name"],
                 "Advantage+ audience is on: age and gender are only suggestions there", "targeting")
            continue
        cur = current_view(t)
        diff = [k for k in keys if _differs(k, cur, op)]
        if not diff:
            skip("adset", a["id"], a["name"], "Already at the requested values", "targeting")
            continue
        new_t = patch_targeting(t, diff, op["want"])
        if new_t.get("age_min", 18) > new_t.get("age_max", 65):
            skip("adset", a["id"], a["name"], "The new age would put the minimum above the maximum", "targeting")
            continue
        try:
            _wp.check_object_update("POST", f"/{a['id']}", {"targeting": new_t})
        except _wp.WriteRefused as exc:
            skip("adset", a["id"], a["name"], f"Its current targeting cannot be re-sent safely ({exc})"[:200],
                 "targeting")
            continue
        before = {k: cur[k] for k in diff}
        after = {k: op["want"][k] for k in diff}
        items.append({"level": "adset", "id": a["id"], "name": a["name"], **head, "field": "targeting",
                      "keys": diff, "before": before, "after": after,
                      "before_display": _show_all(diff, before, op), "after_display": _show_all(diff, after, op),
                      "constraints": [], "result": "", "saved": None, "note": ""})
    return items, skipped


def calls_needed(items: list[dict]) -> int:
    """Graph calls of the execute step: account + re-read + one write per item + readback + margin."""
    campaigns = len({i["campaign_id"] for i in items})
    return 1 + campaigns + len(items) + campaigns + 2


# ───────────────────────── preview ─────────────────────────

def preview(store: OperationStore, open_session: _c.OpenSession, *, account: str, actor: str,
            changes: Any, campaigns: Any, resolver_factory: Callable[[Any], Any] | None = None) -> dict:
    account = str(account or "").strip()
    if not account:
        raise CommandRefused("Pick an account.")
    changes = parse_changes(changes)
    ids = parse_campaigns(campaigns)
    ceiling = _c.call_ceiling()
    langs = changes.get("languages") or []
    if 1 + len(ids) + len(langs) + 2 > ceiling:
        raise CommandRefused(f"Too many campaigns for the current call limit ({_c.MAX_CALLS_ENV}={ceiling}). "
                             "Select fewer campaigns.")
    with open_session(account, False) as sess:
        client = sess.client
        client.reset_budget(min(ceiling, 1 + len(ids) + len(langs) + 4))
        currency, _status = _c._check_account(client, sess.act)
        value_minor = _c.parse_money(changes["budget"], currency) if "budget" in changes else None
        locales: list[int] | None = None
        groups: dict = {}
        names: dict = {}
        if langs:
            from src.fb_api.creator import TargetingResolver

            resolver = (resolver_factory or TargetingResolver)(client)
            locales = []
            for lang in langs:
                try:
                    loc = resolver.locale(lang)
                except PlanError as exc:
                    raise CommandRefused(str(exc)) from None
                if loc is not None and loc not in locales:
                    locales.append(int(loc))
                    names[str(int(loc))] = str(getattr(resolver, "locale_names", {}).get(lang, lang))
            groups = {str(k): [int(m) for m in v] for k, v in (getattr(resolver, "locale_groups", {}) or {}).items()}
            for members in getattr(resolver, "locale_groups", {}).values():
                names.update({str(k): str(v) for k, v in members.items()})
        now = store.clock()
        op = {"op_id": "op_" + _c.secrets.token_hex(8), "state": _c.PREVIEW, "account": account,
              "act": sess.act, "currency": currency, "unit": 1 if currency in ZERO_DECIMAL else 100,
              "command": COMMAND, "value": changes, "value_minor": value_minor, "locales": locales,
              "locale_groups": groups, "locale_names": names, "want": build_want(changes, locales),
              "actor": _c.clean_actor(actor), "created_at": store.now_iso(now),
              "expires_at": store.now_iso(now + _c.PREVIEW_TTL_S), "_expires_ts": now + _c.PREVIEW_TTL_S,
              "message": "", "stale": False, "idempotency_key": ""}
        affected: list[dict] = []
        skipped: list[dict] = []
        for cid in ids:
            try:
                snap = read_campaign(client, sess.act, cid)
            except GraphError as exc:
                skipped.append({"level": "campaign", "id": cid, "name": cid, "campaign_id": cid,
                                "campaign_name": cid, "reason": f"Could not read this campaign (Graph error {exc.code})"})
                continue
            items, skips = plan_campaign(snap, op)
            affected += items
            skipped += skips
    if calls_needed(affected) > ceiling:
        raise CommandRefused(
            f"This change needs {calls_needed(affected)} Graph calls but the limit is {ceiling} "
            f"({_c.MAX_CALLS_ENV}). Select fewer campaigns.")
    op["affected"], op["skipped"] = affected, skipped
    op["revision_id"] = _c._revision_id(op)
    op["summary"] = _summary(op)
    store.add(op)
    with store._lock:
        return _c.snapshot(op)


def _summary(op: dict) -> str:
    n = len({i["campaign_id"] for i in op["affected"]})
    return f"{len(op['affected'])} change(s) on {n} campaign(s), {len(op['skipped'])} skipped"


# ───────────────────────── execution ─────────────────────────

def _find_item(snap: dict, item: dict) -> dict | None:
    if item["level"] == "campaign":
        return snap
    return next((a for a in snap["adsets"] if a["id"] == item["id"]), None)


def _saved_value(item: dict, obj: dict, op: dict) -> tuple[Any, bool]:
    """What the readback shows for this item, and whether it equals what was requested."""
    if item["field"] != "targeting":
        got = obj.get(item["field"])
        return got, got == item["after"]
    t = obj.get("targeting")
    if t is None:
        return None, False
    view = current_view(t)
    got = {k: view[k] for k in item["keys"]}
    ok = all((_locales_equal(view[k], item["after"][k], op["locale_groups"]) if k == "languages"
              else view[k] == item["after"][k]) for k in item["keys"])
    return got, ok


def _saved_text(item: dict, got: Any, op: dict) -> str:
    if item["field"] == "targeting":
        return _show_all(item["keys"], got, op) if isinstance(got, dict) else "unreadable"
    return _c.fmt_minor(got, op["currency"])


def run(store: OperationStore, op: dict, sess: Any, sleep: Callable[[float], None]) -> None:
    client, act = sess.client, sess.act
    if act != op["act"]:
        raise CommandRefused("The session is for a different ad account than the preview; refused.",
                             status=409, code="stale")
    if not getattr(client, "allow_writes", False):
        raise CommandRefused("This session cannot write.")
    client.reset_budget(min(_c.call_ceiling(), calls_needed(op["affected"])))
    _c._check_account(client, act, expect_currency=op["currency"])

    # 1) Re-read every campaign before the first write: any change since the preview -> write nothing.
    order = list(dict.fromkeys(i["campaign_id"] for i in op["affected"]))
    fresh: dict[str, dict] = {}
    for cid in order:
        fresh[cid] = read_campaign(client, act, cid)
        items_now, _ = plan_campaign(fresh[cid], op)
        now = {(i["id"], i["field"]): i for i in items_now}
        for item in (i for i in op["affected"] if i["campaign_id"] == cid):
            cur = now.get((item["id"], item["field"]))
            if cur is None or cur["before"] != item["before"] or cur["after"] != item["after"]:
                item["note"] = f"was {item['before_display']}; it changed after the preview"
                _c._fail_all(op, f"\"{item['name']}\" changed since the preview. "
                                 "Nothing was written; review the change again.", stale=True)
                store.try_save(op)
                return

    # 2) One write per item. One failed object does not stop the others.
    stop_rest = False
    history_blocked = False
    for item in op["affected"]:
        if stop_rest:
            item["result"] = "not_attempted"
            continue
        path = f"/{item['id']}"
        item["requested"] = item["after"]
        item["result"] = "sending"
        try:
            store.save(op)
        except HistoryUnavailable:
            item["result"] = "not_sent"
            item["note"] = "Not sent: " + _c.HISTORY_FAILED[0].lower() + _c.HISTORY_FAILED[1:]
            history_blocked = stop_rest = True
            continue
        try:
            if item["field"] == "targeting":
                target = _find_item(fresh[item["campaign_id"]], item)
                params = {"targeting": patch_targeting(target["targeting"], item["keys"], op["want"])}
            else:
                params = {item["field"]: str(item["after"])}
            client.post(path, params)
            item["result"] = "sent"
        except WriteUnknown:
            item["result"] = "unknown"
            item["note"] = "No answer from Meta; checking by readback"
            # The client blocks further writes until this one is reconciled: settle it now, or stop.
            if not _reconcile(client, act, item, op, sleep):
                stop_rest = True
        except GraphError as exc:
            item["result"], item["note"] = "refused", f"Meta refused: {exc}"[:240]
        except (GraphStop, WritesBlocked, ValueError) as exc:
            item["result"], item["note"] = "not_sent", f"Not sent: {exc}"[:240]
            stop_rest = True
        store.try_save(op)

    # 3) Read every written campaign back once and compare each item.
    op["state"], op["message"] = _c.VERIFYING, "Reading the values back from Meta"
    store.try_save(op)
    for cid in order:
        pending = [i for i in op["affected"] if i["campaign_id"] == cid and i["result"] in ("sent", "unknown")]
        if not pending:
            continue
        try:
            snap = read_campaign(client, act, cid)
        except (GraphStop, GraphError, CommandRefused) as exc:
            for item in pending:
                item["result"] = "unknown"
                item["note"] = f"Could not read it back: {exc}"[:240]
            continue
        for item in pending:
            _verify(item, snap, op, was_unknown=item["result"] == "unknown", client=client)
    _c._finalize(store, op, history_blocked=history_blocked)


def _verify(item: dict, snap: dict, op: dict, *, was_unknown: bool, client: Any = None) -> None:
    obj = _find_item(snap, item)
    if obj is None:
        item["result"], item["note"] = "unknown", "The object is no longer in the readback"
        return
    got, ok = _saved_value(item, obj, op)
    item["saved"] = got
    if ok:
        item["result"] = "saved"
        if was_unknown:
            item["note"] = "Confirmed by readback after a timeout"
            if client is not None:
                client.reconciled(f"/{item['id']}")
    elif was_unknown:
        item["result"] = "unknown"
        item["note"] = f"Readback shows {_saved_text(item, got, op)}; the write may yet apply. Not retried."
    else:
        item["result"] = "mismatch"
        item["note"] = f"Meta accepted the change but saved {_saved_text(item, got, op)}"


def _reconcile(client: Any, act: str, item: dict, op: dict, sleep: Callable[[float], None]) -> bool:
    """Settle an unknown write by reading the object back. True = confirmed, writing may go on."""
    sleep(_c.RECONCILE_PAUSE_S)
    try:
        snap = read_campaign(client, act, item["campaign_id"])
    except (GraphStop, GraphError, CommandRefused) as exc:
        item["note"] = f"Could not read it back: {exc}"[:240]
        return False
    _verify(item, snap, op, was_unknown=True, client=client)
    return item["result"] == "saved"


_c.RUNNERS[COMMAND] = run
