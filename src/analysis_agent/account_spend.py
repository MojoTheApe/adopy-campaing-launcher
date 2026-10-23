"""Спенд, прочитанный прямо с рекламного аккаунта.

Маршрутов будет два — браузер в профиле Multilogin и токен Marketing API, —
и оба обязаны отвечать одинаково. Здесь описан общий ответ и сделан первый.

Три правила, которые здесь не обсуждаются:

* **Только по нажатию.** Никаких таймеров и фоновых обходов: опрос Ads Manager
  по расписанию уже предшествовал чекпоинту на телефон. Нажатие человека —
  человеческое поведение, расписание — нет.
* **Никогда не перезапускать открытое.** Открытый автоматизацией профиль
  читаем в его же окне. Перезапуск ради одного числа разлогинивает аккаунт —
  так и вышло на x01, — а ветка перезапуска вдобавок умеет терять прокси.
  Закрытый профиль чтение запускает само (ACL-1580) тем же путём, что кнопка
  «open profile» — это обычное открытие, не перезапуск, — и после чтения
  мягко закрывает то, что запустило. Обычный браузер не годится ни при каких
  обстоятельствах.
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
import uuid
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

from config import paths
from src import acl_log
from src.fb_ads_rpa.operations import (
    browser_operation,
    profile_lock,
    publish_lease,
    serialized_browser,
)
from src.fb_ads_rpa import profile_trouble as _profile_trouble

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

#: Сколько секунд ждать, пока таблица перестанет быть «занятой» (aria-busy /
#: progressbar), прежде чем отказать чтению дерева.
TABLE_BUSY_TRIES = 15


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
#: Оба порядка: «Sep 16, 2026» и «16 Sep 2026» — 17.09 Doc20 показал второй, и
#: чтение отказывало при спенде на экране.
RE_PERIOD_DATE = (
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\.?\s+\d{1,2},?\s*\d{4}"
    r"|\d{1,2}\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\.?,?\s+\d{4}"
)

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
  // «Total spent» и «Total Spent» (британская локаль Doc20, 17.09) — без учёта регистра.
  const money = /([$€£₪]\s*[\d,]+(?:\.\d{1,2})?)\s*Total spent/i;
  document.querySelectorAll('[data-acl-totals]').forEach(
    e => e.removeAttribute('data-acl-totals'));
  for (const n of document.querySelectorAll('div, span')) {
    const t = (n.innerText || '').replace(/\s+/g, ' ').trim();
    if (!t) continue;
    if (out.spend === null && t.length <= 600 && t.toLowerCase().indexOf('total spent') !== -1) {
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
    #: ACL-1518: устойчивое имя отказа для экрана (`open_by_hand`) — по нему
    #: строка пишет простой текст вместо «failed · retry». Пусто — обычный отказ.
    error_code: str = ""
    started_at: str = ""
    finished_at: str = ""
    took_s: float = 0.0
    shot: str = ""                # скриншот при неудаче
    status: str = ""              # см. _read_status: clear | disabled | restricted | review | unknown
    status_text: str = ""         # словами страницы — человеку с этим идти и разбираться
    #: Снимок страницы с проблемным состоянием (ACL-1430) — id для
    #: `/account-spend/status-shot/<id>`, не путь: путь наружу не отдаётся.
    status_shot: str = ""
    status_shot_at: str = ""

    #: Дерево аккаунта: кампании и объявления с их id и положением тумблера.
    #: Читается тем же нажатием, что и спенд (решение владельца 11.09) — см.
    #: `_read_tree`. Неудача дерева не отменяет спенд: это разные ответы, и
    #: терять прочитанное число из-за второго — хуже, чем вернуть его без
    #: дерева.
    objects: list[dict] = field(default_factory=list)
    objects_error: str = ""
    objects_how: str = ""         # чем разобрана таблица — чтобы знать, что сломалось
    #: ACL-1504: дошло ли чтение до самого списка кампаний. `False` — не
    #: дошло (Ads Manager не открылся, открылся чужой кабинет…), и пустой
    #: `objects` тогда значит «не прочитано», а не «0 кампаний». `None` — запись
    #: старше этого поля: тогда судим по ней самой, см. `tree_not_read`.
    objects_read: bool | None = None
    #: ACL-1504: последнее удачно прочитанное дерево аккаунта, если это чтение
    #: до списка не дошло: `{objects, at, date_from, date_to}`. Иначе пусто.
    objects_prev: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        # ACL-1504: текст ошибки дерева уходит на экран и в «Technical
        # details» — без id кабинета и адресов. Чистим здесь, на выходе, чтобы
        # это касалось и записей, сохранённых до правки.
        out["objects_error"] = scrub_tree_error(self.objects_error)
        if tree_not_read(self):
            out["objects_problem"] = tree_problem(self.objects_error)
        return out


# ─────────────────────── дерево не прочитано (ACL-1504) ───────────────────────

_URL_RE = re.compile(r"""\b(?:https?|wss?)://[^\s"'<>)\]]+""", re.I)
_ACT_ID_RE = re.compile(r"\bact_\d+")
#: Токен Meta сам по себе, без `access_token=` и вне адреса (ревью L1):
#: `run_telemetry.scrub` ловит его только по имени ключа.
_META_TOKEN_RE = re.compile(r"\bEAA[A-Za-z0-9]{20,}")


def scrub_tree_error(text: str) -> str:
    """Текст ошибки дерева без `act=`/`act_` id, адресов и токенов Meta."""
    from src.fb_ads_rpa import run_telemetry

    out = _URL_RE.sub("<url>", str(text or ""))
    out = _ACT_ID_RE.sub("act_<hidden>", out)
    out = _META_TOKEN_RE.sub("<token>", out)
    return run_telemetry.scrub(out)


def tree_not_read(reading: "SpendReading") -> bool:
    """Чтение не дошло до списка кампаний — показывать «not read», не «0».

    Старые записи поля `objects_read` не знают: у них пустое дерево вместе с
    ошибкой значит то же самое — удачно прочитанный пустой список ошибки не
    оставляет.
    """
    if reading.state != "ok":
        return False
    if reading.objects_read is not None:
        return not reading.objects_read
    return not reading.objects and bool(reading.objects_error)


#: Сырые ошибки дерева → одна фраза человеку и что делать. Подход ACL-1437
#: (`failure_policy.GUIDE`), но своё и местное: здесь читают, а не строят.
_TREE_RETRY = "Open the profile in Multilogin, check Ads Manager loads, close it, then Read again."
_TREE_GUIDE: tuple[tuple[str, str, str], ...] = (
    # (признак в сырой ошибке, причина, что делать)
    ("navigation failed", "Ads Manager did not open.", _TREE_RETRY),
    ("did not load in time", "The campaign table did not load in time.", _TREE_RETRY),
    ("a different ad account opened", "Ads Manager opened a different ad account.",
     "Open the profile in Multilogin, switch Ads Manager to this ad account, close it, then Read again."),
    ("a different period is on screen", "Ads Manager showed a different period.",
     "Read again; if it repeats, check the date picker in Ads Manager."),
    ("table was not parsed", "The campaign table could not be read.", _TREE_RETRY),
    ("unexpected page response", "The campaign table could not be read.", _TREE_RETRY),
)


def tree_problem(error: str) -> dict[str, str]:
    """Почему список кампаний не прочитан — одной фразой, и что делать."""
    low = str(error or "").lower()
    for sign, reason, action in _TREE_GUIDE:
        if sign in low:
            if sign == "navigation failed":
                m = re.search(r"timeout\s+(\d+)\s*ms", low)
                if m:
                    reason = f"Ads Manager did not open in {int(m.group(1)) // 1000} s."
            return {"reason": reason, "action": action}
    return {"reason": "The campaign list was not reached.", "action": _TREE_RETRY}


def _last_good_tree(data: dict[str, dict], account: str) -> dict:
    """Последнее удачно прочитанное дерево аккаунта за любой период.

    Берём и из записей, которые сами несут прежнее дерево (`objects_prev`):
    две неудачи подряд не должны стирать то, что было прочитано до них.
    """
    best: dict = {}
    for key, rec in data.items():
        if key.startswith(_STATUS_PREFIX) or not isinstance(rec, dict):
            continue
        if rec.get("account") != account:
            continue
        got: dict = {}
        if rec.get("objects") and rec.get("objects_read") is not False:
            got = {"objects": rec["objects"], "at": str(rec.get("finished_at") or ""),
                   "date_from": str(rec.get("date_from") or ""),
                   "date_to": str(rec.get("date_to") or "")}
        elif isinstance(rec.get("objects_prev"), dict) and rec["objects_prev"].get("objects"):
            got = dict(rec["objects_prev"])
        if got and str(got.get("at") or "") > str(best.get("at") or ""):
            best = got
    return best


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
        # Windows на миг держит файл (прошлая подмена, Defender, индексатор) и
        # отвечает WinError 5 — показание Read терялось (ACL-1466). Ждём, как
        # файл связей (ACL-1216); упорный отказ по-прежнему бросает.
        paths._replace_when_free(tmp, p)
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


#: Те же четыре знака, что `account_spend_api._SIGNS` (импорт оттуда — цикл).
_API_SIGNS = {"USD": "$", "EUR": "€", "GBP": "£", "ILS": "₪"}


def _restore_reading(key: str, raw: dict) -> SpendReading:
    known = SpendReading.__dataclass_fields__
    reading = SpendReading(**{k: v for k, v in raw.items() if k in known})
    if reading.source == "api":
        # API-чтения до 10.10 сохраняли ISO-код («USD 179.47» не влезал в ячейку);
        # показываем знаком, как новые чтения (`account_spend_api.display_currency`).
        reading.currency = _API_SIGNS.get(str(reading.currency or "").strip().upper(), reading.currency)
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


def latest_readings() -> dict[str, SpendReading]:
    """Последнее удачное чтение каждого аккаунта — за любой период (ACL-1271).

    Карта показывает чтение только за период на экране. Открыл 1–22 сентября,
    а читали 1–21 — строка пустела, хотя вчерашнее чтение есть. Это чтение
    показывается запасным: с его собственными датами и не в итогах страны.
    Состояния кабинета (`status|…`), идущие и неудачные чтения не берутся.
    """
    with _STORE_LOCK:
        out: dict[str, SpendReading] = {}
        items = list(_load_all().items()) + [(k, r.to_dict()) for k, r in _UNSAVED.items()]
        for key, raw in items:
            if key.startswith(_STATUS_PREFIX) or not isinstance(raw, dict):
                continue
            # Ключ чтения — `account|from|to`. В хранилищах остались записи
            # старого формата `account|day` без date_from/date_to: одна такая
            # ронила разбор, и запасного чтения не получал ни один аккаунт.
            if key.count("|") < 2 or not raw.get("date_from"):
                continue
            if raw.get("state") != "ok" or raw.get("spend") is None:
                continue
            try:
                reading = _restore_reading(key, raw)
            except (TypeError, ValueError):
                continue  # одна кривая запись не прячет остальные
            best = out.get(reading.account)
            if best is None or (reading.finished_at or "") > (best.finished_at or ""):
                out[reading.account] = reading
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
        if tree_not_read(reading) and not reading.objects_prev:
            # ACL-1504: список не прочитан — прежнее дерево остаётся на экране
            # со своей датой, а не исчезает вместе с неудачей.
            reading.objects_prev = _last_good_tree(data, reading.account)
        data[key] = reading.to_dict()
        if reading.status:
            data[_STATUS_PREFIX + reading.account] = {
                "status": reading.status,
                "status_text": reading.status_text,
                "at": reading.finished_at or _now(),
                "shot_id": reading.status_shot,
                "shot_at": reading.status_shot_at,
            }
        _save_all(data)
        _UNSAVED.pop(key, None)


def get_status(account: str) -> dict[str, str]:
    """Последнее известное состояние кабинета — или пусто, если не спрашивали.

    Пусто и `clear` — разное. Первое значит «не смотрели», второе — «смотрели,
    предупреждений не было». Экран обязан их различать.

    И ещё одно различие (ACL-1391): состояние, которое с тех пор не удалось
    перечитать. 30.09 карта показывала у Doc20 «no warning» от 22.09, хотя
    все чтения после этого упали, а сам кабинет уже был отключён. Старый ответ
    выглядел как сегодняшний. `stale` говорит, что после этого состояния была
    попытка чтения, и она ничего о состоянии не узнала.
    """
    data = _load_all()
    raw = data.get(_STATUS_PREFIX + account)
    if not isinstance(raw, dict):
        return {}
    at = str(raw.get("at") or "")
    out = {
        "status": str(raw.get("status") or ""),
        "status_text": str(raw.get("status_text") or ""),
        "at": at,
        "at_short": f"{at[8:10]}.{at[5:7]}.{at[2:4]}" if len(at) >= 10 else "",
        "stale": "",
        "tried_at": "",
        "tried_error": "",
        "shot_id": "",
        "shot_at": "",
    }
    # Кнопка «Copy screenshot» — только у снимка, который ещё лежит на диске.
    shot_id = str(raw.get("shot_id") or "")
    if shot_id and out["status"] != "clear" and status_shot_path(shot_id) is not None:
        out["shot_id"] = shot_id
        out["shot_at"] = str(raw.get("shot_at") or "")
    last_at, last = "", None
    for key, rec in data.items():
        if key.startswith(_STATUS_PREFIX) or not isinstance(rec, dict):
            continue
        if rec.get("account") != account:
            continue
        finished = str(rec.get("finished_at") or "")
        if finished > last_at:
            last_at, last = finished, rec
    # Чтение, которое прочитало состояние, само его и записало с тем же
    # временем, поэтому «позже» здесь значит только неудачу до состояния.
    if last is not None and at and last_at > at and not last.get("status"):
        out["stale"] = "1"
        out["tried_at"] = last_at
        out["tried_error"] = str(last.get("error") or "")[:200]
    return out


# ─────────────────────────── очередь ───────────────────────────

def _profile_key(account: str, *_a, **_k) -> str:
    """Чей браузер занимает вызов — профиль, а не ярлык аккаунта.

    Ярлыков может оказаться несколько на один профиль: карта этого не
    запрещает, а две очереди к одному браузеру — ровно то, от чего замок и
    защищает. Без физического UUID отказываем до открытия браузера: ярлык
    не защищает тот же профиль в другом процессе.
    """
    try:
        entry = _entry_for(account)
    except Exception:
        entry = None
    profile = (getattr(entry, "profile_id", "") or "").strip() if entry else ""
    from src.fb_ads_rpa.profile_lease import physical_profile_key, ProfileLeaseError

    if entry is None:
        raise ProfileLeaseError(f"{account} is not in the account map; check the account connection.")
    return physical_profile_key(profile)


def _failure_text(exc: BaseException) -> str:
    """Текст сбоя чтения для человека; отказ замка профиля уже написан словами."""
    from src.fb_ads_rpa.profile_lease import ProfileLeaseError

    return str(exc) if isinstance(exc, ProfileLeaseError) else f"{type(exc).__name__}: {exc}"


def _lock_for(account: str):
    """Очередь на профиль, а не на весь браузер (`acl-113e`).

    Прежде здесь стоял один замок на всё: «один браузер за раз, две сессии
    подряд с одного устройства». Два аккаунта — это два профиля, у каждого
    свой прокси, и общего ресурса между ними нет; обоснование самого замка
    говорило про «тот же самый профиль».

    Публикация послабления не получает: она одна за раз по решению владельца
    31.07 — см. `operations`.
    """
    return profile_lock(_profile_key(account))


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

    fn = reader or default_reader()

    def run() -> None:
        started = time.time()
        try:
            with browser_operation.reading(), _lock_for(account):
                done = fn(account, date_from, date_to)
        except Exception as exc:  # noqa: BLE001 — падение чтения не должно ронять сервер
            from src.fb_api import mlx_session as _mlx

            # ACL-1826: вход в прокси (ERR_INVALID_AUTH_CREDENTIALS) читается одной фразой.
            text, code = _mlx.humanize(exc, "", account)
            done = SpendReading(
                account=account, date_from=date_from, date_to=date_to,
                state="failed", error=text or _failure_text(exc), error_code=code,
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


def default_reader() -> Callable[[str, str, str], SpendReading]:
    """Чем читать спенд: Graph API в профиле (`ACL_FB_BACKEND=api`, ACL-1760) или RPA."""
    from src.analysis_agent import account_spend_api

    return account_spend_api.read_via_api if account_spend_api.api_enabled() else read_via_rpa


def _now() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ─────────────────────────── маршрут RPA ───────────────────────────


def _entry_for(account: str):
    from src.meta_campaign_agent.ad_account_map import load_ad_account_map

    entries = load_ad_account_map(paths.get_ad_account_map_path())
    return entries.get(account)


class ProfileNotAttachable(RuntimeError):
    """Профиль есть, но прицепиться к нему нельзя — а перезапускать мы не станем.

    ACL-1518: `code` — устойчивое имя отказа для экрана (`HAND_OPEN_CODE`).
    """

    def __init__(self, message: str = "", code: str = "") -> None:
        super().__init__(message)
        self.code = code


# ── окно, открытое руками ──────────────────────────────────────────────────
#
# ACL-1812 / ACL-1811: дашборд окно, открытое руками, НИКОГДА не останавливает и не
# поднимает заново (так 12.10 в 08:42-08:43 разлогинило 5414). Отказ с понятным текстом;
# закрыть окно и подождать 3 минуты должен человек.

#: Код отказа, когда окно так и осталось открытым руками.
HAND_OPEN_CODE = "open_by_hand"

#: Окно открыто руками (нет порта автоматизации). Текст зависит от бэкенда: на `api` есть
#: кнопка «Open profiles for today», на `rpa` её нет — там кнопка «open profile» в строке.
HAND_OPEN_MESSAGE_API = ("Opened in Multilogin by hand \u2014 close it there, wait about 3 minutes, "
                         "then use Open profiles for today")
HAND_OPEN_MESSAGE = ("Opened in Multilogin by hand \u2014 close it there, wait about 3 minutes, "
                     "then use the open profile button on this row")
HAND_WINDOW_MESSAGE = HAND_OPEN_MESSAGE


def hand_message() -> str:
    """Текст отказа «открыто руками» для текущего бэкенда."""
    import os

    return HAND_OPEN_MESSAGE_API if (os.environ.get("ACL_FB_BACKEND") or "").strip().lower() == "api" \
        else HAND_OPEN_MESSAGE


#: Аккаунт → строка переоткрытия. Живёт только в памяти и только пока окно
#: переоткрывается: опрос `/account-spend/status` отдаёт её строке аккаунта.
_HAND_WAIT: dict[str, str] = {}
_HAND_WAIT_LOCK = threading.Lock()


def hand_wait_note(account: str) -> str:
    """Переоткрывается ли сейчас окно этого аккаунта — строкой для экрана."""
    with _HAND_WAIT_LOCK:
        return _HAND_WAIT.get(str(account or ""), "")


# ── закрытый профиль: чтение открывает его само (ACL-1580) ────────────────
#
# До ACL-1580 чтение на закрытом профиле отказывало («the profile is not
# open»), и человек жал «open profile» руками. Отказ защищал от перезапуска
# ОТКРЫТОГО окна (разлогинил x01); запуск ЗАКРЫТОГО — обычное открытие, ровно
# то, что делает «open profile». Решение владельца 05.10: чтения открывают
# нужное сами.
#
# * Запуск — путём «open profile»: `multilogin_attach.open(allow_restart=False,
#   allow_kill=False)` — только старт, открытое окно не перезапускается,
#   простого браузера нет. Окно, открытое руками, переоткрывается как прежде
#   (ACL-1562) и остаётся открытым.
# * Место — из той же очереди профилей по памяти, что у Publish (ACL-1424):
#   разом не больше, чем тянет машина; пока ждём, строка аккаунта говорит
#   «Waiting for a free profile slot».
# * После чтения профиль, который запустило само чтение, закрывается мягкой
#   остановкой ЭТОГО профиля через MLX API (`allow_kill=False`) — и при
#   упавшем чтении тоже. Уже открытый остаётся открытым.
# * Удачный Read spend закрывает не сразу, а через `CLOSE_GRACE_S`: за ним
#   тем же нажатием идёт чтение стран (ACL-1282), и закрыть профиль между
#   ними значило бы открыть его второй раз. Чтение стран, пришедшее за это
#   время, забирает профиль себе и закрывает его по своему концу.

#: Код отказа «профиль разлогинен в Facebook» — тот же, что у Publish.
LOGGED_OUT_CODE = _profile_trouble.LOGGED_OUT

#: Строка на карте: разлогинен — войти может только человек, повторов нет.
LOGGED_OUT_MESSAGE = ("The {account} profile is logged out of Facebook — "
                      "log in inside Multilogin, then Read again")

#: Строка на карте, пока чтение ждёт места в очереди профилей.
SLOT_WAIT_NOTE = "Waiting for a free profile slot"

#: Строка на карте, пока закрытый профиль поднимается.
STARTING_NOTE = "Opening the {account} profile with automation"

#: Сколько секунд удачный Read spend держит открытым запущенный им профиль —
#: чтобы чтение стран (ACL-1282) пришло в то же окно. Страница просит страны
#: через 3–6 с после конца спенда.
CLOSE_GRACE_S = 30.0


def logged_out_message(account: str) -> str:
    return LOGGED_OUT_MESSAGE.format(account=str(account or "") or "profile")


def _set_wait_note(account: str, note: str) -> None:
    """Строка ожидания на карте (та же, что у переоткрытия окна, ACL-1518)."""
    with _HAND_WAIT_LOCK:
        if note:
            _HAND_WAIT[str(account or "")] = note
        else:
            _HAND_WAIT.pop(str(account or ""), None)


def _slot_queue():
    """Очередь профилей по памяти — та же, что у Publish (ACL-1321/1424)."""
    from src.web import publish_jobs

    return publish_jobs._slots


@dataclass
class _ReadOpen:
    """Что чтение сделало с профилем: запустило ли его само и чьё место держит."""

    account: str
    started: bool = False
    slot_id: str = ""
    profile_id: str = ""
    entry: Any = None
    started_mono: float = 0.0
    #: Вкладка чтения — вкладка окна, которое чтение подняло: её не закрываем
    #: (закрылось бы окно); профиль закроет мягкая остановка или Publish.
    keep_tab: bool = False
    #: Сколько мест очередь выдала профилю к моменту запуска (`grants`).
    grants_seen: int = 0


#: Чтение просит запускать закрытый профиль (`opening_closed_profile`). Флагом
#: контекста, как `_REOPEN_BY_HAND`: `_attach_only` подменяют десятки тестов.
_READ_OPEN: ContextVar["_ReadOpen | None"] = ContextVar("acl_read_open", default=None)

#: Профиль → запуск чтения, ждущий закрытия по `CLOSE_GRACE_S`.
_PENDING_CLOSE: dict[str, _ReadOpen] = {}
_PENDING_LOCK = threading.Lock()


@contextmanager
def opening_closed_profile(opened: _ReadOpen):
    """Внутри блока `_attach_only` запускает закрытый профиль (ACL-1580)."""
    token = _READ_OPEN.set(opened)
    try:
        yield opened
    finally:
        _READ_OPEN.reset(token)


def _record(step: str, ok: bool, duration_s: float, diag: dict) -> None:
    try:
        from src.fb_ads_rpa import run_telemetry as _rt

        _rt.record(step, ok, duration_s=round(float(duration_s), 1), diag=diag)
    except Exception:
        pass


def _slot_wait_note(verdict: dict) -> str:
    ahead = int((verdict or {}).get("ahead") or 0)
    return SLOT_WAIT_NOTE + (f" ({ahead} ahead)" if ahead else "")


def _take_over_pending(profile_id: str, opened: "_ReadOpen | None") -> None:
    """Профиль, запущенный прошлым чтением и ждущий закрытия, — теперь наш."""
    if opened is None or opened.started:
        return
    with _PENDING_LOCK:
        prev = _PENDING_CLOSE.pop(profile_id, None)
    if prev is None:
        return
    opened.started, opened.slot_id = True, prev.slot_id
    opened.profile_id, opened.started_mono = prev.profile_id, prev.started_mono
    opened.entry = opened.entry or prev.entry
    opened.grants_seen = prev.grants_seen


def _start_closed(entry, account_label: str, opened: _ReadOpen, recheck=None):
    """Запустить закрытый профиль для чтения путём «open profile» (ACL-1580).

    Сначала место в очереди профилей Publish (строка ожидания на карте), потом
    старт: `allow_restart=False` — открытое окно не перезапускается,
    `allow_kill=False` — ничего не убивается. Не поднялся — место отпускаем.

    Ревью PR #583:
    * (F2) ожидание места бывает долгим, а `/mlx-profile-open` замок профиля
      не берёт — профиль могли открыть автоматизацией, пока мы ждали.
      `recheck()` после выдачи места: прицепились — профиль не наш, место
      отпускаем и после чтения его не закрываем.
    * (F1) место профиля держит Publish (`shared`) — профиль после чтения
      остаётся Publish: чтение его не закрывает (ни сразу, ни по таймеру).
    """
    from src.fb_ads_rpa import browser as rpa_browser
    from src.fb_api import stop_log as _stop_log

    profile_id = (getattr(entry, "profile_id", "") or "").strip()
    # ACL-1812 (ревью #813, B1-bis): закрыли недавно — не поднимаем (ACL-1811).
    # ACL-2117: пока MLX досохраняет профиль, чтение ждёт (а не отказывает).
    why = _stop_log.wait_or_refusal(profile_id, on_wait=lambda text: _set_wait_note(account_label, text))
    if why:
        raise ProfileNotAttachable(why, code="restart_cooldown")
    queue = _slot_queue()
    slot_id = f"read:{account_label}:{uuid.uuid4().hex[:8]}"
    waited_from = time.monotonic()
    try:
        grant = queue.acquire(
            slot_id, profile_id,
            on_wait=lambda verdict: _set_wait_note(account_label, _slot_wait_note(verdict)),
            share_profile=True,
        )
    finally:
        _set_wait_note(account_label, "")
    shared = bool(grant and grant.get("shared"))
    waited_s = time.monotonic() - waited_from
    if recheck is not None:
        try:
            ready = recheck()
        except Exception:
            ready = None
        if ready is not None:
            if not shared:
                queue.release(slot_id)
            return ready
    _set_wait_note(account_label, STARTING_NOTE.format(account=account_label))
    began = time.monotonic()
    try:
        conn = rpa_browser.get_browser_provider("multilogin_attach").open(
            entry, account_label, allow_missing_dir=True, allow_restart=False, allow_kill=False)
    except Exception as exc:  # noqa: BLE001
        if not shared:
            queue.release(slot_id)
        _record("spend_profile_started", False, time.monotonic() - began,
                {"account": account_label, "slot_wait_s": round(waited_s, 1),
                 "error": type(exc).__name__})
        # ACL-1826: 403 «restricted permission» на слоте B / вход в прокси — понятный текст и код.
        from src.fb_api import mlx_session as _mlx

        text, code = _mlx.humanize(exc, getattr(entry, "multilogin_instance", "") or "A", account_label)
        if code:
            raise ProfileNotAttachable(text, code=code) from exc
        raise ProfileNotAttachable(
            f"{account_label}: the profile is closed and could not be started with automation: {exc}"
        ) from exc
    finally:
        _set_wait_note(account_label, "")
    opened.keep_tab = True
    # Место профиля держит Publish — после чтения профиль его, а не наш.
    opened.started = not shared
    opened.slot_id = "" if shared else slot_id
    opened.profile_id = profile_id
    opened.entry = entry
    opened.started_mono = began
    try:
        opened.grants_seen = int(queue.grants(profile_id))
    except Exception:
        opened.grants_seen = 0
    _record("spend_profile_started", True, time.monotonic() - began,
            {"account": account_label, "slot_wait_s": round(waited_s, 1), "shared_slot": shared})
    return conn


def _close_started(opened: _ReadOpen) -> None:
    """Мягко закрыть профиль, который запустило чтение, и отпустить его место.

    Только MLX API этого профиля, `allow_kill=False` (`stop_profile_on_request`,
    как у ACL-1444): не закрылся — окно остаётся, ничего не убиваем.
    """
    from src.fb_ads_rpa import browser as rpa_browser

    began = time.monotonic()
    if _publish_touched(opened):
        # Ревью PR #583 (F1): профилем пользуется (или воспользовалось) другое
        # задание — окно его: Publish сам решает, закрывать ли (ACL-1444).
        if opened.slot_id:
            try:
                _slot_queue().release(opened.slot_id)
            except Exception:
                log.exception("Could not release the profile slot of %s", opened.account)
        _record("spend_profile_closed_after_read", None, 0.0,
                {"account": opened.account, "skipped": "another_job"})
        opened.started = False
        return
    ok, error = True, ""
    try:
        rpa_browser.stop_profile_on_request(opened.entry, opened.account)
    except Exception as exc:  # noqa: BLE001 — закрытие не отменяет прочитанное
        ok, error = False, type(exc).__name__
        log.warning("%s: could not close the profile the read started (%s); its window stays open",
                    opened.account, exc)
    finally:
        if opened.profile_id:
            # Остановку записываем при любой ПОПЫТКЕ: профиль мог закрыться, хотя вызов упал
            # (ревью #813, B2 minor); лишняя пауза безопасна, лишний запуск — нет.
            from src.fb_api import stop_log as _stop_log

            _stop_log.record_stop(opened.profile_id)
        if opened.slot_id:
            try:
                _slot_queue().release(opened.slot_id)
            except Exception:
                log.exception("Could not release the profile slot of %s", opened.account)
    diag = {"account": opened.account, "held_s": round(time.monotonic() - opened.started_mono, 1)}
    if error:
        diag["error"] = error
    _record("spend_profile_closed_after_read", ok, time.monotonic() - began, diag)
    opened.started = False


def _publish_touched(opened: _ReadOpen) -> bool:
    """Держит ли место профиля другое задание, ждёт ли его, или держало ли
    после нашего запуска (`grants` вырос). Не прочиталось — считаем, что да:
    оставить окно открытым безопаснее, чем закрыть чужое."""
    try:
        queue = _slot_queue()
        return bool(queue.others_on(opened.profile_id, opened.slot_id)
                    or int(queue.grants(opened.profile_id)) != opened.grants_seen)
    except Exception:
        log.exception("Could not ask the profile queue about %s", opened.account)
        return True


def _close_if_still_pending(opened: _ReadOpen) -> None:
    """Конец `CLOSE_GRACE_S`: никто не забрал профиль — закрыть его."""
    from src.fb_ads_rpa.profile_lease import ProfileLeaseError

    # ACL-1853: таймер в своём потоке — отказ замка не должен его ронять; запись остаётся
    # в `_PENDING_CLOSE`, следующая операция с этим профилем возьмёт её или закроет.
    try:
        with browser_operation.reading(), profile_lock(opened.profile_id):
            with _PENDING_LOCK:
                if _PENDING_CLOSE.get(opened.profile_id) is not opened:
                    return
                _PENDING_CLOSE.pop(opened.profile_id, None)
            _close_started(opened)
    except ProfileLeaseError as exc:
        log.warning("Deferred close of %s skipped: %s", opened.account, exc)


def _finish_started(opened: _ReadOpen, *, keep_for_next: bool = False,
                    grace_s: float | None = None, convenience: bool = False) -> None:
    """После чтения: закрыть то, что чтение запустило (сразу или после паузы).

    `grace_s` (ACL-2117) — свой срок вместо `CLOSE_GRACE_S`: Check plan держит окно, пока
    человек решает про Publish; если его не взяли, оно закроется по сроку, как и раньше.
    """
    if not opened.started:
        return
    if keep_for_next and convenience and opened.slot_id:
        # ACL-2117 + ACL-2113: keep-open включён — окно Check plan становится «удобным» оставленным
        # окном: Publish берёт его место (`_reuse_kept`), а простой и вытеснение решает очередь
        # (LRU, `ACL_KEPT_IDLE_CLOSE_MIN`). Своего таймера нет.
        try:
            _slot_queue().keep(opened.slot_id, opened.account, kind="convenience")
        except Exception:
            log.exception("Could not mark the kept window of %s", opened.account)
        return
    grace = CLOSE_GRACE_S if grace_s is None else float(grace_s)
    if keep_for_next and grace > 0 and opened.profile_id:
        if grace_s is not None and opened.slot_id:
            # ACL-2117: окно, которое держим под Publish, — оставленное (kept): Publish берёт его
            # место (`_reuse_kept`) и не стоит в очереди за обычным местом чтения.
            try:
                _slot_queue().keep(opened.slot_id, opened.account)
            except Exception:
                log.exception("Could not mark the kept window of %s", opened.account)
        with _PENDING_LOCK:
            _PENDING_CLOSE[opened.profile_id] = opened
        timer = threading.Timer(grace, _close_if_still_pending, args=(opened,))
        timer.daemon = True
        timer.start()
        return
    _close_started(opened)


def _logged_out(page, *, deep: bool = False) -> bool:
    """Вкладка на странице входа Facebook? `deep` — ещё и форма входа в разметке."""
    try:
        if _profile_trouble.login_url(page.url):
            return True
    except Exception:
        return False
    if not deep:
        return False
    try:
        code, _ = _profile_trouble.classify_page(page, history=False)
    except Exception:
        return False
    return code == _profile_trouble.LOGGED_OUT


def _mlx_profile_dir(profile_id: str, folder_id: str = "") -> Path | None:
    """Папка профиля Multilogin — по адресу, без обхода дерева.

    Логика живёт в одном месте: `fb_ads_rpa.browser._find_mlx_profile_dir`
    (ACL-144 — тот же адресный поиск теперь и у прогона публикации).
    """
    from src.fb_ads_rpa import browser as rpa_browser

    return rpa_browser._find_mlx_profile_dir(profile_id, folder_id)


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


def _attach_open(direct_cls, entry, profile_id: str):
    """Прицепиться к профилю, если он уже открыт автоматизацией; иначе None."""
    udd = _mlx_profile_dir(profile_id, (getattr(entry, "folder_id", "") or ""))
    if udd is None:
        return None
    direct = direct_cls()
    for spelling in _dir_spellings(udd):
        conn = direct.attach_existing_only(profile_id, spelling, new_page=True)
        if conn is not None:
            return conn
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

    Используем публичный attach_existing_only: проверка принадлежности
    профилю закреплена в провайдере, для чтения создаётся отдельная вкладка.

    ACL-1812 / ACL-1811: окно, открытое руками, не останавливается и не переоткрывается
    автоматически — отказ `HAND_OPEN_CODE` с понятным текстом.

    ACL-1580: внутри `opening_closed_profile(...)` (так читают спенд и
    страны) закрытый профиль не отказ — он запускается путём «open profile»
    с местом из очереди профилей Publish (`_start_closed`), и в переданной
    записи отмечается, что его запустило чтение. Без блока — прежний отказ.
    """
    opening = _READ_OPEN.get()
    from src.fb_ads_rpa import browser as rpa_browser

    profile_id = (getattr(entry, "profile_id", "") or "").strip()
    if not profile_id:
        raise ProfileNotAttachable(f"{account_label} has no profile_id in the account map")

    from src.fb_ads_rpa.profile_lease import require_profile_lease

    require_profile_lease(profile_id)

    direct_cls = getattr(rpa_browser, "MultiloginDirectCDPProvider", None)
    if direct_cls is None:
        raise ProfileNotAttachable(
            "The RPA browser layer changed; update the connection (see acl-108e)"
        )

    udd = _mlx_profile_dir(profile_id, (getattr(entry, "folder_id", "") or ""))
    if udd is None:
        # Папку создаёт сам Multilogin при первом запуске — у профиля, ни разу
        # не запускавшегося здесь, её нет; открытым он быть не может.
        if opening is not None:
            return _start_closed(entry, account_label, opening,
                                 recheck=lambda: _attach_open(direct_cls, entry, profile_id))
        raise ProfileNotAttachable(
            f"Profile folder {profile_id} was not found. Is this profile open on this machine?"
        )

    direct = direct_cls()
    for spelling in _dir_spellings(udd):
        conn = direct.attach_existing_only(profile_id, spelling, new_page=True)
        if conn is not None:
            # Профиль, который прошлое чтение запустило и ещё не закрыло
            # (`CLOSE_GRACE_S`), закроет теперь это чтение.
            _take_over_pending(profile_id, opening)
            return conn

    running = False
    try:
        running = any(direct.profile_is_running(p, profile_id) for p in _dir_spellings(udd))
    except Exception:
        pass
    if running:
        # ACL-1812 (ревью #813, B1-bis; ACL-1811): окно, открытое руками, АВТОМАТИЧЕСКИ не
        # останавливаем и не поднимаем заново. «Закрыли — через 10 секунд открыли» разлогинило
        # 5414 (12.10, 08:42-08:43). Остановка и запуск — только руками человека.
        raise ProfileNotAttachable(hand_message(), code=HAND_OPEN_CODE)
    if opening is not None:
        return _start_closed(entry, account_label, opening,
                             recheck=lambda: _attach_open(direct_cls, entry, profile_id))
    raise ProfileNotAttachable(
        f"{account_label}: the profile is not open. Open it through automation and leave the window open; "
        "the read will attach to it without restarting anything."
    )


def _probe_lease_error(exc, *_args, **_kwargs):
    return {"ok": False, "act": "", "account_name": "", "error": str(exc)}


def _open_lease_error(exc, *_args, **_kwargs):
    return False, str(exc)


def _spend_lease_error(exc, account, date_from, date_to=""):
    return SpendReading(account=account, date_from=date_from, date_to=date_to or date_from,
                        source="rpa", state="failed", error=str(exc))


@serialized_browser(reading=True, on_lease_error=_probe_lease_error)
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
        out["error"] = "profile_id is not set"
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
                "Ads Manager opened, but the URL has no ad account number. "
                "This profile may have no access to any ad account."
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


@serialized_browser(reading=True, on_lease_error=_open_lease_error)
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

    ACL-1812 / ACL-1811: окно, открытое руками, не трогается ни этой кнопкой, ни Connect —
    `(False, hand_message())` с кодом `HAND_OPEN_CODE`. Перед запуском закрытого профиля
    проверяется общий журнал остановок (`fb_api.stop_log`).
    """
    profile_id = (profile_id or "").strip()
    who = label or profile_id[:8] or "profile"
    if not profile_id:
        return False, f"{who} has no profile_id in the account map"
    instance = (instance or "A").strip().upper()
    if instance not in {"A", "B"}:
        return False, "Unknown Multilogin instance; choose A or B"

    from src.fb_ads_rpa import browser as rpa_browser

    direct_cls = getattr(rpa_browser, "MultiloginDirectCDPProvider", None)
    if direct_cls is None:
        return False, "The RPA browser layer changed (see acl-108e)"

    udd = _mlx_profile_dir(profile_id, folder_id)
    if udd is not None:
        direct = direct_cls()
        for spelling in _dir_spellings(udd):
            attached = direct.attach_existing_only(profile_id, spelling)
            if attached is not None:
                attached.close(keep_browser_open=True)
                return True, "The profile is already open through automation. You can read it."
        try:
            if any(direct.profile_is_running(p, profile_id) for p in _dir_spellings(udd)):
                # ACL-1812 (B1-bis): и кнопка «open profile» руками открытое окно не трогает.
                return False, CodedMessage(hand_message(), HAND_OPEN_CODE)
        except Exception as exc:
            return False, f"Could not check open profiles: {exc}. Nothing was launched."


    # ACL-1812 (B1-bis): обе кнопки («open profile» на строке и Connect) идут через общий журнал.
    from src.fb_api import stop_log as _stop_log

    why = _stop_log.refusal(profile_id)
    if why:
        return False, CodedMessage(why, "restart_cooldown")

    entry = _Profile(profile_id, folder_id, instance)
    try:
        conn = rpa_browser.get_browser_provider("multilogin_attach").open(
            entry, who, allow_missing_dir=True, allow_restart=False)
    except Exception as exc:  # noqa: BLE001
        from src.fb_api import mlx_session as _mlx

        text, code = _mlx.humanize(exc, instance, who)   # ACL-1826
        if code:
            return False, CodedMessage(text, code)
        return False, f"{type(exc).__name__}: {exc}"
    try:
        # Окно остаётся человеку: отключаемся, не закрывая браузер.
        conn.close(keep_browser_open=True)
    except Exception:
        pass
    return True, "The profile is open. Log in if needed and leave the window open."


class CodedMessage(str):
    """Текст ответа «open profile» с устойчивым кодом отказа (ревью PR #519, F6).

    Строка — чтобы ответ `(ok, message)` остался прежним для всех вызывающих;
    `code` — чтобы маршрут решал по коду, а не по сравнению текста.
    """

    code: str = ""

    def __new__(cls, text: str, code: str = ""):
        obj = super().__new__(cls, text)
        obj.code = code
        return obj


@serialized_browser(key=_profile_key, reading=True, on_lease_error=_open_lease_error)
def open_for_automation(account: str) -> tuple[bool, str]:
    """То же самое для аккаунта, который уже есть в карте."""
    entry = _entry_for(account)
    if entry is None:
        return False, f"{account} is not in the account map"
    return open_profile_for_automation(
        (getattr(entry, "profile_id", "") or ""),
        (getattr(entry, "folder_id", "") or ""),
        (getattr(entry, "multilogin_instance", "") or "A"),
        account,
    )


@serialized_browser(key=_profile_key, reading=True, on_lease_error=_spend_lease_error)
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
        out.error = f"{account} is not in the account map"
        return out
    act = (entry.ad_account_id or "").strip()
    if not act:
        out.error = f"{account} has no ad_account_id in the account map"
        return out
    try:
        url = _url(act, date_from, date_to)
    except ValueError as exc:
        out.error = str(exc)
        return out

    conn = None
    held = _ReadOpen(account)
    try:
        try:
            with opening_closed_profile(held):
                conn = _attach_only(entry, account)
        except ProfileNotAttachable as exc:
            out.error = str(exc)
            out.error_code = getattr(exc, "code", "")
            return out
        page = conn.page
        try:
            page.goto(url, timeout=TABLE_TIMEOUT_MS, wait_until="domcontentloaded")
        except Exception as exc:
            # ACL-1580: переход сорвался на страницу входа — профиль разлогинен.
            if _profile_trouble.classify_error(exc)[0] != LOGGED_OUT_CODE:
                raise
            return _logged_out_reading(out, account)
        if _logged_out(page):
            return _logged_out_reading(out, account)

        # Ads Manager рисуется скриптом, и `domcontentloaded` на нём не значит
        # почти ничего: первый прогон 2026-09-07 проверил период через четыре
        # секунды после перехода, когда на экране был спиннер и пустой скелет.
        # Ждём саму таблицу, и только потом спрашиваем, какой период показан.
        grid_ready = _wait_for_grid(page)
        expected_act = act if act.startswith("act_") else "act_" + act
        opened = _act_from_url(page, tries=1)
        if opened != expected_act:
            if _logged_out(page, deep=True):
                return _logged_out_reading(out, account)
            # Оба номера помогают сверить карту и доступ профиля без нового чтения.
            out.error = (
                f"Ads Manager opened ad account {opened or '(no act in the URL)'}, "
                f"but {account} is mapped to {expected_act}. Reading cancelled: "
                "either the account map has the wrong number, or the profile has no access to that ad account."
            )
            return out
        if not grid_ready:
            if _logged_out(page, deep=True):
                return _logged_out_reading(out, account)
            # Статус спрашиваем и здесь: у отключённого кабинета таблицы может
            # не быть вовсе, а знать о нём надо именно тогда.
            out.status, out.status_text = _read_status(page)
            _shoot_status(page, out)
            out.error = "Ads Manager did not finish loading in time"
            # было `day` — переменной, которой здесь нет. Ошибка жила только на
            # пути отказа, то есть ровно там, где снимок и нужен: вместо
            # «не отрисовался» человек получил бы NameError.
            out.shot = _shoot(page, account, date_from)
            return out

        # Один заход — оба ответа. Отдельный визит ради состояния кабинета
        # ничем не оправдан: каждое касание живого аккаунта чего-то стоит.
        out.status, out.status_text = _read_status(page)
        _shoot_status(page, out)

        shown = _shown_period(page)
        if not _period_matches(shown, date_from, date_to):
            asked = date_from if date_from == date_to else f"{date_from}…{date_to}"
            out.error = (
                f"The period on screen is \"{shown or 'not found'}\", but {asked} was requested; "
                "the amount was not taken"
            )
            out.shot = _shoot(page, account, date_from)
            return out

        spend, how = _read_total(page)
        if spend is None:
            out.error = "The totals row with spend was not found"
            out.how = how
            out.shot = _shoot(page, account, date_from)
            return out

        if _act_from_url(page, tries=1) != expected_act or not _period_matches(_shown_period(page), date_from, date_to):
            out.status = out.status_text = out.status_shot = out.status_shot_at = ""
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
        reached: dict[str, bool] = {}
        try:
            out.objects, out.objects_how, out.objects_error = _read_tree(
                page, act, expected_act, date_from, date_to, reached=reached,
            )
        except Exception as exc:            # noqa: BLE001 — см. выше
            out.objects_error = f"Campaign tree was not read: {exc}"
        # ACL-1504: «0 кампаний» — только если список действительно прочитан.
        out.objects_read = bool(reached.get("list"))
        return out
    finally:
        if conn is not None:
            # ACL-1580: у профиля, который чтение само подняло, это вкладка
            # окна — её не закрываем (закрылось бы окно); профиль закроет
            # мягкая остановка ниже.
            if not held.keep_tab:
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
        # ACL-1580: запущенное чтением — закрыть (и при отказе). Удачное
        # чтение ждёт `CLOSE_GRACE_S`: за ним может прийти чтение стран.
        _finish_started(held, keep_for_next=out.state == "ok")


def _logged_out_reading(out: "SpendReading", account: str) -> "SpendReading":
    """Профиль разлогинен в Facebook: понятная строка, без повторов (ACL-1580)."""
    out.state = "failed"
    out.error = logged_out_message(account)
    out.error_code = LOGGED_OUT_CODE
    return out


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


#: Вид блокировки, который узнаёт RPA (`meta_dom._ACCOUNT_BLOCKED_PATTERNS`), —
#: словом колонки состояния. Неизвестный вид становится `blocked`, не `clear`.
_BLOCKED_KIND_STATUS: dict[str, str] = {
    "account_disabled": "disabled",
    "verification_required": "restricted",
    "start_authentication": "blocked",
    "authentication_error": "blocked",
}

#: Кто весомее, если на странице несколько признаков сразу.
_STATUS_RANK = {"disabled": 0, "restricted": 1, "review": 2, "blocked": 3}


#: Анкер в `meta_dom._ACCOUNT_BLOCKED_JS`, рядом с которым чтение добавляет
#: тот же узел словами `innerText`. `textContent` склеивает строки Meta без
#: пробела («…advertiseComplete verification…»), а человеку в подсказке нужен
#: читаемый текст. Код RPA не правится — меняется копия строки здесь; нет
#: анкера — работает исходный скрипт, просто без пробелов.
_BLOCKED_JS_PUSH = "out.push(t);"
_BLOCKED_JS_SPACED = (
    "{ const s = norm(el.innerText); if (s && s !== t) out.push(s); } out.push(t);"
)

#: Сломанная связка с RPA (переименовали приватные имена) — одно
#: предупреждение на процесс, а не на каждую из восьми попыток чтения.
_blocked_warned = False


def _warn_blocked_once(why: str) -> None:
    global _blocked_warned
    if _blocked_warned:
        return
    _blocked_warned = True
    log.warning(
        "account status: the RPA account-block detector is unavailable (%s); "
        "reading banners only, a disabled account may show as 'no warning'",
        why,
    )


def _blocked_on_page(page) -> list[tuple[str, str]]:
    """Блокировка, которую видит RPA перед публикацией, — по отдельным ячейкам.

    Баннера у отключённого кабинета может не быть вовсе: у Doc20 30.09 было
    только «Account disabled» в колонке Delivery, и чтение отвечало «no
    warning». Признаки берутся у RPA, а не копируются: две копии одного списка
    уже разошлись, и ошиблась та, что показывает статус на экране (ACL-1391).
    Совпадение — весь текст ячейки, а не подстрока страницы: справка «Why is
    my ad account disabled?» есть и у здорового кабинета.

    Возвращаются **все** совпадения во всех фреймах, а не первое по порядку
    DOM: у прогона ecce52f330b8 (22.09) баннер «Verification required to
    advertise» стоял выше ячейки «Account disabled», и первое совпадение дало
    бы `restricted`. Кто весомее, решает `_STATUS_RANK` в `_status_once`.
    """
    try:
        from src.fb_ads_rpa import meta_dom

        js = meta_dom._ACCOUNT_BLOCKED_JS
        match = meta_dom._account_blocked_match
        roots_of = meta_dom._roots
    except Exception as exc:  # noqa: BLE001 — нет детектора: читаем баннеры
        _warn_blocked_once(f"{type(exc).__name__}: {exc}")
        return []
    try:
        roots = roots_of(page)
    except Exception:
        # Подставная страница без фреймов: признаки ячеек — только добавка.
        return []
    if _BLOCKED_JS_PUSH in js:
        js = js.replace(_BLOCKED_JS_PUSH, _BLOCKED_JS_SPACED, 1)
    found: list[tuple[str, str]] = []
    for root in roots:
        try:
            texts = root.evaluate(js) or []
        except Exception:
            # Фрейм ушёл между перечислением и вопросом — обычное дело.
            continue
        for text in texts:
            kind = match(text)
            if kind:
                found.append((_BLOCKED_KIND_STATUS.get(kind, "blocked"), text))
    return found


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
    found: list[tuple[str, str]] = []
    for state, phrases in _STATUS_SIGNS:
        hit = next((p for p in phrases if p in low), None)
        if hit:
            at = low.find(hit)
            start = max(0, at - 60)
            found.append((state, flat[start : at + len(hit) + 140].strip()))
            break
    found.extend(_blocked_on_page(page))
    if found:
        return min(found, key=lambda f: _STATUS_RANK.get(f[0], len(_STATUS_RANK)))

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
        raise ValueError("ad_account_id must contain only digits, with an optional act_ prefix")
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
    # Оба порядка записи: «Sep 16, 2026» и «16 Sep 2026» (17.09, Doc20). Порядок
    # в тексте сохраняется, чтобы отрезок читался «от — до».
    found: list[tuple[int, str]] = []
    for m in re.finditer(
        r"(?<!\d)(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\.?,?\s+(\d{4})",
        text,
        re.I,
    ):
        month = _MONTHS[m.group(2)[:3].lower()]
        found.append((m.start(), f"{int(m.group(3)):04d}-{month:02d}-{int(m.group(1)):02d}"))
    for m in re.finditer(
        r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\.?\s+(\d{1,2}),?\s*(\d{4})",
        text,
        re.I,
    ):
        month = _MONTHS[m.group(1)[:3].lower()]
        found.append((m.start(), f"{int(m.group(3)):04d}-{month:02d}-{int(m.group(2)):02d}"))
    for _, iso in sorted(found):
        out.append(iso)
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


def _written_currency(raw: str) -> str:
    """Чем Meta подписала сумму — знаком или кодом, как написано.

    Ничего не переводим и ничего не достраиваем: `$` остаётся `$`. Знак сам
    по себе не говорит, доллары это США, Мексики или Австралии, — но и врать
    об этом не будет, в отличие от подставленного «USD».
    """
    text = (raw or "").strip()
    if not text:
        return ""
    for sign in ("€", "£", "₪", "$"):
        if text.startswith(sign):
            return sign
    head = text.split(" ", 1)[0]
    return head if len(head) == 3 and head.isalpha() and head.isupper() else ""


def _scan_totals(page) -> tuple[float | None, bool]:
    """Сумма из строки итогов и есть ли надпись, за которую берётся курсор."""
    try:
        found = page.evaluate(_FIND_TOTALS_JS)
    except Exception:
        return None, False
    raw = (found or {}).get("spend") or ""
    # Передаём знак, который написала Meta, и ничего не переводим (решение
    # владельца 15.09). Прежде здесь стояло `("$", "USD")`, и на мексиканском
    # кабинете дешборд молча подписывал песо долларами: цифра выглядела
    # знакомой, и повода усомниться не было.
    #
    # Показывать знак кабинета как есть — честнее перевода и дешевле поиска
    # настоящей валюты: на страницах настроек Meta её больше не пишет.
    page._acl_spend_currency = _written_currency(raw)
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
        return None, "the table did not appear"

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
            return value, "totals row, Total spent"
        if has_label:
            label = page.query_selector(SEL_TOTALS_LABEL)
            if label is not None:
                break
        _sleep(1)
    if label is None:
        return None, (
            f"the \"Results from …\" row did not appear within {TOTALS_TRIES} s "
            "even after scrolling to the bottom of the list"
        )

    try:
        label.scroll_into_view_if_needed(timeout=5_000)
        label.hover(timeout=5_000)
    except Exception:
        return None, "could not hover the totals label"

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
        return None, "View results did not appear on hover"

    # Сумма после раскрытия тоже приходит не мгновенно: фиксированная пауза
    # была ставкой на то, что трёх секунд хватит.
    for _ in range(TOTALS_TRIES):
        value, _label = _scan_totals(page)
        if value is not None:
            return value, "totals row opened on hover, Total spent"
        _sleep(1)
    return None, "totals opened, but no amount next to Total spent"


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


def _shots_dir() -> Path:
    return Path(paths.get_local_config_dir()) / "spend_shots"


def _safe_name(account: str) -> str:
    return "".join(c if c.isascii() and (c.isalnum() or c in "-_") else "_" for c in account)


def _shoot(page, account: str, day: str) -> str:
    """Снимок при неудаче: первый живой прогон стоит тестового аккаунта.

    ACL-1535: тот же `page.screenshot`, что сбрасывал вьюпорт профиля
    (ACL-1532), — поэтому только при `ACL_STATUS_SHOT=1`. Без него "" и
    страница не трогается; HTML-снимки `page_capture` остаются как были.
    """
    if not status_shot_enabled():
        return ""
    try:
        out = _shots_dir()
        out.mkdir(parents=True, exist_ok=True)
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in account)
        target = out / f"{safe}_{day}.png"
        page.screenshot(path=str(target), full_page=False)
        return str(target)
    except Exception:
        return ""


# ── Снимок проблемного состояния (ACL-1430) ──────────────────────────────
#
# Отключённый или ограниченный кабинет человеку надо доказать — поддержке,
# поставщику аккаунта, коллеге. Снимаем видимую часть той самой страницы
# Ads Manager, с которой только что прочитали состояние: баннер и колонка
# Delivery там. Страница — вкладка профиля Multilogin, в которую чтение уже
# вошло (`_attach_only`); другой страницы у чтения нет, и снимка вне
# Multilogin здесь не бывает (решение владельца 26.09).
#
# Наружу уходит id, а не путь: `/account-spend/status-shot/<id>` отдаёт файл
# только из этой папки и только по строгому имени.

#: Сколько снимков держать на аккаунт. Нужен последний; пара предыдущих —
#: чтобы сравнить, а не копить диск при каждом Read all.
STATUS_SHOTS_KEEP = 5
_STATUS_SHOT_PREFIX = "status_"
_STATUS_SHOT_ID = re.compile(r"status_[A-Za-z0-9_-]{1,160}")
_SHOT_STAMP = re.compile(r"\d{8}-\d{6}(?:-\d{1,3})?")


def _shot_stamp() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y%m%d-%H%M%S")


def status_shot_path(shot_id: str) -> Path | None:
    """Файл снимка по id — или None. Ничего за пределами папки снимков."""
    if not isinstance(shot_id, str) or not _STATUS_SHOT_ID.fullmatch(shot_id):
        return None
    try:
        folder = _shots_dir().resolve()
        target = (folder / f"{shot_id}.png").resolve()
    except (OSError, ValueError):
        return None
    if target.parent != folder or not target.is_file():
        return None
    return target


def _prune_status_shots(folder: Path, safe: str) -> None:
    """Оставить последние `STATUS_SHOTS_KEEP` снимков этого аккаунта.

    Имя чужого аккаунта может начинаться так же («Doc2» и «Doc20»), поэтому
    свой файл узнаём по точному хвосту-времени, а не по началу имени.
    """
    head = f"{_STATUS_SHOT_PREFIX}{safe}__"
    mine = sorted(
        p for p in folder.glob(f"{_STATUS_SHOT_PREFIX}*.png")
        if p.stem.startswith(head) and _SHOT_STAMP.fullmatch(p.stem[len(head):])
    )
    for old in mine[:-STATUS_SHOTS_KEEP]:
        try:
            old.unlink()
        except OSError:
            pass


# ── Чёткий обрезанный снимок (ACL-1529) ──────────────────────────────────
#
# Отзыв владельца 03.10 (9943, Account Disabled): снимок мелкий и нечитаемый.
# Окно профиля больше отрисованной страницы, и Ads Manager занимал левый
# верхний угол (~1920×1080 из 3837×2157), остальное — белое. Теперь: снимок
# видимой области как есть, а рамка (видимая область по DOM, снизу — последний
# текст таблицы) вырезается уже в Python, затем срезаются белые поля.
#
# Решение владельца 03.10 (ревью PR #530, B2): отпечаток экрана профиля
# Multilogin не трогаем никак. Любой `clip` (с `scale` или без) в
# `Page.captureScreenshot` из новой CDP-сессии сбрасывает чужую подмену
# метрик устройства: замер на Chromium 149 — DPR 1.5 → 1, screen 1920×1080 →
# настоящий размер окна, и насовсем. Старый `page.screenshot(full_page=False)`
# делает то же самое. Не сбрасывает её только голый
# `Page.captureScreenshot` без clip и без scale — его и зовём, а режем потом.
# Поэтому и запасного пути через `page.screenshot` после неудачи нет: лучше
# без снимка, чем со сброшенным экраном профиля. Он остаётся только для
# страниц без CDP вообще (не Chromium).
#
# Зависание (B1): снимок ждёт кадр композитора, у свёрнутого или скрытого
# окна его может не быть никогда, а `CDPSession.send` без тайм-аута — чтение
# держало бы живой профиль вечно. Скрытую страницу не снимаем, всё остальное
# ограничено `_SHOT_DEADLINE_S`.

#: Поле под последней строкой таблицы, CSS px.
_SHOT_PAD = 24
#: Шире не храним: у хоста с DPR 2 видимая область 1920 CSS px — это 3840 px,
#: а копирование потом гоняет `getImageData` по всей картинке (ревью N1).
STATUS_SHOT_MAX_WIDTH = 2600
#: Сколько ждать снимок целиком (рамка + захват), секунд.
_SHOT_DEADLINE_S = 10.0
#: Отказ снимка — предупреждение в Sentry не чаще раза за столько секунд на кабинет.
_SHOT_WARN_EVERY_S = 6 * 3600.0
#: «Пусто» при обрезке полей — как в `amapTrimShot`: все каналы > 249.
_SHOT_BLANK = 249

#: Рамка снимка в CSS px относительно видимой области: верх не режем никогда
#: (там выбор кабинета с номером, баннер Meta и заголовок таблицы), низ —
#: последний текст верхней таблицы (строки, итоги) плюс поле. Таблицы нет (у
#: отключённого кабинета бывает) — вся видимая высота. Плюс видна ли страница
#: и её devicePixelRatio (для журнала: масштаб снимка берётся из него самого).
_SHOT_FRAME_JS = r"""
(pad) => {
  const de = document.documentElement;
  const vv = window.visualViewport;
  const w = Math.floor((vv && vv.width) || de.clientWidth || window.innerWidth);
  const h = Math.floor((vv && vv.height) || de.clientHeight || window.innerHeight);
  const tables = Array.from(document.querySelectorAll('[role="table"], [role="grid"], table'))
    .filter(t => t.getClientRects().length && !t.parentElement.closest('[role="table"], [role="grid"], table'));
  let bottom = 0;
  const range = document.createRange();
  for (const t of tables) {
    const walk = document.createTreeWalker(t, NodeFilter.SHOW_TEXT);
    let n, seen = 0;
    while ((n = walk.nextNode()) && seen < 20000) {
      seen++;
      if (!n.nodeValue.trim()) continue;
      range.selectNodeContents(n);
      const r = range.getBoundingClientRect();
      if (r.width && r.height && r.top < h && r.bottom > 0) bottom = Math.max(bottom, r.bottom);
    }
  }
  const cut = bottom ? Math.min(h, Math.ceil(bottom) + pad) : h;
  return {visible: document.visibilityState === 'visible', dpr: window.devicePixelRatio || 1,
          width: w, height: Math.max(1, cut)};
}
"""


class _ShotFailed(Exception):
    """Снимок не вышел; `kind` — короткая причина для телеметрии и Sentry."""

    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(detail or kind)
        self.kind = kind


def _within(obj, name: str, deadline: float, *args):
    """`obj.name(*args)`, но не дольше `deadline` (по `time.monotonic`).

    Синхронный Playwright можно звать только из своего потока, поэтому для
    него — его же цикл событий и `asyncio.wait_for` вокруг асинхронной
    реализации; ответ, пришедший после срока, Playwright сам отбрасывает.
    Прочие объекты (не Playwright) — в отдельном потоке с `join(timeout)`.
    """
    left = deadline - time.monotonic()
    if left <= 0:
        raise _ShotFailed("timeout", f"{name}: no time left")
    impl = getattr(obj, "_impl_obj", None)
    sync = getattr(obj, "_sync", None)
    if impl is not None and callable(sync):
        import asyncio

        try:
            return sync(asyncio.wait_for(getattr(impl, name)(*args), left))
        except (TimeoutError, asyncio.TimeoutError) as exc:
            raise _ShotFailed("timeout", f"{name}: over {left:.1f}s") from exc
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["ok"] = getattr(obj, name)(*args)
        except BaseException as exc:  # noqa: BLE001 — передаём вызвавшему
            box["err"] = exc

    t = threading.Thread(target=run, name="acl-status-shot", daemon=True)
    t.start()
    t.join(left)
    if t.is_alive():
        raise _ShotFailed("timeout", f"{name}: over {left:.1f}s")
    if "err" in box:
        raise box["err"]
    return box.get("ok")


def _crop_shot(data: bytes, frame: dict) -> bytes:
    """Вырезать рамку (CSS px → пиксели снимка), срезать белые поля справа и снизу,
    ужать до `STATUS_SHOT_MAX_WIDTH`. Без Pillow — снимок как есть: поля
    тогда срежет `amapTrimShot` при копировании."""
    try:
        from PIL import Image, ImageChops
    except ImportError:
        return data
    import io

    with Image.open(io.BytesIO(data)) as src:
        img = src.convert("RGB")
    # Снимок без clip — ровно видимая область, поэтому пикселей на CSS px —
    # ширина картинки на ширину области. Без подмены это и есть
    # devicePixelRatio; с подменой нет: замер 03.10 на headless Chromium 149 —
    # хост 1.5 и подмена DPR 0.75 дают картинку 1920 на 1280 CSS px (×1.5),
    # хост 1 и подмена DPR 2 — 1280 (×1). Умножение на DPR там срезало бы
    # половину кадра или не обрезало бы низ.
    scale = img.width / float(frame["width"])
    w = img.width
    h = max(1, min(img.height, round(float(frame["height"]) * scale)))
    img = img.crop((0, 0, w, h))
    # Не пусто там, где хоть один канал ≤ 249; поля — только справа и снизу.
    r, g, b = (ch.point(lambda v: 255 if v <= _SHOT_BLANK else 0) for ch in img.split())
    box = ImageChops.lighter(ImageChops.lighter(r, g), b).getbbox()
    if box:
        pad = round(max(w, h) / 200)
        right, bottom = min(w, box[2] + pad), min(h, box[3] + pad)
        if (right, bottom) != (w, h):
            img = img.crop((0, 0, right, bottom))
    if img.width > STATUS_SHOT_MAX_WIDTH:
        size = (STATUS_SHOT_MAX_WIDTH, max(1, round(img.height * STATUS_SHOT_MAX_WIDTH / img.width)))
        img = img.resize(size, Image.Resampling.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def _full_view_shot(page, target: Path, deadline_s: float | None = None) -> None:
    """Снимок видимой области голым `Page.captureScreenshot` (без clip и
    scale), рамка вырезается в Python. Не вышло — `_ShotFailed`."""
    deadline = time.monotonic() + (_SHOT_DEADLINE_S if deadline_s is None else deadline_s)
    try:
        frame = _within(page, "evaluate", deadline, _SHOT_FRAME_JS, _SHOT_PAD)
    except _ShotFailed:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _ShotFailed("no_frame", str(exc)) from exc
    if not isinstance(frame, dict) or float(frame.get("width") or 0) < 1 or float(frame.get("height") or 0) < 1:
        raise _ShotFailed("no_frame")
    if not frame.get("visible"):
        # Свёрнутое или перекрытое окно: кадра композитора может не быть вовсе.
        raise _ShotFailed("hidden")
    session = None
    try:
        import base64

        try:
            session = page.context.new_cdp_session(page)
            got = _within(session, "send", deadline, "Page.captureScreenshot",
                          {"format": "png", "captureBeyondViewport": False})
        except _ShotFailed:
            raise
        except Exception as exc:  # noqa: BLE001
            raise _ShotFailed("error", str(exc)) from exc
        data = base64.b64decode((got or {}).get("data") or "")
        if not data.startswith(b"\x89PNG"):
            raise _ShotFailed("not_png")
        try:
            data = _crop_shot(data, frame)
        except Exception as exc:  # noqa: BLE001 — неразрезанный снимок лучше, чем никакого
            log.debug("status shot: crop failed, keeping the whole view: %s", exc)
        target.write_bytes(data)
    finally:
        if session is not None:
            try:
                _within(session, "detach", max(deadline, time.monotonic() + 3.0))
            except Exception:  # noqa: BLE001
                pass


_shot_warned: dict[str, float] = {}
_shot_warned_lock = threading.Lock()


def _report_shot_failure(account: str, kind: str, detail: str) -> None:
    """Предупреждение в Sentry об отказе снимка: вычищенное, не чаще раза в
    `_SHOT_WARN_EVERY_S` на кабинет. Никогда не бросает."""
    try:
        key = _safe_name(account) or "account"
        now = time.monotonic()
        with _shot_warned_lock:
            last = _shot_warned.get(key)
            if last is not None and now - last < _SHOT_WARN_EVERY_S:
                return
            _shot_warned[key] = now
        from src import acl_sentry

        if not acl_sentry.init():
            return
        import sentry_sdk

        with sentry_sdk.new_scope() as scope:
            scope.fingerprint = ["acl-status-shot", kind]
            scope.set_tag("step", "status_shot")
            scope.set_tag("reason", kind)
            tag = acl_sentry.account_tag(account)
            if tag:
                scope.set_tag("account", tag)
            scope.set_context("status_shot", {"kind": kind, "detail": scrub_tree_error(detail)[:500]})
            sentry_sdk.capture_message(f"status_shot: {kind}", level="warning")
    except Exception:  # noqa: BLE001 — отчёт не должен ломать чтение
        pass


def _record_shot_path(account: str, path: str, kind: str = "") -> None:
    """Каким путём снят снимок: `full`, `fallback` (без CDP) или `none`."""
    try:
        from src.fb_ads_rpa import run_telemetry as _rt

        diag = {"account": account, "path": path}
        if kind:
            diag["reason"] = kind
        _rt.record("status_shot", path != "none", diag=diag)
    except Exception:  # noqa: BLE001
        pass


def _status_shot(page, account: str) -> tuple[str, str]:
    """Снять видимую часть страницы. (id, время) или ("", "") при неудаче."""
    try:
        folder = _shots_dir()
        folder.mkdir(parents=True, exist_ok=True)
        safe = _safe_name(account)[:120] or "account"
        stem = f"{_STATUS_SHOT_PREFIX}{safe}__{_shot_stamp()}"
        shot_id, n = stem, 1
        while (folder / f"{shot_id}.png").exists() and n < 1000:
            shot_id, n = f"{stem}-{n}", n + 1
        target = folder / f"{shot_id}.png"
        context = getattr(page, "context", None)
        if context is None or not hasattr(context, "new_cdp_session"):
            # Не Chromium: CDP нет — и подмены экрана через него тоже.
            page.screenshot(path=str(target), full_page=False)
            _record_shot_path(account, "fallback")
        else:
            try:
                _full_view_shot(page, target)
            except _ShotFailed as exc:
                log.warning("account status: no screenshot for %s (%s)", account, exc.kind)
                _record_shot_path(account, "none", exc.kind)
                _report_shot_failure(account, exc.kind, str(exc))
                return "", ""
            _record_shot_path(account, "full")
        at = _now()
        _prune_status_shots(folder, safe)
        return shot_id, at
    except Exception as exc:  # noqa: BLE001 — снимок не стоит чтения
        log.warning("account status: screenshot for %s was not saved: %s", account, exc)
        return "", ""


def status_shot_enabled() -> bool:
    """Снимать ли состояние (ACL-1535). По умолчанию — нет.

    ACL-1532 измерил вживую: `page.screenshot` сбрасывал чужой (Multilogin)
    override вьюпорта — 1919×1079 → 3837×2157 и так и оставалось, а размер окна
    входит в отпечаток браузера. Решение владельца 03.10: выключить, пока способ
    снимка не доказан безопасным. `ACL_STATUS_SHOT=1` в машинном .env
    (serve.py грузит его через `paths.load_env`) — только для контролируемых
    проверок.
    """
    return (os.environ.get("ACL_STATUS_SHOT") or "").strip() == "1"


def _last_status_shot(account: str) -> tuple[str, str]:
    """Последний сохранённый снимок аккаунта, если он ещё на диске."""
    try:
        got = get_status(account)
    except Exception:  # noqa: BLE001 — без старого снимка чтение не хуже
        return "", ""
    return got.get("shot_id", ""), got.get("shot_at", "")


def _shoot_status(page, out: "SpendReading") -> None:
    """Снимок, если состояние — не «чисто». Чистая страница не снимается.

    Без `ACL_STATUS_SHOT=1` страницу не трогаем вовсе (ACL-1535): ни
    `page.screenshot`, ни CDP. Строке и Copy for group остаётся последний
    сохранённый снимок, если он есть.
    """
    if out.status and out.status != "clear":
        if status_shot_enabled():
            out.status_shot, out.status_shot_at = _status_shot(page, out.account)
        else:
            out.status_shot, out.status_shot_at = _last_status_shot(out.account)


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

    #: Id, взятый не из строки таблицы, а из данных, которые Meta вшивает в
    #: страницу (`campaign_structure_tree`). Бывает только у кампаний аккаунта,
    #: чья таблица id не показывает (Doc19, 18.09). Нужен, чтобы спросить
    #: объявления этой кампании и разложить их под ней, — и только для этого.
    #: В `id` его не кладём намеренно: `id` — это то, что можно найти в строке
    #: и выключить, а в такой таблице строки с этим id нет.
    data_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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
        raise ValueError(f"unknown level {level!r}")
    base = _url(act, date_from, date_to)
    url = ADS_MANAGER_LEVELS[level] + base[len(ADS_MANAGER):]
    if parent_id:
        if level != "ad":
            raise ValueError("only the ads list can be narrowed to a parent")
        param = TREE_PARENT_PARAM.get(parent_level)
        if not param:
            raise ValueError(f"unknown parent level {parent_level!r}")
        if not parent_id.isdigit():
            raise ValueError("parent id must contain only digits")
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
_TOP_TABLE_JS = r"""
() => {
  const t = Array.from(document.querySelectorAll('[role="table"], [role="grid"]'))
    .filter(el => el.getClientRects().length && !el.parentElement.closest('[role="table"], [role="grid"]'));
  return t.length ? t[0].outerHTML : null;
}
"""


def _snapshot_empty_table(page, level: str, how: str) -> None:
    """Таблица нашлась, а строк в ней нет — сохранить, что было на экране.

    18.09 11:24 (FacebookDoc19): спенд прочитался ($228.85), а дерево — «campaign
    table has no rows (no-row-surfaces)», и скриншота не было. Гадать, фильтр ли
    это, недорисованные строки или новая разметка, по пустому отчёту нельзя.
    Страница Ads Manager больше 4 МБ и обрезается, поэтому саму таблицу кладём
    рядом отдельным файлом целиком.
    """
    try:
        from src.fb_ads_rpa import page_capture as _pc

        folder = _pc.capture(page, step=f"tree_{level}_no_rows", diag={"level": level, "how": how})
        if not folder:
            return
        table = page.evaluate(_TOP_TABLE_JS)
        if table:
            body, _cut = _pc.scrub_html(table)
            from pathlib import Path as _P

            (_P(folder) / "block_table.html").write_text(body, encoding="utf-8")
    except Exception:
        pass


#: Надписи, которые Meta рисует в ячейке имени рядом с самим именем (ACL-1395).
#:
#: 30.09, Facebook_6540: объявление прочиталось как
#: «1741 - Pro Bancolombia Unpublished edits Unpublished edits» — надпись о
#: несохранённых правках, дважды, — и книга статистики не нашла под ним ни
#: одной строки. Так же пришли ещё одно объявление и кампания (сохранённые
#: чтения этой машины). «In draft» Meta пишет у черновиков (снятые страницы
#: `rpa_pages`); «In review» и «Draft» — надписи того же ряда по списку
#: владельца.
#:
#: Список один. Им пользуются разбор таблицы и поиск тумблера (в JS он
#: подставляется отсюда) и `clean_object_name`; в `report.html` — копия для
#: уже сохранённых чтений, и тест держит её равной этой.
#:
#: Снимаются только с конца имени, целым словом и точно как написано:
#: «2002 - Draft 5 promo» остаётся как есть.
META_NAME_LABELS: tuple[str, ...] = ("Unpublished edits", "In draft", "In review", "Draft")


def clean_object_name(name: Any) -> str:
    """Имя без надписей Meta в конце; пробелы схлопнуты, как их схлопывает разбор.

    Надпись снимается, только если перед ней есть имя: объект, названный
    просто «Draft», остаётся «Draft», а не пустой строкой.
    """
    text = " ".join(str(name or "").split())
    while True:
        for label in META_NAME_LABELS:
            if text.endswith(" " + label):
                text = text[: -len(label) - 1].rstrip()
                break
        else:
            return text


#: Та же чистка для страницы. Список подставляется из `META_NAME_LABELS`,
#: чтобы у разбора таблицы и у поиска тумблера не было своих копий.
#:
#: Имя ячейки берётся сперва из подсказки вокруг ссылки на объект
#: (`data-tooltip-content`) — там Meta держит одно имя, без надписей рядом;
#: на снятых страницах она есть у каждой строки. Нет подсказки — текст
#: ячейки, и тогда надписи снимает правило.
_CLEAN_NAME_JS = (
    "const META_NAME_LABELS = " + json.dumps(list(META_NAME_LABELS)) + ";\n"
    r"""  const cleanName = (s) => {
    let text = (s || '').replace(/\s+/g, ' ').trim();
    for (;;) {
      const hit = META_NAME_LABELS.find(l => text.endsWith(' ' + l));
      if (!hit) return text;
      text = text.slice(0, -hit.length - 1).trim();
    }
  };
  const cellName = (cell) => {
    const tips = Array.from(cell.querySelectorAll('[data-tooltip-content]'))
      .filter(e => e.querySelector('a'));
    const tip = tips.length === 1 ? tips[0].getAttribute('data-tooltip-content') : '';
    return cleanName(tip && tip.trim() ? tip : cell.innerText);
  };"""
)


_READ_TREE_JS = r"""
(level) => {
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  /*ACL_CLEAN_NAME*/
  // Разметку строк Meta теперь вешает на обёртки `display: contents` (17.09):
  // своих прямоугольников у них нет, и все 88 строк Doc20 считались
  // невидимыми. Видимость такой обёртки — видимость её содержимого.
  const hasBox = (el) => {
    if (el.getClientRects().length > 0) return true;
    if (getComputedStyle(el).display !== 'contents') return false;
    return Array.from(el.children).some(hasBox);
  };
  const visible = (el) => hasBox(el) &&
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
  if (!marked.length) {
    // Новая таблица (FacebookDoc19, 18.09): меток table_row нет вовсе, ссылка
    // на имени ведёт в «#», id кампании в строке нет. Имя — в подсказке у
    // ссылки, переключатель — role=switch, а правые ячейки идут в том же
    // порядке, что заголовки после toggle и name. Читаем по имени и честно
    // говорим, что id нет (how: by-name).
    const heads = Array.from(t.querySelectorAll('[data-surface*="table_column_header:"]'))
      .filter(visible)
      .map(h => (h.getAttribute('data-surface') || '').split(':').pop());
    const dataCols = heads.filter(c => c !== 'toggle' && c !== 'name');
    const tips = Array.from(t.querySelectorAll('[data-tooltip-content]'))
      .filter(e => visible(e) && norm(e.getAttribute('data-tooltip-content')));
    let nameEls = tips.filter(e => e.querySelector('a'));
    if (!nameEls.length) {
      // 22.09 (Doc19, список объявлений): имя объявления — не ссылка, а
      // просто текст с подсказкой. Имя строки тогда — подсказка внутри строки
      // с переключателем; у заголовка колонки («Ad») ни строки, ни тумблера нет.
      nameEls = tips.filter(e => {
        const row = e.closest('[role="presentation"]');
        return row && t.contains(row) && row.querySelector('[role="switch"]');
      });
    }
    if (!nameEls.length) return {rows: [], how: 'no-row-surfaces', kinds: []};
    const firstLeaf = (c) => {
      const leaf = Array.from(c.querySelectorAll('*')).find(e => visible(e) && !e.children.length && norm(e.textContent));
      return leaf ? norm(leaf.textContent) : norm(c.innerText);
    };
    const rows = [];
    for (const n of nameEls) {
      // The recorded layout wraps each virtualized row in presentation.
      // Counting names while walking upwards escapes the row when there is
      // only one campaign, and then reads the table headings as its values.
      const row = n.closest('[role="presentation"]');
      if (!row || !t.contains(row) ||
          nameEls.filter(e => row.contains(e)).length !== 1)
        return refuse('unrecognized-name-row');
      let wide = row;
      const q = [row];
      while (q.length) { const x = q.shift(); if (x.children.length > wide.children.length) wide = x; q.push(...x.children); }
      const cells = Array.from(wide.children);
      const val = (col) => { const i = dataCols.indexOf(col); return i >= 0 && i < cells.length ? firstLeaf(cells[i]) : ''; };
      const sw = row.querySelector('[role="switch"]');
      const aria = sw ? sw.getAttribute('aria-checked') : null;
      rows.push({
        id: '', id_from: 'none', name: cleanName(n.getAttribute('data-tooltip-content')),
        switch: aria === 'true' ? 'on' : aria === 'false' ? 'off' : 'unknown',
        switch_label: sw ? norm(sw.getAttribute('aria-label')) : '',
        delivery: val('delivery'), results: val('results'),
        cost_per_result: val('cost_per_result'), spend: val('spend'), kinds: [],
      });
    }
    // Уровень — из меток подсказок Meta: /am/table/hc(type=recs,level=CAMPAIGN_GROUP,id=…).
    const lk = new Set();
    Array.from(t.querySelectorAll('[data-surface*="level="]')).filter(visible).forEach(e => {
      const m = (e.getAttribute('data-surface') || '').match(/level=([A-Z_]+)/);
      if (m) lk.add(m[1]);
    });
    // 22.09 (Doc19, список объявлений): подсказок с level= на этой странице
    // нет вовсе. Уровень тогда называет заголовок колонки имени — «Ad»,
    // «Ad set», «Campaign». Незнакомое слово (другой язык) — уровня нет, и
    // разбор откажет, как отказывал.
    if (!lk.size) {
      const head = t.querySelector('[data-surface*="table_column_header:name"]');
      const word = head ? norm(head.textContent) : '';
      const byWord = {'Campaign': 'CAMPAIGN_GROUP', 'Ad set': 'CAMPAIGN', 'Ad': 'ADGROUP'};
      if (byWord[word]) lk.add(byWord[word]);
    }
    return {rows, how: 'by-name', kinds: Array.from(lk)};
  }

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
      // Имя без надписей Meta рядом с ним (ACL-1395): «Unpublished edits».
      const txt = cellName(el);
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
""".replace("/*ACL_CLEAN_NAME*/", _CLEAN_NAME_JS)


def read_level(page, level: str) -> tuple[list[AdObject], str, str]:
    """Разобрать открытую таблицу одного уровня.

    Возвращает `(объекты, чем разобрано, ошибка)`. Страница должна быть уже
    на нужном уровне: переходами заведует `_read_tree`, которому есть что
    сверять (кабинет, период), а этой функции — нет.

    Ничего не нажимает. Это чтение, и единственное, что здесь происходит со
    страницей, — вопрос к ней.
    """
    if level not in ADS_MANAGER_LEVELS:
        return [], "", f"unknown level {level!r}"
    # Таблица «занята» — ждём, а не отказываем с первого взгляда. 17.09 на
    # Doc20/Doc21/S15 дерево упало на `table-loading` сразу после раскрытия
    # итогов (таблица перерисовывалась), а через секунды те же таблицы были
    # свободны: 88, 68 и 60 строк.
    got = None
    for _attempt in range(TABLE_BUSY_TRIES):
        try:
            got = page.evaluate(_READ_TREE_JS, level)
        except Exception as exc:
            return [], "", f"{level} table was not parsed: {exc}"
        if not (isinstance(got, dict) and got.get("error") == "table-loading"):
            break
        _sleep(1)
    if not isinstance(got, dict):
        return [], "", f"{level} table: unexpected page response"

    how = str(got.get("how") or "")
    view = str(got.get("view") or "")
    if view:
        # Вид — часть ответа на «что мы прочитали». Список «Active ads» и
        # список «All ads» — разные списки, и одинаково выглядящее число
        # строк в них значит разное.
        how = f"{how}({view})"
    if got.get("error"):
        return [], how, f"{level} table: {got['error']}"
    rows = got.get("rows") or []
    want = _LEVEL_KIND[level]
    by_name = str(got.get("how") or "") == "by-name"
    if by_name:
        # Новая таблица без id: принимаем строку, если у неё есть имя. Выключать
        # такие объекты по id нельзя — turn_off это знает.
        if not isinstance(rows, list) or any(
            not isinstance(r, dict) or not str(r.get("name") or "").strip() for r in rows
        ):
            return [], how, f"{level} table: a row has no name"
    # Every accepted object must identify itself, not borrow another row's kind.
    if not by_name and (not isinstance(rows, list) or any(
        not isinstance(r, dict)
        or not re.fullmatch(r"[0-9]+", str(r.get("id") or ""))
        or not str(r.get("name") or "").strip()
        or set(r.get("kinds") or []) != {want}
        for r in rows
    )):
        return [], how, f"{level} table: a row did not confirm its id, name or object type ({want})"
    objects = [
        AdObject(
            level=level,
            id=str(r.get("id") or ""),
            # Страница уже чистит; правило ещё раз — на случай, если её
            # разметка изменится, а надпись окажется в тексте ячейки.
            name=clean_object_name(r.get("name")),
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
        _snapshot_empty_table(page, level, how)
        return [], how, f"{level} table has no rows ({how})"

    # Уровень сверяется по тому, что Meta написала о самих строках, а не по
    # адресу: открыть `/manage/adsets` и получить другой список — можно, и по
    # одному URL этого не увидеть. Спутать уровни здесь дороже всего: на это
    # встанет тумблер, и разница между ad set'ом и кампанией — разница между
    # «остановить одно» и «погасить всё под ней».
    kinds = {str(k) for k in (got.get("kinds") or [])}
    want = _LEVEL_KIND[level]
    if kinds and kinds != {want}:
        return [], how, (
            f"{level} level: expected {want} objects, but the page marked "
            f"{', '.join(sorted(kinds))}; wrong list, nothing taken"
        )
    if not kinds:
        return [], how, f"{level} level: the page did not say what these objects are"
    if by_name:
        return objects, how, f"read by name: this account's table has no {level} ids"
    warning = (
        "read the rendered rows; the list scrolls, so completeness is not confirmed"
        if got.get("partial") else ""
    )
    return objects, how, warning


def _read_settled_ads(page, *, timeout_s: float = 10.0) -> tuple[list[AdObject], str, str]:
    """Прочитать объявления одной кампании, когда список перестал меняться.

    19.09 Doc20: все десять кампаний вернули один и тот же набор объявлений,
    хотя 17–18.09 при пяти кампаниях тот же путь давал верную разбивку. Meta
    сперва рисует таблицу без фильтра по кампании и сужает её чуть позже;
    чтение сразу по появлению таблицы брало общий список. Читаем, пока два
    чтения подряд не совпадут.
    """
    import time as _time

    got, how, err = read_level(page, "ad")
    pause = getattr(page, "wait_for_timeout", None)
    if pause is None:  # не браузер (подставная страница) — ждать нечего
        return got, how, err
    deadline = _time.monotonic() + timeout_s
    while _time.monotonic() < deadline:
        pause(1500)
        again, how2, err2 = read_level(page, "ad")
        # У таблицы без id (Doc19) id пустые — сравниваем и по имени.
        if [(o.id, o.name) for o in again] == [(o.id, o.name) for o in got]:
            return again, how2, err2
        got, how, err = again, how2, err2
    return got, how, err


#: Данные дерева, которые Meta вшивает в страницу списка кампаний.
#:
#: 22.09 (Doc19, таблица без id): в разметке строк id нет, но в
#: `<script type="application/json">` страницы лежит `campaign_structure_tree`
#: каждой кампании — её id, имя, ad set'ы и объявления с их id и именами.
#: Это собственный предзагруженный ответ Meta для той же страницы, не догадка.
_PAGE_STRUCTURE_JS = r"""
() => {
  const out = [];
  const walk = (v, depth) => {
    if (!v || typeof v !== 'object' || depth > 60) return;
    if (Array.isArray(v)) { for (const x of v) walk(x, depth + 1); return; }
    const t = v.campaign_structure_tree;
    if (t && typeof t === 'object' && /^[0-9]+$/.test(String(t.campaign_id || ''))) {
      const ads = [];
      for (const set of (t.children || [])) {
        for (const ad of ((set && set.children) || [])) {
          if (ad && typeof ad.name === 'string') ads.push(ad.name);
        }
      }
      out.push({id: String(t.campaign_id), name: String(t.name || ''), ads: ads});
    }
    for (const k in v) walk(v[k], depth + 1);
  };
  for (const s of document.querySelectorAll('script[type="application/json"]')) {
    const text = s.textContent || '';
    if (text.indexOf('campaign_structure_tree') === -1) continue;
    try { walk(JSON.parse(text), 0); } catch (e) { /* чужой формат — пропускаем */ }
  }
  return out;
}
"""


def _name_key(name: Any) -> str:
    """Имя для сравнения — как его чистит разбор таблицы: пробелы схлопнуты.

    Таблица схлопывает любые пробелы (и неразрывные) в один, а данные страницы
    отдают имя как есть. Без общей чистки имя с двойным пробелом не совпало бы
    никогда — безопасно, но молча.
    """
    return " ".join(str(name or "").split())


def _campaigns_from_page_data(page) -> dict[str, dict]:
    """Кампании из вшитых данных страницы: `{имя: {"id", "ads"}}`.

    `ads` — `Counter` имён объявлений: у клонов имена повторяются, и сколько
    раз имя есть в кампании, так же важно, как само имя.

    Берём только имена, встретившиеся **с одним id**: две кампании с одним
    именем по имени не различить, и приписать объявления одной из них значило
    бы угадать. Ошибка страницы — пустой ответ, а не падение: без этих данных
    дерево просто остаётся без объявлений, как было до них.
    """
    try:
        got = page.evaluate(_PAGE_STRUCTURE_JS)
    except Exception:
        return {}
    from collections import Counter

    ids: dict[str, set[str]] = {}
    ads: dict[str, Counter] = {}
    for item in got if isinstance(got, list) else []:
        if not isinstance(item, dict):
            continue
        name = _name_key(item.get("name"))
        cid = str(item.get("id") or "")
        if not name or not cid.isdigit():
            continue
        if cid in ids.get(name, set()):
            continue  # тот же узел, встреченный в ответе ещё раз
        ids.setdefault(name, set()).add(cid)
        ads.setdefault(name, Counter()).update(
            _name_key(a) for a in (item.get("ads") or []) if _name_key(a)
        )
    # Имя с двумя id отдаём без id: его не различить, но и «данных нет» про
    # него сказать нельзя — сообщения должны различаться.
    return {name: {"id": next(iter(found)) if len(found) == 1 else "",
                   "ads": ads.get(name, Counter()) if len(found) == 1 else Counter()}
            for name, found in ids.items()}


def _read_tree(
    page, act: str, expected_act: str, date_from: str, date_to: str = "",
    *, reached: dict[str, bool] | None = None,
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
    """
    objects: list[dict] = []
    hows: list[str] = []
    errors: list[str] = []
    data_keys: set[str] = set()  # кампании, чьи объявления сверены по данным страницы

    def _context_matches(what: str, parent_id: str = "") -> bool:
        from urllib.parse import parse_qs, urlsplit

        if _act_from_url(page, tries=1) != expected_act:
            errors.append(f"{what}: a different ad account opened; skipped")
            return False
        if not _period_matches(_shown_period(page), date_from, date_to):
            errors.append(f"{what}: a different period is on screen; skipped")
            return False
        if parent_id:
            params = parse_qs(urlsplit(page.url).query)
            if params.get(TREE_PARENT_PARAM[top]) != [parent_id]:
                errors.append(f"{what}: the parent campaign filter changed; skipped")
                return False
        return True

    def _open(url: str, what: str, parent_id: str = "") -> bool:
        """Перейти и сверить, что открылось именно то, что просили."""
        try:
            page.goto(url, timeout=TABLE_TIMEOUT_MS, wait_until="domcontentloaded")
        except Exception as exc:
            errors.append(f"{what}: navigation failed ({exc})")
            return False
        if not _wait_for_grid(page):
            errors.append(f"{what}: the table did not load in time")
            return False
        return _context_matches(what, parent_id)

    # Сначала верхний уровень: его id нужны, чтобы спросить объявления каждого.
    top = TREE_LEVELS[0]
    if _open(_tree_url(act, top, date_from, date_to), top):
        parents, how, err = read_level(page, top)
        if not _context_matches(top):
            return [], how, "; ".join(errors)
        if reached is not None:
            # ACL-1504: список кампаний прочитан (пусть и пустой) — отказ
            # разбора таблицы приходит пустым списком с ошибкой, это не он.
            reached["list"] = bool(parents) or not err
        if how:
            hows.append(f"{top}:{how}")
        if err:
            errors.append(err)

        # Таблица без id (Doc19): id кампаний берём из данных самой страницы,
        # пока она ещё открыта. Кому не нашлось — остаются без объявлений, и
        # это говорится вслух.
        nameless = [p for p in parents if not p.id]
        structure: dict[str, dict] = {}
        if nameless:
            from collections import Counter

            structure = _campaigns_from_page_data(page)
            # Имя, дважды стоящее в таблице, по имени не различить — даже если в
            # данных страницы оно одно: второй строке достался бы чужой id.
            in_table = Counter(_name_key(p.name) for p in nameless)
            shared = missing = 0
            for p in nameless:
                hit = structure.get(_name_key(p.name))
                if in_table[_name_key(p.name)] > 1 or (hit and not hit["id"]):
                    shared += 1
                elif hit:
                    p.data_id = hit["id"]
                else:
                    missing += 1
            if shared:
                errors.append(
                    f"ads were not read for {shared} campaigns that share a name "
                    "with another one: without ids they cannot be told apart"
                )
            if missing:
                errors.append(
                    f"ads were not read for {missing} of {len(nameless)} campaigns "
                    "read by name: the page did not give their ids"
                )
        objects.extend(o.to_dict() for o in parents)

        wanted = [p for p in parents if p.id or p.data_id]
        if len(wanted) > TREE_MAX_PARENTS:
            # Молчаливое усечение читается как «это всё». Говорим вслух.
            errors.append(
                f"ads were read for the first {TREE_MAX_PARENTS} of "
                f"{len(wanted)} campaigns: each one is a separate visit to the account"
            )
            wanted = wanted[:TREE_MAX_PARENTS]

        ads_how: set[str] = set()
        ads_by_name = False
        for parent in wanted:
            key = parent.id or parent.data_id
            what = f"{top} {key}"
            url = _tree_url(act, "ad", date_from, date_to,
                            parent_level=top, parent_id=key)
            if not _open(url, what, key):
                continue
            got, how, err = _read_settled_ads(page)
            if not _context_matches(what, key):
                continue
            if how:
                ads_how.add(how)
            if not parent.id and err.startswith("read by name"):
                # Одна и та же пометка на каждую кампанию — шум; скажем один раз.
                ads_by_name, err = True, ""
            if err:
                errors.append(f"{what}: {err}")
            if not parent.id:
                # Родитель найден по данным страницы, а не по строке — сверяем,
                # что пришли именно его объявления: каждое имя должно быть в
                # его структуре, и не чаще, чем там. Адрес с фильтром ещё не
                # значит, что Meta его применила: проигнорированный фильтр
                # вернул бы все объявления аккаунта, а у клонов имена одни и те
                # же — одной проверки «имя знакомо» тут мало.
                from collections import Counter

                known = structure.get(_name_key(parent.name), {}).get("ads", Counter())
                seen = Counter(_name_key(o.name) for o in got)
                if any(n > known.get(name, 0) for name, n in seen.items()):
                    errors.append(f"{what}: the ads did not match this campaign; skipped")
                    continue
                data_keys.add(key)
            for o in got:
                o.parent_id = key
                objects.append(o.to_dict())
        if ads_by_name:
            errors.append("read by name: this account's table has no ad ids")
        if ads_how:
            hows.append("ad:" + "/".join(sorted(ads_how)))

    # A stale/ignored filter may return the same ad under different parents.
    # Its ownership is then ambiguous: keep neither association.
    # Объявления без id под кампаниями из данных страницы: сверка по структуре
    # выше ловит проигнорированный фильтр, но не устаревший — у клонов имена
    # одинаковы, и строка кампании A, оставшаяся на экране при заходе в B,
    # проходит сверку B. Такую строку выдаёт то, что она та же самая: те же
    # имя, деньги, результаты, доставка. Одна и та же строка с деньгами под
    # двумя кампаниями — родитель неизвестен, не берём ни одну. Строки без
    # спенда и результатов не сравниваем: у клонов, которые не крутились, они
    # законно одинаковы, а денег чужих на них нет.
    def _print(o: dict) -> tuple:
        return (_name_key(o.get("name")), o.get("spend"), o.get("results"),
                o.get("cost_per_result"), o.get("delivery"), o.get("switch"))

    def _has_money(o: dict) -> bool:
        return bool(re.search(r"[1-9]", str(o.get("spend") or "") + str(o.get("results") or "")))

    seen_under: dict[tuple, set[str]] = {}
    for obj in objects:
        if obj["level"] == "ad" and not obj["id"] and obj["parent_id"] in data_keys \
                and _has_money(obj):
            seen_under.setdefault(_print(obj), set()).add(obj["parent_id"])
    doubled = {fp for fp, parents in seen_under.items() if len(parents) > 1}
    if doubled:
        objects = [o for o in objects if not (
            o["level"] == "ad" and not o["id"] and o["parent_id"] in data_keys
            and _print(o) in doubled)]
        errors.append("the same ads came back under different campaigns; ambiguous links skipped")

    # По пустому id объявления сравнивать нельзя — совпали бы все; такие
    # сверены выше. Остальные — как прежде.
    owners: dict[str, set[str]] = {}
    for obj in objects:
        if obj["level"] == "ad" and (obj["id"] or obj["parent_id"] not in data_keys):
            owners.setdefault(obj["id"], set()).add(obj["parent_id"])
    ambiguous = {oid for oid, parents in owners.items() if len(parents) > 1}
    if ambiguous:
        # Снимок страницы объявлений: что Meta показывает при фильтре по
        # кампании, если фильтр не сработал (Doc20, 19.09).
        try:
            from src.fb_ads_rpa import page_capture as _pc
            _pc.capture(page, step="tree_ad_ambiguous_parents",
                        diag={"ambiguous": len(ambiguous), "url_params": page.url.split("?", 1)[-1][:300]},
                        blocks={"table": _TOP_TABLE_JS})
        except Exception:
            pass
        objects = [o for o in objects if not (o["level"] == "ad" and o["id"] in ambiguous)]
        errors.append("the same ads came back under different campaigns; ambiguous links skipped")
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

    Пишем через `acl_log.append_line`, а не `open(..., "a")`. Замер 15.09: при
    одновременном выключении на нескольких аккаунтах простое дописывание на
    Windows теряло каждую шестую запись — бесследно, потому что там режим
    «a» эмулируется как «перейти в конец, затем записать». Для этого журнала
    потеря особенно дорога: наутро нельзя отличить «действия не было» от
    «запись пропала», а он существует именно ради такого разбора.

    Отказ записи больше не проглатывается молча: прежде нехватка места,
    отсутствие прав и занятый файл выглядели одинаково — никак.
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
        acl_log.append_line(
            _turn_off_log(), json.dumps(out.to_dict(), ensure_ascii=False)
        )
    except acl_log.JournalWriteError as exc:
        log.error("журнал выключений не принял запись: %s", exc)
    except Exception as exc:  # непредвиденное — но результат всё равно вернём
        log.error("журнал выключений: неожиданный отказ %r", exc)


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
  /*ACL_CLEAN_NAME*/
  // display:contents-обёртки строк (Meta, 17.09) своих прямоугольников не имеют —
  // их видимость берётся у содержимого, как в чтении дерева.
  const hasBox = e => e.getClientRects().length > 0 || (getComputedStyle(e).display === 'contents' && [...e.children].some(hasBox));
  const visible = e => hasBox(e) && !e.closest('[hidden],[aria-hidden="true"],[inert]') && getComputedStyle(e).visibility !== 'hidden';
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
    // Имя — как его прочитало дерево: без надписей Meta (ACL-1395). Иначе
    // строку с «Unpublished edits» нельзя было бы выключить: дешборд
    // показывает и присылает чистое имя.
    if (m[2].startsWith('forObjectType(name') && cellName(el)) names.add(cellName(el));
    if (m[2].startsWith('forObjectType(delivery') && norm(el.innerText)) deliveries.add(norm(el.innerText));
    if (m[2].startsWith('forObjectType(toggle')) {
      el.querySelectorAll('[role="switch"],input[type="checkbox"]').forEach(sw => {
        if (visible(sw)) switches.add(sw);
      });
    }
  }
  if (kinds.size !== 1 || !kinds.has(args.kind) || names.size !== 1 || switches.size !== 1) return fail('row identity or switch is ambiguous');
  const name = [...names][0], sw = [...switches][0];
  if (!args.name || name !== cleanName(args.name)) return fail('row name changed');
  if (sw.disabled || sw.closest('[aria-disabled="true"]')) return fail('switch is disabled');
  const aria = sw.getAttribute('aria-checked');
  const state = aria === 'true' ? 'on' : aria === 'false' ? 'off' :
    aria === null && sw.matches('input[type="checkbox"]') && !sw.indeterminate ? (sw.checked ? 'on' : 'off') : 'unknown';
  const label = norm(sw.getAttribute('aria-label'));
  const result = {found:true, name, delivery:deliveries.size === 1 ? [...deliveries][0] : '', on:state === 'on', off:state === 'off', label};
  // Метка до нажатия: при «covered» вызывающий прокручивает именно этот тумблер.
  sw.setAttribute('data-acl-switch', '1');
  if (args.click) {
    // Identity, context and state are read and acted upon in one JS task.
    // If Meta switched it off meanwhile, clicking again would turn it ON.
    if (location.href !== args.url || label !== args.label || state === 'unknown') return fail('context or switch changed before action');
    if (state === 'on') {
      const r = sw.getBoundingClientRect(), hit = document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);
      // Сам input тумблера Meta прячет под нарисованной «дорожкой» — соседом в
      // той же ячейке (17.09 Doc20: «switch is covered» на свободной странице).
      // Попадание в эту же ячейку тумблера этой строки — наш тумблер; всё,
      // что вне ячейки (диалог, баннер), по-прежнему закрывает его.
      const cell = sw.closest('[data-surface*="/table_cell:forObjectType(toggle"]');
      // A cell can also host a menu or confirmation popup. Only accept
      // non-interactive decoration from this exact toggle cell; never click
      // through a dialog or a different control, even if it shares the cell.
      const overlay = hit && hit.closest('[role="dialog"],[role="alertdialog"],[aria-modal="true"],[role="menu"],[role="tooltip"]');
      const control = hit && hit.closest('button,a,input,select,textarea,[role="button"],[role="switch"],[role="checkbox"],[role="menuitem"],[contenteditable="true"]');
      const own = hit && !overlay && (
        hit === sw || sw.contains(hit) ||
        (cell && cell.contains(hit) && (!control || control === sw || control.contains(sw)))
      );
      if (!own) {
        const d = hit ? (hit.tagName + (hit.getAttribute('role') ? '[' + hit.getAttribute('role') + ']' : '') + ' ' + norm(hit.innerText || '').slice(0, 40)) : 'nothing';
        return {found: false, why: 'switch is covered', covered_by: d, rect: [Math.round(r.x), Math.round(r.y)]};
      }
      sw.click();
      result.clicked = true;
    }
  }
  sw.setAttribute('data-acl-switch', '1');
  return result;
}
""".replace("/*ACL_CLEAN_NAME*/", _CLEAN_NAME_JS)

# Re-read the same visible period in the exact browser task that may click.
_SWITCH_ACTION_JS = """args => {
  const period = (""" + _FIND_PERIOD_JS + """)(args.period_args);
  if (!period || period !== args.period) return {found:false, why:'period changed before action'};
  return (""" + _FIND_SWITCH_JS + """)(args);
}"""


def _turn_off_lease_error(exc, account, level, object_id, *, expected_name="", requester="", **_kwargs):
    # No browser action started: this is a refusal, not an unconfirmed click.
    out = TurnOffResult(account=account, level=level, object_id=str(object_id),
                        name=expected_name, state="failed", error=str(exc), at=_now(),
                        requester=(requester or "").strip(), route="rpa")
    _record_turn_off(out)
    return out


@serialized_browser(key=_profile_key, on_lease_error=_turn_off_lease_error)
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
        out.error = f"only a campaign or an ad can be turned off, not {level!r}"
        return out
    if not expected_name.strip():
        out.error = "row name is required — read the account again"
        return out
    if not str(object_id).strip():
        out.error = ("This account shows the new Ads Manager table without campaign ids. "
                     "Turning off by name is not available yet; turn it off in Ads Manager.")
        return out
    if not str(object_id).isdigit():
        out.error = "object id must contain only digits"
        return out
    if level == "ad" and not str(parent_id).isdigit():
        # Список объявлений сужается по кампании: другого проверенного способа
        # добраться до одной строки у нас нет.
        out.error = "turning off an ad requires its campaign"
        return out

    entry = _entry_for(account)
    if entry is None:
        out.error = f"{account} is not in the account map"
        return out
    act = (entry.ad_account_id or "").strip()
    if not act:
        out.error = f"{account} has no ad_account_id in the account map"
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
            out.error = "Ads Manager did not finish loading in time"
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
        # «Занятая» таблица — ждём, как в чтении дерева: 17.09 выключение на
        # Doc20 отказало «table is still loading» сразу после открытия списка.
        found = None
        for _attempt in range(TABLE_BUSY_TRIES):
            found = page.evaluate(_FIND_SWITCH_JS, args)
            if not (isinstance(found, dict) and found.get("why") == "table is still loading"):
                break
            page.wait_for_timeout(1000)
        if not found or not found.get("found"):
            out.error = (found or {}).get("why") or "row not found"
            return out
        if not context_matches():
            out.error = "context changed during lookup"
            return out
        out.name = str(found.get("name") or expected_name)

        # Имя сверяется, если дешборд его прислал: id мог устареть, а строка
        # под ним — оказаться чужой. Выключить не то хуже, чем не выключить.
        # Сравниваются имена без надписей Meta (ACL-1395): страница их уже
        # сняла, а чтение, сохранённое раньше, могло прислать имя с ними.
        if expected_name and out.name and out.name != clean_object_name(expected_name):
            out.error = (
                f"the page shows \"{out.name}\" under this id, but the dashboard showed "
                f"\"{expected_name}\"; nothing was touched"
            )
            return out

        # Запрет на опасные нажатия — тот же, что у мастера создания
        # (`acl-113c`). Тумблер подписан «On/off» и под него не попадает; смысл
        # в том, чтобы поймать случай, когда мы схватили не тот элемент.
        why = wg.forbidden_click_reason(found.get("label"))
        if why:
            out.error = f"this cannot be clicked: {why}"
            return out

        if found.get("off"):
            out.delivery = str(found.get("delivery") or "")
            out.state = "already_off"
            out.how = "the toggle is already off; nothing was clicked"
            return out
        if not found.get("on"):
            out.error = "the toggle state is unclear; nothing was touched"
            return out

        page.locator(SEL_MARKED_SWITCH).scroll_into_view_if_needed(timeout=15_000)
        if not context_matches():
            out.error = "context changed before action — nothing touched"
            return out
        period = _period_once(page)
        if not _period_matches(period, date_from, date_to):
            out.error = "period could not be confirmed before action"
            return out
        # Перекрытие бывает мгновенным: 17.09 Doc20 дважды «switch is covered»,
        # а через минуту на той же странице в центре тумблера лежал он сам —
        # строку у края накрывала липкая шапка таблицы. Строку — в центр
        # экрана, и ещё раз, не больше трёх.
        acted = None
        for _attempt in range(3):
            acted = page.evaluate(_SWITCH_ACTION_JS, dict(args, click=True, url=approved_url,
                label=found.get("label"), period=period,
                period_args=[list(SEL_PERIOD), SEL_PERIOD_FALLBACK, RE_PERIOD_DATE]))
            if not (isinstance(acted, dict) and acted.get("why") == "switch is covered"):
                break
            try:
                page.evaluate(
                    "sel => { const e = document.querySelector(sel); if (e) e.scrollIntoView({block: 'center', behavior: 'instant'}); }",
                    SEL_MARKED_SWITCH,
                )
            except Exception:
                pass
            page.wait_for_timeout(1000)
        if not acted or not acted.get("found"):
            why = (acted or {}).get("why") or "action refused"
            if (acted or {}).get("covered_by"):
                why = f"{why} by {acted['covered_by']}"
            out.error = why
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
            out.error = "after reload the row was not found; state unknown"
            return out
        out.error = "after reload the toggle is on again; Meta did not accept it"
        return out
    except Exception as exc:                      # noqa: BLE001
        out.error = f"failed: {exc}"
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
