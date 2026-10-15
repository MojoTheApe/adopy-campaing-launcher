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

Вложение (ACL-1554). К итогу прикладываются строки `rpa_telemetry.jsonl`
этого прогона — `run_telemetry_<run_id>.jsonl`, каждая через `scrub_diag` и
`_EXTRA_SCRUB`, не больше `TELEMETRY_MAX_BYTES` (сверх — выброшены самые
старые и строка-метка). Что вышло — в контексте `telemetry_attachment`:
`ok` / `truncated` / `missing` / `error`. Скриншоты не прикладываются.

Никогда не тормозит и не роняет прогон: отправка — в фоновом потоке, все
ошибки глотаются. Выключается `ACL_SENTRY_RUN_SUMMARY=0`.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
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
    # ACL-1554: имя пользователя в пути (`C:\Users\ivan.petrov\…`, короткое
    # 8.3 `EVGENI~1`, UNC `\\srv\Users\…`, `/Users/…`, `/home/…`) — по нему
    # опознаётся человек. Имя берётся до следующего разделителя пути, а не до
    # пробела: локальная учётка Windows часто «Имя Фамилия» (ревью PR #556,
    # F1). Сам путь остаётся: «AppData\Local\ACL\…» полезен для разбора.
    (re.compile(r"(?i)([\\/](?:Users|Documents and Settings|home)[\\/]+)"
                r"[^\\/\"'<>\r\n]+"), r"\1<user>"),
)

#: ACL-1554: строки телеметрии прогона — вложением к итогу.
TELEMETRY_MAX_BYTES = 256 * 1024
TELEMETRY_OK = "ok"
TELEMETRY_MISSING = "missing"
TELEMETRY_ERROR = "error"
TELEMETRY_TRUNCATED = "truncated"
TELEMETRY_MARKER_STEP = "telemetry_truncated"


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


def _scrub_strings(value: Any) -> Any:
    """Адреса, почта, `act_`-номера и пути с именем — в каждой строке, на любой
    глубине. Без обрезки `MAX_TEXT`: это журнал, его потолок — весь файл."""
    if isinstance(value, dict):
        # Ключи тоже (ревью PR #556, F3): диагностика бывает словарём «номер
        # кабинета → состояние». Только во вложении — общий `scrub_diag` не
        # трогаем. Совпавшие после вычистки ключи не затирают друг друга.
        out_dict: dict[Any, Any] = {}
        for key, item in value.items():
            clean_key = _scrub_strings(key) if isinstance(key, str) else key
            if clean_key in out_dict:
                n = 2
                while f"{clean_key}#{n}" in out_dict:
                    n += 1
                clean_key = f"{clean_key}#{n}"
            out_dict[clean_key] = _scrub_strings(item)
        return out_dict
    if isinstance(value, (list, tuple)):
        return [_scrub_strings(item) for item in value]
    if isinstance(value, str):
        out = value
        for pattern, replacement in _EXTRA_SCRUB:
            out = pattern.sub(replacement, out)
        return _telemetry.scrub(out)
    return value


def _attachment_row(row: dict[str, Any], run_id: str) -> dict[str, Any]:
    """Одна строка журнала, вычищенная тем же `scrub_diag`, что и контексты
    Sentry, и сверх того — как текст итога (`_EXTRA_SCRUB`)."""
    job = row.get("job")
    clean = _scrub_strings(_telemetry.scrub_diag(row))
    if not isinstance(clean, dict):
        raise TypeError("telemetry row is not a dict")
    # Номер прогона — наш, он и так в контексте итога; вычиститель мог бы
    # принять его за `<digits>`.
    clean["run_id"] = run_id
    if job:
        # Номер задания — наш uuid4, как в `acl_sentry.report_step_failure`:
        # без дефисов он остаётся сопоставимым с журналом дешборда.
        clean["job"] = _telemetry.scrub(str(job).replace("-", ""))
    return clean


def _run_rows(run_id: str, path: Path) -> list[dict[str, Any]]:
    """Строки журнала только этого прогона, в порядке записи.

    Журнал на машине менеджера растёт месяцами — разбирается только строка,
    в которой вообще есть номер прогона.
    """
    needle = json.dumps(run_id)
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if needle not in line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue  # недописанная строка — не повод терять остальные
            if isinstance(rec, dict) and rec.get("run_id") == run_id:
                rows.append(rec)
    return rows


def _marker(run_id: str, dropped: int, cap: int) -> bytes:
    rec = {"run_id": run_id, "step": TELEMETRY_MARKER_STEP, "ok": None,
           "dropped_rows": dropped,
           "note": f"{dropped} oldest rows dropped to keep the attachment under {cap} bytes"}
    return (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8")


def telemetry_attachment(run_id: str, path: Path | None = None) -> tuple[dict[str, Any] | None, str]:
    """Вложение `run_telemetry_<run_id>.jsonl` для итога (ACL-1554) и его статус.

    `ok` — все строки прогона; `truncated` — сверх `TELEMETRY_MAX_BYTES`
    выброшены самые старые, последняя строка-метка говорит, сколько;
    `missing` — нет номера прогона, журнала или строк; `error` — журнал не
    прочитался или строка не вычистилась (тогда без вложения: невычищенное
    не уезжает). Никогда не бросает. Скриншоты не прикладываются никогда —
    только текст журнала.
    """
    try:
        run_id = str(run_id or "").strip()
        if not run_id:
            return None, TELEMETRY_MISSING
        p = path or _telemetry.TELEMETRY_PATH
        if not p.exists():
            return None, TELEMETRY_MISSING
        rows = _run_rows(run_id, p)
        if not rows:
            return None, TELEMETRY_MISSING
        lines = [(json.dumps(_attachment_row(r, run_id), ensure_ascii=False) + "\n").encode("utf-8")
                 for r in rows]
        cap = int(TELEMETRY_MAX_BYTES)
        total = sum(len(b) for b in lines)
        dropped = 0
        if total > cap:
            # Сначала выбрасываются самые старые: конец прогона — где отказ.
            while lines and total + len(_marker(run_id, dropped, cap)) > cap:
                total -= len(lines.pop(0))
                dropped += 1
            lines.append(_marker(run_id, dropped, cap))
        payload = b"".join(lines)
        if len(payload) > cap:
            return None, TELEMETRY_ERROR
        safe_id = re.sub(r"[^A-Za-z0-9_]", "", run_id)[:64] or "run"
        attachment = {
            "bytes": payload,
            "filename": f"run_telemetry_{safe_id}.jsonl",
            "content_type": "application/x-ndjson",
        }
        return attachment, (TELEMETRY_TRUNCATED if dropped else TELEMETRY_OK)
    except Exception:
        return None, TELEMETRY_ERROR


def _deliver(summary: dict[str, Any]) -> str:
    # ACL-1554: журнал читается здесь, в фоновом потоке отправки, — не на
    # пути прогона. Любой отказ — итог уходит без вложения, со статусом.
    attachment: dict[str, Any] | None = None
    status = TELEMETRY_ERROR
    try:
        from src import acl_sentry

        # Sentry выключен (нет адреса) — итог никуда не уйдёт, журнал не
        # читаем зря (ревью PR #556, F5).
        if not acl_sentry.dsn():
            return ""
    except Exception:
        return ""
    try:
        attachment, status = telemetry_attachment(str(summary.get("run_id") or ""))
    except Exception:
        attachment, status = None, TELEMETRY_ERROR
    try:
        summary = {**summary, "telemetry_attachment": status}
    except Exception:
        pass
    try:
        from src import acl_sentry

        return acl_sentry.report_run_summary(summary, attachment=attachment) or ""
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
