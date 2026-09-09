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
import tempfile
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

from config import paths
from src.fb_ads_rpa.operations import browser_operation, serialized_browser

_STORE_LOCK = threading.RLock()
_UNSAVED: dict[str, "SpendReading"] = {}
log = logging.getLogger(__name__)

ADS_MANAGER = "https://adsmanager.facebook.com/adsmanager/manage/campaigns"

#: сколько ждать таблицу — профиль поднимается небыстро, а спешка тут ничего
#: не экономит: читаем мы по одному аккаунту и по просьбе человека
TABLE_TIMEOUT_MS = 90_000

#: сколько секунд ждать строку итогов после таблицы. Она приходит отдельным
#: запросом и позже строк: на снимке сорвавшегося прогона таблица нарисована, а
#: над колонкой Results ещё крутится спиннер. Двадцати секунд хватает с запасом,
#: а платит за ожидание только неудачный прогон — на удачном сумма находится с
#: первой попытки.
TOTALS_TRIES = 20


def yesterday(today: date | None = None) -> str:
    return ((today or date.today()) - timedelta(days=1)).isoformat()


@dataclass
class SpendReading:
    """Один ответ на вопрос «сколько потратил этот аккаунт за этот день»."""

    account: str
    date_from: str
    date_to: str
    source: str = "rpa"           # rpa | token
    spend: float | None = None
    currency: str = ""
    state: str = "idle"           # idle | running | ok | failed
    how: str = ""                 # каким приёмом прочитано — чтобы знать, что сломалось
    error: str = ""
    started_at: str = ""
    finished_at: str = ""
    took_s: float = 0.0
    shot: str = ""                # скриншот при неудаче
    status: str = ""              # см. _read_status: clear | disabled | restricted | review | unknown
    status_text: str = ""         # словами страницы — человеку с этим идти и разбираться

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ─────────────────────────── хранилище ───────────────────────────


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


# ─────────────────────────── очередь ───────────────────────────

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


# ─────────────────────────── маршрут RPA ───────────────────────────


def _entry_for(account: str):
    from src.meta_campaign_agent.ad_account_map import load_ad_account_map

    entries = load_ad_account_map(paths.get_ad_account_map_path())
    return entries.get(account)


class ProfileNotAttachable(RuntimeError):
    """Профиль есть, но прицепиться к нему нельзя — а перезапускать мы не станем."""


def _mlx_profile_dir(profile_id: str, folder_id: str = "") -> Path | None:
    """Папка профиля Multilogin — по адресу, а не обходом дерева.

    Штатный `_find_mlx_profile_dir` делает `rglob` по всему `~/mlx/profiles`, а
    там лежат полные каталоги Chrome. Замерено на этой машине: **около десяти
    секунд на каждый вызов**, и не только на первый. Адресный поиск — 0.001 с,
    ответ тот же.

    Путь описан в самом слое RPA: `profiles/{ws}/{folder_id}/{profile_id}`.
    Раскладку проверяем в трёх видах, все — на глубину не больше двух уровней.

    Живёт здесь, а не в `src/fb_ads_rpa`, потому что тот файл принадлежит
    другому треку. Эти десять секунд платит и каждый прогон публикации —
    заведено как `acl-144`.
    """
    profile_id = (profile_id or "").strip()
    if not profile_id:
        return None
    root = Path(os.environ.get("MLX_HOME", str(Path.home() / "mlx"))) / "profiles"
    if not root.is_dir():
        return None
    folder_id = (folder_id or "").strip()
    try:
        for ws in root.iterdir():
            if not ws.is_dir():
                continue
            if folder_id:
                p = ws / folder_id / profile_id
                if p.is_dir():
                    return p
            p = ws / profile_id
            if p.is_dir():
                return p
            for folder in ws.iterdir():
                if not folder.is_dir():
                    continue
                p = folder / profile_id
                if p.is_dir():
                    return p
    except OSError:
        return None
    return None


def _attach_only(entry, account_label: str):
    """Подключиться к уже открытому профилю. Никогда его не перезапускать.

    Обычный путь RPA, не сумев прицепиться, останавливает профиль через API
    Multilogin и поднимает заново с автоматизацией. Для публикации это
    оправдано — там ради этого и приходят. Ради одного числа за вчера — нет:
    перезапуск разлогинивает аккаунт (так и вышло на x01), а ветка
    перезапуска вдобавок умеет терять прокси аккаунта.

    Прицепиться можно только к профилю, поднятому автоматизацией: у окна,
    открытого руками в MLX Desktop, нет отладочного порта, и это свойство
    Multilogin, а не наше решение.

    Зовём внутренности чужого трека сознательно и в одном месте: публичного
    «прицепись, но не поднимай» там пока нет — заведено как `acl-108e`.
    """
    from src.fb_ads_rpa import browser as rpa_browser

    profile_id = (getattr(entry, "profile_id", "") or "").strip()
    if not profile_id:
        raise ProfileNotAttachable(f"у {account_label} не заполнен profile_id")

    direct_cls = getattr(rpa_browser, "MultiloginDirectCDPProvider", None)
    if direct_cls is None:
        raise ProfileNotAttachable(
            "слой браузера RPA изменился — обновите подключение (см. acl-108e)"
        )

    udd = _mlx_profile_dir(profile_id, (getattr(entry, "folder_id", "") or ""))
    if udd is None:
        raise ProfileNotAttachable(
            f"папка профиля {profile_id} не найдена — открыт ли этот профиль на этой машине?"
        )

    direct = direct_cls()
    conn = direct._try_attach_existing(profile_id, str(udd), strict_profile=True, new_page=True)
    if conn is not None:
        return conn

    running = False
    try:
        running = bool(direct._profile_process_running(str(udd), profile_id))
    except Exception:
        pass
    if running:
        raise ProfileNotAttachable(
            f"{account_label}: профиль открыт вручную в MLX Desktop — у такого окна нет "
            "отладочного порта, прицепиться не к чему. Перезапускать не будем: "
            "именно перезапуск разлогинил x01. Закройте профиль и откройте его "
            "автоматизацией — дальше чтения будут цепляться к этому же окну."
        )
    raise ProfileNotAttachable(
        f"{account_label}: профиль не открыт. Откройте его автоматизацией и оставьте окно — "
        "чтение прицепится к нему, ничего не перезапуская."
    )


@serialized_browser
def read_ad_account(profile_id: str, folder_id: str = "", instance: str = "A") -> dict:
    """Спросить у самого аккаунта, какой у него номер.

    Ads Manager называет рекламный кабинет в собственном адресе: открыв его без
    параметров, попадаешь на кабинет по умолчанию и `act=` оказывается в URL.
    Это первичный источник — в отличие от номера, перепечатанного человеком в
    таблицу или в тикет, который может с ним разойтись.

    Профиль должен быть уже открыт автоматизацией; перезапускать мы его не
    станем и здесь — по той же причине, по которой не перезапускаем ради
    спенда.

    Возвращает словарь, а не пару: у ответа три исхода — прочитали, не смогли
    прицепиться, прицепились но номера не увидели, — и каждый требует своего
    объяснения на экране.
    """
    out = {"ok": False, "act": "", "account_name": "", "error": ""}
    profile_id = (profile_id or "").strip()
    if not profile_id:
        out["error"] = "не задан profile_id"
        return out

    entry = _probe_entry(profile_id, folder_id, instance)
    conn = None
    try:
        try:
            conn = _attach_only(entry, f"profile {profile_id[:8]}")
        except ProfileNotAttachable as exc:
            out["error"] = str(exc)
            return out

        page = conn.page
        page.goto(ADS_MANAGER, timeout=TABLE_TIMEOUT_MS, wait_until="domcontentloaded")
        act = _act_from_url(page)
        if not act:
            out["error"] = (
                "Ads Manager открылся, но номера кабинета в адресе нет — "
                "возможно, у этого профиля нет доступа ни к одному кабинету"
            )
            return out
        out["act"] = act
        out["account_name"] = _ads_manager_account_name(page)
        out["ok"] = True
        return out
    except Exception as exc:  # noqa: BLE001 — чтение не должно ронять сервер
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out
    finally:
        if conn is not None:
            try:
                conn.page.close()
            except Exception:
                pass
            try:
                conn.close(keep_browser_open=True)
            except Exception:
                pass


def _probe_entry(profile_id: str, folder_id: str, instance: str):
    """Запись карты для профиля, которого в карте ещё нет.

    Подключение как раз и создаёт эту строку, поэтому взять её неоткуда —
    собираем ровно то, что нужно провайдеру, чтобы открыть профиль.
    """
    from src.meta_campaign_agent.ad_account_map import AccountMapEntry

    return AccountMapEntry(
        ad_account_id="",
        profile_id=profile_id,
        folder_id=(folder_id or "").strip() or None,
        multilogin_instance=(instance or "A").strip() or "A",
    )


def _act_from_url(page, tries: int = 20) -> str:
    """Дождаться, пока Ads Manager сам подставит кабинет в адрес."""
    import re
    import time

    for _ in range(tries):
        m = re.search(r"[?&]act=(?:act_)?(\d+)", page.url or "")
        if m:
            return "act_" + m.group(1)
        time.sleep(0.5)
    return ""


def _ads_manager_account_name(page) -> str:
    """Имя кабинета с экрана — чтобы человек увидел, что подтверждает.

    Профиль может видеть несколько кабинетов, и открывается тот, что по
    умолчанию. Номер без имени человеку ни о чём не говорит, а подставить его
    молча значит время от времени выбирать не тот кабинет так, что на экране
    об этом ничего не сказано.
    """
    for sel in (
        '[aria-label*="Ad account" i]',
        '[data-testid*="account_selector" i]',
        'div[role="button"]:has-text("act_")',
    ):
        try:
            el = page.query_selector(sel)
            if el:
                text = (el.get_attribute("aria-label") or el.inner_text() or "").strip()
                if text and len(text) < 120:
                    return text.replace("\n", " ")
        except Exception:
            continue
    return ""


@serialized_browser
def open_for_automation(account: str) -> tuple[bool, str]:
    """Поднять профиль автоматизацией и оставить окно открытым.

    Недостающее звено: прицепиться можно только к профилю, поднятому
    автоматизацией, а поднимал такой профиль до сих пор только путь публикации.
    Здесь он поднимается и больше ничего не делается — человек логинится, если
    надо, и окно остаётся; дальше чтения цепляются к нему.

    Закрытый профиль просто запускается — это не перезапуск, и разлогинить
    он не может. А вот открытый руками пришлось бы остановить и поднять
    заново: ровно то, что разлогинило x01. Такой мы трогать отказываемся.
    """
    entry = _entry_for(account)
    if entry is None:
        return False, f"{account} нет в карте аккаунтов"
    profile_id = (getattr(entry, "profile_id", "") or "").strip()
    if not profile_id:
        return False, f"у {account} не заполнен profile_id"

    from src.fb_ads_rpa import browser as rpa_browser

    direct_cls = getattr(rpa_browser, "MultiloginDirectCDPProvider", None)
    if direct_cls is None:
        return False, "слой браузера RPA изменился (см. acl-108e)"
    udd = _mlx_profile_dir(profile_id, (getattr(entry, "folder_id", "") or ""))
    if udd is None:
        return False, f"папка профиля {profile_id} не найдена"

    direct = direct_cls()
    attached = direct._try_attach_existing(profile_id, str(udd), strict_profile=True)
    if attached is not None:
        attached.close(keep_browser_open=True)
        return True, "профиль уже открыт автоматизацией — можно читать"
    try:
        if direct._profile_process_running(str(udd), profile_id):
            return False, (
                f"{account}: профиль открыт вручную в MLX Desktop. Поднять его "
                "автоматизацией можно только остановив — а это разлогинивает. "
                "Закройте окно в MLX Desktop и нажмите ещё раз."
            )
    except Exception:
        pass

    try:
        conn = rpa_browser.get_browser_provider("multilogin_attach").open(entry, account)
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    try:
        # Окно остаётся человеку: отключаемся, не закрывая браузер.
        conn.close(keep_browser_open=True)
    except Exception:
        pass
    return True, "профиль открыт — залогиньтесь, если нужно, и оставьте окно"


@serialized_browser
def read_via_rpa(account: str, date_from: str, date_to: str = "") -> SpendReading:
    """Прочитать спенд аккаунта за период, выбранный на дешборде.

    Период приходит с экрана, а не берётся за вчера: число, снятое не за тот
    отрезок, что показан рядом, выглядело бы как расхождение источников — а на
    деле было бы ответом на другой вопрос.
    """
    date_to = date_to or date_from
    out = SpendReading(
        account=account, date_from=date_from, date_to=date_to,
        source="rpa", state="failed",
    )

    entry = _entry_for(account)
    if entry is None:
        out.error = f"{account} нет в карте аккаунтов"
        return out
    act = (entry.ad_account_id or "").strip()
    if not act:
        out.error = f"у {account} не заполнен ad_account_id"
        return out

    conn = None
    try:
        try:
            conn = _attach_only(entry, account)
        except ProfileNotAttachable as exc:
            out.error = str(exc)
            return out
        page = conn.page
        page.goto(_url(act, date_from, date_to), timeout=TABLE_TIMEOUT_MS, wait_until="domcontentloaded")

        # Ads Manager рисуется скриптом, и `domcontentloaded` на нём не значит
        # почти ничего: первый прогон 2026-09-07 проверил период через четыре
        # секунды после перехода, когда на экране был спиннер и пустой скелет.
        # Ждём саму таблицу, и только потом спрашиваем, какой период показан.
        grid_ready = _wait_for_grid(page)
        expected_act = act if act.startswith("act_") else "act_" + act
        opened = _act_from_url(page, tries=1)
        if opened != expected_act:
            # Оба номера в тексте, а не только сам факт расхождения. Прежнее
            # «opened a different account» не говорило ни какой кабинет ждали,
            # ни какой открылся — а починка это ровно эти два числа. 9 сентября
            # разницу пришлось доставать отдельным запросом; у человека перед
            # экраном такой возможности нет.
            out.error = (
                f"Ads Manager открыл кабинет {opened or '(в адресе нет act)'}, "
                f"а у {account} записан {expected_act} — чтение отменено. "
                "Либо в карте не тот номер, либо у профиля нет доступа к этому кабинету."
            )
            return out
        if not grid_ready:
            # Статус спрашиваем и здесь: у отключённого кабинета таблицы может
            # не быть вовсе, а знать о нём надо именно тогда.
            out.status, out.status_text = _read_status(page)
            out.error = "Ads Manager не отрисовался за отведённое время"
            # было `day` — переменной, которой здесь нет. Ошибка жила только на
            # пути отказа, то есть ровно там, где снимок и нужен: вместо
            # «не отрисовался» человек получил бы NameError.
            out.shot = _shoot(page, account, date_from)
            return out

        # Один заход — оба ответа. Отдельный визит ради состояния кабинета
        # ничем не оправдан: каждое касание живого аккаунта чего-то стоит.
        out.status, out.status_text = _read_status(page)

        shown = _shown_period(page)
        if not _period_matches(shown, date_from, date_to):
            asked = date_from if date_from == date_to else f"{date_from}…{date_to}"
            out.error = (
                f"на экране период «{shown or 'не найден'}», а спрашивали {asked} — "
                "число не берём"
            )
            out.shot = _shoot(page, account, date_from)
            return out

        spend, how = _read_total(page)
        if spend is None:
            out.error = "не нашёл итоговую строку со спендом"
            out.how = how
            out.shot = _shoot(page, account, date_from)
            return out

        if _act_from_url(page, tries=1) != expected_act or not _period_matches(_shown_period(page), date_from, date_to):
            out.status = out.status_text = ""
            out.error = "Account or period changed during reading; result refused."
            return out
        out.spend = spend
        out.currency = _currency(page)
        out.how = how
        out.state = "ok"
        return out
    finally:
        if conn is not None:
            try:
                conn.page.close()
            except Exception:
                pass
            # Окно остаётся открытым: профиль, закрытый сразу после захода,
            # выглядит хуже, чем человек, который посмотрел и ушёл.
            try:
                conn.close(keep_browser_open=True)
            except Exception:
                pass


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


def _read_status(page) -> tuple[str, str]:
    """Состояние кабинета — словами самой страницы.

    Отсутствие предупреждения **не значит «активен»**: баннер мог не успеть
    появиться, вёрстка Meta меняется, а формулировку могли поменять вчера.
    Поэтому чистая страница — это `clear`, «предупреждений не видно», и ничего
    больше. Написать здесь `active` значило бы выдать незнание за проверку —
    именно тот вид ошибки, которая выглядит нормальным результатом.
    """
    try:
        raw = page.inner_text("body") or ""
    except Exception:
        return "unknown", ""
    flat = " ".join(raw.split())
    if not flat:
        return "unknown", ""
    low = flat.lower()
    for state, phrases in _STATUS_SIGNS:
        for phrase in phrases:
            at = low.find(phrase)
            if at != -1:
                start = max(0, at - 60)
                return state, flat[start : at + len(phrase) + 140].strip()
    return "clear", ""


def _url(act: str, date_from: str, date_to: str = "") -> str:
    """Адрес Ads Manager на нужный период.

    **Конец в этом параметре исключающий.** Проверено на живом аккаунте
    2026-09-07: `date=2026-09-01_2026-09-06` показывает «Sep 1 – Sep 5», а
    `..._2026-09-07` — «Sep 1 – Sep 6». Поэтому к концу прибавляется день, и
    тогда один день укладывается в то же правило: `06_07` — это шестое.

    Без этого шесть дней превращались в пять, а сумма выходила за другой
    отрезок и выглядела правильной: 67.87 вместо числа, включающего 259.48 за
    шестое. Сверка периода на экране это ловит, но лучше не создавать повода.

    **В параметре — только цифры, без `act_`.** Мы клали туда `act=act_<цифры>`,
    как номер записан в карте. Meta такой параметр не понимает и **молча
    игнорирует**: открывается кабинет профиля по умолчанию.

    Полтора месяца это выглядело работающим, потому что у профилей с одним
    кабинетом умолчание и есть тот самый кабинет — Meta игнорировала параметр и
    попадала куда надо по другой причине. Семь удачных чтений подряд получены
    по сломанному адресу. Сломанность стала видна на первом профиле с
    несколькими кабинетами: 9 сентября `FacebookDQ438` открыл чужой кабинет, и
    сторож отказался читать — верно отказался.

    Проверено живьём на нём же в тот же день:

        act=act_<цифры>  →  открылся кабинет профиля по умолчанию
        act=<цифры>      →  открылся тот, о котором спрашивали

    Разборщик адреса `_act_from_url` префикс прощает — `(?:act_)?(\\d+)`. Мы
    были снисходительны к своей же ошибке на чтении и совершали её на записи;
    поэтому она и не всплывала.
    """
    from datetime import date as _date, timedelta as _td

    act_id = act.strip().removeprefix("act_")
    end = date_to or date_from
    try:
        y, m, d = (int(x) for x in end.split("-"))
        end = (_date(y, m, d) + _td(days=1)).isoformat()
    except Exception:
        pass
    return f"{ADS_MANAGER}?act={act_id}&date={date_from}_{end}"


def _wait_for_grid(page) -> bool:
    """Дождаться таблицы кампаний — признак, что страница ожила."""
    try:
        page.wait_for_selector(
            'div[role="table"], [role="grid"], [aria-label*="Amount spent" i]',
            timeout=TABLE_TIMEOUT_MS,
        )
        return True
    except Exception:
        return False


def _shown_period(page, tries: int = 12) -> str:
    """Что за период показывает сама страница.

    Несколько попыток: таблица появляется раньше шапки с датами, и один
    промах здесь стоил бы отказа на полностью исправной странице.
    """
    import time

    for _ in range(tries):
        found = _period_once(page)
        if found:
            return found
        time.sleep(0.5)
    return ""


def _period_once(page) -> str:
    for sel in (
        '[aria-label*="Date"], [aria-label*="date range" i]',
        'div[role="button"][aria-label*="Date"]',
        'span:has-text("Yesterday")',
    ):
        try:
            el = page.query_selector(sel)
            if el:
                text = (el.get_attribute("aria-label") or el.inner_text() or "").strip()
                if text:
                    return text
        except Exception:
            continue

    # Запасной путь, и на деле основной: кнопка периода — просто кнопка с
    # датой внутри, без aria-label, за который цеплялись выше. Снимок первого
    # живого прогона показал ровно это: справа вверху «Sep 6, 2026».
    try:
        import re as _r

        pat = _r.compile(
            r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{1,2},\s*\d{4}",
            _r.I,
        )
        for el in page.query_selector_all('div[role="button"], [role="combobox"]'):
            text = " ".join((el.inner_text() or "").split())
            if 0 < len(text) < 80 and pat.search(text):
                return text
    except Exception:
        pass
    return ""


_MONTHS = {
    m.lower(): i
    for i, m in enumerate(
        "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), start=1
    )
}


def _dates_in(text: str) -> list[str]:
    """Все даты из подписи периода, приведённые к ISO.

    Разбираем по-настоящему, а не ищем подстроки. Прежняя проверка спрашивала,
    есть ли в тексте месяц, число и год по отдельности, — и «Sep 1, 2026 – Sep
    5, 2026» сошло за конец «2026-09-06», потому что цифра 6 нашлась внутри
    года. Меня попросили шесть дней, Ads Manager показал пять, сверка сказала
    «совпадает», и вернулась сумма за другой отрезок с видом правильной.
    """
    import re

    out: list[str] = []
    for m in re.finditer(r"(\d{4})-(\d{2})-(\d{2})", text):
        out.append(m.group(0))
    for m in re.finditer(
        r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+(\d{1,2}),?\s*(\d{4})",
        text,
        re.I,
    ):
        month = _MONTHS[m.group(1)[:3].lower()]
        out.append(f"{int(m.group(3)):04d}-{month:02d}-{int(m.group(2)):02d}")
    seen: list[str] = []
    for d in out:
        if d not in seen:
            seen.append(d)
    return seen


def _period_matches(shown: str, date_from: str, date_to: str = "") -> bool:
    """Совпадает ли показанный период с запрошенным — точно, а не примерно.

    Экран должен назвать ровно те даты, о которых спросили: одну для дня, обе
    для отрезка. Ни больше, ни меньше. Любое расхождение значит, что число
    рядом отвечает на другой вопрос, и брать его нельзя.
    """
    if not shown or not date_from:
        return False
    date_to = date_to or date_from
    found = _dates_in(shown)
    if not found:
        # «Yesterday» — единственная подпись без даты, которую мы принимаем,
        # и только когда именно вчера и спрашивали.
        return "yesterday" in shown.lower() and date_from == date_to == yesterday()
    want = [date_from] if date_from == date_to else [date_from, date_to]
    return sorted(found) == sorted(want)


#: Поиск строки итогов целиком внутри страницы — один вызов вместо обхода по
#: узлу за раз. Обход `query_selector_all("div, span")` с `inner_text()` на
#: каждом узле — это тысячи обращений к браузеру: пока он делался однократно,
#: он стоил секунды, но повторные попытки (TOTALS_TRIES) умножили цену, и
#: чтение за три дня заняло 175 секунд против 40 за один. Ждать надо страницу,
#: а не собственный обход.
#:
#: Надпись помечается атрибутом, чтобы потом взять её обычным селектором: к ней
#: ещё нужно подвести курсор, а через evaluate ручку не получить.
_FIND_TOTALS_JS = r"""
() => {
  const out = {spend: null, label: false};
  const money = /([$€£₪]\s*[\d,]+(?:\.\d{1,2})?)\s*Total spent/;
  document.querySelectorAll('[data-acl-totals]').forEach(
    e => e.removeAttribute('data-acl-totals'));
  for (const n of document.querySelectorAll('div, span')) {
    const t = (n.innerText || '').replace(/\s+/g, ' ').trim();
    if (!t) continue;
    if (out.spend === null && t.length <= 600 && t.indexOf('Total spent') !== -1) {
      const m = t.match(money);
      if (m) out.spend = m[1];
    }
    if (t.indexOf('Results from') === 0 && t.indexOf('campaign') !== -1) {
      const r = n.getBoundingClientRect();
      if (r.width > 0 && r.height > 0) {
        // как и в прежнем обходе, побеждает последняя видимая копия
        document.querySelectorAll('[data-acl-totals]').forEach(
          e => e.removeAttribute('data-acl-totals'));
        n.setAttribute('data-acl-totals', '1');
        out.label = true;
      }
    }
  }
  return out;
}
"""


def _scan_totals(page) -> tuple[float | None, bool]:
    """Сумма из строки итогов и есть ли надпись, за которую берётся курсор."""
    try:
        found = page.evaluate(_FIND_TOTALS_JS)
    except Exception:
        return None, False
    raw = (found or {}).get("spend") or ""
    page._acl_spend_currency = next((code for sign, code in (("$", "USD"), ("€", "EUR"), ("£", "GBP"), ("₪", "ILS")) if raw.strip().startswith(sign)), "")
    return (_first_money(raw) if raw else None), bool((found or {}).get("label"))


def _read_total(page) -> tuple[float | None, str]:
    """Итоговый спенд из строки итогов.

    На таких аккаунтах итоги свёрнуты: видно «Results from N campaigns», а сумм
    нет. Их показывает кнопка **View results**, которая появляется только при
    наведении курсора на эту надпись — подсказал пользователь 2026-09-07, и без
    этого читать было нечего.

    Раскрытие запоминается: на уже раскрытом аккаунте кнопки не будет вовсе,
    там написано «Hide results». Поэтому сперва смотрим, нет ли суммы прямо
    сейчас, и только потом трогаем мышь.
    """
    if not _wait_for_grid(page):
        return None, "таблица не появилась"

    # Итоги появляются позже таблицы: строки кампаний уже нарисованы, а над
    # колонкой Results ещё крутится спиннер. Один взгляд сразу после таблицы
    # приходился ровно на этот момент — 8 сентября прогон на 5–7 сентября так и
    # сорвался с «строка не найдена», хотя страница была совершенно исправна и
    # даты на ней стояли верные. У периода такая же повторная попытка есть с
    # самого начала (`_shown_period`), у итогов её не было.
    label = None
    for _ in range(TOTALS_TRIES):
        value, has_label = _scan_totals(page)
        if value is not None:
            return value, "строка итогов, «Total spent»"
        if has_label:
            label = page.query_selector('[data-acl-totals="1"]')
            if label is not None:
                break
        _sleep(1)
    if label is None:
        return None, f"строка «Results from …» не появилась за {TOTALS_TRIES} с"

    try:
        label.scroll_into_view_if_needed(timeout=5_000)
        label.hover(timeout=5_000)
    except Exception:
        return None, "к надписи итогов не удалось подвести курсор"

    _sleep(1.5)
    clicked = False
    try:
        for b in page.query_selector_all('[role="button"]'):
            try:
                if " ".join((b.inner_text() or "").split()) == "View results" and b.is_visible():
                    b.click(timeout=5_000)
                    clicked = True
                    break
            except Exception:
                continue
    except Exception:
        pass
    if not clicked:
        return None, "кнопки «View results» не появилось при наведении"

    # Сумма после раскрытия тоже приходит не мгновенно: фиксированная пауза
    # была ставкой на то, что трёх секунд хватит.
    for _ in range(TOTALS_TRIES):
        value, _label = _scan_totals(page)
        if value is not None:
            return value, "строка итогов раскрыта наведением, «Total spent»"
        _sleep(1)
    return None, "итоги раскрыты, но суммы рядом с «Total spent» нет"


def _sleep(seconds: float) -> None:
    import time

    time.sleep(seconds)


def _first_money(text: str) -> float | None:
    """Деньги из строки ячейки.

    Знак валюты обязателен, а без него годится только число, которое само по
    себе выглядит суммой — с разделителем разрядов или копейками. Иначе из
    «Results from 4 campaigns $12,044.00» берётся «4»: количество кампаний,
    выданное за спенд аккаунта, и выглядит оно совершенно правдоподобно.
    """
    import re

    text = " ".join(text.split())
    number = r"(?:\d{1,3}(?:[, ]\d{3})+|\d+)(?:\.\d{1,2})?(?![\d.,])"
    signed = re.search(rf"[$€£₪]\s*({number})", text)
    m = signed or re.search(r"(?<![\d.,])(\d{1,3}(?:[, ]\d{3})+(?:\.\d{1,2})?|\d+\.\d{1,2})(?![\d.,])", text)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", "").replace(" ", ""))
    except ValueError:
        return None


def _currency(page) -> str:
    # Bound to the exact totals cell, never to an unrelated dollar sign in body.
    return getattr(page, "_acl_spend_currency", "")


def _shoot(page, account: str, day: str) -> str:
    """Снимок при неудаче: первый живой прогон стоит тестового аккаунта."""
    try:
        out = Path(paths.get_local_config_dir()) / "spend_shots"
        out.mkdir(parents=True, exist_ok=True)
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in account)
        target = out / f"{safe}_{day}.png"
        page.screenshot(path=str(target), full_page=False)
        return str(target)
    except Exception:
        return ""
