"""Что из отказов робота уходит в Sentry (ACL-1318).

До ACL-1318 в Sentry уезжала каждая строка телеметрии с `ok=False`. 25.09
список нерешённых тикетов состоял из диагностики, а не из поломок кампаний:
`advantage_plus_off: still-on` (36 событий — это предварительная проверка
значка до возраста, настоящая — `advantage_plus_audience` после него),
`click_target_covered: silent`, `create_took`, `conversion_website_pick`,
`locations_open`, строки попыток, которые потом удались, и общий
`ad_level_summary: silent`. Настоящий отказ `ad2_copy: copy-fields-not-revealed`
среди них терялся.

Теперь событие — только то, что портит кампанию:

* **остановка попытки** (`report_campaign_stop`) — `run_recorded_campaign_flow`
  вернул имя шага, на котором попытка кончилась. По событию на каждую
  кончившуюся попытку: три отказа `campaign_name` подряд — три события, и
  это правда о трёх попытках;
* **блокирующий отказ в итоге кампании** (`report_blocking_failures`) — то,
  из-за чего черновик помечен «DO NOT PUBLISH AS IS»
  (`failure_policy.split(...)[0]`). По событию на отказ, один раз на кампанию.

Исключение, которым кончилась работа задания, отправляет
`publish_jobs._run_rpa` через `acl_sentry.report_job_exception` (со стеком,
тем же вычистителем) — этот модуль его не касается. Всё остальное с
`ok=False` остаётся в локальной телеметрии.

Отправка никогда не роняет прогон: любые ошибки здесь глотаются.

ACL-1325: к обоим видам событий прикладывается вычищенный раздел страницы
упавшего шага (`page_capture.section_attachment`, решение владельца 26.09;
скриншоты — нет). Имена кампании, кабинета, групп и объявлений прогона
известны здесь и заменяются заглушками; выключается `ACL_SENTRY_PAGE_SNAPSHOT=0`.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

from src.fb_ads_rpa import failure_policy
from src.fb_ads_rpa import run_telemetry as _telemetry

CAMPAIGN_STOP = "campaign_stop"
BLOCKING = "blocking"

#: `ad2_copy`, `ad3_copy` → `ad_copy` для отпечатка: номер объявления —
#: номер, а номера в ключ группировки не едут.
_AD_INDEX_RE = re.compile(r"^ad\d+_")


#: Шаг, который отказывает молча, и строка, где причину пишет его помощник.
#: Реплей 25.09: `conversion_website` останавливал попытку без причины, а
#: строкой раньше `conversion_website_pick` писал `not_selected`;
#: `ad3_create` молчал, а `create_additional_ad` — `ad_name_field_not_visible`.
#: Таблица явная: чужую причину берём только у названного здесь помощника.
_RELATED: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = (
    (re.compile(r"^conversion_website$"), ("conversion_website_pick",)),
    (re.compile(r"^further_limit_reach_radio$"), ("pick_further_limit_radio",)),
    (re.compile(r"^ad\d*_create$"), ("create_additional_ad",)),
    # ACL-1348 (PYTHON-14 `ad2_creative_upload: silent`): причину пишут
    # помощники. Запасной путь — для строк шага без своей причины (прогоны
    # старых версий); с ACL-1348 поток пишет причину в строку шага сам, и
    # она выигрывает: строка помощника общая для всех объявлений кампании.
    (re.compile(r"^ad\d*_creative_upload$"), ("ad_creative_upload", "ad_creative_reuse")),
)


def _seen(step: str, run_id: str) -> dict[str, Any]:
    """Причина и снимок последней строки шага; молчит сам — спросить помощника."""
    own = _telemetry.last_failure(step, run_id) or {}
    if own.get("why"):
        return own
    for pattern, helpers in _RELATED:
        if not pattern.match(step):
            continue
        for helper in helpers:
            found = _telemetry.last_failure(helper, run_id) or {}
            if found.get("why"):
                diag = dict(own.get("diag") or found.get("diag") or {})
                diag["reason_from_step"] = helper
                return {"why": found["why"], "diag": diag}
    return own


def group_step(step: str) -> str:
    """Шаг без номера объявления — то, по чему складываются одинаковые отказы."""
    return _AD_INDEX_RE.sub("ad_", step or "")


def _send(
    failure: str,
    *,
    kind: str,
    campaign: str,
    account: str,
    run_id: str,
    attempt: int | None = None,
    names: Iterable[str] = (),
    attach_section: bool = True,
) -> str:
    from src import acl_sentry

    step = failure_policy.base_name(failure)
    seen = _seen(step, run_id)
    why = seen.get("why") or ""
    reason = acl_sentry.failure_reason(failure, why)
    diag: dict[str, Any] = {}
    if seen.get("diag"):
        diag.update(seen["diag"])
    if failure != step:
        # Полный текст отказа из итога — в контексте, не в отпечатке: в нём
        # бывают имена объявлений и номера.
        diag["failure"] = failure
    if kind == BLOCKING:
        diag["policy"] = failure_policy.reason(failure)
    attachment = (_section(step, run_id, campaign=campaign, account=account, names=names)
                  if attach_section else None)
    return acl_sentry.report_step_failure(
        step,
        why or failure,
        run_id=run_id,
        campaign=campaign,
        account=account,
        diag=diag or None,
        reason=reason,
        job=_telemetry.current_job_id(),
        kind=kind,
        group_step=group_step(step),
        attempt=attempt,
        attachment=attachment,
    ) or ""


def _section(step: str, run_id: str, *, campaign: str, account: str,
             names: Iterable[str]) -> dict[str, Any] | None:
    """Вычищенный раздел страницы шага — или его помощника из `_RELATED`."""
    try:
        from src.fb_ads_rpa import page_capture

        related = [helper for pattern, helpers in _RELATED if pattern.match(step) for helper in helpers]
        return page_capture.section_attachment(
            step, run_id, campaign=campaign, account=account, names=names, related=related,
        )
    except Exception:
        return None


def draft_names(draft: Any) -> list[str]:
    """Имена групп и объявлений черновика — их нет в разделе, который уезжает."""
    names: list[str] = []
    try:
        for ad_set in getattr(draft, "ad_sets", None) or []:
            names.append(str(getattr(ad_set, "name", "") or ""))
            for ad in getattr(ad_set, "ads", None) or []:
                try:
                    names.append(str(ad.ad_name() or ""))
                except Exception:
                    continue
    except Exception:
        pass
    return [n for n in names if n]


def report_step_stuck(step: str, idle_s: float, *, job: str = "", account: str = "",
                      campaign: str = "") -> str:
    """Задание молчит дольше порога — одно событие на простой (ACL-1614)."""
    try:
        from src import acl_sentry

        return acl_sentry.report_step_stuck(step, idle_s, job=job, account=account,
                                            campaign=campaign) or ""
    except Exception:
        return ""


def report_tab_unblocked(step: str, method: str, stuck_s: float, *, ok: bool, job: str = "",
                         account: str = "", campaign: str = "", api: str = "", where: str = "",
                         tried: list | None = None) -> str:
    """Сторож разблокировал зависшую вкладку — одно событие (ACL-1702)."""
    try:
        from src import acl_sentry

        return acl_sentry.report_tab_unblocked(step, method, stuck_s, ok=ok, job=job, account=account,
                                               campaign=campaign, api=api, where=where,
                                               tried=tried) or ""
    except Exception:
        return ""


def report_campaign_stop(
    fail_step: str,
    *,
    campaign: str = "",
    account: str = "",
    run_id: str = "",
    attempt: int | None = None,
    names: Iterable[str] = (),
    attach_section: bool = True,
) -> str:
    """Попытка кампании кончилась на шаге `fail_step` — одно событие.

    `attach_section=False` — остановка, у которой страницы кампании не было
    (профиль не подключился): раздел страницы к ней не прикладывается
    (ревью PR #328, F2).
    """
    if not fail_step:
        return ""
    try:
        return _send(
            fail_step,
            kind=CAMPAIGN_STOP,
            campaign=campaign or _telemetry.current_campaign(),
            account=account,
            run_id=run_id or _telemetry.current_run_id(),
            attempt=attempt,
            names=names,
            attach_section=attach_section,
        )
    except Exception:
        return ""


def report_blocking_failures(
    failures: list[str],
    *,
    campaign: str = "",
    account: str = "",
    run_id: str = "",
    names: Iterable[str] = (),
) -> list[str]:
    """По событию на каждый блокирующий отказ итога кампании.

    Предупреждения (`failure_policy` → warning) не уходят: черновик с ними
    публикуем. Список зовут один раз на кампанию — по итогу выжившей попытки.
    """
    sent: list[str] = []
    try:
        blocking, _warnings = failure_policy.split(list(failures or []))
    except Exception:
        return sent
    run_id = run_id or _telemetry.current_run_id()
    campaign = campaign or _telemetry.current_campaign()
    for failure in blocking:
        try:
            sent.append(_send(failure, kind=BLOCKING, campaign=campaign,
                              account=account, run_id=run_id, names=names))
        except Exception:
            sent.append("")
    return sent
