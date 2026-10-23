"""Rules T3 (ACL-1816): минимальный API только для чтения оценок.

* `GET /rules/evaluations?rule=…` - журнал оценок (последние записи);
* `POST /rules/evaluate-now` - ставит оценку в очередь и сразу отвечает.

Оба - только при `ACL_FB_BACKEND=api` и привязке сервера к loopback, как команды
над кампаниями. Ничего не пишут в Meta и не открывают профили.
"""
from __future__ import annotations

from typing import Any

from src.web import campaign_commands as _guards

DEFAULT_LIMIT = 50
MAX_LIMIT = 500


def _guard() -> tuple[dict, int] | None:
    if not _guards.api_enabled():
        return {"error": "Rules need the Graph API backend (ACL_FB_BACKEND=api).", "code": "rpa_backend"}, 409
    if not _guards.bound_to_loopback():
        return {"error": "Rules are only available when the dashboard listens on this computer alone (127.0.0.1).",
                "code": "not_loopback"}, 403
    return None


def list_evaluations(rule_id: str, limit: Any = None, *, store: Any = None) -> tuple[dict, int]:
    refused = _guard()
    if refused:
        return refused
    if store is None:
        from src.analysis_agent.rules.store import RulesStore
        store = RulesStore()
    try:
        n = max(1, min(MAX_LIMIT, int(limit))) if limit not in (None, "") else DEFAULT_LIMIT
    except (TypeError, ValueError):
        n = DEFAULT_LIMIT
    rows = [r for r in store.evaluations() if not rule_id or r.get("rule_id") == rule_id]
    return {"ok": True, "rule": rule_id, "evaluations": rows[-n:]}, 200


def evaluate_now(body: dict, *, engine: Any = None) -> tuple[dict, int]:
    refused = _guard()
    if refused:
        return refused
    rule_id = str((body or {}).get("rule_id") or "").strip()
    if engine is None:
        from src.web import rules_runtime
        engine = rules_runtime.get_engine()
    queued = engine.enqueue("manual", rule_id or None)
    return {"ok": True, "queued": queued, "rule_id": rule_id, "status": engine.status()}, 202


# ───────────────────────── Rules T4 (ACL-1822): страница правил ─────────────────────────
#
# Всё ниже - локальные чтения и записи в файлы правил (rules.json, журналы). В Meta
# ничего не пишется и профили не открываются: единственный путь записи в кампании -
# человеческие команды (`/campaign-commands/*`) по кнопке «Apply» на странице.

import copy
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from src.analysis_agent.rules import model as _model
from src.analysis_agent.rules import store as _store_mod

AUTOMATIC_REASON = "Automatic comes with the rules executor (ACL rules T5) — needs owner settings"
SETTABLE_MODES = ("disabled", "observe", "suggest")
#: Какие части версии редактор вправе менять. Остальное (strategy, priority) - только через шаблон.
EDITABLE_PARTS = ("name", "scope", "target", "evidence", "conditions", "action", "caps")
TEMPLATE_TEXT = {
    "stop_loss_new_tests": "Pauses a new ad set that has spent 3x the target CPA with no FTDs (alert at 2x).",
    "scale_proven": "Suggests +10% daily budget on a campaign with proven CPA over two mature blocks.",
}
_MODE_RANK = {"disabled": 0, "observe": 1, "suggest": 2, "automatic": 3}
_KIND_WORD = {"pause": "Pause", "budget_up": "Scale", "alert": "Alert", "block_scale": "Block scale",
              "draft_replacement": "Replacement draft", "enable": "Enable"}


def _store(store: Any) -> Any:
    return store if store is not None else _store_mod.RulesStore()


def _bad(message: str, code: str = "bad_request", status: int = 400, **extra: Any) -> tuple[dict, int]:
    return {"ok": False, "error": message, "code": code, **extra}, status


def _parse_time(value: Any) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def _age_hours(value: Any, now: datetime) -> float | None:
    stamp = _parse_time(value)
    return None if stamp is None else max(0.0, (now - stamp).total_seconds() / 3600.0)


def _scope_counts(version: dict) -> dict:
    objects = (version.get("scope") or {}).get("objects") or []
    out = {"campaign": 0, "adset": 0, "ad": 0}
    for o in objects:
        if o.get("level") in out:
            out[o["level"]] += 1
    out["accounts"] = len({o.get("account") for o in objects})
    out["total"] = len(objects)
    return out


def _latest_evaluations(store: Any) -> dict[str, dict]:
    last: dict[str, dict] = {}
    for rec in store.evaluations():
        if rec.get("rule_id"):
            last[rec["rule_id"]] = rec
    return last


def _open_proposals(store: Any) -> list[dict]:
    """Ждущие решения человека предложения: последняя запись по action_id - `proposed`."""
    return [r for r in store.latest_actions().values() if r.get("state") == "proposed"]


def _health(rule: dict, last: dict | None, now: datetime, bi_max_hours: float) -> dict:
    if rule.get("mode") == "disabled":
        return {"state": "disabled", "text": "Disabled"}
    if last is None:
        return {"state": "never", "text": "Not evaluated yet"}
    if last.get("error"):
        return {"state": "error", "text": "Evaluation failed: " + str(last["error"])}
    age = _age_hours(last.get("bi_built_at"), now)
    reasons = list(last.get("reasons") or [])
    if "Stale BI" in reasons or (age is not None and age > bi_max_hours):
        return {"state": "stale", "text": "Stale BI — the workbook is older than " + str(int(bi_max_hours)) + " h"}
    if last.get("verdict") == "blocked":
        return {"state": "blocked", "text": "Blocked: " + (", ".join(reasons[:2]) or "see the objects")}
    return {"state": "ok", "text": "Healthy"}


def _rule_view(store: Any, rule: dict, last: dict | None, proposals: list[dict], now: datetime,
               bi_max_hours: float, recent: list[dict]) -> dict:
    version = store.current_version(rule)
    body = {k: copy.deepcopy(v) for k, v in version.items() if k not in ("version", "created_at", "created_by")}
    mine = [p for p in proposals if p.get("rule_id") == rule["id"] and p.get("version") == version["version"]]
    return {
        "id": rule["id"], "name": version.get("name"), "mode": rule.get("mode"),
        "strategy": version.get("strategy"), "version": version["version"], "owner": rule.get("owner_cm"),
        "created_at": rule.get("created_at"), "body": body, "counts": _scope_counts(version),
        "automatic_blockers": _model.automatic_blockers(version),
        "last_eval": last, "health": _health(rule, last, now, bi_max_hours),
        "proposals": mine,
        "recent_actions": [a for a in recent if a.get("rule_id") == rule["id"]][-20:],
    }


def _sources(latest: dict[str, dict], now: datetime) -> dict:
    built = through = None
    for rec in latest.values():
        if rec.get("bi_built_at") and (built is None or str(rec["bi_built_at"]) > built):
            built, through = str(rec["bi_built_at"]), rec.get("bi_through")
    snaps: dict[str, str] = {}
    for rec in latest.values():
        for acct, at in (rec.get("snapshot_at") or {}).items():
            if at and (acct not in snaps or str(at) < snaps[acct]):
                snaps[acct] = str(at)
    oldest = min(snaps.values()) if snaps else None
    return {"bi_through": through, "bi_built_at": built, "snapshot_accounts": len(snaps),
            "snapshot_oldest_at": oldest, "snapshot_oldest_age_h": _age_hours(oldest, now) if oldest else None}


def overview(owner: str = "", *, store: Any = None, engine_status: Any = None,
             now: datetime | None = None) -> tuple[dict, int]:
    """Список правил менеджера с последней оценкой, здоровьем и ждущими предложениями."""
    refused = _guard()
    if refused:
        return refused
    store = _store(store)
    now = now or datetime.now(timezone.utc)
    owner = (owner or "").strip()
    settings = store.load_settings()
    bi_max = float((settings.get("freshness") or {}).get("bi_max_age_hours") or 36)
    latest = _latest_evaluations(store)
    proposals = _open_proposals(store)
    recent = [a for a in store.latest_actions().values() if a.get("state") in ("verified", "undone")]
    rules = [_rule_view(store, r, latest.get(r["id"]), proposals, now, bi_max, recent)
             for r in store.list_rules(owner or None)]
    status = engine_status() if callable(engine_status) else {}
    templates = []
    for pid in _model.PRESET_IDS:
        version, mode = _model.preset(pid)
        templates.append({"id": pid, "name": version["name"], "strategy": version["strategy"],
                          "action": version["action"]["kind"], "start_mode": mode,
                          "text": TEMPLATE_TEXT.get(pid, "")})
    kpis = {"observing": sum(1 for r in rules if r["mode"] == "observe"),
            "suggesting": sum(1 for r in rules if r["mode"] == "suggest"),
            "awaiting_review": sum(len(r["proposals"]) for r in rules),
            "blocked": sum(1 for r in rules if r["health"]["state"] in ("blocked", "stale", "error"))}
    return {"ok": True, "owner": owner, "rules": rules, "templates": templates, "kpis": kpis,
            "sources": _sources(latest, now),
            "engine": status, "automatic_reason": AUTOMATIC_REASON,
            "settings": {"automatic_enabled": bool(settings.get("automatic_enabled"))}}, 200


def _owned(store: Any, rule_id: str, owner: str) -> tuple[dict | None, tuple[dict, int] | None]:
    try:
        rule = store.get_rule(rule_id)
    except _store_mod.RuleNotFound:
        return None, _bad("This rule no longer exists.", "not_found", 404)
    if owner and rule.get("owner_cm") != owner:
        return None, _bad("This rule belongs to another buyer.", "not_owner", 403)
    return rule, None


def _check_mode(mode: Any) -> tuple[dict, int] | None:
    if mode == "automatic":
        return _bad(AUTOMATIC_REASON, "automatic_unavailable", 409)
    if mode not in SETTABLE_MODES:
        return _bad("Mode must be Disabled, Observe or Suggest.", "bad_mode")
    return None


def save_rule(body: dict, *, store: Any = None) -> tuple[dict, int]:
    """Создать правило из шаблона (`preset`) или сохранить правку (`rule_id`). Ничего не включает:
    новое правило Disabled; режим меняется только тем, что человек выбрал, и никогда не Automatic."""
    refused = _guard()
    if refused:
        return refused
    store = _store(store)
    body = body or {}
    owner = str(body.get("owner") or "").strip()
    if not owner:
        return _bad("Pick the buyer in the header first.", "no_owner")
    rule_id = str(body.get("rule_id") or "").strip()
    try:
        if not rule_id:
            preset_id = str(body.get("preset") or "")
            if preset_id not in _model.PRESET_IDS:
                return _bad("Pick a template.", "bad_preset")
            extra = {}
            name = str(body.get("name") or "").strip()
            if name:
                extra["name"] = name
            rule = store.create_from_preset(preset_id, owner, by=owner, **extra)
            rule = store.set_mode(rule["id"], "disabled", by=owner)
            return {"ok": True, "rule_id": rule["id"], "version": rule["current_version"], "created": True}, 201
        _rule, refusal = _owned(store, rule_id, owner)
        if refusal:
            return refusal
        mode = body.get("mode")
        if mode is not None:
            wrong = _check_mode(mode)
            if wrong:
                return wrong
        changes = {k: v for k, v in (body.get("changes") or {}).items() if k in EDITABLE_PARTS}
        for o in ((changes.get("scope") or {}).get("objects") or []):
            if not isinstance(o, dict) or not re.fullmatch(r"[0-9]+", str(o.get("id") or "")):
                return _bad("Object ids must be numeric Meta ids.", "bad_object_id", 422)
        expected = body.get("expected_version")
        rule = store.edit_rule(rule_id, changes, by=owner,
                               expected_version=int(expected) if expected not in (None, "") else None)
        if mode is not None and mode != rule.get("mode"):
            rule = store.set_mode(rule_id, mode, by=owner)
        return {"ok": True, "rule_id": rule_id, "version": rule["current_version"], "mode": rule["mode"]}, 200
    except _model.RuleValidationError as exc:
        return _bad("; ".join(exc.problems), "invalid", 422, problems=exc.problems)
    except _store_mod.VersionConflict:
        return _bad("The rule was changed elsewhere. Reload the page and edit again.", "version_conflict", 409)
    except _store_mod.RuleNotFound:
        return _bad("This rule no longer exists.", "not_found", 404)
    except (TypeError, ValueError) as exc:
        return _bad(f"Could not save: {exc}", "bad_request")


def set_rule_mode(body: dict, *, store: Any = None) -> tuple[dict, int]:
    refused = _guard()
    if refused:
        return refused
    store = _store(store)
    body = body or {}
    wrong = _check_mode(body.get("mode"))
    if wrong:
        return wrong
    owner = str(body.get("owner") or "").strip()
    if not owner:
        return _bad("Pick the buyer in the header first.", "no_owner")
    _rule, refusal = _owned(store, str(body.get("rule_id") or ""), owner)
    if refusal:
        return refusal
    rule = store.set_mode(str(body["rule_id"]), body["mode"], by=owner)
    return {"ok": True, "rule_id": rule["id"], "mode": rule["mode"]}, 200


def duplicate_rule(body: dict, *, store: Any = None) -> tuple[dict, int]:
    refused = _guard()
    if refused:
        return refused
    store = _store(store)
    body = body or {}
    owner = str(body.get("owner") or "").strip()
    if not owner:
        return _bad("Pick the buyer in the header first.", "no_owner")
    _rule, refusal = _owned(store, str(body.get("rule_id") or ""), owner)
    if refusal:
        return refusal
    copy_ = store.duplicate(str(body["rule_id"]), by=owner)
    return {"ok": True, "rule_id": copy_["id"], "mode": copy_["mode"]}, 201


def _proposal(store: Any, action_id: str, owner: str) -> tuple[dict | None, tuple[dict, int] | None]:
    if not owner:
        return None, _bad("Pick the buyer in the header first.", "no_owner")
    rec = store.latest_actions().get(action_id)
    if rec is None or not rec.get("rule_id"):
        return None, _bad("This suggestion no longer exists.", "not_found", 404)
    _rule, refusal = _owned(store, rec["rule_id"], owner)
    if refusal:
        return None, refusal
    if rec.get("state") != "proposed":
        return None, _bad("This suggestion is already " + str(rec.get("state")) + ".", "not_pending", 409)
    return rec, None


def dismiss_proposal(body: dict, *, store: Any = None) -> tuple[dict, int]:
    """«Dismiss»: только пометка в журнале правил. Ни команд, ни Meta."""
    refused = _guard()
    if refused:
        return refused
    store = _store(store)
    body = body or {}
    owner = str(body.get("owner") or "").strip()
    rec, refusal = _proposal(store, str(body.get("action_id") or ""), owner)
    if refusal:
        return refusal
    store.append_action({**rec, "state": "dismissed", "actor": "human:" + owner, "reason": "dismissed by " + owner})
    return {"ok": True, "action_id": rec["action_id"], "state": "dismissed"}, 200


def _op_mismatch(rec: dict, op: dict, state: str) -> str:
    """Пусто, если операция - именно та, что предложена: счёт, вид команды, значение и объект совпадают,
    а при успехе запись по объекту `saved`, а не пропуск. Иначе - что не сошлось."""
    req = (rec.get("requested") or {})
    if rec.get("kind") == "pause" and req.get("status") == "PAUSED":
        command, want = "set_status", "PAUSED"
    elif rec.get("kind") == "budget_up" and req.get("daily_budget"):
        command, want = "set_budget", req["daily_budget"]
    else:
        return "the suggestion has nothing to apply"
    return _command_mismatch(rec, op, state, command, want)


def _command_mismatch(rec: dict, op: dict, state: str, command: str, want: Any) -> str:
    """Bind either human command to the exact account, value and single saved object."""
    if op.get("account") != rec.get("account"):
        return "another account"
    if op.get("command") != command:
        return "another kind of command"
    if command == "set_status":
        if str(op.get("value")) != want:
            return "another status"
    else:
        try:
            if Decimal(str(op.get("value"))) != Decimal(str(want)):
                return "another budget value"
        except (TypeError, ValueError, InvalidOperation):
            return "another budget value"
    ours = [a for a in op.get("affected") or []
            if str(a.get("id")) == str(rec.get("id")) and a.get("level") == rec.get("level")]
    if len(ours) != 1 or len(op.get("affected") or []) != 1:
        return "another object"
    if state == "verified" and ours[0].get("result") != "saved":
        return "the object was not saved"
    return ""


def _op_used(store: Any, op_id: str) -> bool:
    return any(op_id in (r.get("op_id"), r.get("undo_op_id")) for r in store.actions())


def record_action_result(body: dict, *, store: Any = None, status: Any = None) -> tuple[dict, int]:
    """После человеческой команды: переход предложения берётся из ОТВЕТА сервера команд (`get_status`),
    а не из слов страницы. Сама запись в Meta уже сделана командой с подтверждением."""
    refused = _guard()
    if refused:
        return refused
    store = _store(store)
    body = body or {}
    owner = str(body.get("owner") or "").strip()
    rec, refusal = _proposal(store, str(body.get("action_id") or ""), owner)
    if refusal:
        return refusal
    op_id = str(body.get("op_id") or "").strip()
    if not op_id:
        return _bad("op_id is required", "no_op")
    if status is None:
        from src.web import campaign_commands
        status = campaign_commands.get_status
    op, code = status(op_id)
    if code != 200:
        return _bad("The command is unknown to this dashboard.", "unknown_op", 404)
    state = {"Succeeded": "verified", "Failed": "failed", "Outcome unknown": "unknown"}.get(op.get("state"))
    if state is None:
        return _bad("The command is still running.", "running", 409)
    mismatch = _op_mismatch(rec, op, state)
    if mismatch:
        return _bad("This command is not the one that was suggested: " + mismatch, "op_mismatch", 409)
    if _op_used(store, op_id):
        return _bad("This command was already used for a suggestion.", "op_reused", 409)
    store.append_action({**rec, "state": state, "op_id": op_id, "actor": "human:" + owner,
                         "reason": "applied by " + owner + " through the confirm flow"})
    return {"ok": True, "action_id": rec["action_id"], "state": state}, 200


def _undo_source(body: dict, store: Any) -> tuple[dict | None, dict | None, tuple[dict, int] | None]:
    """Read the original confirmed operation; an evaluator's older 'before' is insufficient."""
    owner = str(body.get("owner") or "").strip()
    if not owner:
        return None, None, _bad("Pick the buyer in the header first.", "no_owner")
    rec = store.latest_actions().get(str(body.get("action_id") or ""))
    if not rec or not rec.get("rule_id"):
        return None, None, _bad("This suggestion no longer exists.", "not_found", 404)
    _rule, refusal = _owned(store, rec["rule_id"], owner)
    if refusal:
        return None, None, refusal
    if rec.get("state") != "verified" or rec.get("undo_state") == "unknown":
        return None, None, _bad("Only an applied, verified action with no uncertain undo can be undone.", "not_undoable", 409)
    op, code = _guards.get_status(str(rec.get("op_id") or ""))
    if code != 200 or op.get("state") != "Succeeded" or _op_mismatch(rec, op, "verified"):
        return None, None, _bad("The original confirmed command is unavailable or does not match this action.", "not_undoable", 409)
    item = op["affected"][0]
    before, after = item.get("before"), item.get("after")
    command = op["command"]
    if command == "set_status" and item.get("field") == "status" and before in ("ACTIVE", "PAUSED"):
        value = before
        valid = after == op.get("value") and item.get("saved") == after
    elif command == "set_budget" and item.get("field") == "daily_budget":
        unit = op.get("unit")
        valid = type(before) is int and before > 0 and type(unit) is int and unit in (1, 100)
        try:
            valid = valid and Decimal(str(after)) == Decimal(str(op.get("value"))) * unit and item.get("saved") == after
        except (TypeError, InvalidOperation):
            valid = False
        value = format(Decimal(before) / unit, "f") if valid else ""
    else:
        valid, value = False, ""
    if not valid:
        return None, None, _bad("The confirmed command has no verified previous value to restore.", "not_undoable", 409)
    request = {"account": rec["account"], "command": command, "value": value,
               "targets": [{"level": rec["level"], "id": str(rec["id"])}]}
    return rec, request, None


def prepare_undo(body: dict, *, store: Any = None) -> tuple[dict, int]:
    """Prepare a request for the existing human preview/confirm flow. No session or write."""
    refused = _guard()
    if refused:
        return refused
    rec, request, refusal = _undo_source(body or {}, _store(store))
    if refusal:
        return refusal
    return {"ok": True, "action_id": rec["action_id"], "request": request}, 200


def record_undo_result(body: dict, *, store: Any = None) -> tuple[dict, int]:
    """Record only a matching new human command's server result; never perform the undo."""
    refused = _guard()
    if refused:
        return refused
    body, store = body or {}, _store(store)
    rec, request, refusal = _undo_source(body, store)
    if refusal:
        return refusal
    op_id = str(body.get("op_id") or "").strip()
    if not op_id:
        return _bad("op_id is required", "no_op")
    if _op_used(store, op_id):
        return _bad("This command was already used for a suggestion.", "op_reused", 409)
    op, code = _guards.get_status(op_id)
    if code != 200:
        return _bad("The command is unknown to this dashboard.", "unknown_op", 404)
    state = {"Succeeded": "verified", "Failed": "failed", "Outcome unknown": "unknown"}.get(op.get("state"))
    if state is None:
        return _bad("The undo command is still running.", "running", 409)
    mismatch = _command_mismatch(rec, op, state, request["command"], request["value"])
    if mismatch:
        return _bad("This command is not the requested undo: " + mismatch, "op_mismatch", 409)
    store.append_action({**rec, "state": "undone" if state == "verified" else "verified",
                         "undo_state": state, "undo_op_id": op_id, "actor": "human:" + str(body["owner"]).strip(),
                         "reason": "undo through the human confirm flow"})
    return {"ok": True, "action_id": rec["action_id"], "undo_state": state}, 200


def scope_options(account: str, *, snapshot: Any = None) -> tuple[dict, int]:
    """Объекты последнего снимка аккаунта для диалога «Choose campaigns»: только с id."""
    refused = _guard()
    if refused:
        return refused
    account = (account or "").strip()
    if not account:
        return _bad("account is required")
    from src.analysis_agent import campaign_inventory as ci

    snap = snapshot if snapshot is not None else ci.get_snapshot(account)
    if snap is None or (snap.state in ("idle", "failed") and not snap.campaigns):
        return {"ok": True, "account": account, "has_snapshot": False,
                "note": "Read campaigns first — this account has no saved snapshot.", "campaigns": []}, 200
    tree = ci.build_tree(snap)
    campaigns = []
    for c in snap.campaigns:
        if not str(c.get("id") or "").strip():
            continue
        sets = [{"id": a["id"], "name": a.get("name") or "", "status": a.get("effective_status") or a.get("status")}
                for a in tree["adsets_of"].get(c["id"], []) if str(a.get("id") or "").strip()]
        campaigns.append({"id": c["id"], "name": c.get("name") or "",
                          "status": c.get("effective_status") or c.get("status"), "adsets": sets})
    return {"ok": True, "account": account, "has_snapshot": True, "state": snap.state,
            "read_at": snap.finished_at_utc or snap.finished_at, "currency": snap.currency,
            "problems": list(snap.problems or []), "campaigns": campaigns}, 200


def _cell_result(rec: dict | None, obj: dict | None, proposal: dict | None, hold: dict | None) -> str:
    if hold:
        return "Held · manual"
    if proposal:
        return _KIND_WORD.get(proposal.get("kind"), "Action") + " suggested"
    if rec is None or obj is None:
        return "Not evaluated yet"
    verdict = obj.get("verdict")
    if verdict == "matched":
        word = _KIND_WORD.get((obj.get("action") or {}).get("kind"), "Match")
        return word + (" · would act" if rec.get("mode") == "observe" else " · matched")
    if verdict == "blocked":
        return "Blocked · " + ((obj.get("reasons") or ["see rule"])[0])
    return "No match"


def cells(account: str, owner: str = "", *, store: Any = None) -> tuple[dict, int]:
    """Ячейки колонки Rules: по ключу объекта (`level:id`) - число правил, режим, итог. Без Meta."""
    refused = _guard()
    if refused:
        return refused
    store = _store(store)
    account = (account or "").strip()
    if not (owner or "").strip():
        return _bad("Pick the buyer in the header first.", "no_owner")
    latest = _latest_evaluations(store)
    proposals = _open_proposals(store)
    holds = store.active_holds()
    applied = [a for a in store.latest_actions().values() if a.get("state") in ("verified", "undone")]
    out: dict[str, dict] = {}
    for rule in store.list_rules(owner.strip() or None):
        version = store.current_version(rule)
        rec = latest.get(rule["id"])
        if rec is not None and rec.get("version") != version["version"]:
            rec = None  # итог чужой версии правила не показываем как свежий
        by_key = {o.get("key"): o for o in (rec or {}).get("objects") or []}
        for o in (version.get("scope") or {}).get("objects") or []:
            if account and o.get("account") != account:
                continue
            key = f"{o['level']}:{o['id']}"
            obj = by_key.get(key)
            prop = next((p for p in proposals if p.get("rule_id") == rule["id"] and p.get("id") == o["id"]), None)
            hold = holds.get(o["id"]) or holds.get(key)
            entry = out.setdefault(key, {"count": 0, "mode": "disabled", "rules": [], "result": "", "tone": ""})
            entry["count"] += 1
            if _MODE_RANK.get(rule["mode"], 0) > _MODE_RANK.get(entry["mode"], 0):
                entry["mode"] = rule["mode"]
            entry["rules"].append({
                "rule_id": rule["id"], "name": version.get("name"), "mode": rule["mode"], "version": version["version"],
                "result": _cell_result(rec, obj, prop, hold), "evaluated_at": (rec or {}).get("at"),
                "verdict": (obj or {}).get("verdict"), "reasons": list((obj or {}).get("reasons") or []),
                "notes": list((obj or {}).get("notes") or []), "identity": (obj or {}).get("identity"),
                "observed": (obj or {}).get("observed") or {}, "proposal": prop, "held": bool(hold),
                "applied": next((a for a in reversed(applied) if a.get("rule_id") == rule["id"]
                                 and a.get("account") == o.get("account") and a.get("level") == o.get("level")
                                 and a.get("id") == o.get("id")), None),
                "assigned_name": o.get("name_at_assign") or o.get("name") or ""})
    for entry in out.values():
        # итог ячейки: самый «громкий» - ждущее предложение, затем совпадение, затем остальное
        order = sorted(entry["rules"], key=lambda r: (0 if r["proposal"] else 1 if r["verdict"] == "matched" else 2))
        entry["result"] = order[0]["result"]
        entry["tone"] = "act" if order[0]["proposal"] else "warn" if order[0]["verdict"] in ("matched", "blocked") else ""
    return {"ok": True, "account": account, "cells": out}, 200


def activity(owner: str, limit: Any = None, *, store: Any = None) -> tuple[dict, int]:
    """Вкладка Activity: оценки и действия правил этого менеджера, только чтение журналов."""
    refused = _guard()
    if refused:
        return refused
    store = _store(store)
    owner = (owner or "").strip()
    if not owner:
        return _bad("Pick the buyer in the header first.", "no_owner")
    try:
        n = max(1, min(MAX_LIMIT, int(limit))) if limit not in (None, "") else 100
    except (TypeError, ValueError):
        n = 100
    names = {}
    for rule in store.list_rules(owner):
        names[rule["id"]] = store.current_version(rule).get("name")
    rows: list[dict] = []
    for rec in store.evaluations():
        if rec.get("rule_id") in names:
            rows.append({"at": rec.get("at"), "kind": "evaluation", "rule_id": rec["rule_id"], "rule": names[rec["rule_id"]],
                         "text": f"Evaluation ({rec.get('trigger')}): {rec.get('verdict')}"
                                 + (f" · {', '.join(rec.get('reasons') or [])}" if rec.get("reasons") else ""),
                         "state": rec.get("verdict")})
    for rec in store.latest_actions().values():
        if rec.get("rule_id") in names and rec.get("kind"):
            rows.append({"at": rec.get("at"), "kind": "action", "rule_id": rec["rule_id"], "rule": names[rec["rule_id"]],
                         "text": f"{_KIND_WORD.get(rec.get('kind'), rec.get('kind'))} · {rec.get('level')} {rec.get('id')}",
                         "state": rec.get("state"), "actor": rec.get("actor"), "op_id": rec.get("op_id")})
    rows.sort(key=lambda r: str(r.get("at") or ""), reverse=True)
    return {"ok": True, "rows": rows[:n]}, 200
