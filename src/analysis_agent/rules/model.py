"""Модель правила Rules: схема, валидация, пресеты S1/S3, разрешение цели T.

Дизайн: C:\\acl-notes\\rules-design.md, разделы 1-2 и 5.8. Модуль чистый: не
читает диск и не ходит в сеть. Цели My Goals передаются снаружи списком
(формат `src.web.goals.for_cm`), поэтому тесты не зависят от машины.

Версия правила - неизменяемый словарь. Правка создаёт новую версию (store.py).
Режим и грант живут на самом правиле, а не в версии.
"""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

# --- словари -----------------------------------------------------------------

MODES = ("disabled", "observe", "suggest", "automatic")
STRATEGIES = (1, 2, 3, 4, 5)
#: S5 (pacing & bid) в v1 выключена: нужны ставки и почасовой spend.
UNSUPPORTED_STRATEGIES = (5,)
LEVELS = ("campaign", "adset", "ad")
ACTION_KINDS = ("pause", "enable", "budget_up", "alert", "block_scale", "draft_replacement")
#: Действия, которые пишут в Meta. Остальные - только внутри дешборда.
WRITING_ACTIONS = ("pause", "enable", "budget_up")
EVIDENCE_BASES = ("report_period", "cohort", "lifetime")
METRICS = ("spend", "ftd", "leads", "cpa", "cr", "ftd_total", "ftd_block", "cpa_block",
           "cpa_change_pct", "cr_change_pct", "age_days")
#: t_multiple - доля цели T, abs - число или деньги, pct - проценты.
UNITS = ("t_multiple", "abs", "pct")
CMPS = (">=", ">", "<=", "<", "==")
TARGET_SOURCES = ("goal_asset", "goal", "override")
#: Части, правка которых аннулирует грант Automatic (дизайн 5.8).
#: Консервативно: цель T, доказательства и стратегия тоже входят - диалог гранта
#: показывает T, поэтому его смена должна спросить владельца заново.
GRANT_PARTS = ("strategy", "scope", "target", "evidence", "conditions", "action", "caps")
NO_TARGET = "No target"

DEFAULT_CAPS = {
    "per_account_day": 3,
    "per_machine_day": 10,
    "per_rule_day": 5,
    "max_daily_budget": None,        # потолок на объект, в валюте аккаунта
    "max_total_daily_budget": None,  # потолок на country/asset, в валюте аккаунта
    "cooldown_hours": 24,
}


class RuleValidationError(ValueError):
    """Правило не проходит проверку; в .problems - список понятных причин."""

    def __init__(self, problems: list[str]):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


# --- хеши частей (для гранта) ------------------------------------------------

def part_hash(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def grant_hashes(version: dict) -> dict:
    return {f"{part}_hash": part_hash(version.get(part)) for part in GRANT_PARTS}


# --- заготовки ---------------------------------------------------------------

def empty_scope() -> dict:
    return {"accounts": [], "objects": [], "country": "", "asset": "", "include_new_objects": False}


def empty_target() -> dict:
    """По умолчанию цель берётся из My Goals по стране; asset - только если он там есть."""
    return {"use_goal": True, "override": None}


def make_version(**fields: Any) -> dict:
    """Версия правила с умолчаниями (номер и время ставит store)."""
    base = {
        "name": "",
        "strategy": 1,
        "scope": empty_scope(),
        "target": empty_target(),
        "evidence": {
            "outcome": "ftd", "base": "report_period", "window_days": 21,
            "maturity_days": 2, "min_age_days": 3, "min_sample": 0,
        },
        "conditions": [],
        "action": {"kind": "alert"},
        "caps": copy.deepcopy(DEFAULT_CAPS),
        "priority": 50,
    }
    for key, val in fields.items():
        if isinstance(val, dict) and isinstance(base.get(key), dict):
            base[key] = {**base[key], **copy.deepcopy(val)}
        else:
            base[key] = copy.deepcopy(val)
    return base


# --- валидация ---------------------------------------------------------------

def _is_num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _is_int(x: Any) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def validate_version(v: dict) -> list[str]:
    """Список проблем версии; пустой - версия годна. Исключений не бросает."""
    p: list[str] = []
    if not isinstance(v, dict):
        return ["version must be an object"]
    if not str(v.get("name") or "").strip():
        p.append("name is required")
    if v.get("strategy") not in STRATEGIES:
        p.append(f"strategy must be one of {STRATEGIES}")
    if not _is_int(v.get("priority")) or not 0 <= v["priority"] <= 100:
        p.append("priority must be an integer 0..100")

    scope = v.get("scope")
    if not isinstance(scope, dict):
        p.append("scope must be an object")
    else:
        accounts = scope.get("accounts")
        if not isinstance(accounts, list) or not all(isinstance(a, str) and a.strip() for a in accounts):
            p.append("scope.accounts must be a list of account ids")
        objects = scope.get("objects")
        if not isinstance(objects, list):
            p.append("scope.objects must be a list")
        else:
            for i, o in enumerate(objects):
                if not isinstance(o, dict) or not str(o.get("id") or "").strip() \
                        or not str(o.get("account") or "").strip():
                    p.append(f"scope.objects[{i}] needs account and id")
                elif o.get("level") not in LEVELS:
                    p.append(f"scope.objects[{i}].level must be one of {LEVELS}")
                elif isinstance(accounts, list) and o["account"] not in accounts:
                    p.append(f"scope.objects[{i}] account is not in scope.accounts")
        if scope.get("include_new_objects") not in (False, None):
            # дизайн 5.8: новые объекты входят только как Suggest-only по отдельному opt-in
            p.append("scope.include_new_objects is not supported yet")

    target = v.get("target")
    if not isinstance(target, dict):
        p.append("target must be an object")
    else:
        ov = target.get("override")
        if ov is not None:
            if not isinstance(ov, dict) or not _is_num(ov.get("value")) or ov["value"] <= 0:
                p.append("target.override.value must be a positive number")
            elif str(ov.get("currency") or "USD") != "USD":
                p.append("target.override.currency must be USD")
        elif not target.get("use_goal", True):
            p.append("target needs either use_goal or an override")

    ev = v.get("evidence")
    if not isinstance(ev, dict):
        p.append("evidence must be an object")
    else:
        if ev.get("outcome") != "ftd":
            p.append("evidence.outcome must be ftd")
        if ev.get("base") not in EVIDENCE_BASES:
            p.append(f"evidence.base must be one of {EVIDENCE_BASES}")
        for key, lo in (("window_days", 1), ("maturity_days", 0), ("min_age_days", 0), ("min_sample", 0)):
            val = ev.get(key)
            if not _is_int(val) or val < lo:
                p.append(f"evidence.{key} must be an integer >= {lo}")

    conds = v.get("conditions")
    if not isinstance(conds, list):
        p.append("conditions must be a list")
    else:
        for i, c in enumerate(conds):
            if isinstance(c, dict) and "items" in c:
                if c.get("op") not in ("AND", "OR") or not isinstance(c["items"], list) or not c["items"]:
                    p.append(f"conditions[{i}] group needs op AND/OR and items")
                    continue
                items = c["items"]
            else:
                items = [c]
            for j, it in enumerate(items):
                if not isinstance(it, dict) or it.get("metric") not in METRICS:
                    p.append(f"conditions[{i}][{j}].metric must be a known metric")
                elif it.get("cmp") not in CMPS:
                    p.append(f"conditions[{i}][{j}].cmp must be one of {CMPS}")
                elif not _is_num(it.get("value")):
                    p.append(f"conditions[{i}][{j}].value must be a number")
                elif it.get("unit") not in UNITS:
                    p.append(f"conditions[{i}][{j}].unit must be one of {UNITS}")

    act = v.get("action")
    if not isinstance(act, dict) or act.get("kind") not in ACTION_KINDS:
        p.append(f"action.kind must be one of {ACTION_KINDS}")
    elif act["kind"] == "budget_up":
        step = act.get("step_pct")
        if not _is_num(step) or not 0 < step <= 10:
            # дизайн 5.3: шаг не больше +10% за раз
            p.append("action.step_pct must be in (0, 10]")

    caps = v.get("caps")
    if not isinstance(caps, dict):
        p.append("caps must be an object")
    else:
        for key in ("per_account_day", "per_machine_day", "per_rule_day"):
            if not _is_int(caps.get(key)) or caps[key] < 0:
                p.append(f"caps.{key} must be an integer >= 0")
        for key in ("max_daily_budget", "max_total_daily_budget"):
            val = caps.get(key)
            if val is not None and (not _is_num(val) or val <= 0):
                p.append(f"caps.{key} must be a positive number or null")
        cd = caps.get("cooldown_hours")
        if not _is_num(cd) or cd < 0:
            p.append("caps.cooldown_hours must be >= 0")
    return p


def check_version(v: dict) -> dict:
    problems = validate_version(v)
    if problems:
        raise RuleValidationError(problems)
    return v


def automatic_blockers(version: dict) -> list[str]:
    """Что мешает выдать грант Automatic этой версии (kill switch'и не считаем)."""
    out = validate_version(version)
    if version.get("strategy") in UNSUPPORTED_STRATEGIES:
        out.append("strategy is not supported")
    act = (version.get("action") or {}).get("kind")
    if act not in WRITING_ACTIONS:
        out.append("action does not write to Meta, Automatic makes no sense")
    scope = version.get("scope") or {}
    if not scope.get("objects"):
        out.append("Automatic needs an explicit list of objects")
    caps = version.get("caps") or {}
    if act == "budget_up" and not caps.get("max_daily_budget"):
        out.append("budget_up in Automatic needs caps.max_daily_budget")
    if act == "enable":
        out.append("Automatic enable is not allowed (Suggest only)")
    return out


# --- пресеты (дизайн 2.3) ----------------------------------------------------

PRESET_IDS = ("stop_loss_new_tests", "scale_proven")


def preset(preset_id: str, **overrides: Any) -> tuple[dict, str]:
    """(версия, стартовый режим). Все числа - редактируемые умолчания, не истина."""
    if preset_id == "stop_loss_new_tests":
        v = make_version(
            name="Stop-loss · new tests", strategy=1, priority=10,
            evidence={"base": "report_period", "window_days": 21, "maturity_days": 2,
                      "min_age_days": 3, "min_sample": 0},
            # alert при spend >= 2T и 0 FTD; pause при spend >= 3T и 0 FTD
            conditions=[
                {"op": "AND", "items": [
                    {"metric": "spend", "cmp": ">=", "value": 3, "unit": "t_multiple"},
                    {"metric": "ftd", "cmp": "==", "value": 0, "unit": "abs"}]}],
            action={"kind": "pause", "level": "adset", "alert_at_t_multiple": 2},
            caps={"per_account_day": 3, "cooldown_hours": 24},
        )
        mode = "observe"
    elif preset_id == "scale_proven":
        v = make_version(
            name="Scale proven", strategy=3, priority=70,
            evidence={"base": "report_period", "window_days": 6, "maturity_days": 2,
                      "min_age_days": 6, "min_sample": 10},
            conditions=[
                {"op": "AND", "items": [
                    {"metric": "ftd_total", "cmp": ">=", "value": 10, "unit": "abs"},
                    {"metric": "ftd_block", "cmp": ">=", "value": 3, "unit": "abs"},
                    {"metric": "cpa_block", "cmp": "<=", "value": 0.8, "unit": "t_multiple"}]}],
            action={"kind": "budget_up", "step_pct": 10, "level": "campaign",
                    "blocks": 2, "block_days": 3},
            caps={"cooldown_hours": 24, "per_rule_day": 1},
        )
        mode = "suggest"
    else:
        raise KeyError(f"unknown preset {preset_id!r}")
    for key, val in overrides.items():
        if isinstance(val, dict) and isinstance(v.get(key), dict):
            v[key] = {**v[key], **val}
        else:
            v[key] = val
    return v, mode


# --- цель T (дизайн 2.4) -----------------------------------------------------

def _positive(x: Any) -> float | None:
    return float(x) if _is_num(x) and x > 0 else None


def resolve_target(version: dict, goals: list[dict] | None, country: str = "",
                   asset: str = "") -> dict:
    """Цель T для правила: override, затем goal_asset, затем goal, иначе «No target».

    `goals` - список целей менеджера (`goals.for_cm`). Необязательное расширение
    записи цели: `assets: {"<Ad Asset>": {"cpa": ..}}`; в My Goals его пока нет,
    поэтому без него работает страна. Источник пишется в оценку (цели могут
    поменять задним числом).
    Возвращает {value, source, currency, reason}; value=None, если цели нет.
    """
    target = (version or {}).get("target") or {}
    ov = target.get("override")
    if isinstance(ov, dict) and _positive(ov.get("value")):
        return {"value": float(ov["value"]), "source": "override",
                "currency": str(ov.get("currency") or "USD"), "reason": ""}
    scope = (version or {}).get("scope") or {}
    country = (country or scope.get("country") or "").strip()
    asset = (asset or scope.get("asset") or "").strip()
    if target.get("use_goal", True) and country:
        for g in goals or []:
            if not isinstance(g, dict):
                continue
            names = {str(g.get("country") or "").casefold(), str(g.get("code") or "").casefold()} - {""}
            if country.casefold() not in names:
                continue
            assets = g.get("assets")
            if asset and isinstance(assets, dict):
                a = assets.get(asset)
                val = _positive(a.get("cpa")) if isinstance(a, dict) else None
                if val:
                    return {"value": val, "source": "goal_asset", "currency": "USD", "reason": ""}
            val = _positive(g.get("cpa"))
            if val:
                return {"value": val, "source": "goal", "currency": "USD", "reason": ""}
            break
    why = f"{NO_TARGET} for {country}" if country else NO_TARGET
    return {"value": None, "source": None, "currency": "USD", "reason": why}
