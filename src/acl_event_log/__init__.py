"""Общий журнал действий ACL: локальный ящик, отправка, чистка.

Важно: `emit()` никогда не бросает исключение и не ходит в сеть.
"""
from src.acl_event_log.writer import emit  # noqa: F401


def start_shipper() -> None:
    from src.acl_event_log import shipper

    shipper.start_shipper()


def wake_shipper() -> None:
    from src.acl_event_log import shipper

    shipper.wake_shipper()
