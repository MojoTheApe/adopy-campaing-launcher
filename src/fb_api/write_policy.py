"""Закрытый список записей в Graph API (ACL-1761).

Решение владельца: кампании через API создаются ТОЛЬКО на паузе (PAUSED);
включает их человек в Ads Manager. Запись разрешена лишь в шесть рёбер
проверенного кабинета `act_X` и лишь с ключами из списка ниже. Тот же список
вшивается в помощник страницы (`token_capture`) — две линии, одна политика.

Отдельная узкая цепочка для форм (ACL-1776, `check_form_create`): ровно
`POST /{page_id}/leadgen_forms` на Страницу плана, ключи из явного списка,
`tos_accepted` и любое упоминание условий - никогда, DELETE и прочие рёбра
Страницы - никогда. С цепочкой `act_X` она не пересекается.

ACL-2114: единственное исключение из «только PAUSED» - `status=ACTIVE` у набора объявлений и объявления
(`adsets`, `ads`), и только когда вызывающий передал `children_active=True`: кампания-родитель
подтверждена чтением как PAUSED (под паузой кампании ничего не показывается). Кампания, креатив,
вложенные статусы и любая другая запись с ACTIVE по-прежнему отказ.

В обычных записях никогда: DELETE, ACTIVE (кроме случая выше), `effective_status`/`configured_status`,
`execution_options`, согласие с условиями, создание форм лидов, запись куда-то,
кроме `/act_X/<ребро>`.

ACL-2105: отдельный `page_validation`/`InPageGraph.validate_page` не создаёт объектов:
он сам строит фиксированный creative с `validate_only` для закреплённого кабинета.
Этот запрос не проходит через обычную `check`; запрет `execution_options` ниже
сохранён для всех обычных записей и их параметров.

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
                         "adlabels", "is_dynamic_creative"}),
    "adcreatives": frozenset({"name", "object_story_spec", "degrees_of_freedom_spec", "adlabels",
                              "contextual_multi_ads", "asset_feed_spec"}),
    "ads": frozenset({"name", "adset_id", "creative", "status", "adlabels"}),
    "adimages": frozenset(),
    "advideos": frozenset({"name", "title"}),
}
#: Рёбра, где статус обязателен и равен PAUSED.
PAUSED_REQUIRED = frozenset({"campaigns", "adsets", "ads"})
#: ACL-2114: ребра, где ACTIVE допустим при `children_active` (родитель-кампания подтверждена чтением
#: как PAUSED: созданная этим запуском или найденная с прошлого). Кампания - никогда ACTIVE; прочие записи - тоже.
CHILD_EDGES = frozenset({"adsets", "ads"})
ACTIVE = "ACTIVE"
#: Поле файла (multipart) — только у загрузок.
FILE_FIELDS = {"adimages": "filename", "advideos": "source"}
#: Ключи, которых не должно быть нигде, на любой глубине.
FORBIDDEN_ANYWHERE = frozenset({"effective_status", "configured_status", "execution_options",
                                "access_token", "method", "_method", "batch",
                                "suppress_http_code", "lead_gen_form_id", "tos_accepted",
                                "instagram_actor_id"})
PAUSED = "PAUSED"

#: Загрузка большого видео кусками (ACL-1891): `POST /act_X/advideos` с `upload_phase`.
#: Справочник Graph «Ad Account / Advideos» (POST, chunked upload): `upload_phase` = start | transfer |
#: finish (| cancel - не даём), `file_size`, `upload_session_id`, `start_offset`, `video_file_chunk`.
#: Ровно три фазы с ЗАКРЫТЫМ набором ключей на фазу; всё прочее - отказ.
UPLOAD_PHASES = ("start", "transfer", "finish")
#: 17.10 живой Doc120 (файл 247 МБ): Meta отказала в start с подкодом 1363049
#: «A waterfall ID is required when the caller is a Facebook App» — токен Ads Manager
#: считается приложением. `waterfall_id` — наш случайный id одной загрузки (32 hex),
#: один и тот же во всех трёх фазах, как у загрузчика Ads Manager.
UPLOAD_WATERFALL_RE = re.compile(r"[0-9a-f]{32}", re.ASCII)
UPLOAD_KEYS = {
    "start": frozenset({"upload_phase", "file_size", "waterfall_id"}),
    "transfer": frozenset({"upload_phase", "upload_session_id", "start_offset", "waterfall_id"}),
    "finish": frozenset({"upload_phase", "upload_session_id", "title", "name", "waterfall_id"}),
}
#: Файл фазы transfer (multipart); у start и finish файла нет.
UPLOAD_CHUNK_FIELD = "video_file_chunk"
#: Потолок одного куска, который мы отправляем (память профиля и одного запроса).
UPLOAD_CHUNK_MAX = 8 * 1024 * 1024
#: Потолок файла на пути кусками: безопасный для профиля и для бюджета вызовов (Meta сама - 4 ГБ).
UPLOAD_FILE_MAX = 1536 * 1024 * 1024
UPLOAD_ID_RE = re.compile(r"^[0-9]{5,25}$")
UPLOAD_NUMBER_RE = re.compile(r"^[0-9]{1,13}$")
#: Ключ существующей формы лидов: разрешён только в `_without_lead_form` (ACL-1775).
LEAD_FORM_KEY = "lead_gen_form_id"
#: Форматы креатива, в `call_to_action.value` которых форма может стоять.
LEAD_FORM_FORMATS = ("link_data", "video_data")
#: Instagram-идентичность креатива (ACL-1896): ОДНО числовое поле, и только прямым ключом
#: `object_story_spec`. Справочник Meta: `object_story_spec.instagram_user_id` - строка из цифр.
#: Старое `instagram_actor_id` не разрешено нигде.
INSTAGRAM_KEY = "instagram_user_id"
INSTAGRAM_ID_RE = re.compile(r"^[0-9]{5,25}$")
#: Id формы — только цифры; всё прочее не форма.
LEAD_FORM_ID_RE = re.compile(r"^[0-9]{5,25}$")
#: Ключи, которые Graph читает как JSON. Строку в них Graph разберёт сам —
#: поэтому и мы разбираем её до проверок статуса (ревью, minor 1).
JSON_KEYS = frozenset({"special_ad_categories", "adlabels", "promoted_object", "targeting",
                       "object_story_spec", "creative", "degrees_of_freedom_spec",
                       "contextual_multi_ads", "asset_feed_spec"})
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

#: Multi-advertiser ads (ACL-1767): ключ креатива, который можно только ВЫКЛЮЧИТЬ — ровно
#: {"enroll_status": "OPT_OUT"}. Включить его (OPT_IN) политика не даёт никогда.
MULTI_ADS_KEY = "contextual_multi_ads"

#: Кнопки (call_to_action.type), которые билдер может отправить для цели Leads (ACL-1767).
#: Имена — из справочника Graph «Ad Creative Link Data Call To Action» (enum проверен);
#: какие из них принимает именно цель OUTCOME_LEADS, Meta не публикует списком — берём
#: те, что предлагает Ads Manager для лидов; лишнее Meta отвергнет известной ошибкой #100.
#: BOOK_NOW, ORDER_NOW, REQUEST_TIME убраны (ревью ACL-1767): в справочнике Meta они есть в enum,
#: но для сайта с целью Leads нигде не документированы.
CTA_WEBSITE = ("LEARN_MORE", "SIGN_UP", "APPLY_NOW", "GET_QUOTE", "SUBSCRIBE", "CONTACT_US",
               "DOWNLOAD", "GET_OFFER")
#: Для Instant Form (link_data + lead_gen_form_id) Meta документирует РОВНО шесть кнопок
#: (Marketing API, Lead Ads: «call_to_action must be one of …»). Остальные из списка сайта
#: (CONTACT_US, GET_OFFER, BOOK_NOW, REQUEST_TIME, ORDER_NOW) для формы не предлагаются и
#: не подменяются другими: Meta отвергла бы их, а молчаливая замена меняла бы смысл кнопки.
CTA_FORM = ("LEARN_MORE", "SIGN_UP", "APPLY_NOW", "GET_QUOTE", "SUBSCRIBE", "DOWNLOAD")
CTA_TYPES = frozenset(CTA_WEBSITE) | frozenset(CTA_FORM)

#: Тексты и вертикальный файл (ACL-1774): ключ креатива `asset_feed_spec` с ЗАКРЫТОЙ схемой.
#: Справочник Graph «Ad Asset Feed Spec» / «Asset Feed Options»: до 5 bodies (1024 знака),
#: до 5 titles и descriptions (255), для правил кастомизации нужно минимум два правила.
#: Допустимо ровно то, что шлёт создатель: тексты, ОДНА или ДВЕ картинки/видео с метками,
#: ссылка сайта, кнопка, формат и (при двух файлах) ровно два правила по площадкам.
ASSET_FEED_KEY = "asset_feed_spec"
ASSET_FEED_MAX_TEXTS = 5
#: Живой прогон 2026-10-14 (Doc120): Meta отказала (100/1885878) на ленте с 3 bodies и двумя правилами
#: площадок - правило применяет по ОДНОМУ ассету каждого типа. Решение владельца 2026-10-20: до 5 текстов
#: идут как Dynamic Creative (`is_dynamic_creative: true` на наборе, лента БЕЗ правил, один файл), а
#: вертикальный файл с правилами площадок - только с одним текстом каждого вида.
ASSET_FEED_TEXTS_WITH_RULES = 1
#: Ключ набора, включающий Dynamic Creative: только значение True.
DYNAMIC_KEY = "is_dynamic_creative"
#: Тексты отказов (один источник для плана, предпроверки и страницы).
MSG_TEXTS_VERTICAL = ("Several primary texts and a Stories/Reels file can't be combined: Meta applies one "
                      "text per placement group. Use several texts without the vertical file, or one text with it.")
MSG_TEXTS_DYNAMIC_SET = ("An ad with several texts runs as Dynamic Creative, and Meta allows only one ad in such an "
                         "ad set: move the other ads of this ad set to a separate ad set")
ASSET_FEED_TEXT_MAX = {"bodies": 1024, "titles": 255, "descriptions": 255}
ASSET_FEED_MAX_MEDIA = 2
ASSET_FEED_FORMATS = ("SINGLE_IMAGE", "SINGLE_VIDEO")
#: Метки файлов: только наши (`acl_main`, `acl_vertical`), чужие имена в Graph не попадают.
ASSET_LABEL_RE = re.compile(r"^acl_[a-z0-9_]{1,30}$")
IMAGE_HASH_RE = re.compile(r"^[0-9a-f]{32}$")
VIDEO_ID_RE = re.compile(r"^[0-9]{5,25}$")
#: Площадки правил — подмножество ручного списка создателя (ACL-1761).
RULE_PLATFORMS = frozenset({"facebook", "instagram"})
RULE_FACEBOOK_POSITIONS = frozenset({"feed", "story", "facebook_reels", "instream_video"})
RULE_INSTAGRAM_POSITIONS = frozenset({"stream", "story", "reels"})

#: Ключи `targeting`, которые создатель вообще отправляет (ACL-1767: закрытый список).
TARGETING_KEYS = frozenset({"geo_locations", "age_min", "age_max", "genders", "locales",
                            "flexible_spec", "targeting_automation", "publisher_platforms",
                            "facebook_positions", "instagram_positions", "device_platforms",
                            "user_os"})
#: genders: 1 — мужчины, 2 — женщины (Graph «Targeting»); «все» = ключа нет.
GENDER_VALUES = frozenset({1, 2})
#: device_platforms: Graph «Targeting» — mobile, desktop.
DEVICE_PLATFORMS = frozenset({"mobile", "desktop"})
#: user_os: «iOS» / «Android» или с минимальной версией «iOS_ver_14.0_and_above».
#: Минимальные версии ОС, которые Meta перечисляет в справочнике таргетинга (user_os,
#: «iOS_ver_14.0_and_above»). Статический список: вне него отказ ДО записи, а не ошибка #100
#: после создания кампании. Ключ - ОС в нижнем регистре; значения - «N.M».
OS_VERSIONS = {
    "ios": ["2.0", "3.0"] + [f"{v}.0" for v in range(4, 19)],
    "android": ["2.0", "2.1", "2.2", "2.3", "3.0", "3.1", "3.2", "4.0", "4.1", "4.2", "4.3", "4.4",
                "5.0", "5.1", "6.0", "7.0", "7.1"] + [f"{v}.0" for v in range(8, 17)],
}


def os_version_ok(os_name: str, ver: str) -> bool:
    """«14» или «14.0» из списка версий этой ОС."""
    ver = str(ver or "").strip()
    ver = ver if "." in ver else ver + ".0"
    return ver in OS_VERSIONS.get(str(os_name or "").strip().lower(), [])


USER_OS_RE = re.compile(r"^(iOS|Android)(_ver_[0-9]{1,2}(\.[0-9]{1,2})?_and_above)?$")

_PATH_RE = re.compile(r"/(act_\d{5,})/([a-z]+)\Z")


class WriteRefused(ValueError):
    """Запись не прошла политику; к Meta ничего не ушло."""


def policy_for_page(act: str, page: str = "") -> dict:
    """Та же политика для помощника страницы (JSON-сериализуемая)."""
    return {
        "act": act,
        "edges": {k: sorted(v) for k, v in EDGE_KEYS.items()},
        "paused_required": sorted(PAUSED_REQUIRED),
        "child_edges": sorted(CHILD_EDGES),
        "files": dict(FILE_FIELDS),
        "upload": {"phases": list(UPLOAD_PHASES), "keys": {k: sorted(v) for k, v in UPLOAD_KEYS.items()},
                   "chunk_field": UPLOAD_CHUNK_FIELD, "chunk_max": UPLOAD_CHUNK_MAX,
                   "file_max": UPLOAD_FILE_MAX, "id_re": UPLOAD_ID_RE.pattern,
                   "number_re": UPLOAD_NUMBER_RE.pattern},
        "forbidden": sorted(FORBIDDEN_ANYWHERE),
        "json_keys": sorted(JSON_KEYS),
        "opt_out_keys": sorted(OPT_OUT_KEYS),
        "dof_features": sorted(DOF_FEATURES),
        "multi_ads_key": MULTI_ADS_KEY,
        "asset_feed": {"key": ASSET_FEED_KEY, "max_texts": ASSET_FEED_MAX_TEXTS, "max_texts_with_rules": ASSET_FEED_TEXTS_WITH_RULES,
                       "dynamic_key": DYNAMIC_KEY,
                       "text_max": dict(ASSET_FEED_TEXT_MAX), "max_media": ASSET_FEED_MAX_MEDIA,
                       "formats": list(ASSET_FEED_FORMATS), "label_re": ASSET_LABEL_RE.pattern,
                       "hash_re": IMAGE_HASH_RE.pattern, "video_id_re": VIDEO_ID_RE.pattern,
                       "platforms": sorted(RULE_PLATFORMS),
                       "facebook_positions": sorted(RULE_FACEBOOK_POSITIONS),
                       "instagram_positions": sorted(RULE_INSTAGRAM_POSITIONS)},
        "cta_types": sorted(CTA_TYPES),
        "cta_website": list(CTA_WEBSITE),
        "cta_form": list(CTA_FORM),
        "targeting": {"keys": sorted(TARGETING_KEYS), "genders": sorted(GENDER_VALUES),
                      "devices": sorted(DEVICE_PLATFORMS), "user_os_re": USER_OS_RE.pattern},
        "instagram": {"key": INSTAGRAM_KEY, "id_re": INSTAGRAM_ID_RE.pattern},
        "lead_form": {"key": LEAD_FORM_KEY, "edge": "adcreatives",
                      "formats": list(LEAD_FORM_FORMATS), "id_re": LEAD_FORM_ID_RE.pattern},
        "form_create": policy_for_form_create(page),
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
            if low in ("degrees_of_freedom_spec", MULTI_ADS_KEY, ASSET_FEED_KEY) and depth > 0:
                # Ревью R6: только на верхнем уровне креатива, где его проверяет `_check_dof`.
                raise WriteRefused(f"write refused: {key} is only allowed at the top ({where})")
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


def _check_multi_ads(value: Any) -> None:
    """Ровно {"enroll_status": "OPT_OUT"}: Multi-advertiser ads можно только выключить (ACL-1767)."""
    if value != {"enroll_status": OPT_OUT} or not isinstance(value, dict):
        raise WriteRefused("write refused: contextual_multi_ads may only be exactly OPT_OUT")


def _is_text_list(items: Any, kind: str, *, required: bool, limit_count: int = ASSET_FEED_MAX_TEXTS) -> bool:
    if not isinstance(items, list) or len(items) > limit_count or (required and not items):
        return False
    limit = ASSET_FEED_TEXT_MAX[kind]
    return all(isinstance(x, dict) and set(x) == {"text"} and isinstance(x["text"], str)
               and x["text"].strip() != "" and len(x["text"]) <= limit for x in items)


def _labels_of(item: dict) -> list[str] | None:
    """Метки файла: нет ключа - пусто; иначе ровно одна наша метка."""
    if "adlabels" not in item:
        return []
    raw = item["adlabels"]
    if not (isinstance(raw, list) and len(raw) == 1 and isinstance(raw[0], dict)
            and set(raw[0]) == {"name"} and isinstance(raw[0]["name"], str)
            and ASSET_LABEL_RE.fullmatch(raw[0]["name"])):
        return None
    return [raw[0]["name"]]


def _check_asset_feed(spec: Any, creative: dict) -> None:
    """Закрытая схема `asset_feed_spec` (ACL-1774); всё сверх неё - отказ.

    Рядом допустим `object_story_spec` со Страницей и проверенной Instagram-идентичностью: формат объявления
    (`link_data`/`video_data`) вместе с лентой ассетов Meta не принимает, и мы его не даём.
    Форма лидов сюда не попадает вовсе: `lead_gen_form_id` в этом ключе запрещён везде.
    """
    def refuse(why: str) -> None:
        raise WriteRefused(f"write refused: asset_feed_spec {why}")

    if not isinstance(spec, dict):
        refuse("must be an object")
    allowed = {"bodies", "titles", "descriptions", "images", "videos", "link_urls",
               "call_to_action_types", "ad_formats", "asset_customization_rules"}
    extra = sorted(set(spec) - allowed)
    if extra:
        refuse(f"key {extra[0]!r} is not allowed")
    story = creative.get("object_story_spec")
    if not (isinstance(story, dict) and set(story) <= {"page_id", INSTAGRAM_KEY}
            and isinstance(story.get("page_id"), str)):
        refuse("needs object_story_spec with page_id and optional Instagram identity only")
    has_images, has_videos = bool(spec.get("images")), bool(spec.get("videos"))
    if has_images == has_videos or ("images" in spec and "videos" in spec):
        refuse("needs either images or videos")
    kind = "images" if has_images else "videos"
    media = spec[kind]
    if not (isinstance(media, list) and 1 <= len(media) <= ASSET_FEED_MAX_MEDIA):
        refuse(f"{kind} must hold 1-{ASSET_FEED_MAX_MEDIA} files")
    cap = ASSET_FEED_TEXTS_WITH_RULES if len(media) > 1 else ASSET_FEED_MAX_TEXTS
    for kind_name in ("bodies", "titles", "descriptions"):
        # Длина - отдельная причина: «1-N текстов» скрыла бы её (ACL-1774).
        limit = ASSET_FEED_TEXT_MAX[kind_name]
        for x in spec.get(kind_name) or []:
            if isinstance(x, dict) and isinstance(x.get("text"), str) and len(x["text"]) > limit:
                refuse(f"{kind_name} text is {len(x['text'])} characters long, the limit is {limit}")
    if not _is_text_list(spec.get("bodies"), "bodies", required=True, limit_count=cap):
        refuse(f"bodies must hold 1-{cap} non-empty texts")
    if not _is_text_list(spec.get("titles"), "titles", required=True, limit_count=cap):
        refuse(f"titles must hold 1-{cap} non-empty texts")
    if "descriptions" in spec and not _is_text_list(spec["descriptions"], "descriptions", required=True,
                                                    limit_count=cap):
        refuse(f"descriptions must hold 1-{cap} non-empty texts")
    names: list[str] = []
    for item in media:
        if not isinstance(item, dict):
            refuse(f"{kind} item must be an object")
        if kind == "images":
            ok = set(item) <= {"hash", "adlabels"} and isinstance(item.get("hash"), str) \
                and bool(IMAGE_HASH_RE.fullmatch(item["hash"]))
        else:
            thumb = item.get("thumbnail_url")
            ok = set(item) <= {"video_id", "thumbnail_url", "adlabels"} \
                and isinstance(item.get("video_id"), str) and bool(VIDEO_ID_RE.fullmatch(item["video_id"])) \
                and isinstance(thumb, str) and thumb.startswith("https://")
        labels = _labels_of(item) if ok else None
        if not ok or labels is None:
            refuse(f"{kind} item is not allowed")
        names += labels
    if len(set(names)) != len(names):
        refuse("file labels must differ")
    if "link_urls" in spec:
        links = spec["link_urls"]
        if not (isinstance(links, list) and len(links) == 1 and isinstance(links[0], dict)
                and set(links[0]) == {"website_url"} and isinstance(links[0]["website_url"], str)
                and links[0]["website_url"].startswith("https://")):
            refuse("link_urls must hold one https website_url")
    ctas = spec.get("call_to_action_types")
    if not (isinstance(ctas, list) and len(ctas) == 1 and ctas[0] in CTA_WEBSITE):
        refuse("call_to_action_types must hold one allowed button")
    want = "SINGLE_IMAGE" if kind == "images" else "SINGLE_VIDEO"
    if spec.get("ad_formats") != [want]:
        refuse(f"ad_formats must be exactly [{want!r}]")
    rules = spec.get("asset_customization_rules")
    if len(media) == 1:
        if "asset_customization_rules" in spec:
            refuse("rules need two files")
        if names:
            refuse("a single file takes no label")
        return
    # Два файла: метки обязательны и ровно два правила (Meta требует минимум два).
    if len(names) != 2:
        refuse("two files need a label each")
    if not (isinstance(rules, list) and len(rules) == 2):
        refuse("two files need exactly two rules")
    label_key = "image_label" if kind == "images" else "video_label"
    used: list[str] = []
    for rule in rules:
        if not (isinstance(rule, dict) and set(rule) == {"customization_spec", label_key}):
            refuse("rule keys are not allowed")
        lab = rule[label_key]
        if not (isinstance(lab, dict) and set(lab) == {"name"} and lab["name"] in names):
            refuse("rule label must name one of the files")
        used.append(lab["name"])
        cs = rule["customization_spec"]
        if not (isinstance(cs, dict) and cs and set(cs) <= {"publisher_platforms", "facebook_positions",
                                                           "instagram_positions"}):
            refuse("customization_spec keys are not allowed")
        for key, allowed_values in (("publisher_platforms", RULE_PLATFORMS),
                                    ("facebook_positions", RULE_FACEBOOK_POSITIONS),
                                    ("instagram_positions", RULE_INSTAGRAM_POSITIONS)):
            if key in cs and not (isinstance(cs[key], list) and cs[key]
                                  and all(isinstance(x, str) and x in allowed_values for x in cs[key])):
                refuse(f"{key} value is not allowed")
    if sorted(used) != sorted(names):
        refuse("each file needs its own rule")


def _check_cta(spec: Any, on_ad: bool = False) -> None:
    """Тип кнопки — из списка; всё прочее Graph отвергнет, мы не даём и отправить (ACL-1767)."""
    if not isinstance(spec, dict):
        return
    # Назначение известно, когда вызывающий сказал `on_ad` (Instant Form): тогда список формы,
    # без «Order now». Иначе - список сайта (он шире и включает список формы).
    allowed = CTA_FORM if on_ad else CTA_WEBSITE
    for fmt in LEAD_FORM_FORMATS:
        data = spec.get(fmt)
        if not isinstance(data, dict) or "call_to_action" not in data:
            continue
        cta = data["call_to_action"]      # null тоже отказ, как в слое страницы
        if not (isinstance(cta, dict) and cta.get("type") in allowed):
            raise WriteRefused("write refused: call_to_action.type is not on the allowed list")


def _check_targeting(t: Any) -> None:
    """Закрытый список ключей таргетинга и допустимые значения новых полей (ACL-1767)."""
    if not isinstance(t, dict):
        raise WriteRefused("write refused: targeting must be an object")
    extra = sorted(set(t) - TARGETING_KEYS)
    if extra:
        raise WriteRefused(f"write refused: targeting key {extra[0]!r} is not allowed")
    if "genders" in t:
        g = t["genders"]
        if not (isinstance(g, list) and g and all(type(x) is int and x in GENDER_VALUES for x in g)):
            raise WriteRefused("write refused: targeting.genders may hold only 1 and 2")
    if "device_platforms" in t:
        d = t["device_platforms"]
        if not (isinstance(d, list) and d and all(x in DEVICE_PLATFORMS for x in d)):
            raise WriteRefused("write refused: targeting.device_platforms may hold only mobile and desktop")
    if "user_os" in t:
        o = t["user_os"]
        if not (isinstance(o, list) and o and all(isinstance(x, str) and USER_OS_RE.fullmatch(x) for x in o)):
            raise WriteRefused("write refused: targeting.user_os value is not allowed")
    if "locales" in t:
        loc = t["locales"]
        if not (isinstance(loc, list) and loc and all(type(x) is int and x > 0 for x in loc)):
            raise WriteRefused("write refused: targeting.locales must be a list of numeric ids")


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


def _count_key(value: Any, key: str) -> int:
    if isinstance(value, dict):
        return sum((1 if k == key else 0) + _count_key(v, key) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return sum(_count_key(v, key) for v in value)
    return 0


def _check_instagram(edge: str, checked: dict) -> None:
    """`instagram_user_id` - ровно один раз, прямым ключом object_story_spec креатива, строка из цифр (ACL-1896)."""
    seen = _count_key(checked, INSTAGRAM_KEY)
    if not seen:
        return
    story = checked.get("object_story_spec") if edge == "adcreatives" else None
    value = story.get(INSTAGRAM_KEY) if isinstance(story, dict) else None
    if seen != 1 or not isinstance(story, dict) or INSTAGRAM_KEY not in story:
        raise WriteRefused(f"write refused: {INSTAGRAM_KEY} is allowed only in object_story_spec of a creative")
    if not (isinstance(value, str) and INSTAGRAM_ID_RE.fullmatch(value)):
        raise WriteRefused(f"write refused: {INSTAGRAM_KEY} must be the numeric id of an Instagram account")

def _digits(value: Any) -> int | None:
    """Целое из int или строки цифр (не bool, не float); иначе None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and UPLOAD_NUMBER_RE.fullmatch(value):
        return int(value)
    return None


def _check_upload(params: dict, files: dict) -> None:
    """Фазы загрузки видео кусками (ACL-1891): имя фазы, числа, id сессии, размер куска."""
    phase = params.get("upload_phase")
    if not (isinstance(phase, str) and phase in UPLOAD_PHASES):
        raise WriteRefused("write refused: upload_phase must be start, transfer or finish")
    extra = sorted(set(params) - UPLOAD_KEYS[phase])
    if extra:
        raise WriteRefused(f"write refused: key {extra[0]!r} is not allowed in upload phase {phase}")
    if "waterfall_id" in params and not (isinstance(params["waterfall_id"], str)
                                         and UPLOAD_WATERFALL_RE.fullmatch(params["waterfall_id"])):
        raise WriteRefused("write refused: waterfall_id must be 32 lowercase hex characters")
    if phase == "start":
        size = _digits(params.get("file_size"))
        if not size or size > UPLOAD_FILE_MAX:
            raise WriteRefused("write refused: file_size must be a positive size within the video limit")
    else:
        sid = params.get("upload_session_id")
        if not (isinstance(sid, str) and UPLOAD_ID_RE.fullmatch(sid)):
            raise WriteRefused("write refused: upload_session_id must be the numeric id Meta returned")
    if phase == "transfer":
        off = _digits(params.get("start_offset"))
        if off is None or off >= UPLOAD_FILE_MAX:
            raise WriteRefused("write refused: start_offset must be a number inside the video limit")
    if phase == "finish":
        for key in ("title", "name"):
            if key in params and not (isinstance(params[key], str) and 0 < len(params[key]) <= 200):
                raise WriteRefused(f"write refused: {key} must be a short text")
    if phase == "transfer":
        if set(files) != {UPLOAD_CHUNK_FIELD}:
            raise WriteRefused(f"write refused: transfer needs exactly the file field {UPLOAD_CHUNK_FIELD!r}")
        chunk = files[UPLOAD_CHUNK_FIELD]
        if not isinstance(chunk, (bytes, bytearray)) or not 0 < len(chunk) <= UPLOAD_CHUNK_MAX:
            raise WriteRefused(f"write refused: a chunk must be 1..{UPLOAD_CHUNK_MAX} bytes")
    elif files:
        raise WriteRefused(f"write refused: upload phase {phase} takes no file")


def check(act: str, method: str, path: str, params: dict | None,
          files: dict | None = None, *, on_ad: bool = False, children_active: bool = False) -> str:
    """Проверить запись. Возвращает ребро; иначе `WriteRefused`.

    `on_ad` — набор объявлений того же плана создаётся с `destination_type=ON_AD`
    (только тогда креатив может нести `lead_gen_form_id`).

    `children_active` (ACL-2114) — вызывающий подтвердил чтением, что кампания-родитель
    в статусе PAUSED. Только тогда набор объявлений/объявление (`adsets`, `ads`) может
    быть создан с `status=ACTIVE`: пока кампания на паузе, ничего не показывается.
    Кампания всегда PAUSED; вложенные статусы - только PAUSED.
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
    if edge == "advideos" and "upload_phase" in params:
        _check_upload(params, dict(files or {}))
        return edge
    extra = sorted(set(params) - EDGE_KEYS[edge])
    if extra:
        raise WriteRefused(f"write refused: key {extra[0]!r} is not allowed on {edge}")
    child_active = (children_active is True and edge in CHILD_EDGES
                    and params.get("status") == ACTIVE)
    if edge in PAUSED_REQUIRED and params.get("status") != PAUSED and not child_active:
        raise WriteRefused(f"write refused: {edge} must be created with status PAUSED"
                           + (" (ACTIVE only for ad sets and ads under a confirmed PAUSED campaign)"
                              if edge in CHILD_EDGES else ""))
    checked = dict(params)
    if child_active:
        checked["status"] = PAUSED  # верхний статус разрешён выше; вложенные проверяет `_walk`
    for key in JSON_KEYS & set(params):
        checked[key] = _parsed(params[key], f"{edge}.{key}")
        if not isinstance(checked[key], (dict, list)):
            raise WriteRefused(f"write refused: {key} must be a JSON object or list")
    if "degrees_of_freedom_spec" in checked:
        _check_dof(checked["degrees_of_freedom_spec"])
    if MULTI_ADS_KEY in checked:
        _check_multi_ads(checked[MULTI_ADS_KEY])
    if ASSET_FEED_KEY in checked:
        _check_asset_feed(checked[ASSET_FEED_KEY], checked)
    if edge == "adcreatives":
        _check_cta(checked.get("object_story_spec"), on_ad)
    if edge == "adsets" and "targeting" in checked:
        _check_targeting(checked["targeting"])
    if DYNAMIC_KEY in checked and checked[DYNAMIC_KEY] is not True:
        raise WriteRefused(f"write refused: {DYNAMIC_KEY} may only be true")
    if edge == "ads" and "creative" in checked:
        # Объявление ссылается на готовый креатив — и только (ревью R5-M1:
        # встроенный креатив провёз бы свой degrees_of_freedom_spec).
        creative = checked["creative"]
        if not (isinstance(creative, dict) and set(creative) == {"creative_id"}
                and isinstance(creative["creative_id"], (str, int))):
            raise WriteRefused("write refused: an ad may only reference a creative by creative_id")
    _check_instagram(edge, checked)
    _walk(_without_lead_form(edge, checked, on_ad), edge)
    files = dict(files or {})
    want = FILE_FIELDS.get(edge)
    if files:
        if not want or set(files) != {want}:
            raise WriteRefused(f"write refused: file field not allowed on {edge}")
    elif want:
        raise WriteRefused(f"write refused: {edge} needs a file in field {want!r}")
    return edge


# ───────────────────────── создание формы лидов (ACL-1776) ─────────────────────────
#
# Отдельная цепочка: одно ребро Страницы, явный список ключей, никаких условий.
# Зеркало на странице-помощнике (`token_capture`, функция `checkForm`) читает ту же
# таблицу из `policy_for_form_create()`.

FORM_EDGE = "leadgen_forms"
#: Ключи верхнего уровня запроса. `tos_accepted` сюда не попадёт никогда.
FORM_KEYS = frozenset({"name", "questions", "privacy_policy", "thank_you_page", "context_card",
                       "question_page_custom_headline", "locale", "follow_up_action_url",
                       "is_optimized_for_quality", "block_display_for_non_targeted_viewer"})
#: Булевы ключи верхнего уровня (ACL-2053): только true/false, не строки и не числа.
FORM_BOOL_KEYS = frozenset({"is_optimized_for_quality", "block_display_for_non_targeted_viewer"})
#: Ключи, которые Graph читает как JSON (строку разбираем до проверок, как у `act_X`).
FORM_JSON_KEYS = frozenset({"questions", "privacy_policy", "thank_you_page", "context_card"})
#: Допустимые ключи вложенных объектов.
FORM_NESTED: dict[str, list[str]] = {
    "privacy_policy": ["link_text", "url"],
    "thank_you_page": ["body", "button_text", "button_type", "title", "website_url"],
    "context_card": ["button_text", "content", "style", "title"],
    "question": ["key", "label", "options", "type"],
    "option": ["key", "value"],
}
FORM_QUESTION_TYPES = ("CUSTOM", "EMAIL", "FIRST_NAME", "FULL_NAME", "LAST_NAME", "PHONE")
#: `NONE` - кнопки нет (нет ссылки на сайт): `button_type` у thank_you_page обязателен
#: (справочник Meta page/leadgen_forms), для «без ссылки» там предусмотрен `NONE`.
FORM_BUTTON_TYPES = ("VIEW_WEBSITE", "NONE")
FORM_CARD_STYLES = ("PARAGRAPH_STYLE",)
FORM_MAX_QUESTIONS = 15
FORM_MAX_OPTIONS = 20
FORM_MAX_TEXT = 1000
#: Имя формы несёт метку робота и хэш содержания (ключ идемпотентности).
FORM_NAME_RE = r" · ACL [0-9a-f]{6}$"
FORM_PAGE_RE = r"^[0-9]{1,25}$"
FORM_LOCALE_RE = r"^[A-Z]{2}_[A-Z]{2}$"
FORM_URL_RE = r"^https://[^\s/?#:]+\.[^\s/?#:]+(:\d+)?([/?#]\S*)?$"
#: Ключи, которых нет нигде и на любой глубине (условия, токены, обходные пути).
FORM_FORBIDDEN_ANYWHERE = frozenset({"tos_accepted", "leadgen_tos_accepted", "tos", "terms",
                                     "terms_accepted", "access_token", "method", "_method", "batch",
                                     "suppress_http_code", "status", "execution_options"})


def policy_for_form_create(page: str = "") -> dict:
    """Таблица цепочки форм для страницы-помощника (JSON-сериализуемая)."""
    return {
        "edge": FORM_EDGE, "page": str(page or ""), "keys": sorted(FORM_KEYS), "bool_keys": sorted(FORM_BOOL_KEYS), "json_keys": sorted(FORM_JSON_KEYS),
        "nested": {k: list(v) for k, v in FORM_NESTED.items()},
        "question_types": list(FORM_QUESTION_TYPES), "button_types": list(FORM_BUTTON_TYPES),
        "card_styles": list(FORM_CARD_STYLES), "max_questions": FORM_MAX_QUESTIONS,
        "max_options": FORM_MAX_OPTIONS, "max_text": FORM_MAX_TEXT,
        "name_re": FORM_NAME_RE, "page_re": FORM_PAGE_RE, "locale_re": FORM_LOCALE_RE,
        "url_re": FORM_URL_RE, "forbidden": sorted(FORM_FORBIDDEN_ANYWHERE),
    }


def _form_forbidden_walk(value: Any, where: str) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            low = str(key).lower()
            parts = set(re.split(r"[^a-z0-9]+", low))
            if low in FORM_FORBIDDEN_ANYWHERE or parts & {"tos", "terms"} or "tos_accepted" in low:
                raise WriteRefused(f"write refused: key {key!r} is not allowed ({where}); "
                                   "the robot never accepts Lead Ads terms")
            _form_forbidden_walk(item, where)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _form_forbidden_walk(item, where)


def _form_text(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WriteRefused(f"write refused: {what} must be a non-empty string")
    if len(value) > FORM_MAX_TEXT:
        raise WriteRefused(f"write refused: {what} is too long")
    return value


def _form_url(value: Any, what: str) -> None:
    if not (isinstance(value, str) and re.match(FORM_URL_RE, value.strip(), re.I)):
        raise WriteRefused(f"write refused: {what} must be an https:// URL")


def _form_keys(obj: Any, kind: str, what: str) -> dict:
    if not isinstance(obj, dict):
        raise WriteRefused(f"write refused: {what} must be an object")
    extra = sorted(set(obj) - set(FORM_NESTED[kind]))
    if extra:
        raise WriteRefused(f"write refused: key {extra[0]!r} is not allowed in {what}")
    return obj


def _check_form_question(q: Any, i: int) -> None:
    where = f"questions[{i}]"
    q = _form_keys(q, "question", where)
    kind = q.get("type")
    if kind not in FORM_QUESTION_TYPES:
        raise WriteRefused(f"write refused: {where}.type {kind!r} is not allowed")
    if kind == "CUSTOM":
        _form_text(q.get("key"), f"{where}.key")
        _form_text(q.get("label"), f"{where}.label")
    elif set(q) != {"type"}:
        raise WriteRefused(f"write refused: a {kind} question carries only its type")
    if "options" in q:
        opts = q["options"]
        if kind != "CUSTOM" or not isinstance(opts, list) or not (1 <= len(opts) <= FORM_MAX_OPTIONS):
            raise WriteRefused(f"write refused: {where}.options is not allowed here")
        for j, o in enumerate(opts):
            o = _form_keys(o, "option", f"{where}.options[{j}]")
            _form_text(o.get("key"), f"{where}.options[{j}].key")
            _form_text(o.get("value"), f"{where}.options[{j}].value")


def check_form_create(page_id: str, method: str, path: str, params: dict | None,
                      files: dict | None = None, pinned_page: str = "") -> str:
    """Проверить создание формы лидов. Возвращает имя ребра; иначе `WriteRefused`.

    Только `POST /{page_id}/leadgen_forms` ТОЙ Страницы, что в плане; ключи из
    `FORM_KEYS`; условия Lead Ads и любой другой край Страницы - отказ.
    """
    if method != "POST":
        raise WriteRefused(f"write refused: method {method!r} (only POST; never DELETE)")
    if not re.match(FORM_PAGE_RE, str(page_id or "")):
        raise WriteRefused("write refused: the Page of the plan is not set")
    if not re.match(FORM_PAGE_RE, str(pinned_page or "")):
        raise WriteRefused("write refused: no Page is pinned for this session; form writes are off")
    if str(page_id) != str(pinned_page):
        raise WriteRefused("write refused: the Page is not the Page of the plan")
    if path != f"/{page_id}/{FORM_EDGE}":
        raise WriteRefused(f"write refused: path must be /{page_id}/{FORM_EDGE} (no other Page edges)")
    if files:
        raise WriteRefused("write refused: a form is created without files")
    params = dict(params or {})
    _form_forbidden_walk(params, FORM_EDGE)
    extra = sorted(set(params) - FORM_KEYS)
    if extra:
        raise WriteRefused(f"write refused: key {extra[0]!r} is not allowed on {FORM_EDGE}")
    for key in ("name", "questions", "locale"):
        if key not in params:
            raise WriteRefused(f"write refused: {key} is required")
    checked = dict(params)
    for key in FORM_JSON_KEYS & set(params):
        checked[key] = _parsed(params[key], f"{FORM_EDGE}.{key}")
    _form_forbidden_walk(checked, FORM_EDGE)
    name = _form_text(checked["name"], "name")
    if not re.search(FORM_NAME_RE, name) or len(name) > 200:
        raise WriteRefused("write refused: the form name must end with the robot label ' · ACL <hash>'")
    if not re.match(FORM_LOCALE_RE, str(checked["locale"])):
        raise WriteRefused("write refused: locale must look like EN_US")
    qs = checked["questions"]
    if not (isinstance(qs, list) and 1 <= len(qs) <= FORM_MAX_QUESTIONS):
        raise WriteRefused("write refused: questions must be a list of 1-%d" % FORM_MAX_QUESTIONS)
    for i, q in enumerate(qs):
        _check_form_question(q, i)
    if "question_page_custom_headline" in checked:
        _form_text(checked["question_page_custom_headline"], "question_page_custom_headline")
    if "follow_up_action_url" in checked:
        _form_url(checked["follow_up_action_url"], "follow_up_action_url")
        if len(checked["follow_up_action_url"]) > FORM_MAX_TEXT:
            raise WriteRefused("write refused: follow_up_action_url is too long")
    for key in FORM_BOOL_KEYS & set(checked):
        if not isinstance(checked[key], bool):
            raise WriteRefused(f"write refused: {key} must be true or false")
    if "privacy_policy" in checked:
        pp = _form_keys(checked["privacy_policy"], "privacy_policy", "privacy_policy")
        _form_url(pp.get("url"), "privacy_policy.url")
        _form_text(pp.get("link_text"), "privacy_policy.link_text")
    if "thank_you_page" in checked:
        ty = _form_keys(checked["thank_you_page"], "thank_you_page", "thank_you_page")
        _form_text(ty.get("title"), "thank_you_page.title")
        if "body" in ty:
            _form_text(ty["body"], "thank_you_page.body")
        if "button_type" in ty and ty["button_type"] not in FORM_BUTTON_TYPES:
            raise WriteRefused("write refused: thank_you_page.button_type is not allowed")
        if "button_text" in ty:
            _form_text(ty["button_text"], "thank_you_page.button_text")
        if "website_url" in ty:
            _form_url(ty["website_url"], "thank_you_page.website_url")
    if "context_card" in checked:
        cc = _form_keys(checked["context_card"], "context_card", "context_card")
        _form_text(cc.get("title"), "context_card.title")
        if cc.get("style") not in FORM_CARD_STYLES:
            raise WriteRefused("write refused: context_card.style is not allowed")
        content = cc.get("content")
        if not (isinstance(content, list) and content and all(isinstance(x, str) and x.strip() for x in content)):
            raise WriteRefused("write refused: context_card.content must be a list of texts")
    return FORM_EDGE


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
