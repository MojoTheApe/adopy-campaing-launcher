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
import re
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


# ── Чем чтение цепляется за экраны Meta ──────────────────────────────────
#
# Всё в одном месте, как в `meta_dom.py` у RPA. Причина не в порядке ради
# порядка: Meta двигает контролы сразу для всех, и правка, ломающая публикацию,
# ломает и чтение — экраны те же. Разбросанные по файлу литералы означают, что
# чинить придётся по одному, находя их поиском.
#
# Здесь же они переживут переезд в RPA-трек (`acl-108d`): перенести один блок —
# работа механическая, собрать его заново по всему файлу — нет.
#
# Каждый список — по убыванию надёжности: сначала то, что Meta пометила сама,
# потом то, что видно человеку. Первое переживает перевод интерфейса, второе
# нет; но первое Meta и убирает чаще.

#: Название кабинета в шапке Ads Manager.
SEL_ACCOUNT_NAME = (
    '[aria-label*="Ad account" i]',
    '[data-testid*="account_selector" i]',
    'div[role="button"]:has-text("act_")',
)

#: Кнопка периода. Запасной путь ниже на деле основной: у кнопки часто нет
#: aria-label, и опознаётся она по дате внутри — снимок первого живого прогона
#: показал справа вверху «Sep 6, 2026».
SEL_PERIOD = (
    '[aria-label*="Date"], [aria-label*="date range" i]',
    'div[role="button"][aria-label*="Date"]',
    'span:has-text("Yesterday")',
)
SEL_PERIOD_FALLBACK = 'div[role="button"], [role="combobox"]'
RE_PERIOD_DATE = r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{1,2},\s*\d{4}"

#: Надпись строки итогов, помеченная нашим же атрибутом из `_FIND_TOTALS_JS`:
#: к ней надо подвести курсор, а через evaluate ручку не получить.
SEL_TOTALS_LABEL = '[data-acl-totals="1"]'

#: Любая кнопка — среди них ищется «View results» по тексту.
SEL_BUTTON = '[role="button"]'

#: Таблица кампаний — признак, что страница ожила. Три написания: Meta успела
#: подержать список и таблицей, и гридом, а подпись колонки переживает оба.
SEL_GRID = 'div[role="table"], [role="grid"], [aria-label*="Amount spent" i]'

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



#: Прокручивается самый большой прокручиваемый блок — сам список. Ищем его по
#: `overflowY`, а не по классу: имена классов у Meta меняются от сборки к
#: сборке, а поведение блока — нет.
_SCROLL_TO_TOTALS_JS = r"""
() => {
  let best = null, bestArea = 0;
  for (const n of document.querySelectorAll('div')) {
    const st = getComputedStyle(n);
    if (!/(auto|scroll)/.test(st.overflowY)) continue;
    if (n.scrollHeight <= n.clientHeight + 40) continue;
    const r = n.getBoundingClientRect();
    const area = r.width * r.height;
    if (area > bestArea) { bestArea = area; best = n; }
  }
  if (best) { best.scrollTop = best.scrollHeight; return true; }
  window.scrollTo(0, document.body.scrollHeight);
  return false;
}
"""




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

    #: Дерево аккаунта: кампании и объявления с их id и положением тумблера.
    #: Читается тем же нажатием, что и спенд (решение владельца 11.09) — см.
    #: `_read_tree`. Неудача дерева не отменяет спенд: это разные ответы, и
    #: терять прочитанное число из-за второго — хуже, чем вернуть его без
    #: дерева.
    objects: list[dict] = field(default_factory=list)
    objects_error: str = ""
    objects_how: str = ""         # чем разобрана таблица — чтобы знать, что сломалось

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


def _dir_spellings(path) -> list[str]:
    """Написания одной и той же папки, которые Windows считает равными.

    9 сентября чтение спенда X84 отказывало словами «профиль открыт вручную»
    сразу после того, как дешборд сам его и открыл. Прицепиться мешало
    единственное: **у папки два написания**. Multilogin запускает Mimic с
    коротким именем 8.3 —

        C:\\Users\\EXAMPL~1\\mlx\\profiles\\WORKSP~1\\FOLDER~1\\PROFIL~1

    — а мы вычисляем длинное, и проверка владения портом сравнивает их как
    строки. `os.path.abspath` и `normcase` короткое имя не разворачивают, и
    два имени одной папки расходятся.

    Проверять владение по папке — правильно: без этого можно прицепиться к
    браузеру **другого** аккаунта, а это хуже любого непрочитанного числа.
    Поэтому сравнение остаётся, а сюда добавляется второе написание.

    Разворачивать короткое в длинное было бы половиной решения: неизвестно,
    какое из двух придёт следующим, и на машине с выключенным 8.3 короткого
    имени нет вовсе. Поэтому возвращаются оба, без повторов и в понятном
    порядке — длинное первым.
    """
    out: list[str] = []
    text = str(path)
    for value in (text, _short_path(text)):
        if value and value not in out:
            out.append(value)
    return out


def _short_path(path: str) -> str:
    """Имя 8.3 для существующей папки — или пусто, если его нет.

    Пусто — обычный ответ, а не сбой: на томах с выключенным 8.3 короткого
    имени не существует, и тогда написание всего одно.
    """
    if os.name != "nt":
        return ""
    try:
        import ctypes

        buf = ctypes.create_unicode_buffer(4096)
        length = ctypes.windll.kernel32.GetShortPathNameW(str(path), buf, 4096)
        return buf.value if 0 < length < 4096 and buf.value != str(path) else ""
    except Exception:
        return ""


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

    Используем публичный attach_existing_only: проверка принадлежности
    профилю закреплена в провайдере, для чтения создаётся отдельная вкладка.
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
    for spelling in _dir_spellings(udd):
        conn = direct.attach_existing_only(profile_id, spelling, new_page=True)
        if conn is not None:
            return conn

    running = False
    try:
        running = any(direct.profile_is_running(p, profile_id) for p in _dir_spellings(udd))
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
    for sel in SEL_ACCOUNT_NAME:
        try:
            el = page.query_selector(sel)
            if el:
                text = (el.get_attribute("aria-label") or el.inner_text() or "").strip()
                if text and len(text) < 120:
                    return text.replace("\n", " ")
        except Exception:
            continue
    return ""


class _Profile:
    """То немногое, что провайдеру нужно знать о профиле, чтобы его поднять.

    Карта аккаунтов для этого не требуется: `MultiloginAttachOnlyProvider`
    спрашивает только эти три поля. Отдельный класс нужен ровно затем, чтобы
    профиль можно было открыть **до** подключения — иначе получалась курица и
    яйцо: чтобы подключить, дешборд просил номер кабинета; узнать номер можно
    у открытого профиля; открыть профиль дешборд соглашался только у уже
    подключённого аккаунта.
    """

    def __init__(self, profile_id: str, folder_id: str = "", instance: str = "A"):
        self.profile_id = (profile_id or "").strip()
        self.folder_id = (folder_id or "").strip()
        self.multilogin_instance = (instance or "A").strip() or "A"


@serialized_browser
def open_profile_for_automation(profile_id: str, folder_id: str = "",
                                instance: str = "A", label: str = "") -> tuple[bool, str]:
    """Поднять профиль автоматизацией и оставить окно открытым.

    Прицепиться можно только к профилю, поднятому автоматизацией, а поднимал
    такой профиль до сих пор только путь публикации. Здесь он поднимается и
    больше ничего не делается — человек логинится, если надо, и окно остаётся;
    дальше чтения цепляются к нему.

    Закрытый профиль просто запускается — это не перезапуск, и разлогинить он
    не может. А вот открытый руками пришлось бы остановить и поднять заново:
    ровно то, что разлогинило x01. Такой мы трогать отказываемся.

    **Отсутствие папки профиля больше не отказ.** Папку создаёт сам Multilogin
    при первом запуске на этой машине, так что для нового профиля её и не
    может быть: мы отказывались запускать профиль потому, что его ни разу не
    запускали. Без папки пробуем API Multilogin; если профиль уже занят,
    возвращаем отказ и никогда не останавливаем его ради этой кнопки.
    """
    profile_id = (profile_id or "").strip()
    who = label or profile_id[:8] or "профиль"
    if not profile_id:
        return False, f"у {who} не заполнен profile_id"
    instance = (instance or "A").strip().upper()
    if instance not in {"A", "B"}:
        return False, "Unknown Multilogin instance; choose A or B"

    from src.fb_ads_rpa import browser as rpa_browser

    direct_cls = getattr(rpa_browser, "MultiloginDirectCDPProvider", None)
    if direct_cls is None:
        return False, "слой браузера RPA изменился (см. acl-108e)"

    udd = _mlx_profile_dir(profile_id, folder_id)
    if udd is not None:
        direct = direct_cls()
        for spelling in _dir_spellings(udd):
            attached = direct.attach_existing_only(profile_id, spelling)
            if attached is not None:
                attached.close(keep_browser_open=True)
                return True, "профиль уже открыт автоматизацией — можно читать"
        try:
            if any(direct.profile_is_running(p, profile_id) for p in _dir_spellings(udd)):
                return False, (
                    f"{who}: профиль открыт вручную в MLX Desktop. Поднять его "
                    "автоматизацией можно только остановив — а это разлогинивает. "
                    "Закройте окно в MLX Desktop и нажмите ещё раз."
                )
        except Exception as exc:
            return False, f"Не удалось проверить открытые профили: {exc}. Новый запуск не выполнялся."


    entry = _Profile(profile_id, folder_id, instance)
    try:
        conn = rpa_browser.get_browser_provider("multilogin_attach").open(
            entry, who, allow_missing_dir=True, allow_restart=False)
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    try:
        # Окно остаётся человеку: отключаемся, не закрывая браузер.
        conn.close(keep_browser_open=True)
    except Exception:
        pass
    return True, "профиль открыт — залогиньтесь, если нужно, и оставьте окно"


@serialized_browser
def open_for_automation(account: str) -> tuple[bool, str]:
    """То же самое для аккаунта, который уже есть в карте."""
    entry = _entry_for(account)
    if entry is None:
        return False, f"{account} нет в карте аккаунтов"
    return open_profile_for_automation(
        (getattr(entry, "profile_id", "") or ""),
        (getattr(entry, "folder_id", "") or ""),
        (getattr(entry, "multilogin_instance", "") or "A"),
        account,
    )


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
    try:
        url = _url(act, date_from, date_to)
    except ValueError as exc:
        out.error = str(exc)
        return out

    conn = None
    try:
        try:
            conn = _attach_only(entry, account)
        except ProfileNotAttachable as exc:
            out.error = str(exc)
            return out
        page = conn.page
        page.goto(url, timeout=TABLE_TIMEOUT_MS, wait_until="domcontentloaded")

        # Ads Manager рисуется скриптом, и `domcontentloaded` на нём не значит
        # почти ничего: первый прогон 2026-09-07 проверил период через четыре
        # секунды после перехода, когда на экране был спиннер и пустой скелет.
        # Ждём саму таблицу, и только потом спрашиваем, какой период показан.
        grid_ready = _wait_for_grid(page)
        expected_act = act if act.startswith("act_") else "act_" + act
        opened = _act_from_url(page, tries=1)
        if opened != expected_act:
            # Оба номера помогают сверить карту и доступ профиля без нового чтения.
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

        # Дерево — тем же нажатием (решение владельца 11.09): Read и так уже
        # здесь, а каждое отдельное касание живого аккаунта чего-то стоит.
        #
        # Строго после того, как спенд принят. Неудача дерева не отменяет
        # число: это разные ответы на разные вопросы, и терять прочитанный
        # спенд из-за второго — хуже, чем вернуть его без дерева. Поэтому же
        # здесь `except Exception`: сюда мы приходим с готовым результатом, и
        # уронить его необработанным исключением — самое плохое, что может
        # случиться.
        try:
            out.objects, out.objects_how, out.objects_error = _read_tree(
                page, act, expected_act, date_from, date_to
            )
        except Exception as exc:            # noqa: BLE001 — см. выше
            out.objects_error = f"дерево не прочитано: {exc}"
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
        # По образцу увиденного ниже: Meta пишет о себе действительным залогом.
        "we disabled your ad account",
        "we disabled your account",
        "we've disabled your ad account",
        "we have disabled your ad account",
    )),
    ("restricted", (
        # Снято с DQ438 12.09. Все формулировки выше этой строки были
        # придуманы и ни одна не сработала: аккаунт был ограничен, а дешборд
        # показывал «no warning». Meta пишет не «account is restricted», а
        # **«We restricted your ad account»** — от себя и действительным
        # залогом. Догадка о чужих словах и здесь стоила молчания об отказе.
        "we restricted your ad account",
        "we restricted your account",
        "we've restricted your ad account",
        "we have restricted your ad account",
        "account is restricted",
        "restricted from advertising",
        "advertising access is restricted",
    )),
    ("review", (
        "account is under review",
        "is being reviewed",
    )),
)

#: Признаки беды, из которых не видно, какой именно. Нужны затем, что список
#: выше — перечень **известных** формулировок, а Meta их меняет. Пока банер не
#: опознан, честный ответ — «что-то не так, вот её слова», и уж точно не «no
#: warning»: под этой надписью аккаунт стоял ограниченным.
_TROUBLE_SIGNS: tuple[str, ...] = (
    "you can't run ads",
    "you can’t run ads",
    "you cannot run ads",
    "you can no longer run ads",
    "request review",
)


#: Сколько ждать баннер о состоянии кабинета, прежде чем сказать «чисто».
#:
#: Он приходит отдельным запросом и позже таблицы. 12.09 из-за этого
#: ограниченный кабинет прошёл как `clear` **уже с исправленными
#: формулировками**: спрашивали через мгновение после отрисовки грида, когда
#: баннера на странице ещё не было. Ручная проверка ждала шесть секунд и
#: видела его.
#:
#: Платит за ожидание только чистая страница: найденный баннер возвращается
#: сразу. То есть цена — секунды на здоровом аккаунте и правильный ответ на
#: больном.
STATUS_TRIES = 8
STATUS_PAUSE_S = 0.75


def _read_status(page, tries: int = STATUS_TRIES) -> tuple[str, str]:
    """Состояние кабинета — словами самой страницы.

    Отсутствие предупреждения **не значит «активен»**: баннер мог не успеть
    появиться, вёрстка Meta меняется, а формулировку могли поменять вчера.
    Поэтому чистая страница — это `clear`, «предупреждений не видно», и ничего
    больше. Написать здесь `active` значило бы выдать незнание за проверку —
    именно тот вид ошибки, которая выглядит нормальным результатом.
    """
    answer = ("unknown", "")
    for attempt in range(max(1, tries)):
        if attempt:
            _sleep(STATUS_PAUSE_S)
        answer = _status_once(page)
        # Ждём только «нечего сказать»: и «чисто», и «страница ничего не дала»
        # бывают потому, что рано спросили. Всё, что названо, — ответ.
        if answer[0] not in ("clear", "unknown"):
            return answer
    return answer


def _status_once(page) -> tuple[str, str]:
    """Один взгляд на страницу. Ожиданием заведует `_read_status`."""
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

    # Банер есть, а какой — не опознали. Говорим это вслух её же словами.
    #
    # Не `unknown`: он означает «страница ничего не дала прочитать», и так его
    # и подписывает дешборд. Здесь прочиталось прекрасно — просто формулировка
    # новая. Отдельное слово попадает в колонку состояния красным, а фраза
    # Meta — в подсказку, с ней человеку и идти разбираться.
    #
    # Ложное «blocked» на здоровом аккаунте — досада; ложное «no warning» на
    # ограниченном — то, что уже случилось: аккаунт не мог крутить рекламу, а
    # дешборд молчал. Размен осознанный.
    for phrase in _TROUBLE_SIGNS:
        at = low.find(phrase)
        if at != -1:
            start = max(0, at - 120)
            return "blocked", flat[start : at + len(phrase) + 200].strip()
    return "clear", ""


def _url(act: str, date_from: str, date_to: str = "") -> str:
    """Адрес Ads Manager на нужный период.

    Конец диапазона исключающий: к последнему запрошенному дню прибавляем один.
    В URL передаём только цифры, без префикса `act_` из карты: иначе Ads Manager
    может открыть кабинет профиля по умолчанию. Разборщик входящего URL допускает
    оба формата, поэтому его успешный round trip сам по себе не проверяет запрос.
    """
    from datetime import date as _date, timedelta as _td
    import re

    act_id = act.strip().removeprefix("act_")
    if not re.fullmatch(r"[0-9]+", act_id):
        raise ValueError("ad_account_id должен содержать только цифры, с необязательным префиксом act_")
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
        page.wait_for_selector(SEL_GRID, timeout=TABLE_TIMEOUT_MS)
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


#: Поиск показанного периода целиком внутри страницы — один вызов вместо
#: обхода по узлу за раз.
#:
#: Запасной путь ходил по всем `div[role="button"]` и спрашивал `inner_text()`
#: у каждого **из Python**: на странице Ads Manager их сотни, и каждый вопрос —
#: обращение в браузер. Замер 12.09: сверка периода стоила 22 секунды из 54 на
#: чтение аккаунта с пятью кампаниями — сорок процентов всего времени, при
#: шести переходах по 3.7 с.
#:
#: Ровно так же однажды тормозило чтение итогов: обход `query_selector_all` с
#: `inner_text()` стоил 175 секунд против 40. Лечение то же — спросить страницу
#: один раз.
_FIND_PERIOD_JS = r"""
([marked, fallback, pattern]) => {
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();

  const visible = el => el.getClientRects().length && !el.closest('[hidden],[aria-hidden="true"],[inert]') && getComputedStyle(el).visibility !== 'hidden';
  const markedText = new Set();
  for (const sel of marked) {
    let candidates = [];
    try { candidates = document.querySelectorAll(sel); } catch (_) { continue; }
    for (const el of candidates) {
      if (!visible(el)) continue;
      const text = norm(el.getAttribute('aria-label') || el.innerText || '');
      if (text) markedText.add(text);
    }
  }
  if (markedText.size) return markedText.size === 1 ? [...markedText][0] : '';

  let re;
  try { re = new RegExp(pattern, 'i'); } catch (e) { return ''; }
  let nodes = [];
  try { nodes = document.querySelectorAll(fallback); } catch (e) { return ''; }
  const texts = new Set();
  for (const el of nodes) {
    if (!visible(el)) continue;
    const text = norm(el.innerText || '');
    if (text.length > 0 && text.length < 80 && re.test(text)) texts.add(text);
  }
  return texts.size === 1 ? [...texts][0] : '';
}
"""


def _period_once(page) -> str:
    """Один взгляд на страницу, одним обращением в браузер."""
    try:
        found = page.evaluate(_FIND_PERIOD_JS,
                              [list(SEL_PERIOD), SEL_PERIOD_FALLBACK, RE_PERIOD_DATE])
    except Exception:
        return ""
    return str(found or "").strip()


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


#: Прокрутить список кампаний к низу. Строка итогов стоит **под** ними, и в
#: невысоком окне Ads Manager её не отрисовывает вовсе — искать в DOM нечего.
#:
#: 8 сентября прогон на 1–8 сентября сорвался именно так: на снимке окно
#: обрывается сразу под первой строкой кампании, даты стоят верные, а строки
#: итогов на странице нет. Двадцать секунд ожидания ничего не меняли: ждать
#: можно сколько угодно того, что не рисуется.
#:
def _scroll_to_totals(page) -> None:
    """Показать низ списка, где живёт строка итогов. Молча, если не вышло."""
    try:
        page.evaluate(_SCROLL_TO_TOTALS_JS)
    except Exception:
        pass


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
        # Прокрутка перед каждым осмотром, а не однажды: список догружается по
        # мере прокрутки, и низ у него появляется не сразу.
        _scroll_to_totals(page)
        value, has_label = _scan_totals(page)
        if value is not None:
            return value, "строка итогов, «Total spent»"
        if has_label:
            label = page.query_selector(SEL_TOTALS_LABEL)
            if label is not None:
                break
        _sleep(1)
    if label is None:
        return None, (
            f"строка «Results from …» не появилась за {TOTALS_TRIES} с "
            "даже после прокрутки к низу списка"
        )

    try:
        label.scroll_into_view_if_needed(timeout=5_000)
        label.hover(timeout=5_000)
    except Exception:
        return None, "к надписи итогов не удалось подвести курсор"

    _sleep(1.5)
    clicked = False
    try:
        for b in page.query_selector_all(SEL_BUTTON):
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


# ══ Дерево аккаунта: кампании и объявления ════════════════════════════════
#
# То, на что встанет переключатель (`acl-113`). Строки, которые дешборд рисует
# под аккаунтом сегодня, приходят из книги статистики: у них есть имя и нет
# ничего больше. Тремя способами это не годится для тумблера — в книге нет id
# объектов Meta, она отстаёт на сутки, и столбца on/off в ней нет вовсе.
#
# Всё три есть на экране, который чтение уже открывает.
#
# Переезжает в RPA-трек вместе с остальным чтением (`acl-1121`). Отдельным
# модулем это не заведено намеренно: селекторы, ожидание таблицы и путь
# прицепки лежат здесь, и новый модуль в `fb_ads_rpa` импортировал бы их
# обратно — то есть RPA зависел бы от дешборда. Переносить надо одним куском.

ADS_MANAGER_LEVELS: dict[str, str] = {
    "campaign": ADS_MANAGER,
    "adset": "https://adsmanager.facebook.com/adsmanager/manage/adsets",
    "ad": "https://adsmanager.facebook.com/adsmanager/manage/ads",
}

#: Уровни, которые читаем и на которые встанет тумблер.
#:
#: **Кампания и объявление** — решение владельца 12.09, поправляющее прежнее.
#: Сначала было записано «ad set и объявление, кампания вне объёма», и это
#: оказалось неверно для того, как здесь работают: кампания и ad set идут один
#: к одному, а деньги останавливает именно кампания. Живой аккаунт показал
#: почему — строки с тумблером `on` и доставкой `Campaign off`: ad set включён,
#: а не крутится ничего, потому что выключена кампания над ним.
#:
#: Ad set остаётся известным уровнем (его умеет читать `read_level`), но в
#: обход не входит: лишний заход на живой аккаунт ради строки, которую никто
#: не трогает.
TREE_LEVELS: tuple[str, ...] = ("campaign", "ad")

#: Каким параметром сузить список объявлений до одной кампании.
TREE_PARENT_PARAM: dict[str, str] = {
    "campaign": "selected_campaign_ids",
    "adset": "selected_adset_ids",
}

#: Сколько кампаний обойти за объявлениями. Каждая — отдельный заход на живой
#: аккаунт, и на большом кабинете чтение иначе растянется на десятки минут.
#: Превышение не замалчивается: сколько осталось непрочитанным, сказано вслух.
TREE_MAX_PARENTS = 25


@dataclass
class AdObject:
    """Одна строка списка: то, что нужно, чтобы её показать и переключить."""

    level: str                # campaign | adset | ad
    id: str = ""              # id объекта в Meta
    name: str = ""            # как аккаунт его называет
    switch: str = "unknown"   # on | off | unknown — положение тумблера
    switch_label: str = ""    # подпись тумблера словами Meta
    delivery: str = ""        # колонка Delivery, дословно

    #: Числа строки — теми же словами, какими их написала Meta. Не разбираем в
    #: числа намеренно: «$22.89 Per Lead (Form)» несёт и сумму, и за что она,
    #: а `22.89` — только половину. Считать по ним мы ничего не собираемся;
    #: они нужны, чтобы человек видел, что именно останавливает.
    spend: str = ""
    results: str = ""
    cost_per_result: str = ""

    id_from: str = ""         # откуда взят id — чтобы знать, что сломалось

    #: Чей это объект. У объявления — id его кампании, и известен он **по
    #: построению**: список объявлений спрашивается по одной кампании за раз.
    #: Вычитать родителя со страницы нечем — колонка «Ad set name» есть в
    #: заголовках, а ячейки у неё в разметке не нашлось.
    parent_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _looks_spent(text: str) -> bool:
    """Есть ли в строке спенда ненулевая сумма.

    Число не разбираем: в колонке лежит то, что написала Meta, — с её валютой,
    разделителями и локалью. Достаточно вопроса «есть ли хоть одна ненулевая
    цифра»: `$0.00` и `—` его не проходят, `$0.14` и `1 957,42 ₪` проходят.
    Разбирать всерьёз значило бы завести вторую копию правил форматирования
    Meta и однажды разойтись с ней на чужой валюте.
    """
    return any(ch in "123456789" for ch in str(text or ""))


def _worth_visiting(parent: "AdObject") -> bool:
    """Стоит ли отдельного захода за объявлениями этой кампании.

    Каждый заход — загрузка страницы Ads Manager, и на них уходит всё время
    чтения: один заход за списком кампаний плюс по одному на каждую
    (`acl-113d`). Сократить их можно только одним честным способом — не
    ходить туда, где смотреть нечего.

    Пропускаем ровно один случай: **кампания выключена и за период ничего не
    потратила**. Её объявления не тратят деньги и не подлежат выключению —
    ради чего дерево и читается.

    Всё остальное читается как прежде. Включённая без спенда может быть
    запущена сегодня; выключенная со спендом — это деньги, потраченные за
    период, и человек вправе увидеть, на что. Неизвестное положение тумблера
    (`unknown`) считается поводом сходить: не знаем — значит не пропускаем.
    """
    if _looks_spent(parent.spend):
        return True
    return parent.switch != "off"


def _tree_url(act: str, level: str, date_from: str, date_to: str = "",
              parent_level: str = "", parent_id: str = "") -> str:
    """Адрес уровня. Разбор периода и номера кабинета — общий с `_url`.

    `parent_id` сужает список объявлений до одного родителя — кампании или ad
    set'а. Это единственный способ узнать, чьё объявление: в самой строке
    родителя нет — колонка «Ad set name» в заголовках есть, а ячейки у неё в
    разметке не оказалось.

    Он же оказался единственным способом увидеть объявления вообще. Общий
    список 12.09 отдал шесть строк на $661 при спенде аккаунта $2 117; проход
    по родителям дал двадцать на $1 957. Чем именно был сужен «общий» список,
    страница не говорит — а раз так, доверять ему нельзя.
    """
    if level not in ADS_MANAGER_LEVELS:
        raise ValueError(f"неизвестный уровень {level!r}")
    base = _url(act, date_from, date_to)
    url = ADS_MANAGER_LEVELS[level] + base[len(ADS_MANAGER):]
    if parent_id:
        if level != "ad":
            raise ValueError("сузить до родителя можно только список объявлений")
        param = TREE_PARENT_PARAM.get(parent_level)
        if not param:
            raise ValueError(f"неизвестный родительский уровень {parent_level!r}")
        if not parent_id.isdigit():
            raise ValueError("id родителя должен состоять из цифр")
        url += "&" + param + "=" + parent_id
    return url


#: Какой тип объекта Meta пишет в разметке на каждом уровне.
#:
#: **Словарь здесь легаси, и читать его буквально нельзя.** На странице ad
#: set'ов строки помечены `CAMPAIGN`, на странице объявлений — `ADGROUP`: так
#: назывались эти уровни в старом Ads API, где «campaign» значил нынешний ad
#: set, а «adgroup» — объявление. Кто прочтёт `forObjectType(toggle,CAMPAIGN)`
#: как «тумблер кампании», ошибётся на целый уровень — а это разница между
#: «остановить один ad set» и «погасить всё под кампанией».
#:
#: Зато токен даёт проверку, которой иначе нет: адрес уровня можно открыть и
#: получить не тот список, и заметить это по одному URL нельзя.
#: Кампания — `CAMPAIGN_GROUP`, ad set — `CAMPAIGN`, объявление — `ADGROUP`.
#: Все три сняты с живого аккаунта; словарь легаси целиком, и `CAMPAIGN` здесь
#: значит ad set.
_LEVEL_KIND: dict[str, str] = {
    "campaign": "CAMPAIGN_GROUP", "adset": "CAMPAIGN", "ad": "ADGROUP",
}

#: Строка итогов. Не объект: переключать там нечего, а в список она попадает
#: такой же разметкой, как настоящие строки.
_TOTALS_ROW_ID = "-1"

#: Разбор таблицы одним заходом в страницу.
#:
#: Идём по `data-surface` — собственной разметке Meta для её же логов. Она
#: несёт и id строки, и назначение ячейки:
#:
#:     /am/table/table_row:123456789000000001unit/table_cell:forObjectType(name,CAMPAIGN)
#:
#: Первая версия искала `role="row"` и `role="cell"` и не нашла ни одной
#: строки: у грида Ads Manager размечен заголовком только он сам —
#: пятнадцать `columnheader` и **ноль** `role="cell"`, а тело таблицы лежит под
#: `role="presentation"`. Заголовки при этом прочитались, то есть разбор
#: возвращал «ноль строк» с видом работающего.
#:
#: id тоже брался не оттуда: искалось любое число в 15-18 цифр в атрибутах, и
#: первыми находились `<path d="M 25.19238815542512 ...">` — в путях SVG таких
#: цепочек сколько угодно. Здесь id берётся из одного определённого места.
_READ_TREE_JS = r"""
(level) => {
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const visible = (el) => el.getClientRects().length > 0 &&
    !el.closest('[hidden], [aria-hidden="true"]') &&
    getComputedStyle(el).visibility !== 'hidden';
  const tables = Array.from(document.querySelectorAll('[role="table"], [role="grid"]'))
    .filter(visible).filter(el => !el.parentElement.closest('[role="table"], [role="grid"]'));
  const refuse = (how) => ({rows: [], how, kinds: [], error: how});
  if (!tables.length) return refuse('no-table');
  if (tables.length !== 1) return refuse('ambiguous-tables');
  const t = tables[0];
  if (t.closest('[aria-busy="true"]') ||
      Array.from(t.querySelectorAll('[aria-busy="true"], [role="progressbar"]')).some(visible))
    return refuse('table-loading');

  const marked = t.querySelectorAll('[data-surface*="table_row:"]');
  if (!marked.length) return {rows: [], how: 'no-row-surfaces', kinds: []};

  const rows = new Map();
  const kinds = new Set();
  const CELL = /table_row:(-?\d+)[a-z]*\/table_cell:([^\/]+)/;
  const KIND = /forObjectType\((?:name|toggle|delivery),([A-Z_]+)\)/;

  for (const el of marked) {
    if (!visible(el)) continue;
    const m = (el.getAttribute('data-surface') || '').match(CELL);
    if (!m) continue;
    const id = m[1], cell = m[2];
    if (id === '-1') continue;   // строка итогов — не объект

    let r = rows.get(id);
    if (!r) {
      r = {id: id, name: '', delivery: '', switch: 'unknown', switch_label: '',
           id_from: 'data-surface', spend: '', results: '', cost_per_result: '', kinds: []};
      rows.set(id, r);
    }
    const k = cell.match(KIND);
    if (k) {
      kinds.add(k[1]);
      if (!r.kinds.includes(k[1])) r.kinds.push(k[1]);
    }

    // Одна и та же разметка висит на нескольких вложенных узлах: берём первое
    // непустое, а не последнее — внутренние копии часто пустые.
    if (cell.startsWith('forObjectType(name')) {
      const txt = norm(el.innerText);
      if (txt && !r.name) r.name = txt;
    } else if (cell.startsWith('forObjectType(delivery')) {
      const txt = norm(el.innerText);
      if (txt && !r.delivery) r.delivery = txt;
    } else if (cell.startsWith('forObjectType(toggle')) {
      const sw = el.querySelector('[role="switch"], input[type="checkbox"]');
      if (sw && r.switch === 'unknown') {
        r.switch_label = norm(sw.getAttribute('aria-label') || '');
        const c = sw.getAttribute('aria-checked');
        if (c === 'true') r.switch = 'on';
        else if (c === 'false') r.switch = 'off';
      }
    } else if (cell === 'spend') {
      const txt = norm(el.innerText);
      if (txt && !r.spend) r.spend = txt;
    } else if (cell.startsWith('forAttributionWindow(results')) {
      const txt = norm(el.innerText);
      if (txt && !r.results) r.results = txt;
    } else if (cell.startsWith('forAttributionWindow(cost_per_result')) {
      const txt = norm(el.innerText);
      if (txt && !r.cost_per_result) r.cost_per_result = txt;
    }
  }

  // Какой вид списка выбран. У Meta это ничем не помечено, кроме цвета
  // подчёркивания: у выбранной вкладки оно синее, у остальных серое, и ни
  // aria-selected, ни aria-current, ни tabindex здесь нет.
  //
  // Поэтому ищем «не такую, как все», а не конкретный цвет: тема у Meta
  // меняется, а то, что выбранная вкладка одна и подчёркнута иначе, — нет.
  // Две «не такие» значит, что правило перестало работать; тогда честнее
  // промолчать, чем назвать вид наугад.
  let view = '';
  const tabs = Array.from(document.querySelectorAll('[data-surface$="quickviewTab"]'));
  if (tabs.length > 1) {
    const marks = tabs.map(e => {
      let colour = '';
      for (const k of e.querySelectorAll('*')) {
        const st = getComputedStyle(k);
        if (parseFloat(st.borderBottomWidth) > 0) { colour = st.borderBottomColor; break; }
      }
      return {text: norm(e.innerText), colour: colour};
    });
    const seen = {};
    marks.forEach(m => { if (m.colour) seen[m.colour] = (seen[m.colour] || 0) + 1; });
    for (const m of marks) {
      if (!m.colour || seen[m.colour] !== 1) continue;
      if (view) { view = ''; break; }
      view = m.text;
    }
  }

  // Ads Manager can virtualize rows outside a scrollable list. This snapshot
  // cannot certify that it contains the entire account; expose that limit.
  const containers = [t, ...t.querySelectorAll('*')];
  for (let el = t.parentElement; el; el = el.parentElement) containers.push(el);
  const scrollable = containers.some(el => visible(el) &&
    /auto|scroll/.test(getComputedStyle(el).overflowY) &&
    el.scrollHeight > el.clientHeight + 2);
  return {rows: Array.from(rows.values()), how: 'data-surface',
          kinds: Array.from(kinds), view: view, partial: scrollable};
}
"""


def read_level(page, level: str) -> tuple[list[AdObject], str, str]:
    """Разобрать открытую таблицу одного уровня.

    Возвращает `(объекты, чем разобрано, ошибка)`. Страница должна быть уже
    на нужном уровне: переходами заведует `_read_tree`, которому есть что
    сверять (кабинет, период), а этой функции — нет.

    Ничего не нажимает. Это чтение, и единственное, что здесь происходит со
    страницей, — вопрос к ней.
    """
    if level not in ADS_MANAGER_LEVELS:
        return [], "", f"неизвестный уровень {level!r}"
    try:
        got = page.evaluate(_READ_TREE_JS, level)
    except Exception as exc:
        return [], "", f"таблица {level} не разобрана: {exc}"
    if not isinstance(got, dict):
        return [], "", f"таблица {level}: страница ответила не тем"

    how = str(got.get("how") or "")
    view = str(got.get("view") or "")
    if view:
        # Вид — часть ответа на «что мы прочитали». Список «Active ads» и
        # список «All ads» — разные списки, и одинаково выглядящее число
        # строк в них значит разное.
        how = f"{how}({view})"
    if got.get("error"):
        return [], how, f"таблица {level}: {got['error']}"
    rows = got.get("rows") or []
    want = _LEVEL_KIND[level]
    # Every accepted object must identify itself, not borrow another row's kind.
    if not isinstance(rows, list) or any(
        not isinstance(r, dict)
        or not re.fullmatch(r"[0-9]+", str(r.get("id") or ""))
        or not str(r.get("name") or "").strip()
        or set(r.get("kinds") or []) != {want}
        for r in rows
    ):
        return [], how, f"таблица {level}: строка не подтвердила id, имя или что это за объекты ({want})"
    objects = [
        AdObject(
            level=level,
            id=str(r.get("id") or ""),
            name=str(r.get("name") or ""),
            switch=str(r.get("switch") or "unknown"),
            switch_label=str(r.get("switch_label") or ""),
            delivery=str(r.get("delivery") or ""),
            spend=str(r.get("spend") or ""),
            results=str(r.get("results") or ""),
            cost_per_result=str(r.get("cost_per_result") or ""),
            id_from=str(r.get("id_from") or ""),
        )
        for r in rows
        if isinstance(r, dict)
    ]
    if not objects:
        return [], how, f"в таблице {level} не нашлось ни одной строки ({how})"

    # Уровень сверяется по тому, что Meta написала о самих строках, а не по
    # адресу: открыть `/manage/adsets` и получить другой список — можно, и по
    # одному URL этого не увидеть. Спутать уровни здесь дороже всего: на это
    # встанет тумблер, и разница между ad set'ом и кампанией — разница между
    # «остановить одно» и «погасить всё под ней».
    kinds = {str(k) for k in (got.get("kinds") or [])}
    want = _LEVEL_KIND[level]
    if kinds and kinds != {want}:
        return [], how, (
            f"на уровне {level} ждали объекты {want}, а страница разметила "
            f"{', '.join(sorted(kinds))} — список не тот, ничего не берём"
        )
    if not kinds:
        return [], how, f"на уровне {level} страница не сказала, что это за объекты"
    warning = (
        "прочитаны отрисованные строки; список прокручивается, полнота не подтверждена"
        if got.get("partial") else ""
    )
    return objects, how, warning


def _read_tree(
    page, act: str, expected_act: str, date_from: str, date_to: str = "",
) -> tuple[list[dict], str, str]:
    """Пройти уровни и собрать дерево. Возвращает `(объекты, чем, ошибка)`.

    Кабинет и период сверяются **на каждом уровне заново**. Переход — это новый
    адрес, а Ads Manager умеет открыть кабинет профиля по умолчанию вместо
    запрошенного; без сверки чужое дерево пришло бы с видом правильного ответа.
    Ровно та же осторожность, что и со спендом, и по той же причине.

    Редактор объявления не открывается ни на каком шаге. Meta при каждом его
    открытии предвыбирает «Instant form (suggested)» в Browser add-ons и
    автосохраняет — чистое Leads-объявление получает ошибку #100 от одного
    визита (DQ204, 02.08). Всё читается со списка.

    Заходов столько же, сколько кампаний, плюс один, и в этом вся стоимость
    чтения (`acl-113d`: 1,5–3 минуты). Обойтись одним заходом нельзя — общий
    список объявлений неполон и не говорит, чем сужен, — поэтому сокращается
    не число заходов на кампанию, а число кампаний: выключенные и ничего не
    потратившие за период пропускаются (`_worth_visiting`), и сколько именно
    пропущено, сказано в ошибках. Молчаливый пропуск читался бы как «это
    всё» — та же беда, что и молчаливое усечение по `TREE_MAX_PARENTS`.
    """
    objects: list[dict] = []
    hows: list[str] = []
    errors: list[str] = []

    def _context_matches(what: str, parent_id: str = "") -> bool:
        from urllib.parse import parse_qs, urlsplit

        if _act_from_url(page, tries=1) != expected_act:
            errors.append(f"{what}: открылся не тот кабинет — пропущено")
            return False
        if not _period_matches(_shown_period(page), date_from, date_to):
            errors.append(f"{what}: на экране другой период — пропущено")
            return False
        if parent_id:
            params = parse_qs(urlsplit(page.url).query)
            if params.get(TREE_PARENT_PARAM[top]) != [parent_id]:
                errors.append(f"{what}: фильтр родительской кампании изменился — пропущено")
                return False
        return True

    def _read_settled(level: str, tries: int = 8):
        """Дочитаться до таблицы, а не до её загрузки.

        `read_level` отказывается, пока таблица помечена `aria-busy` или
        показывает полоску загрузки, — и правильно делает: читать
        недогруженную значит получить половину строк с видом целого. Но
        звали её ровно один раз, и отказ был окончательным.

        15.09, попытка измерить `acl-113d`: на живом кабинете дерево не
        прочиталось ни разу из четырёх — `таблица campaign: table-loading`
        при исправном спенде рядом. То есть дерево не работало вовсе, а
        выглядело это как «дерево пустое».

        Столько же попыток и с тем же смыслом, сколько у пострановой
        разбивки: там та же таблица Ads Manager и та же беда.
        """
        rows, how, err = [], "", ""
        for attempt in range(tries):
            rows, how, err = read_level(page, level)
            if "table-loading" not in f"{how} {err}":
                return rows, how, err
            if attempt < tries - 1:
                _sleep(1.5)
        return rows, how, err

    def _open(url: str, what: str, parent_id: str = "") -> bool:
        """Перейти и сверить, что открылось именно то, что просили."""
        try:
            page.goto(url, timeout=TABLE_TIMEOUT_MS, wait_until="domcontentloaded")
        except Exception as exc:
            errors.append(f"{what}: переход не удался ({exc})")
            return False
        if not _wait_for_grid(page):
            errors.append(f"{what}: таблица не отрисовалась за отведённое время")
            return False
        return _context_matches(what, parent_id)

    # Сначала верхний уровень: его id нужны, чтобы спросить объявления каждого.
    top = TREE_LEVELS[0]
    if _open(_tree_url(act, top, date_from, date_to), top):
        parents, how, err = _read_settled(top)
        if not _context_matches(top):
            return [], how, "; ".join(errors)
        if how:
            hows.append(f"{top}:{how}")
        if err:
            errors.append(err)
        objects.extend(o.to_dict() for o in parents)

        wanted = [p for p in parents if p.id]

        # Выключенные кампании без спенда за период пропускаем: заход стоит
        # загрузки страницы, а смотреть там нечего — ни трат, ни того, что
        # можно выключить. Это `acl-113d`: чтение аккаунта занимало 1,5–3
        # минуты именно на этих заходах.
        idle = [p for p in wanted if not _worth_visiting(p)]
        if idle:
            wanted = [p for p in wanted if _worth_visiting(p)]
            # Пропуск не менее молчалив, чем усечение, и так же вводит в
            # заблуждение: дерево без этих объявлений выглядит как полное.
            errors.append(
                f"объявления не спрашивались у {len(idle)} выключенных кампаний "
                f"без спенда за период"
            )

        if len(wanted) > TREE_MAX_PARENTS:
            # Молчаливое усечение читается как «это всё». Говорим вслух.
            errors.append(
                f"объявления прочитаны у первых {TREE_MAX_PARENTS} из "
                f"{len(wanted)}: каждая — отдельный заход на аккаунт"
            )
            wanted = wanted[:TREE_MAX_PARENTS]

        ads_how: set[str] = set()
        for parent in wanted:
            what = f"{top} {parent.id}"
            url = _tree_url(act, "ad", date_from, date_to,
                            parent_level=top, parent_id=parent.id)
            if not _open(url, what, parent.id):
                continue
            got, how, err = _read_settled("ad")
            if not _context_matches(what, parent.id):
                continue
            if how:
                ads_how.add(how)
            if err:
                errors.append(f"{what}: {err}")
            for o in got:
                o.parent_id = parent.id
                objects.append(o.to_dict())
        if ads_how:
            hows.append("ad:" + "/".join(sorted(ads_how)))

    # A stale/ignored filter may return the same ad under different parents.
    # Its ownership is then ambiguous: keep neither association.
    owners: dict[str, set[str]] = {}
    for obj in objects:
        if obj["level"] == "ad":
            owners.setdefault(obj["id"], set()).add(obj["parent_id"])
    ambiguous = {oid for oid, parents in owners.items() if len(parents) > 1}
    if ambiguous:
        objects = [o for o in objects if not (o["level"] == "ad" and o["id"] in ambiguous)]
        errors.append("одни объявления вернулись у разных кампаний — неоднозначные связи пропущены")
    return objects, " ".join(hows), "; ".join(errors)


# ══ Выключить кампанию или объявление ═════════════════════════════════════
#
# Первое действие ACL, меняющее живой аккаунт. До сих пор он только строил
# черновики, которые публиковал человек, и читал.
#
# **Только выключение** (решение владельца 12.09). Включать отсюда нельзя, и
# это не недоделка: выключение денег не тратит, включение тратит. Обратно —
# руками в Ads Manager, там же, где человек видит последствия.
#
# Ничего, кроме тумблера, здесь не нажимается. Редактор объявления не
# открывается: Meta портит Leads-объявление одним его открытием (DQ204,
# 02.08), а тумблер лежит в строке списка.

#: Сколько ждать, пока Meta подтвердит переключение. Тумблер рисуется сразу, а
#: сохраняется запросом — и отката ждать дольше, чем рисования.
TURN_OFF_TRIES = 12
TURN_OFF_PAUSE_S = 0.75


def _turn_off_log() -> Path:
    """Журнал выключений — рядом с показаниями, тем же путём."""
    return Path(_store_path()).with_name("turn_off_log.jsonl")


def _record_turn_off(out: "TurnOffResult") -> None:
    """Записать, что случилось. Строкой в журнал и строкой в лог лаунчера.

    Первое живое нажатие 12.09 вернуло отказ, и прочитать его причину было
    негде: результат уходил в ответ HTTP и исчезал. Действие, меняющее чужой
    аккаунт, обязано оставлять след — «что-то изменилось вчера» не ответ.

    Ни запись, ни лог не смеют уронить сам результат, поэтому оба под
    `except`: потерять журнал плохо, потерять ответ — хуже.
    """
    try:
        log.info(
            "turn_off %s %s %s → %s%s",
            out.account, out.level, out.object_id, out.state,
            f" ({out.error or out.how})" if (out.error or out.how) else "",
        )
    except Exception:
        pass
    try:
        path = _turn_off_log()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(out.to_dict(), ensure_ascii=False) + "\n")
    except Exception:
        pass


def _mark_object_off(account: str, object_id: str, delivery: str = "") -> int:
    """Поправить сохранённые чтения: этот объект теперь выключен.

    Иначе дерево остаётся старше действия. 12.09 чтение легло в 15:52:14, а
    выключение случилось в 15:52:38 — после перезагрузки страницы объявление
    снова показывалось включённым, да ещё с кнопкой на том, что уже выключено.

    Правим во **всех** чтениях этого аккаунта: выключенность к периоду не
    относится, объект выключен вообще, а не «за восьмое сентября».

    Доставку ставим ту, что реально прочитали при подтверждении, — Meta сразу
    после выключения пишет «Processing», а не «Off». Не прочитали — не
    выдумываем: пусть остаётся прежняя, чем ложная.
    """
    touched = 0
    with _STORE_LOCK:
        data = _load_all()
        prefix = account + "|"
        for key, raw in data.items():
            if not key.startswith(prefix) or not isinstance(raw, dict):
                continue
            for obj in (raw.get("objects") or []):
                if str(obj.get("id") or "") != str(object_id):
                    continue
                obj["switch"] = "off"
                if delivery:
                    obj["delivery"] = delivery
                touched += 1
        if touched:
            _save_all(data)
        # Живая копия в памяти — тот же ответ, что отдаёт `get_reading`.
        for key, reading in list(_UNSAVED.items()):
            if not key.startswith(prefix):
                continue
            for obj in (reading.objects or []):
                if str(obj.get("id") or "") != str(object_id):
                    continue
                obj["switch"] = "off"
                if delivery:
                    obj["delivery"] = delivery
                touched += 1
    return touched


def _same_account(a: str, b: str) -> bool:
    """Одна и та же подпись аккаунта, записанная по-разному.

    В журнал попадает то, что прислала страница, а спрашивают его по подписи
    из карты. Расхождение в регистре или лишнем пробеле не должно прятать от
    человека его собственное действие.
    """
    return " ".join((a or "").split()).casefold() == " ".join((b or "").split()).casefold()


def turn_off_history(limit: int = 50, account: str = "") -> list[dict]:
    """Последние выключения — новыми вперёд. Для дешборда и для разбора.

    `account` отбирает записи одного кабинета: журнал общий на машину, а
    дерево показывает один аккаунт, и чужая строка в нём читалась бы как своя.
    Отбор идёт ДО счёта, иначе десять действий на соседнем аккаунте вытеснят
    единственное нужное и человек решит, что его нажатие не записалось.
    """
    try:
        lines = _turn_off_log().read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        # A new installation has no journal; other read failures mean unavailable.
        return []
    out: list[dict] = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        if not isinstance(row, dict):
            continue
        row_account = row.get("account")
        if not isinstance(row_account, str) or not row_account.strip():
            continue
        if account and not _same_account(row_account, account):
            continue
        out.append(row)
        if len(out) >= limit:
            break
    return out


@dataclass
class TurnOffResult:
    """Что случилось с одним объектом. Никаких «наверное»."""

    account: str
    level: str
    object_id: str
    name: str = ""
    #: off | already_off | failed | unconfirmed
    #:
    #: `failed` — нажали, и тумблер НЕ стал выключенным: это установленный
    #: факт. `unconfirmed` — попытка оборвалась, и как оно там на самом деле,
    #: мы не знаем: выключиться могло. Выдавать второе за первое нельзя ровно
    #: по той же причине, по какой ниже не выдаётся «Processing» за «Off».
    state: str = "failed"
    #: Что страница написала о доставке в момент подтверждения. Словами Meta:
    #: сразу после выключения она пишет «Processing», а не «Off», и выдавать
    #: одно за другое нельзя.
    delivery: str = ""
    error: str = ""
    how: str = ""             # чем подтверждено — чтобы знать, чему верить
    at: str = ""
    #: Кто нажал. На машине байера это он сам, но «кто-то с этой машины» —
    #: не ответ на вопрос «кто остановил кампанию»: у карты десять хозяев, а
    #: разбирают случившееся потом и не за своим столом.
    requester: str = ""
    #: Чем сделано. Сегодня всегда `rpa`, и поле выглядит лишним — но пустым
    #: по умолчанию оно остаётся намеренно: если однажды появится второй путь
    #: и забудет представиться, журнал промолчит, а не соврёт, что это был RPA.
    route: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


#: Метка, которую поиск вешает на найденный тумблер: через `evaluate` ручку
#: наружу не получить, а нажимать надо именно его.
SEL_MARKED_SWITCH = '[data-acl-switch="1"]'

#: Найти строку по её id и рассказать про её тумблер. Ничего не нажимает.
_FIND_SWITCH_JS = r"""
(args) => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const visible = e => e.getClientRects().length && !e.closest('[hidden],[aria-hidden="true"],[inert]') && getComputedStyle(e).visibility !== 'hidden';
  const fail = why => ({found: false, why});
  document.querySelectorAll('[data-acl-switch]').forEach(e => e.removeAttribute('data-acl-switch'));
  const tables = [...document.querySelectorAll('[role="table"],[role="grid"]')].filter(visible);
  if (tables.length !== 1) return fail('expected one visible table');
  const t = tables[0];
  if (t.closest('[aria-busy="true"]') || [...t.querySelectorAll('[role="progressbar"],[aria-busy="true"]')].some(visible)) return fail('table is still loading');
  const CELL = /table_row:(-?\d+)[a-z]*\/table_cell:([^\/]+)/;
  const names = new Set(), deliveries = new Set(), switches = new Set(), kinds = new Set();
  for (const el of t.querySelectorAll('[data-surface*="table_row:"]')) {
    if (!visible(el)) continue;
    const m = (el.getAttribute('data-surface') || '').match(CELL);
    if (!m || m[1] !== args.id) continue;
    const kind = m[2].match(/forObjectType\([^,]+,([^,)]+)/);
    if (kind) kinds.add(kind[1]);
    if (m[2].startsWith('forObjectType(name') && norm(el.innerText)) names.add(norm(el.innerText));
    if (m[2].startsWith('forObjectType(delivery') && norm(el.innerText)) deliveries.add(norm(el.innerText));
    if (m[2].startsWith('forObjectType(toggle')) {
      el.querySelectorAll('[role="switch"],input[type="checkbox"]').forEach(sw => {
        if (visible(sw)) switches.add(sw);
      });
    }
  }
  if (kinds.size !== 1 || !kinds.has(args.kind) || names.size !== 1 || switches.size !== 1) return fail('row identity or switch is ambiguous');
  const name = [...names][0], sw = [...switches][0];
  if (!args.name || name !== norm(args.name)) return fail('row name changed');
  if (sw.disabled || sw.closest('[aria-disabled="true"]')) return fail('switch is disabled');
  const aria = sw.getAttribute('aria-checked');
  const state = aria === 'true' ? 'on' : aria === 'false' ? 'off' :
    aria === null && sw.matches('input[type="checkbox"]') && !sw.indeterminate ? (sw.checked ? 'on' : 'off') : 'unknown';
  const label = norm(sw.getAttribute('aria-label'));
  const result = {found:true, name, delivery:deliveries.size === 1 ? [...deliveries][0] : '', on:state === 'on', off:state === 'off', label};
  if (args.click) {
    // Identity, context and state are read and acted upon in one JS task.
    // If Meta switched it off meanwhile, clicking again would turn it ON.
    if (location.href !== args.url || label !== args.label || state === 'unknown') return fail('context or switch changed before action');
    if (state === 'on') {
      const r = sw.getBoundingClientRect(), hit = document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);
      if (!hit || (hit !== sw && !sw.contains(hit))) return fail('switch is covered');
      sw.click();
      result.clicked = true;
    }
  }
  sw.setAttribute('data-acl-switch', '1');
  return result;
}
"""

# Re-read the same visible period in the exact browser task that may click.
_SWITCH_ACTION_JS = """args => {
  const period = (""" + _FIND_PERIOD_JS + """)(args.period_args);
  if (!period || period !== args.period) return {found:false, why:'period changed before action'};
  return (""" + _FIND_SWITCH_JS + """)(args);
}"""


@serialized_browser
def turn_off(account: str, level: str, object_id: str, *, parent_id: str = "",
             expected_name: str = "", date_from: str = "", date_to: str = "",
             requester: str = "") -> "TurnOffResult":
    """Выключить и записать, что вышло.

    Запись стоит здесь, а не внутри: у `_turn_off` двадцать путей выхода, и
    расставленные по ним вызовы рано или поздно разойдутся — один забудут.

    По той же причине здесь же проставляются «кто» и «чем»: это единственное
    место, через которое проходит любой исход, включая отказ.

    «Любой исход» включает и падение. `_turn_off` возвращает результат на
    двадцати путях, но не на всех: маршрут в `src/web/app.py` прямо ловит
    `Exception` и отвечает «Could not reach the account» — значит исключение
    здесь предусмотрено, и раньше в этом случае в журнале не оставалось
    ничего. Действие на живом кабинете было, следа нет.

    Для журнала, который обязан отличать неудавшуюся попытку от
    подтверждённого изменения, это дыра в середине: самая неудавшаяся попытка
    из всех не записывалась. Пишем и её — и пробрасываем исключение дальше,
    потому что решать, что ответить человеку, не здесь.

    Пишем её как `unconfirmed`, а не `failed`. `failed` означает «нажали, и
    тумблер не стал выключенным» — установленный факт. Оборванная попытка
    такого факта не даёт: объявление могло и выключиться, просто подтвердить
    это не успели. Записать «не удалось» значило бы соврать в ту сторону, из
    которой потом делают вывод «значит, оно ещё крутится».
    """
    who = (requester or "").strip()
    try:
        out = _turn_off(account, level, object_id, parent_id=parent_id,
                        expected_name=expected_name, date_from=date_from, date_to=date_to)
    except Exception as exc:
        _record_turn_off(TurnOffResult(
            account=account, level=level, object_id=object_id, name=expected_name,
            state="unconfirmed", error=f"{type(exc).__name__}: {exc}"[:300],
            at=_now(), requester=who, route="rpa",
        ))
        raise
    out.requester = who
    out.route = "rpa"
    if out.state in ("off", "already_off"):
        # Не «наверное выключилось»: сюда приходят только подтверждённые
        # исходы, и сохранённое дерево обязано их отражать — иначе оно врёт
        # ровно про то, что человек только что сделал.
        try:
            _mark_object_off(out.account, out.object_id, out.delivery)
        except Exception:
            pass
    _record_turn_off(out)
    return out


def _turn_off(account: str, level: str, object_id: str, *, parent_id: str = "",
              expected_name: str = "", date_from: str = "", date_to: str = "") -> "TurnOffResult":
    """Выключить одну кампанию или одно объявление. Включить отсюда нельзя.

    Период нужен не ради чисел, а ради сверки: на экране должен стоять тот же
    отрезок, что и на дешборде, иначе мы смотрим не на то, что показывали
    человеку, — то же правило, что и у чтения.
    """
    from src.fb_ads_rpa import wizard_guard as wg

    out = TurnOffResult(account=account, level=level, object_id=str(object_id),
                        name=expected_name, at=_now())
    if level not in ("campaign", "ad"):
        out.error = f"выключать умеем кампанию и объявление, а не {level!r}"
        return out
    if not expected_name.strip():
        out.error = "row name is required — read the account again"
        return out
    if not str(object_id).isdigit():
        out.error = "id объекта должен состоять из цифр"
        return out
    if level == "ad" and not str(parent_id).isdigit():
        # Список объявлений сужается по кампании: другого проверенного способа
        # добраться до одной строки у нас нет.
        out.error = "чтобы выключить объявление, нужна его кампания"
        return out

    entry = _entry_for(account)
    if entry is None:
        out.error = f"{account} нет в карте аккаунтов"
        return out
    act = (entry.ad_account_id or "").strip()
    if not act:
        out.error = f"у {account} не заполнен ad_account_id"
        return out
    date_from = date_from or yesterday()
    date_to = date_to or date_from

    if level == "campaign":
        url = _tree_url(act, "campaign", date_from, date_to)
    else:
        url = _tree_url(act, "ad", date_from, date_to,
                        parent_level="campaign", parent_id=str(parent_id))

    conn = None
    try:
        try:
            conn = _attach_only(entry, account)
        except ProfileNotAttachable as exc:
            out.error = str(exc)
            return out
        page = conn.page
        page.goto(url, timeout=TABLE_TIMEOUT_MS, wait_until="domcontentloaded")
        if not _wait_for_grid(page):
            out.error = "Ads Manager не отрисовался за отведённое время"
            return out

        from urllib.parse import parse_qs, urlsplit
        expected_act = act if act.startswith("act_") else "act_" + act
        approved_url = page.url
        def context_matches():
            parts = urlsplit(page.url)
            params = parse_qs(parts.query)
            return (page.url == approved_url and wg.is_ads_manager_url(page.url)
                    and parts.path == urlsplit(ADS_MANAGER_LEVELS[level]).path
                    and _act_from_url(page, tries=1) == expected_act
                    and _period_matches(_shown_period(page), date_from, date_to)
                    and (level != "ad" or params.get("selected_campaign_ids") == [str(parent_id)]))

        if not context_matches():
            out.error = "account, period, level or parent changed — nothing touched"
            return out
        args = {"id":out.object_id, "kind":_LEVEL_KIND[level], "name":expected_name}
        found = page.evaluate(_FIND_SWITCH_JS, args)
        if not found or not found.get("found"):
            out.error = (found or {}).get("why") or "строку не нашли"
            return out
        if not context_matches():
            out.error = "context changed during lookup"
            return out
        out.name = str(found.get("name") or expected_name)

        # Имя сверяется, если дешборд его прислал: id мог устареть, а строка
        # под ним — оказаться чужой. Выключить не то хуже, чем не выключить.
        if expected_name and out.name and out.name != expected_name:
            out.error = (
                f"под этим id на странице «{out.name}», а дешборд показывал "
                f"«{expected_name}» — ничего не трогаем"
            )
            return out

        # Запрет на опасные нажатия — тот же, что у мастера создания
        # (`acl-113c`). Тумблер подписан «On/off» и под него не попадает; смысл
        # в том, чтобы поймать случай, когда мы схватили не тот элемент.
        why = wg.forbidden_click_reason(found.get("label"))
        if why:
            out.error = f"нажимать это нельзя: {why}"
            return out

        if found.get("off"):
            out.delivery = str(found.get("delivery") or "")
            out.state = "already_off"
            out.how = "тумблер уже выключен — ничего не нажимали"
            return out
        if not found.get("on"):
            out.error = "положение тумблера непонятно — ничего не трогаем"
            return out

        page.locator(SEL_MARKED_SWITCH).scroll_into_view_if_needed(timeout=15_000)
        if not context_matches():
            out.error = "context changed before action — nothing touched"
            return out
        period = _period_once(page)
        if not _period_matches(period, date_from, date_to):
            out.error = "period could not be confirmed before action"
            return out
        acted = page.evaluate(_SWITCH_ACTION_JS, dict(args, click=True, url=approved_url,
            label=found.get("label"), period=period,
            period_args=[list(SEL_PERIOD), SEL_PERIOD_FALLBACK, RE_PERIOD_DATE]))
        if not acted or not acted.get("found"):
            out.error = (acted or {}).get("why") or "action refused"
            return out
        if not acted.get("clicked"):
            out.state = "already_off"
            out.delivery = str(acted.get("delivery") or "")
            out.how = "already off at the final check; no click"
            return out

        # Wait for the optimistic update, then independently verify persistence.
        for _ in range(TURN_OFF_TRIES):
            now = page.evaluate(_FIND_SWITCH_JS, args)
            if now and now.get("off"):
                break
            page.wait_for_timeout(int(TURN_OFF_PAUSE_S * 1000))
        else:
            out.error = "clicked but off was not confirmed; read the account again"
            return out

        page.goto(url, timeout=TABLE_TIMEOUT_MS, wait_until="domcontentloaded")
        if not _wait_for_grid(page):
            out.error = "confirmation table did not load; state unknown"
            return out
        again = None
        for attempt in range(TURN_OFF_TRIES):
            if attempt:
                page.wait_for_timeout(int(TURN_OFF_PAUSE_S * 1000))
            if not context_matches():
                out.error = "confirmation context changed; state unknown"
                return out
            again = page.evaluate(_FIND_SWITCH_JS, args)
            if again and again.get("off") and context_matches():
                out.delivery = str(again.get("delivery") or "")
                out.state = "off"
                out.how = f"confirmed after reload (observation {attempt + 1})"
                return out

        if not again or not again.get("found"):
            out.error = "после перезагрузки строку не нашли — состояние неизвестно"
            return out
        out.error = "после перезагрузки тумблер снова включён — Meta не приняла"
        return out
    except Exception as exc:                      # noqa: BLE001
        out.error = f"не получилось: {exc}"
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
