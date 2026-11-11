"""ACL-2353: «Before create review» — отпечаток кампании, разбор `review_json`, события.

Чистые функции без сети и без профиля. Страница показывает ревью в окне подтверждения
(ACL-2118) и шлёт на `/publish-jobs` снимок формы + `review_json`; сервер строит черновики
ОДИН раз, сверяет отпечатки и дальше публикует именно эти черновики (замороженные).
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from typing import Any

from src.web.plan_cache import _file_fingerprint as _file_stat  # same (path, size, mtime_ns) mark as the plan cache

#: Версия формата `review_json`.
REVIEW_VERSION = 1
#: Коды отказов (в ответе `/publish-jobs` и в строке аккаунта).
CODE_STALE = "review_stale"
#: Потолок размера `review_json` (защита от мусора; обычный — единицы килобайт).
MAX_REVIEW_BYTES = 256 * 1024


class ReviewStale(Exception):
    """Все кампании аккаунта изменились после ревью: задания нет."""

    def __init__(self, changed: list[str]) -> None:
        super().__init__("Campaigns changed after the review: " + ", ".join(changed))
        self.changed = list(changed)
        self.code = CODE_STALE


class ReviewBad(ValueError):
    """`review_json` не разобрать (не JSON, не та версия, не те поля)."""


def stale_text(names: list[str]) -> str:
    """Текст строки аккаунта, когда кампании изменились после ревью (английский, по правилу)."""
    return ("Not created: " + ", ".join(names) + " changed after the review "
            "(library text, form template, account map or file). Review again.")


def publish_stale_text(name: str) -> str:
    """Тот же отказ, замеченный в `publish_draft` (за время очереди сменился шаблон формы или файл)."""
    return f"Not created: {name} changed after the review (form template or file). Nothing was created."


def review_fingerprint(plan: Any) -> str:
    """sha256 канонического JSON плана без `warnings` + (путь, размер, mtime_ns) файлов.

    План уже содержит разобранные тексты, ссылку, Page и пиксель карты, площадки и весь словарь
    шаблона формы. Содержимое файлов не хэшируется (видео в 1,5 ГБ на каждый просмотр дороже, чем
    защищает): замену файла ловят размер и mtime.
    """
    try:
        body = dataclasses.asdict(plan) if dataclasses.is_dataclass(plan) else dict(plan)
    except Exception:  # noqa: BLE001 - отпечаток не должен ронять публикацию
        body = {"repr": repr(plan)}
    body.pop("warnings", None)
    files = []
    for ad_set in getattr(plan, "ad_sets", None) or []:
        for ad in getattr(ad_set, "ads", None) or []:
            files.append([_file_stat(getattr(ad, "path", None)), _file_stat(getattr(ad, "vertical_path", None))])
    raw = json.dumps({"plan": body, "files": files}, sort_keys=True, ensure_ascii=False,
                     default=repr, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def fingerprints(drafts: list[Any]) -> dict[str, str]:
    """{имя кампании: отпечаток}. Черновик, из которого не выходит план, в словарь не попадает."""
    from src.fb_api.creator import PlanError, plan_from_draft

    out: dict[str, str] = {}
    for draft in drafts or []:
        name = str(getattr(draft, "name", "") or "")
        if not name:
            continue
        try:
            out[name] = review_fingerprint(plan_from_draft(draft))
        except PlanError:
            continue
        except Exception:  # noqa: BLE001
            continue
    return out


def parse_review_json(text: str) -> dict[str, Any]:
    """Разобрать поле `review_json`; нарушение формата - `ReviewBad`."""
    raw = str(text or "")
    if len(raw.encode("utf-8")) > MAX_REVIEW_BYTES:
        raise ReviewBad("review_json is too large")
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ReviewBad("review_json is not valid JSON") from exc
    if not isinstance(data, dict) or data.get("v") != REVIEW_VERSION:
        raise ReviewBad("review_json has an unknown version")
    campaigns = data.get("campaigns")
    if not isinstance(campaigns, dict) or not campaigns:
        raise ReviewBad("review_json has no campaigns")
    clean: dict[str, dict[str, str]] = {}
    for name, entry in campaigns.items():
        if not isinstance(entry, dict) or not str(entry.get("fp") or ""):
            raise ReviewBad("review_json has a campaign without a fingerprint")
        clean[str(name)] = {"fp": str(entry.get("fp")),
                            "page_id": str(entry.get("page_id") or ""),
                            "pixel_id": str(entry.get("pixel_id") or "")}
    return {
        "v": REVIEW_VERSION,
        "campaigns": clean,
        "warnings": {str(k): int(v) for k, v in (data.get("warnings") or {}).items()
                     if isinstance(v, (int, float))} if isinstance(data.get("warnings"), dict) else {},
        "blocked": {str(k): int(v) for k, v in (data.get("blocked") or {}).items()
                    if isinstance(v, (int, float))} if isinstance(data.get("blocked"), dict) else {},
        "checked": _int(data.get("checked")),
        "not_checked": _int(data.get("not_checked")),
        "open_s": _int(data.get("open_s")),
    }


def _int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def check_submit(drafts: list[Any], review: dict[str, Any]) -> tuple[list[Any], list[str]]:
    """Сверить свежесобранные черновики с отпечатками ревью.

    Возвращает (черновики, что совпали; имена изменённых). Кампания, которой нет в ревью, или с другим
    отпечатком - изменённая (D5: отказ только по ней, остальные идут). Имена совпадений и отказов
    берутся из черновиков: ключ ревью - имя кампании из билдера.
    """
    reviewed = review.get("campaigns") or {}
    now = fingerprints(drafts)
    kept: list[Any] = []
    changed: list[str] = []
    for draft in drafts or []:
        name = str(getattr(draft, "name", "") or "")
        want = (reviewed.get(name) or {}).get("fp")
        if want and now.get(name) == want:
            kept.append(draft)
        else:
            changed.append(name)
    return kept, changed


def ids_mismatch(review: dict[str, Any] | None, name: str, *, page_id: str, pixel_id: str) -> str:
    """'' - Page и пиксель те же, что в ревью (или в ревью они не были известны); иначе - текст отказа.

    Пустое значение ревью («Automatic», «account default», проверки не было) ничего не утверждает.
    """
    entry = ((review or {}).get("campaigns") or {}).get(name) or {}
    old_page = str(entry.get("page_id") or "")
    old_pixel = str(entry.get("pixel_id") or "")
    if old_page and page_id and old_page != page_id:
        return f"Not created: the Page changed after the review ({old_page} → {page_id}). Nothing was created."
    if old_pixel and pixel_id and old_pixel != pixel_id:
        return f"Not created: the pixel changed after the review ({old_pixel} → {pixel_id}). Nothing was created."
    return ""
