"""Проверочное событие для цепочки Sentry -> релей -> облачная routine (ACL-1330).

Шлёт в Sentry ОДНО синтетическое событие отказа — тем же путём, что робот
(`acl_sentry.report_step_failure`): шаг `relay_selftest`, вид `blocking`,
причина `selftest-<время UTC>`. Причина каждый раз новая, поэтому Sentry
заводит новый тикет, правило «ACL - new failure kind» срабатывает, релей
вызывает routine. Routine узнаёт шаг `relay_selftest` и только отвечает
владельцу, что цепочка работает (docs/system/ROUTINE_SENTRY_PROMPT.md);
в `config/sentry_triage_map.json` этот шаг помечен как известный, чтобы
ноутбучный разбор не считал его новой поломкой.

Ни кампаний, ни браузера, ни аккаунтов — только одно событие.

  python scripts/sentry_selftest_event.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

STEP = "relay_selftest"


def send(now: datetime | None = None) -> tuple[str, str]:
    """Отправить событие. Возвращает (id события или "", причина)."""
    from src import acl_sentry

    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%S")
    reason = f"selftest-{stamp}"
    event_id = acl_sentry.report_step_failure(
        STEP,
        "synthetic self-test of the Sentry -> cloud routine trigger (ACL-1330)",
        reason=reason,
        kind="blocking",
    )
    if event_id:
        try:
            import sentry_sdk

            sentry_sdk.flush(timeout=10)
        except Exception:
            pass
    return event_id, reason


def main() -> int:
    from scripts._console import use_utf8

    use_utf8()
    event_id, reason = send()
    if not event_id:
        print("Sentry is off on this machine (ACL_SENTRY_DSN is empty or sentry-sdk is missing); nothing was sent.")
        return 1
    print(f"Sent a self-test event: step={STEP} reason={reason}. "
          "Within a minute a new run should appear at claude.ai/code/routines.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
