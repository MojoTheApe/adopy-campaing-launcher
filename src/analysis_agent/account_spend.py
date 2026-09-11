"""Спенд, прочитанный прямо с рекламного аккаунта.

Маршрутов будет два — браузер в профиле Multilogin и токен Marketing API, —
и оба обязаны отвечать одинаково. Здесь описан общий ответ и сделан первый.

Три правила, которые здесь не обсуждаются:

* **Только по нажатию.** Никаких таймеров и фоновых обходов: опрос Ads Manager
  по расписанию уже предшествовал чекпоинту на телефон. Нажатие человека —
  человеческое поведение, расписание — нет.
* **Только прицепиться, никогда не перезапускать.** Профиль должен быть уже
  открыт автоматизацией; читаем в этом же окне. Перезапуск ради одного числа
  разлогинивает аккаунт — так и вышло на x01, — а ветка перезапуска вдобавок
  умеет терять прокси. Обычный браузер не годится ни при каких обстоятельствах.
* **Число без подтверждённого периода — не число.** Прежде чем вернуть спенд,
  на странице сверяется показанный диапазон дат. Иначе достаточно смены
  параметра в URL, чтобы получить чужой день с видом правильного ответа.
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

from config import paths
from src.fb_ads_rpa.operations import browser_operation

# Страницы Meta и профили Multilogin переехали в RPA-трек (`acl-108d`):
# селекторы должны лежать рядом с остальными, у той сессии, что чинит их,
# когда Meta двигает кнопку. Здесь осталось хранилище показаний, статусы и
# очередь — вопросы дешборда.
#
# Имена ре-экспортируются: снаружи `account_spend` остаётся той же дверью, и
# ни `src/web/app.py`, ни тесты о переезде знать не обязаны.
from src.fb_ads_rpa.ads_manager_read import (  # noqa: F401
    ADS_MANAGER,
    TABLE_TIMEOUT_MS,
    TOTALS_TRIES,
    ProfileNotAttachable,
    SpendReading,
    attach_only,
    open_for_automation,
    open_profile_for_automation,
    read_ad_account,
    read_via_rpa,
)

_STORE_LOCK = threading.RLock()
_UNSAVED: dict[str, "SpendReading"] = {}
log = logging.getLogger(__name__)


_STORE_LOCK = threading.RLock()
_UNSAVED: dict[str, "SpendReading"] = {}
log = logging.getLogger(__name__)
def yesterday(today: date | None = None) -> str:
    return ((today or date.today()) - timedelta(days=1)).isoformat()
def _store_path() -> Path:
    # get_local_config_dir отдаёт строку, а не Path
    return Path(paths.get_local_config_dir()) / "account_spend.json"
def _load_all() -> dict[str, dict]:
    p = _store_path()
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        # Испорченный файл — это потеря последних показаний, а не повод
        # уронить страницу: прочитаем заново по кнопке.
        return {}
def _save_all(data: dict[str, dict]) -> None:
    p = _store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=p.parent, prefix=p.name + ".", suffix=".tmp", delete=False) as handle:
            tmp = Path(handle.name)
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, p)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
def _key(account: str, date_from: str, date_to: str) -> str:
    return f"{account}|{date_from}|{date_to}"
def get_reading(account: str, date_from: str, date_to: str) -> SpendReading:
    with _STORE_LOCK:
        key = _key(account, date_from, date_to)
        if key in _UNSAVED:
            return _UNSAVED[key]
        raw = _load_all().get(key)
        if not raw:
            return SpendReading(account=account, date_from=date_from, date_to=date_to)
        return _restore_reading(key, raw)
def _restore_reading(key: str, raw: dict) -> SpendReading:
    known = SpendReading.__dataclass_fields__
    reading = SpendReading(**{k: v for k, v in raw.items() if k in known})
    if reading.state == "running" and key not in _RUNNING:
        reading.state = "failed"
        reading.error = "Reading was interrupted. You can retry."
    return reading
def all_readings(date_from: str, date_to: str) -> dict[str, SpendReading]:
    """Readings for the exact dashboard period, recovering interrupted jobs."""
    with _STORE_LOCK:
        want = f"|{date_from}|{date_to}"
        out = {key[:-len(want)]: _restore_reading(key, raw) for key, raw in _load_all().items() if key.endswith(want)}
        out.update({r.account: r for key, r in _UNSAVED.items() if key.endswith(want)})
        return out
#: Состояние кабинета хранится отдельно от показаний спенда и без периода.
#: Спенд отвечает на вопрос «сколько за эти дни», состояние — «что с ним
#: сейчас». Сложи их в один ключ — и колонка статуса пустела бы при каждом
#: другом отрезке, хотя кабинет как был отключён, так и остался.
_STATUS_PREFIX = "status|"
def _put(reading: SpendReading) -> None:
    with _STORE_LOCK:
        data = _load_all()
        key = _key(reading.account, reading.date_from, reading.date_to)
        data[key] = reading.to_dict()
        if reading.status:
            data[_STATUS_PREFIX + reading.account] = {
                "status": reading.status,
                "status_text": reading.status_text,
                "at": reading.finished_at or _now(),
            }
        _save_all(data)
        _UNSAVED.pop(key, None)
def get_status(account: str) -> dict[str, str]:
    """Последнее известное состояние кабинета — или пусто, если не спрашивали.

    Пусто и `clear` — разное. Первое значит «не смотрели», второе — «смотрели,
    предупреждений не было». Экран обязан их различать.
    """
    raw = _load_all().get(_STATUS_PREFIX + account)
    if not isinstance(raw, dict):
        return {}
    return {
        "status": str(raw.get("status") or ""),
        "status_text": str(raw.get("status_text") or ""),
        "at": str(raw.get("at") or ""),
    }
#: один браузер за раз. Два профиля, поднятых одновременно с одной машины,
#: это не «быстрее», это две сессии подряд с одного устройства.
_LOCK = browser_operation
_RUNNING: set[str] = set()
def is_running(account: str, date_from: str, date_to: str) -> bool:
    with _STORE_LOCK:
        return _key(account, date_from, date_to) in _RUNNING
def running_now() -> list[str]:
    with _STORE_LOCK:
        return sorted(k.split("|")[0] for k in _RUNNING)
def start_read(
    account: str,
    date_from: str = "",
    date_to: str = "",
    *,
    reader: Callable[[str, str, str], SpendReading] | None = None,
) -> SpendReading:
    """Поставить чтение в работу и сразу вернуть его состояние.

    Чтение идёт в отдельном потоке: профиль поднимается десятки секунд, и
    держать всё это время страницу — значит показать человеку зависший
    дешборд вместо ответа.
    """
    date_from = date_from or yesterday()
    date_to = date_to or date_from
    key = _key(account, date_from, date_to)
    with _STORE_LOCK:
        if key in _RUNNING:
            return get_reading(account, date_from, date_to)
        reading = SpendReading(
            account=account, date_from=date_from, date_to=date_to,
            state="running", started_at=_now(),
        )
        _RUNNING.add(key)
        try:
            _put(reading)
        except Exception:
            _RUNNING.discard(key)
            raise

    fn = reader or read_via_rpa

    def run() -> None:
        started = time.time()
        try:
            with _LOCK:
                done = fn(account, date_from, date_to)
        except Exception as exc:  # noqa: BLE001 — падение чтения не должно ронять сервер
            done = SpendReading(
                account=account, date_from=date_from, date_to=date_to,
                state="failed", error=f"{type(exc).__name__}: {exc}",
            )
        done.started_at = reading.started_at
        done.finished_at = _now()
        done.took_s = round(time.time() - started, 1)
        with _STORE_LOCK:
            try:
                _put(done)
            except Exception:
                log.exception("Could not persist completed spend reading")
                done.state = "failed"
                done.error = "Could not save the reading. Check free disk space and retry."
                _UNSAVED[key] = done
            finally:
                _RUNNING.discard(key)

    try:
        threading.Thread(target=run, name=f"spend:{account}", daemon=True).start()
    except Exception:
        with _STORE_LOCK:
            _RUNNING.discard(key)
        raise
    return reading
def _now() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
#: Что Ads Manager пишет человеку, когда с кабинетом что-то не так. Ищем его
#: же слова, а не код: баннер написан для человека, и с ним человеку и идти
#: разбираться. Порядок важен — «disabled» весомее «review».
_STATUS_SIGNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("disabled", (
        "ad account is disabled",
        "ad account was disabled",
        "account has been disabled",
        "this ad account is disabled",
    )),
    ("restricted", (
        "account is restricted",
        "restricted from advertising",
        "advertising access is restricted",
    )),
    ("review", (
        "account is under review",
        "is being reviewed",
    )),
)
