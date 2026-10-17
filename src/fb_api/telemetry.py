"""Что из Graph-пилота можно записать и отправить — только вычищенное.

Граница одна на всё, что покидает процесс или ложится на диск: вывод
пилота, строки журнала `output/fb_api_pilot.jsonl`, логи и события Sentry.
Вычиститель — общий `run_telemetry.scrub` (ключи `access_token=`, `act=`,
uuid профилей, длинные числа) плюс то, чего он не ловит:

- голый токен `EAA…` без имени ключа рядом;
- `act_<цифры>` — между `_` и цифрой нет границы слова, и общий `\\b\\d{10,}\\b`
  такой номер пропускает;
- номера бизнесов и кабинетов от 8 цифр (ключи `x-business-use-case-usage`).

Токена у Python нет вовсе (он живёт во вкладке, см. `token_capture`), так что
маска токена — страховка на случай, если страница вернёт его в ответе.
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

#: Токен пользователя Graph API (у Ads Manager — `EAAB…`/`EAAG…`).
TOKEN_RE = re.compile(r"EAA[A-Za-z0-9]{20,}")
_ACT_RE = re.compile(r"act_\d+")
_LONG_ID_RE = re.compile(r"(?<![\w.])\d{8,}(?![\w.])")


def scrub(text: Any) -> str:
    """Строка без токенов, номеров кабинетов, бизнесов и профилей."""
    out = TOKEN_RE.sub("<token>", str(text if text is not None else ""))
    out = _ACT_RE.sub("act_<id>", out)
    try:
        from src.fb_ads_rpa import run_telemetry

        out = run_telemetry.scrub(out)
    except Exception:
        pass
    return _LONG_ID_RE.sub("<id>", out)


def scrub_value(value: Any) -> Any:
    """Вычистить вложенную структуру: строки, ключи словарей, списки."""
    if isinstance(value, dict):
        try:  # ACL-1558: почта и короткие номера в ключе — общим `clean_key`
            from src.fb_ads_rpa import run_telemetry
        except Exception:
            run_telemetry = None
        out = {}
        for key, item in value.items():
            if isinstance(key, str) and key.lower().replace("-", "_") in {"access_token", "token"}:
                out[key] = "<hidden>"
                continue
            clean = scrub_value(item)
            if run_telemetry is None:
                out[scrub(key) if isinstance(key, str) else key] = clean
                continue
            new_key = run_telemetry.clean_key(key)
            run_telemetry.put_unique(out, scrub(new_key) if isinstance(new_key, str) else new_key, clean)
        return out
    if isinstance(value, (list, tuple)):
        return [scrub_value(item) for item in value]
    if isinstance(value, str):
        return scrub(value)
    return value


_TOKEN_PARAM_RE = re.compile(r"(access_token(?:=|%3D|\\u003d))[^&\"'\s\\]+", re.I)


def strip_tokens(value: Any) -> Any:
    """Убрать токены из ответа страницы, НЕ трогая номера.

    Ответ Graph нужен целиком (номера кабинетов, кампаний), а токен — нет:
    в `paging.next` Graph кладёт `access_token=` прямо в адрес. Помощник во
    вкладке вырезает его сам; это вторая линия.
    """
    if isinstance(value, dict):
        return {k: ("<hidden>" if isinstance(k, str) and k.lower() == "access_token"
                    else strip_tokens(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [strip_tokens(v) for v in value]
    if isinstance(value, str):
        return TOKEN_RE.sub("<token>", _TOKEN_PARAM_RE.sub(r"\1<hidden>", value))
    return value


def mask_id(value: Any) -> str:
    """Номер для вывода: видны только последние 4 цифры (`act_…1234`, `…5678`)."""
    text = str(value or "").strip()
    prefix = "act_" if text.startswith("act_") else ""
    digits = text[len(prefix):]
    if len(digits) <= 4:
        return prefix + "…" + digits if digits else ""
    return f"{prefix}…{digits[-4:]}"


class ScrubFilter(logging.Filter):
    """Фильтр логов: сообщение уходит уже собранным и вычищенным."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = scrub(record.getMessage())
            record.args = None
        except Exception:
            record.msg = "<unprintable log message>"
            record.args = None
        return True


def journal_path() -> Path:
    from config.paths import get_runtime_workspace

    return get_runtime_workspace() / "output" / "fb_api_pilot.jsonl"


def record(event: dict, path: Path | None = None) -> Path | None:
    """Строка в журнал пилота. Запись не роняет пилот: ошибка — `None`."""
    try:
        target = path or journal_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        line = scrub_value({"ts": round(time.time(), 3), **event})
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")
        return target
    except Exception:
        return None


def report_stop(kind: str, message: str, context: dict | None = None) -> str:
    """Остановку пилота (logged out / checkpoint / лимит / 368) — в Sentry.

    Отправка — через общий `acl_sentry` (его `before_send` вычищает ещё раз);
    `ACL_SENTRY_DSN=` (пусто) выключает. Возвращает id события или "".
    """
    try:
        from src import acl_sentry

        if not acl_sentry.init():
            return ""
        import sentry_sdk

        with sentry_sdk.new_scope() as scope:
            scope.fingerprint = ["acl-fb-api", "stop", str(kind)]
            scope.set_tag("kind", "fb_api_stop")
            scope.set_tag("fb_api_stop", str(kind))
            if context:
                scope.set_context("fb_api", scrub_value(context))
            return sentry_sdk.capture_message(
                "Graph API pilot stopped: " + scrub(f"{kind}: {message}")[:400], level="warning"
            ) or ""
    except Exception:
        return ""
