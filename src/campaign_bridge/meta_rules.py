"""Реестр правил Meta и проверка кампании до загрузки (ACL-1781).

Правила лежат в `config/meta_rules/` (JSON, в git): `meta.json` — версии API,
`rules.json` — список правил с источником и датой проверки. Формат фиксирован,
чтобы наблюдатель за документацией Meta (отдельная задача) мог писать в него
параллельно. Живых обращений к Meta нет — только то, что записано в реестре.

Правило разрешать значение может только со статусом `ga`; значения из
`limited/beta/test/announced/deprecated` отклоняются с пометкой «not generally
available». Чего реестр не знает (нет подходящего правила) — не проверяется:
выдумывать ограничения нельзя.

Длины текстов Meta лишь рекомендует (не жёсткий лимит), поэтому правил
`text_limit` в реестре нет: блокировать по рекомендации нельзя. Вид правила
оставлен для жёстких лимитов, если Meta когда-нибудь их объявит.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

RULES_DIR = Path(__file__).resolve().parents[2] / "config" / "meta_rules"

STATUSES = ("ga", "limited", "beta", "test", "announced", "deprecated")

#: kind -> поле задания, значение которого сверяется со списком `allowed`.
ALLOWED_KINDS: dict[str, str] = {
    "objective": "objective",
    "optimization_goal_by_objective": "optimization_goal",
    "optimization_goal_enum": "optimization_goal",
    "billing_event_by_goal": "billing_event",
    "billing_event_enum": "billing_event",
    "bid_strategy_enum": "bid_strategy",
    "destination_type_enum": "destination_type",
    "cta_by_objective": "call_to_action",
    "cta_enum": "call_to_action",
    "placements_by_objective": "placements",
}
#: `fact` — справочная запись без проверки: факт о Meta, который билдер пока не
#: умеет проверить (лимиты, снятие с поддержки, живые наблюдения). `check_job`
#: её не читает; обязательна только пометка `note` с самим фактом.
KINDS = tuple(ALLOWED_KINDS) + ("text_limit", "required_field", "fact")

#: Как поле называется для человека в сообщении.
FIELD_LABELS = {
    "objective": "Campaign objective",
    "optimization_goal": "Optimization goal",
    "billing_event": "Billing event",
    "bid_strategy": "Bid strategy",
    "destination_type": "Destination type",
    "call_to_action": "Call to action",
    "placements": "Placements",
    "ad_title": "Ad title",
    "ad_body": "Ad body",
    "ad_link_description": "Link description",
}

_META_KEYS = ("api_version_current", "api_versions", "checked_date", "sources")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class MetaRulesError(ValueError):
    """Реестр не соответствует схеме."""


@dataclass(frozen=True)
class Violation:
    rule_id: str
    field: str
    value: Any
    message_en: str
    source_url: str


@dataclass(frozen=True)
class RuleSet:
    meta: dict[str, Any]
    rules: tuple[dict[str, Any], ...]


def _check_date(value: Any, where: str) -> None:
    if not isinstance(value, str) or not _ISO_DATE.match(value):
        raise MetaRulesError(f"{where}: date must be YYYY-MM-DD, got {value!r}")
    try:
        date.fromisoformat(value)
    except ValueError as e:
        raise MetaRulesError(f"{where}: {e}") from None


def validate_meta(meta: Any) -> None:
    if not isinstance(meta, dict):
        raise MetaRulesError("meta.json must be an object")
    for key in _META_KEYS:
        if key not in meta:
            raise MetaRulesError(f"meta.json: missing '{key}'")
    versions = meta["api_versions"]
    if not isinstance(versions, list) or not versions:
        raise MetaRulesError("meta.json: api_versions must be a non-empty list")
    names = []
    for v in versions:
        if not isinstance(v, dict) or not {"version", "released", "end_of_support"} <= set(v):
            raise MetaRulesError("meta.json: each api_versions item needs version, released, end_of_support")
        _check_date(v["released"], f"meta.json {v['version']}.released")
        if v["end_of_support"] is not None:
            _check_date(v["end_of_support"], f"meta.json {v['version']}.end_of_support")
        names.append(v["version"])
    if meta["api_version_current"] not in names:
        raise MetaRulesError("meta.json: api_version_current is not in api_versions")
    _check_date(meta["checked_date"], "meta.json checked_date")
    if not isinstance(meta["sources"], list) or not all(isinstance(s, str) for s in meta["sources"]):
        raise MetaRulesError("meta.json: sources must be a list of URLs")


def validate_rules(rules: Any) -> None:
    if not isinstance(rules, list):
        raise MetaRulesError("rules.json must be a list")
    seen: set[str] = set()
    for i, rule in enumerate(rules):
        if not isinstance(rule, dict):
            raise MetaRulesError(f"rules.json[{i}]: must be an object")
        rid = rule.get("id")
        if not isinstance(rid, str) or not rid:
            raise MetaRulesError(f"rules.json[{i}]: missing id")
        if rid in seen:
            raise MetaRulesError(f"rules.json: duplicate id {rid!r}")
        seen.add(rid)
        kind = rule.get("kind")
        if kind not in KINDS:
            raise MetaRulesError(f"{rid}: unknown kind {kind!r}")
        if rule.get("status") not in STATUSES:
            raise MetaRulesError(f"{rid}: status must be one of {STATUSES}")
        if not isinstance(rule.get("applies_to"), dict):
            raise MetaRulesError(f"{rid}: applies_to must be an object")
        if not str(rule.get("source_url") or "").startswith("https://"):
            raise MetaRulesError(f"{rid}: source_url must be an https URL")
        _check_date(rule.get("checked_date"), f"{rid}.checked_date")
        if "note" in rule and not isinstance(rule["note"], str):
            raise MetaRulesError(f"{rid}: note must be a string")
        if kind in ALLOWED_KINDS:
            allowed = rule.get("allowed")
            if not isinstance(allowed, list) or not allowed:
                raise MetaRulesError(f"{rid}: kind {kind} needs a non-empty 'allowed' list")
        elif kind == "text_limit":
            max_len = rule.get("max_len")
            if not isinstance(max_len, int) or isinstance(max_len, bool) or max_len <= 0:
                raise MetaRulesError(f"{rid}: text_limit needs a positive int max_len")
            if not isinstance(rule["applies_to"].get("field"), str):
                raise MetaRulesError(f"{rid}: text_limit needs applies_to.field")
        elif kind == "fact":
            if not isinstance(rule.get("note"), str) or not rule["note"].strip():
                raise MetaRulesError(f"{rid}: kind fact needs a non-empty 'note'")
        elif kind == "required_field":
            req = rule.get("required")
            if not isinstance(req, list) or not req or not all(isinstance(x, str) for x in req):
                raise MetaRulesError(f"{rid}: required_field needs a non-empty 'required' list")


def load_rules(directory: str | Path | None = None) -> RuleSet:
    """Читает и проверяет реестр; при нарушении схемы — MetaRulesError."""
    base = Path(directory) if directory else RULES_DIR
    try:
        meta = json.loads((base / "meta.json").read_text(encoding="utf-8"))
        rules = json.loads((base / "rules.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise MetaRulesError(f"cannot read Meta rules registry in {base}: {e}") from None
    validate_meta(meta)
    validate_rules(rules)
    return RuleSet(meta=meta, rules=tuple(rules))


_VERSION_RE = re.compile(r"^v(\d+)\.(\d+)$")


def _version_key(version: Any) -> tuple[int, int] | None:
    m = _VERSION_RE.match(str(version or "").strip().lower())
    return (int(m.group(1)), int(m.group(2))) if m else None


def graph_api_version(env_override: str | None = None, directory: str | Path | None = None) -> str:
    """Graph API version: `api_version_current` from meta.json is the single source (ACL-2123).

    An env override wins only when it is a well-formed version strictly newer than
    the registry's; an equal, older (e.g. the stale v22.0 pin) or malformed value
    is ignored.
    """
    base = Path(directory) if directory else RULES_DIR
    try:
        meta = json.loads((base / "meta.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise MetaRulesError(f"cannot read Meta rules registry in {base}: {e}") from None
    validate_meta(meta)
    current = str(meta["api_version_current"]).strip()
    override = (env_override or "").strip().lower()
    new, cur = _version_key(override), _version_key(current)
    if new is not None and cur is not None and new > cur:
        return override
    return current


# ── Чтение значений из задания ──────────────────────────────────────────

def _dig(obj: Any, path: str) -> Any:
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            cur = getattr(cur, part, None)
        if cur is None:
            return None
    return cur


def _field_value(job: Any, field: str) -> Any:
    """Значение поля задания; CTA и placements ищутся и в api_preview."""
    val = _dig(job, field)
    if val is None and field == "call_to_action":
        val = _dig(job, "api_preview.ad_creative.call_to_action")
        if isinstance(val, dict):
            val = val.get("type")
    if val is None and field == "placements":
        val = _dig(job, "api_preview.ad_set.placements")
    return val


def _norm(v: Any) -> str:
    return str(v).strip().upper()


def _matches(rule: dict[str, Any], job: Any) -> bool:
    for key, want in rule["applies_to"].items():
        if key == "field":
            continue
        have = _field_value(job, key)
        wants = want if isinstance(want, list) else [want]
        if have is None or _norm(have) not in {_norm(w) for w in wants}:
            return False
    return True


def _label(field: str) -> str:
    return FIELD_LABELS.get(field, field)


def _check_allowed(rules: list[dict[str, Any]], kind: str, job: Any) -> list[Violation]:
    field = ALLOWED_KINDS[kind]
    value = _field_value(job, field)
    if value is None or value == "" or value == []:
        return []
    applicable = [r for r in rules if r["kind"] == kind and _matches(r, job)]
    if not applicable:
        return []
    values = value if isinstance(value, (list, tuple, set)) else [value]
    out: list[Violation] = []
    for item in values:
        key = _norm(item)
        if any(r["status"] == "ga" and key in {_norm(a) for a in r["allowed"]} for r in applicable):
            continue
        blocker = next((r for r in applicable if key in {_norm(a) for a in r["allowed"]}), None)
        label = _label(field)
        if blocker is not None:
            what = "deprecated by Meta" if blocker["status"] == "deprecated" else "not generally available"
            hint = " Use an OUTCOME_* objective." if kind == "objective" else ""
            msg = f"{label} '{item}' is {what} (status: {blocker['status']}).{hint}"
            out.append(Violation(blocker["id"], field, item, msg, blocker["source_url"]))
        else:
            first = applicable[0]
            msg = f"{label} '{item}' is not an accepted Meta value for this campaign."
            out.append(Violation(first["id"], field, item, msg, first["source_url"]))
    return out


#: Как кампанию называет форма / RPA-черновик -> код objective в Marketing API.
OBJECTIVE_LABELS = {
    "awareness": "OUTCOME_AWARENESS",
    "traffic": "OUTCOME_TRAFFIC",
    "engagement": "OUTCOME_ENGAGEMENT",
    "leads": "OUTCOME_LEADS",
    "app promotion": "OUTCOME_APP_PROMOTION",
    "sales": "OUTCOME_SALES",
}


def objective_code(label: Any) -> str | None:
    """Код objective по подписи из формы/черновика ("Leads" -> OUTCOME_LEADS).

    Код (OUTCOME_* или старый) возвращается как есть; неизвестная подпись —
    None, то есть такое поле не проверяется (ложных блокировок нет).
    """
    text = str(label or "").strip()
    if not text:
        return None
    mapped = OBJECTIVE_LABELS.get(text.lower())
    if mapped:
        return mapped
    if re.fullmatch(r"[A-Z][A-Z_]+", text):
        return text
    return None


class DraftView:
    """То, что RPA-публикация реально отправляет: objective черновика.

    Цель оптимизации, billing event, destination и CTA в RPA выбирает рецепт
    Ads Manager, черновик их не несёт — поэтому они здесь не заданы и не
    проверяются (их проверяет `check_job` для Graph API-задания).
    """

    def __init__(self, draft: Any):
        self.objective = objective_code(getattr(draft, "objective", None))


def check_job(job: Any, ruleset: RuleSet | None = None, *, skip_kinds: tuple[str, ...] = ()) -> list[Violation]:
    """Нарушения правил Meta в задании (PublishJob или похожий объект/словарь).

    Пустой список — кампанию можно отправлять. Поле, которого в задании нет
    или про которое реестр ничего не знает, не проверяется. `skip_kinds` —
    виды правил, которые для этого пути не применимы.
    """
    rs = ruleset or load_rules()
    rules = [r for r in rs.rules if r["kind"] not in skip_kinds]
    out: list[Violation] = []
    for kind in ALLOWED_KINDS:
        out.extend(_check_allowed(rules, kind, job))
    for rule in rules:
        if rule["kind"] == "text_limit" and _matches(rule, job):
            field = rule["applies_to"]["field"]
            text = _field_value(job, field)
            if isinstance(text, str) and len(text) > rule["max_len"]:
                msg = f"{_label(field)} is {len(text)} characters; Meta allows at most {rule['max_len']}."
                out.append(Violation(rule["id"], field, text, msg, rule["source_url"]))
        elif rule["kind"] == "required_field" and _matches(rule, job):
            for path in rule["required"]:
                val = _dig(job, path)
                if val is None or val == "":
                    msg = f"Required field '{path}' is missing ({rule.get('note') or rule['id']})"
                    out.append(Violation(rule["id"], path, val, msg, rule["source_url"]))
    return out


def format_violations(violations: list[Violation]) -> list[str]:
    """Строки для показа человеку: причина и источник."""
    return [f"Meta rules: {v.message_en} (rule {v.rule_id}, source: {v.source_url})" for v in violations]


# ── Допустимые значения для формы билдера (ACL-1807) ────────────────────

#: Значения билдера -> код destination_type в реестре (Website / Instant Form).
BUILDER_DESTINATIONS = {"website": "WEBSITE", "instant_form": "ON_AD"}


def _ga_lists(ruleset: RuleSet, kind: str) -> list[tuple[dict[str, Any], list[str]]]:
    return [(r, list(r["allowed"])) for r in ruleset.rules if r["kind"] == kind and r["status"] == "ga"]


def _uniq(items: Any) -> list[str]:
    seen: dict[str, None] = {}
    for x in items:
        seen.setdefault(str(x), None)
    return list(seen)


def has_norm(items: list[str], value: str) -> bool:
    return _norm(value) in {_norm(i) for i in items}


def capabilities(ruleset: RuleSet | None = None) -> dict[str, Any]:
    """Что реестр разрешает выбрать в билдере: только значения со статусом `ga`.

    {objectives: {OUTCOME_X: {goals: {GOAL: [billing events]}, ctas: [...]}},
     destination_types: [...], builder_destinations: {website: {...}}, ...}.
    Если для цели оптимизации/CTA у objective нет своего правила, берётся общий
    enum; если нет и его — список пуст, а `restricted` = False (форма не режет:
    чего реестр не знает, того не проверяем — как и `check_job`).
    """
    rs = ruleset or load_rules()
    billing_by_goal: dict[str, list[str]] = {}
    for r, allowed in _ga_lists(rs, "billing_event_by_goal"):
        goal = str(r["applies_to"].get("optimization_goal") or "")
        if goal:
            billing_by_goal.setdefault(_norm(goal), []).extend(allowed)
    goal_enum = _uniq(a for _, al in _ga_lists(rs, "optimization_goal_enum") for a in al)
    billing_enum = _uniq(a for _, al in _ga_lists(rs, "billing_event_enum") for a in al)
    cta_enum = _uniq(a for _, al in _ga_lists(rs, "cta_enum") for a in al)
    dest_enum = _uniq(a for _, al in _ga_lists(rs, "destination_type_enum") for a in al)
    objectives: dict[str, Any] = {}
    for r, allowed in _ga_lists(rs, "objective"):
        for code in allowed:
            objectives.setdefault(code, {"goals": {}, "ctas": [], "restricted_goals": False, "restricted_ctas": False})
    for code, entry in objectives.items():
        goals: list[str] = []
        ctas: list[str] = []
        for kind, bucket in (("optimization_goal_by_objective", goals), ("cta_by_objective", ctas)):
            for r, allowed in _ga_lists(rs, kind):
                if _matches(r, {"objective": code}):
                    bucket.extend(allowed)
        entry["restricted_goals"] = bool(goals)
        entry["restricted_ctas"] = bool(ctas)
        # Как в check_job: значение должно пройти и правило объекта, и общий enum.
        goals = [g for g in _uniq(goals) if not goal_enum or has_norm(goal_enum, g)] if goals else goal_enum
        # Цели без своего правила billing_event_by_goal не режутся: общий enum.
        entry["goals"] = {
            g: ([b for b in _uniq(billing_by_goal[_norm(g)]) if not billing_enum or has_norm(billing_enum, b)]
                if _norm(g) in billing_by_goal else billing_enum)
            for g in goals
        }
        entry["ctas"] = _uniq(ctas) or cta_enum
    return {
        "api_version": rs.meta["api_version_current"],
        "checked_date": rs.meta["checked_date"],
        "objectives": objectives,
        "destination_types": dest_enum,
        "builder_destinations": {
            key: {"destination_type": code, "allowed": (not dest_enum) or code in dest_enum}
            for key, code in BUILDER_DESTINATIONS.items()
        },
        "billing_events": billing_enum,
    }
