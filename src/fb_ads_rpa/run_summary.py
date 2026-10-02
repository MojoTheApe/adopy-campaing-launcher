"""Итог каждой кампании — одним событием в Sentry (ACL-1447).

Зачем. До ACL-1447 в Sentry уезжали только отказы (`sentry_signal`), а
пошаговая телеметрия прогона оставалась на машине байера
(`%LOCALAPPDATA%\\ACL\\workspace\\output\\rpa_telemetry.jsonl`). Таблица
Google Sheets требует отдельного согласия на каждой машине и в установленных
сборках выключена. Итог: как прошёл прогон у байера — с предупреждениями,
медленными шагами, что прошло, — без присланного им архива не видно, а
большинство событий подписаны только именем компьютера (`LAPTOP-UA006`).

Что уходит. По кампании — одно событие уровня info (чисто, с
предупреждениями) или warning («не публиковать», остановлена); не error,
алертов не плодит. Отпечаток — вид итога (`acl-run-summary` + `clean` /
`warnings` / `do-not-publish` / `stopped`): четыре тикета на весь парк, а не
тикет на каждую кампанию. Внутри: релиз и машина (теги `acl_sentry.init`),
имя менеджера (тег `manager`), аккаунт, кампания, вид редактора (classic /
single-screen), шаги прогона (шаг, ok, длительность, причина),
предупреждения, общая длительность, число объявлений.

Имена (решение владельца 01.10): отображаемое имя аккаунта
(«FacebookDoc19») и имя кампании отправляются — и в событии, и тегами. Номера
кабинетов, профилей, адреса Ads Manager, токены и почта вычищаются
(`run_telemetry.scrub` + здесь же адреса и почта). Вычищенный раздел
страницы в `sentry_signal` по-прежнему с заглушками вместо имён — это
другое решение (ACL-1325), его итог не трогает.

Никогда не тормозит и не роняет прогон: отправка — в фоновом потоке, все
ошибки глотаются. Выключается `ACL_SENTRY_RUN_SUMMARY=0`.
"""
from __future__ import annotations

import os
import re
import threading
import time
from typing import Any, Iterable

from src.fb_ads_rpa import failure_policy
from src.fb_ads_rpa import run_telemetry as _telemetry

SWITCH = "ACL_SENTRY_RUN_SUMMARY"

CLEAN = "clean"
WARNINGS = "warnings"
DO_NOT_PUBLISH = "do-not-publish"
STOPPED = "stopped"
RESULTS = (CLEAN, WARNINGS, DO_NOT_PUBLISH, STOPPED)

CLASSIC = "classic"
SINGLE_SCREEN = "single-screen"
UNKNOWN = "unknown"

#: Потолки: событие — сводка, а не журнал.
MAX_STEPS = 150
MAX_TEXT = 200

#: Сверх `run_telemetry.scrub`: адрес Ads Manager целиком (в нём и номер
#: кабинета, и номер бизнеса, и выбранные строки), почта и `act_<номер>`
#: короче десяти цифр, который общий вычиститель пропускает.
_EXTRA_SCRUB = (
    (re.compile(r"https?://[^\s\"'<>]*(?:facebook|fb)\.com[^\s\"'<>]*", re.I), "<ads-manager-url>"),
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "<email>"),
    (re.compile(r"\bact_\d+\b"), "act_<hidden>"),
)


def enabled() -> bool:
    """Включено, пока `ACL_SENTRY_RUN_SUMMARY` не выключил явно."""
    value = (os.environ.get(SWITCH) or "").strip().lower()
    return value not in ("0", "false", "no", "off")


def scrub_text(text: Any) -> str:
    out = str(text or "")
    for pattern, replacement in _EXTRA_SCRUB:
        out = pattern.sub(replacement, out)
    return _telemetry.scrub(out)[:MAX_TEXT]


def result_kind(*, stopped_at: str = "", blocking: Iterable[str] = (),
                warnings: Iterable[str] = ()) -> str:
    """Вид итога. Остановка важнее блокера, блокер — предупреждения."""
    if stopped_at:
        return STOPPED
    if list(blocking or ()):
        return DO_NOT_PUBLISH
    if list(warnings or ()):
        return WARNINGS
    return CLEAN


def editor_kind(steps: list[dict[str, Any]]) -> str:
    """Новый редактор «в один экран» пишет шаги `simple_*` (ACL-1327)."""
    if not steps:
        return UNKNOWN
    if any(str(s.get("step") or "").startswith("simple_") for s in steps):
        return SINGLE_SCREEN
    return CLASSIC


def account_label(account: str) -> str:
    """Короткий ярлык аккаунта («FacebookDoc19»), как и тег `account`."""
    try:
        from src import acl_sentry

        return acl_sentry.account_tag(account)
    except Exception:
        parts = (account or "").strip().split()
        return parts[0] if parts else ""


def build(
    *,
    campaign: str,
    account: str = "",
    stopped_at: str = "",
    failures: Iterable[str] = (),
    steps: list[dict[str, Any]] | None = None,
    duration_s: float | None = None,
    ads: int = 0,
    attempts: int = 1,
    run_id: str = "",
) -> dict[str, Any]:
    """Итог кампании как словарь — то, что уедет в контекст `run_summary`.

    `failures` — отказы итога (`ad_soft_fails`); делятся на блокеры и
    предупреждения той же `failure_policy`, что и черновик.
    """
    try:
        blocking, warnings = failure_policy.split(list(failures or []))
    except Exception:
        blocking, warnings = list(failures or []), []
    steps_given = steps is not None
    steps = list(steps if steps_given else _telemetry.run_steps(run_id))
    result = result_kind(stopped_at=stopped_at, blocking=blocking, warnings=warnings)
    step_rows: list[dict[str, Any]] = []
    for s in steps:
        if s.get("ok") is None:
            continue  # информационные строки — не шаги
        row: dict[str, Any] = {"step": scrub_text(s.get("step")), "ok": bool(s.get("ok"))}
        if s.get("duration_s") is not None:
            row["duration_s"] = s["duration_s"]
        if s.get("why"):
            row["reason"] = scrub_text(s["why"])
        step_rows.append(row)
    truncated = max(0, len(step_rows) - MAX_STEPS)
    summary: dict[str, Any] = {
        "result": result,
        # Имя едет (решение владельца 01.10), но набранные в него почта,
        # `act_`-номер или адрес вычищаются здесь же, а не только общим
        # вычистителем в `before_send` (ревью PR #450, LOW).
        "campaign": scrub_text((campaign or "").strip()),
        "account": account_label(account),
        "editor": editor_kind(steps),
        "stopped_at": scrub_text(stopped_at),
        "blocking": [scrub_text(f) for f in blocking],
        "warnings": [scrub_text(f) for f in warnings],
        "steps": step_rows[:MAX_STEPS],
        "steps_failed": sum(1 for r in step_rows if not r["ok"]),
        "duration_s": round(float(duration_s), 1) if duration_s is not None else None,
        "ads": int(ads or 0),
        "attempts": int(attempts or 1),
        "run_id": run_id or ("" if steps_given else _telemetry.current_run_id()),
    }
    if truncated:
        summary["steps_truncated"] = truncated
    return summary


def _deliver(summary: dict[str, Any]) -> str:
    try:
        from src import acl_sentry

        return acl_sentry.report_run_summary(summary) or ""
    except Exception:
        return ""


def send(summary: dict[str, Any], *, background: bool = True) -> threading.Thread | str | None:
    """Отправить итог. В фоне — по умолчанию; ошибки не выходят наружу.

    Возвращает поток (фон), номер события (`background=False`) или `None`,
    если итог выключен.
    """
    if not enabled():
        return None
    if not background:
        return _deliver(summary)
    try:
        worker = threading.Thread(target=_deliver, args=(summary,),
                                  name="acl-run-summary", daemon=True)
        worker.start()
        return worker
    except Exception:
        return None


def report_finished(
    *,
    campaign: str,
    account: str = "",
    stopped_at: str = "",
    failures: Iterable[str] = (),
    ads: int = 0,
    started: float | None = None,
    attempts: int = 1,
    run_id: str = "",
    steps: list[dict[str, Any]] | None = None,
    background: bool = True,
) -> Any:
    """Кампания кончилась — собрать итог и отправить. Никогда не бросает.

    `steps=[]` — у кампании шагов не было (остановка при подключении профиля):
    шаги текущего прогона потока принадлежат предыдущей кампании.
    """
    try:
        if not enabled():
            return None
        duration = (time.monotonic() - started) if started is not None else None
        summary = build(campaign=campaign, account=account, stopped_at=stopped_at,
                        failures=failures, steps=steps, duration_s=duration, ads=ads,
                        attempts=attempts,
                        run_id=run_id or ("" if steps is not None else _telemetry.current_run_id()))
        return send(summary, background=background)
    except Exception:
        return None
