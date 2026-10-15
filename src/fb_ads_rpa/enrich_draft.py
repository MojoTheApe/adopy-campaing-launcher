"""Resolve Upload FB + creatives folder into CampaignDraft fields."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from config.paths import get_ad_account_map_path, get_creatives_root, get_upload_fb_excel_path
from src.meta_campaign_agent.ad_account_map import load_ad_account_map, resolve_account_entry
from src.meta_campaign_agent.creative_finder import AmbiguousCreativeError, CreativeNotFoundError, find_creative_file
from src.upload_agent.build_upload_excel import (
    get_account_info,
    load_reference_data,
    lookup_country,
    lookup_creative_file,
    lookup_interest,
)
from src.web.ad_copy import lookup_ad_copy_by_title


def _interest_names_from_targeting(ref: dict, targeting_name: str) -> str:
    """Resolve a named targeting profile (e.g. "Day") to a comma-separated
    list of real Meta interest/behavior names, for typing one at a time into
    the browser's Detailed Targeting search box.

    "targeting" in the slot/plan data is a KEY into the Upload FB.xlsx
    "Interests" sheet (see lookup_interest), not literal text to type —
    confirmed live 2026-07-25: "Day" resolves to a JSON blob of real
    interests like "Swing trading"/"Day trading"/"Stock market index". The
    RPA browser path needs plain names (it types each one into a search
    box and picks an autocomplete suggestion), unlike the Graph-API publish
    path which can pass this same JSON directly as a structured spec.
    """
    targeting_name = (targeting_name or "").strip()
    if not targeting_name:
        return ""
    raw = lookup_interest(ref, targeting_name)
    if not raw:
        return targeting_name
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return targeting_name
    names: list[str] = []
    groups = parsed if isinstance(parsed, list) else [parsed]
    for group in groups:
        if not isinstance(group, dict):
            continue
        for bucket in ("interests", "behaviors"):
            for item in group.get(bucket) or []:
                name = (item or {}).get("name")
                if name:
                    names.append(str(name).strip())
    return ", ".join(names) if names else targeting_name

_GCC_ARABIC_COUNTRIES = {"KW", "SA", "AE", "BH", "OM", "QA"}
_LATAM_SPANISH_COUNTRIES = {
    "CL", "CO", "EC", "MX", "CR", "PE", "AR", "PY", "SV", "GT", "BO", "DO", "PA", "UY",
}


def _geo_language_for_country(country_code: str) -> str:
    """Business rule: GCC/Arabic countries -> Arabic, LatAm -> Spanish (all).

    Confirmed with user (2026-07-27): this account only ever runs these two
    campaign families, so the country code alone reliably determines the
    ad's audience language — used as the fallback when the Upload FB.xlsx
    "Countries" sheet doesn't have an explicit language value for that row.
    """
    # Pass the base name only — fill_audience_languages() already searches
    # for a "<name> (all)" suggestion first and falls back to the suggestion
    # that is exactly "<name>" (confirmed live: Arabic has no such variant).
    # Регионального варианта «<name> (Egypt)» шаг не берёт никогда (25.09).
    c = (country_code or "").strip().upper()
    if c in _GCC_ARABIC_COUNTRIES:
        return "Arabic"
    if c in _LATAM_SPANISH_COUNTRIES:
        return "Spanish"
    return ""


_ref_cache: dict | None = None
_ref_cache_mtime: float = -1.0
_ref_cache_lock = __import__("threading").Lock()


def _get_ref(excel_path: str) -> dict:
    global _ref_cache, _ref_cache_mtime
    try:
        mtime = os.path.getmtime(excel_path)
    except OSError:
        return {}
    with _ref_cache_lock:
        if _ref_cache is not None and mtime == _ref_cache_mtime:
            return _ref_cache
        try:
            ref = load_reference_data(excel_path)
        except Exception as exc:
            return {"_load_error": str(exc)}
        _ref_cache = ref
        _ref_cache_mtime = mtime
        return ref


#: ACL-1313: библиотеку, которую ЭТОТ ЖЕ шаг стянул меньше минуты назад, повторно
#: не тянем. Кнопка Publish в дешборде сначала зовёт /preview-publish, и тот
#: уже делает здесь `pull(force=True)` обоих реестров; сразу за ним то же
#: самое делал запуск задания — два сетевых похода к GitHub на старте каждого
#: задания. Минута — больше, чем проходит между предпросмотром и запуском, и
#: меньше, чем нужно, чтобы кто-то успел поправить цель или текст.
#:
#: Считаются только наши принудительные походы, а не любое обновление копии:
#: своя запись в библиотеку тоже обновляет её, но чужих правок после неё не
#: приносит — после неё запуск тянет, как прежде.
LIBRARY_FRESH_S = 60.0

#: kind → time.monotonic() последнего удачного `pull(force=True)` отсюда.
_forced_pull_at: dict[str, float] = {}


def _pull_unless_just_pulled(kind: str, pull, info: dict) -> dict:
    """`pull(force=True)`, если этот шаг не тянул библиотеку `kind` < минуты назад.

    Когда тянул — удачно, в этом же процессе, — отдаётся статус копии без
    похода в сеть (`team_library` — код дешборда, его не меняем).
    """
    import time as _time

    last = _forced_pull_at.get(kind)
    if last is not None and _time.monotonic() - last < LIBRARY_FRESH_S:
        try:
            from src.analysis_agent import team_library

            state = team_library.status(kind)
        except Exception:
            state = {}
        if state.get("ok"):
            info.setdefault("fresh", []).append(kind)
            return state
    info.setdefault("pulled", []).append(kind)
    state = pull(force=True)
    if (state or {}).get("ok"):
        _forced_pull_at[kind] = _time.monotonic()
    else:
        _forced_pull_at.pop(kind, None)
    return state


def enrich_campaign_drafts(
    drafts: list[Any],
    *,
    account_label: str,
    upload_fb_path: str | None = None,
    ad_account_map_path: str | None = None,
    creatives_root: str | None = None,
    preview_mode: bool = False,
) -> list[Any]:
    """Fill link, pixel, page_id, creative paths, ad copy on each draft."""
    excel_path = (upload_fb_path or "").strip() or get_upload_fb_excel_path()
    map_path = (ad_account_map_path or "").strip() or get_ad_account_map_path()
    root = (creatives_root or "").strip() or get_creatives_root()

    ref: dict = {}
    if os.path.isfile(excel_path):
        ref = _get_ref(excel_path)

    account_map = load_ad_account_map(map_path)
    entry = resolve_account_entry(account_map, account_label)
    account_info = get_account_info(ref, account_label, account_map=account_map)

    # A publish must see changes made since the builder opened, even inside
    # the per-ad read throttle. Refresh each needed registry once per batch.
    from src.analysis_agent import targeting_registry, ad_text_registry
    from src.fb_ads_rpa import run_telemetry as _telemetry
    library_states = []
    with _telemetry.phase("library_pull", diag={"preview": bool(preview_mode)}) as info:
        if any(getattr(a, "targeting_label", "") for d in drafts for a in d.ad_sets):
            library_states.append(("Targeting", _pull_unless_just_pulled(
                "targets", targeting_registry.pull, info)))
        if any(ad.copy_name or ad.copy2_name for d in drafts for a in d.ad_sets for ad in a.ads):
            library_states.append(("Ad texts", _pull_unless_just_pulled(
                "ad_texts", ad_text_registry.pull, info)))

    for draft in drafts:
        for label, state in library_states:
            if state.get("connected") and not state.get("ok"):
                stamp = state.get("synced_at") or "unknown time"
                draft.warnings.append(f"{label}: team library unavailable; using the saved copy from {stamp}. "
                                      + state.get("error", ""))
        draft.ad_account_id = entry.ad_account_id if entry else None
        # ACL-1766: что уже в черновике, то и берём; карта — следующий источник.
        # Источник запоминаем для «Check plan»; RPA эти поля не читает.
        draft.page_source = "draft" if draft.page_id else ("map" if entry and entry.page_id else "")
        draft.pixel_source = "draft" if draft.pixel_id else ("map" if entry and getattr(entry, "pixel_id", None) else "")
        draft.page_id = draft.page_id or (entry.page_id if entry else None)
        draft.pixel_id = draft.pixel_id or ((getattr(entry, "pixel_id", None) or "") if entry else "")
        # Что набрано в билдере, то и берём. Раньше строка ниже перетирала
        # ссылку значением из книги безусловно: человек вписывал её в форму,
        # видел «Link empty» и не мог понять, куда она делась.
        draft.link = str(draft.link or "").strip()
        if not draft.link and not getattr(draft, "link_provided", False):
            draft.link = str(account_info.get("link") or "").strip()
        # Язык — так же: пришёл из формы, значит его и выбрали. Правило по
        # стране осталось умолчанием формы, а не заменой ответа человека.
        # Книга спрашивается последней и только за страны вне GCC и LatAm —
        # для них правило и так побеждало её всегда.
        if not str(getattr(draft, "language", "") or "").strip() and not getattr(draft, "language_provided", False):
            excel_language = str(lookup_country(ref, draft.country).get("language") or "").strip() if ref else ""
            draft.language = _geo_language_for_country(draft.country) or excel_language

        if ref.get("_load_error"):
            draft.warnings.append(f"Upload FB.xlsx not loaded: {ref['_load_error']}")
        if not entry:
            draft.errors.append("Account not in ad_account_map.csv — add row and re-run")
            # remaining checks require entry; skip to avoid duplicate noise
            continue
        if not entry.ad_account_id:
            draft.errors.append("ad_account_id not mapped in ad_account_map.csv")
        # Черновик без адреса — это и есть черновик: плеер печатает ссылку как
        # `d.link or None`, пустую просто не набирает. Требовать её значило
        # запрещать то, ради чего RPA и существует. Договорённость с
        # пользователем 8 сентября: подсветить и предупредить, но не запрещать.
        # Соседний `page_id` двумя строками ниже устроен так с самого начала.
        if not draft.link:
            if draft.status == "DRAFT":
                draft.warnings.append("Tracking link is empty; add it to the draft before publishing in Meta")
            else:
                draft.errors.append("Link empty in Upload FB Accounts sheet")
        # Пусто здесь бывает по двум причинам: язык сняли намеренно или для
        # этой страны у нас нет правила. Обе разрешены, обе стоит назвать: шаг
        # выбора языка в Ads Manager просто не выполнится.
        if not str(getattr(draft, "language", "") or "").strip():
            draft.warnings.append("audience language not set — the Language step will be skipped")
        # page_id is only required for ACTIVE campaigns; drafts publish without it
        if not draft.page_id:
            draft.warnings.append("page_id not set — required for ACTIVE campaigns only")
        if not entry.profile_id:
            if preview_mode:
                draft.warnings.append("profile_id missing in ad_account_map.csv (set via ML panel before publishing)")
            else:
                draft.errors.append("profile_id missing in ad_account_map.csv (browser session)")

        for ad_set in draft.ad_sets:
            if getattr(ad_set, "targeting_label", ""):
                # Источник один — библиотека таргетов на этой машине. Книга
                # запасным ходом больше не служит: она подставляла интересы,
                # которых человек не выбирал, и делала это предупреждением, а
                # не вопросом. Не нашлось — так и сказать; кампания при этом
                # поднимается, просто без детального таргетинга.
                resolved = ""
                try:
                    from src.analysis_agent.targeting_registry import resolve_interests

                    resolved = resolve_interests(ad_set.targeting_label)
                except Exception as exc:
                    draft.errors.append(f"Targeting library could not resolve {ad_set.targeting_label!r}: {exc}")
                    continue
                if resolved:
                    ad_set.targeting_label = resolved
                else:
                    draft.warnings.append(
                        f"targeting {ad_set.targeting_label!r} is not in the library — "
                        "the campaign will be raised without detailed targeting"
                    )
                    ad_set.targeting_label = ""

            for ad in ad_set.ads:
                # If creative_filename was set directly from folder scan — skip Excel lookup
                if not ad.creative_filename:
                    if ref:
                        ad.creative_filename = lookup_creative_file(ref, ad.creative_type, ad.creative_id) or ""
                    if not ad.creative_filename:
                        draft.errors.append(
                            f"Creative file not mapped for {ad.creative_id or '?'} "
                            f"(pick a file from the folder or set creative_id in Upload FB)"
                        )

                # Resolve against the same local library shown by the builder.
                # Missing/deleted copy must be corrected before opening a browser.
                if ad.copy_name:
                    from src.analysis_agent.ad_text_registry import AdTextError
                    try:
                        copy = lookup_ad_copy_by_title(ad.copy_name)
                    except AdTextError as exc:
                        draft.errors.append(f"Ad text {ad.copy_name!r}: {exc}")
                        continue
                    ad.ad_title = (copy.get("headline") or ad.copy_name or "").strip()
                    ad.ad_body = (copy.get("body") or "").strip()
                    ad.ad_link_description = (copy.get("description") or "").strip()
                    if not ad.ad_body:
                        draft.warnings.append(
                            f"ad text {ad.copy_name!r} has no body — check it in Ad titles"
                        )

                # ACL-1344: второй вариант основного текста — body другой записи
                # той же библиотеки. Нет записи — ошибка до открытия браузера,
                # как у первого текста; пустой или повтор первого — предупреждение,
                # и объявление идёт с одним текстом, как раньше.
                if ad.copy2_name:
                    from src.analysis_agent.ad_text_registry import AdTextError
                    try:
                        copy2 = lookup_ad_copy_by_title(ad.copy2_name)
                    except AdTextError as exc:
                        draft.errors.append(f"Primary text 2 {ad.copy2_name!r}: {exc}")
                        continue
                    ad.ad_body2 = (copy2.get("body") or "").strip()
                    if ad.ad_body2 and not ad.ad_body:
                        # Ревью PR #346: без основного текста второй вариант
                        # некуда добавить — говорим об этом до браузера.
                        draft.warnings.append(
                            f"Primary text 2 {ad.copy2_name!r} needs an Ad Title with a primary text — ignored"
                        )
                        ad.ad_body2 = ""
                    elif not ad.ad_body2:
                        draft.warnings.append(
                            f"Primary text 2 {ad.copy2_name!r} has no body — the ad gets one primary text"
                        )
                    elif ad.ad_body and "".join(ad.ad_body2.split()) == "".join(ad.ad_body.split()):
                        draft.warnings.append(
                            f"Primary text 2 {ad.copy2_name!r} repeats the first primary text — ignored"
                        )
                        ad.ad_body2 = ""

                if ad.creative_filename and not preview_mode:
                    try:
                        ad.creative_path = str(find_creative_file(root, ad.creative_filename, fresh=True))
                    except (CreativeNotFoundError, AmbiguousCreativeError, FileNotFoundError) as exc:
                        draft.errors.append(str(exc))
                elif ad.creative_filename:
                    ad.creative_path = str(Path(root) / ad.creative_filename)

                if not ad.ad_title:
                    draft.warnings.append(f"Ad title empty for creative {ad.creative_id or '?'}")

    return drafts
