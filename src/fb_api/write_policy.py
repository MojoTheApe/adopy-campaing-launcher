"""Закрытый список записей в Graph API (ACL-1761).

Решение владельца: кампании через API создаются ТОЛЬКО на паузе (PAUSED);
включает их человек в Ads Manager. Запись разрешена лишь в шесть рёбер
проверенного кабинета `act_X` и лишь с ключами из списка ниже. Тот же список
вшивается в помощник страницы (`token_capture`) — две линии, одна политика.

Никогда: DELETE, ACTIVE, `effective_status`/`configured_status`,
`execution_options`, согласие с условиями, создание форм лидов, запись куда-то,
кроме `/act_X/<ребро>`.

Единственное исключение для форм (ACL-1775): `lead_gen_form_id` ссылается на
УЖЕ СУЩЕСТВУЮЩУЮ форму Страницы и допустим ровно в одном месте — креатив
(`adcreatives`) → `object_story_spec.{link_data|video_data}.call_to_action.value`
— и только когда набор объявлений того же плана создан с `destination_type=ON_AD`
(вызывающий передаёт `on_ad=True`). Везде в другом месте ключ по-прежнему
запрещён.
"""
from __future__ import annotations

import copy
import json
import re
from typing import Any

#: Ребро → ключи, которые можно передать. Ничего сверх.
EDGE_KEYS: dict[str, frozenset[str]] = {
    "campaigns": frozenset({"name", "objective", "status", "special_ad_categories", "buying_type",
                            "daily_budget", "bid_strategy", "adlabels"}),
    "adsets": frozenset({"name", "campaign_id", "status", "optimization_goal", "billing_event",
                         "destination_type", "promoted_object", "targeting", "start_time",
                         "adlabels"}),
    "adcreatives": frozenset({"name", "object_story_spec", "degrees_of_freedom_spec", "adlabels"}),
    "ads": frozenset({"name", "adset_id", "creative", "status", "adlabels"}),
    "adimages": frozenset(),
    "advideos": frozenset({"name", "title"}),
}
#: Рёбра, где статус обязателен и равен PAUSED.
PAUSED_REQUIRED = frozenset({"campaigns", "adsets", "ads"})
#: Поле файла (multipart) — только у загрузок.
FILE_FIELDS = {"adimages": "filename", "advideos": "source"}
#: Ключи, которых не должно быть нигде, на любой глубине.
FORBIDDEN_ANYWHERE = frozenset({"effective_status", "configured_status", "execution_options",
                                "access_token", "method", "_method", "batch",
                                "suppress_http_code", "lead_gen_form_id", "tos_accepted"})
PAUSED = "PAUSED"
#: Ключ существующей формы лидов: разрешён только в `_without_lead_form` (ACL-1775).
LEAD_FORM_KEY = "lead_gen_form_id"
#: Форматы креатива, в `call_to_action.value` которых форма может стоять.
LEAD_FORM_FORMATS = ("link_data", "video_data")
#: Id формы — только цифры; всё прочее не форма.
LEAD_FORM_ID_RE = re.compile(r"^[0-9]{5,25}$")
#: Ключи, которые Graph читает как JSON. Строку в них Graph разберёт сам —
#: поэтому и мы разбираем её до проверок статуса (ревью, minor 1).
JSON_KEYS = frozenset({"special_ad_categories", "adlabels", "promoted_object", "targeting",
                       "object_story_spec", "creative", "degrees_of_freedom_spec"})
#: Ключи-«статусы», которые не про показ объявления: участие в улучшениях
#: Advantage+ creative. Разрешено только выключить (OPT_OUT), никогда — включить.
OPT_OUT_KEYS = frozenset({"enroll_status"})
OPT_OUT = "OPT_OUT"
#: Функции Advantage+ creative, которые можно ВЫКЛЮЧИТЬ (ревью R5-M1). Ключ
#: `degrees_of_freedom_spec` открыт ровно для одного: ровно
#: {"creative_features_spec": {<функция>: {"enroll_status": "OPT_OUT"}}}.
#: `text_optimizations` — в справочнике Graph «Ad Creative Features Spec».
#: Отдельного документированного ключа «text generation» там нет — не гадаем.
DOF_FEATURES = frozenset({"text_optimizations"})

_PATH_RE = re.compile(r"/(act_\d{5,})/([a-z]+)\Z")


class WriteRefused(ValueError):
    """Запись не прошла политику; к Meta ничего не ушло."""


def policy_for_page(act: str) -> dict:
    """Та же политика для помощника страницы (JSON-сериализуемая)."""
    return {
        "act": act,
        "edges": {k: sorted(v) for k, v in EDGE_KEYS.items()},
        "paused_required": sorted(PAUSED_REQUIRED),
        "files": dict(FILE_FIELDS),
        "forbidden": sorted(FORBIDDEN_ANYWHERE),
        "json_keys": sorted(JSON_KEYS),
        "opt_out_keys": sorted(OPT_OUT_KEYS),
        "dof_features": sorted(DOF_FEATURES),
        "lead_form": {"key": LEAD_FORM_KEY, "edge": "adcreatives",
                      "formats": list(LEAD_FORM_FORMATS), "id_re": LEAD_FORM_ID_RE.pattern},
    }


def _parsed(value: Any, where: str) -> Any:
    """Значение JSON-ключа верхнего уровня: строку Graph разберёт сам — разбираем и мы.

    Только для `JSON_KEYS` верхнего уровня (ревью R2-B1): строки ВНУТРИ
    разобранного объекта — обычный текст («[TEST] Camp», «{Free} gift»), Graph
    их не разбирает, и мы не трогаем.
    """
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError as exc:
            raise WriteRefused(f"write refused: {where} is not valid JSON") from exc
    return value


def _walk(value: Any, where: str, depth: int = 0) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            low = str(key).lower()
            if low in FORBIDDEN_ANYWHERE:
                raise WriteRefused(f"write refused: key {key!r} is not allowed ({where})")
            if low == "degrees_of_freedom_spec" and depth > 0:
                # Ревью R6: только на верхнем уровне креатива, где его проверяет `_check_dof`.
                raise WriteRefused(f"write refused: degrees_of_freedom_spec is only allowed at the top ({where})")
            if low in OPT_OUT_KEYS:
                if item != OPT_OUT:
                    raise WriteRefused(f"write refused: {key} may only be OPT_OUT ({where})")
            elif "status" in low and item != PAUSED:
                raise WriteRefused(f"write refused: {key} must be PAUSED ({where})")
            _walk(item, where, depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _walk(item, where, depth + 1)


def _check_dof(value: Any) -> None:
    """Ровно {"creative_features_spec": {<функция из списка>: {"enroll_status": "OPT_OUT"}}}."""
    if not (isinstance(value, dict) and set(value) == {"creative_features_spec"}):
        raise WriteRefused("write refused: degrees_of_freedom_spec may only opt features out")
    spec = value["creative_features_spec"]
    if not (isinstance(spec, dict) and spec):
        raise WriteRefused("write refused: creative_features_spec must list the features to opt out")
    for feature, item in spec.items():
        if feature not in DOF_FEATURES:
            raise WriteRefused(f"write refused: creative feature {feature!r} is not on the opt-out list")
        if not (isinstance(item, dict) and item == {"enroll_status": OPT_OUT}):
            raise WriteRefused(f"write refused: creative feature {feature!r} must be exactly OPT_OUT")


def _without_lead_form(edge: str, checked: dict, on_ad: bool) -> dict:
    """Копия параметров БЕЗ единственного разрешённого `lead_gen_form_id` (ACL-1775).

    Разрешённое место: креатив → object_story_spec → link_data|video_data →
    call_to_action → value, и значение там — ровно {lead_gen_form_id: "<цифры>"}.
    Ключ в любом другом месте остаётся в копии, и `_walk` его отвергнет.
    Без `on_ad` (набор объявлений плана не ON_AD) форма не допускается и там.
    """
    spec = checked.get("object_story_spec") if edge == "adcreatives" else None
    if not isinstance(spec, dict):
        return checked
    for fmt in LEAD_FORM_FORMATS:
        data = spec.get(fmt)
        cta = data.get("call_to_action") if isinstance(data, dict) else None
        value = cta.get("value") if isinstance(cta, dict) else None
        if not (isinstance(value, dict) and LEAD_FORM_KEY in value):
            continue
        if not on_ad:
            raise WriteRefused("write refused: lead_gen_form_id is allowed only when the ad set "
                               "of the same plan is ON_AD")
        form = value[LEAD_FORM_KEY]
        if not (isinstance(form, str) and LEAD_FORM_ID_RE.fullmatch(form)):
            raise WriteRefused("write refused: lead_gen_form_id must be the numeric id of a Page form")
        if set(value) != {LEAD_FORM_KEY}:
            raise WriteRefused("write refused: call_to_action.value may hold only lead_gen_form_id "
                               "next to an Instant Form")
        out = copy.deepcopy(checked)
        del out["object_story_spec"][fmt]["call_to_action"]["value"][LEAD_FORM_KEY]
        return out
    return checked


def check(act: str, method: str, path: str, params: dict | None,
          files: dict | None = None, *, on_ad: bool = False) -> str:
    """Проверить запись. Возвращает ребро; иначе `WriteRefused`.

    `on_ad` — набор объявлений того же плана создаётся с `destination_type=ON_AD`
    (только тогда креатив может нести `lead_gen_form_id`).
    """
    if method != "POST":
        raise WriteRefused(f"write refused: method {method!r} (only POST; never DELETE)")
    m = _PATH_RE.fullmatch(path or "")
    if not m:
        raise WriteRefused("write refused: path must be /act_<id>/<edge>")
    if m.group(1) != act:
        raise WriteRefused("write refused: not the verified ad account")
    edge = m.group(2)
    if edge not in EDGE_KEYS:
        raise WriteRefused(f"write refused: edge {edge!r} is not on the allowlist")
    params = dict(params or {})
    extra = sorted(set(params) - EDGE_KEYS[edge])
    if extra:
        raise WriteRefused(f"write refused: key {extra[0]!r} is not allowed on {edge}")
    if edge in PAUSED_REQUIRED and params.get("status") != PAUSED:
        raise WriteRefused(f"write refused: {edge} must be created with status PAUSED")
    checked = dict(params)
    for key in JSON_KEYS & set(params):
        checked[key] = _parsed(params[key], f"{edge}.{key}")
        if not isinstance(checked[key], (dict, list)):
            raise WriteRefused(f"write refused: {key} must be a JSON object or list")
    if "degrees_of_freedom_spec" in checked:
        _check_dof(checked["degrees_of_freedom_spec"])
    if edge == "ads" and "creative" in checked:
        # Объявление ссылается на готовый креатив — и только (ревью R5-M1:
        # встроенный креатив провёз бы свой degrees_of_freedom_spec).
        creative = checked["creative"]
        if not (isinstance(creative, dict) and set(creative) == {"creative_id"}
                and isinstance(creative["creative_id"], (str, int))):
            raise WriteRefused("write refused: an ad may only reference a creative by creative_id")
    _walk(_without_lead_form(edge, checked, on_ad), edge)
    files = dict(files or {})
    want = FILE_FIELDS.get(edge)
    if files:
        if not want or set(files) != {want}:
            raise WriteRefused(f"write refused: file field not allowed on {edge}")
    elif want:
        raise WriteRefused(f"write refused: {edge} needs a file in field {want!r}")
    return edge


# ───────────────────── правка существующего объекта (ACL-1800) ─────────────────────
#
# Отдельный, узкий список — НЕ часть политики создания выше. Человек включает и
# выключает кампании, меняет бюджет и ставку из дешборда; робот сам ничего не
# включает. Разрешено: POST на id УЖЕ существующего объекта (никогда на ребро,
# никогда не создание) ровно с одним из трёх полей. Ничего больше, никогда DELETE.

#: Поля, которые можно менять у существующего объекта.
OBJECT_FIELDS = frozenset({"status", "daily_budget", "bid_amount"})
#: Допустимые значения `status` (в отличие от создания, ACTIVE тут можно —
#: но только по команде человека; решение о подтверждении — выше, в `commands`).
OBJECT_STATUSES = frozenset({"ACTIVE", "PAUSED"})
#: Сумма — целое число мелких единиц валюты, не больше 15 знаков.
_AMOUNT_RE = re.compile(r"[1-9][0-9]{0,14}", re.ASCII)
_OBJECT_PATH_RE = re.compile(r"/([1-9][0-9]{4,})\Z", re.ASCII)
_MAX_AMOUNT = 10 ** 15


def policy_for_commands(act: str) -> dict:
    """Политика правок для помощника страницы: без рёбер создания вовсе.

    Помощник с такой политикой отказывает любому пути, кроме `/<id объекта>`;
    а помощник создателя (`policy_for_page`) отказывает пути объекта.
    """
    return {
        "act": act,
        "edges": {},
        "paused_required": [],
        "files": {},
        "forbidden": sorted(FORBIDDEN_ANYWHERE),
        "json_keys": [],
        "opt_out_keys": [],
        "dof_features": [],
        "object_update": {"fields": sorted(OBJECT_FIELDS), "statuses": sorted(OBJECT_STATUSES)},
    }


def amount_ok(value: Any) -> bool:
    """Положительное целое в мелких единицах: int или строка из цифр; не bool, не float."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return 0 < value < _MAX_AMOUNT
    return isinstance(value, str) and _AMOUNT_RE.fullmatch(value) is not None


def check_object_update(method: str, path: str, params: dict | None,
                        files: dict | None = None) -> str:
    """Проверить правку существующего объекта. Возвращает имя поля; иначе `WriteRefused`."""
    if method != "POST":
        raise WriteRefused(f"write refused: method {method!r} (only POST; never DELETE)")
    if not _OBJECT_PATH_RE.fullmatch(path or ""):
        raise WriteRefused("write refused: path must be the id of an existing object "
                           "(no edges, no creation)")
    if files:
        raise WriteRefused("write refused: no files in an object update")
    params = dict(params or {})
    if len(params) != 1:
        raise WriteRefused("write refused: exactly one field per update")
    (key, value), = params.items()
    if key in FORBIDDEN_ANYWHERE or key not in OBJECT_FIELDS:
        raise WriteRefused(f"write refused: field {key!r} is not allowed in an object update")
    if key == "status":
        if not isinstance(value, str) or value not in OBJECT_STATUSES:
            raise WriteRefused("write refused: status may only be ACTIVE or PAUSED")
    elif not amount_ok(value):
        raise WriteRefused(f"write refused: {key} must be a positive whole number in minor units")
    return key
