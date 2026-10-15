# -*- coding: utf-8 -*-
"""Пострановая разбивка спенда, снятая с самого кабинета (acl-357).

Маршрут — Ads Reporting, решение владельца 2026-09-10 (issue #113):
сводная таблица с брейкдауном **Country** за период отвечает таблицей
«Страна | Amount spent» — точные пострановые числа без парсинга имён
кампаний и без брейкдауна внутри общей таблицы кампаний.

Как и всё чтение, это attach-only: цепляемся к уже открытому окну
профиля и никогда его не перезапускаем. Пустой отчёт — это «за период
ничего не крутилось», и об этом говорится вслух; ноль и «не читали» —
разные вещи.

Первая версия требует новую сводную при каждом чтении и сверяет итог
с полным числом отображённых строк. Неподтверждённая полнота — отказ. Сохранённый
view на аккаунте («один раз настроили — дальше открываем») — возможное
ускорение потом, когда живые прогоны покажут, что дороже: клики по
панели Customize или лишняя сущность на кабинете.

Селекторные шаги написаны по скриншотам владельца от 2026-09-10 и до
первого живого прогона остаются догадкой о вёрстке Meta — ровно как
список фраз в acl-124. После подключения отказ сопровождается попыткой
сохранить снимок экрана; до подключения доступен только текст ошибки.
"""
from __future__ import annotations

import json
import math
import re
import threading
from datetime import date, timedelta
from decimal import Decimal
from dataclasses import dataclass, field, asdict
from pathlib import Path

from config import paths
from src.fb_ads_rpa.operations import serialized_browser
from src.analysis_agent.account_spend import (
    _act_from_url,
    _profile_key,
    ProfileNotAttachable,
    _attach_only,
    _entry_for,
    _period_matches,
    _read_status,
    _shoot,
    _sleep,
    LOGGED_OUT_CODE,
    _ReadOpen,
    _finish_started,
    _logged_out,
    logged_out_message,
    opening_closed_profile,
)
from src.fb_ads_rpa import profile_trouble as _profile_trouble

#: Экран отчётов Ads Manager. `?act=` обязателен — без него Reporting
#: откроет «какой-нибудь» кабинет, а чтение чужого кабинета — отказ.
REPORTING = "https://adsmanager.facebook.com/adsmanager/reporting"

REPORT_TIMEOUT_MS = 60_000

#: Валютные коды и знаки, которые Reporting пишет рядом с суммой.
#: Знак доллара нарочно НЕ превращается в "USD": $ пишут и за AUD, и за
#: MXN, и угаданная валюта хуже пустой — пустая честно говорит «смотри
#: на кабинет», угаданная выглядит как знание.
_CURRENCY_SIGNS = {"€": "EUR", "£": "GBP", "₪": "ILS"}


@dataclass
class CountrySplit:
    """Metadata follows SpendReading; per-country rows replace its single sum."""

    account: str
    date_from: str
    date_to: str
    source: str = "rpa-reporting"
    state: str = "failed"  # ok | empty | failed
    rows: list = field(default_factory=list)  # [{country, spend, currency}]
    status: str = ""
    status_text: str = ""
    how: str = ""
    #: Как мы попали на страницу, с которой можно создать отчёт. Reporting
    #: подставляет последний открытый отчёт, и путь туда — часть ответа на
    #: «что мы вообще читали»: в отчёте байера могут висеть чужие фильтры.
    entered: str = ""
    error: str = ""
    #: ACL-1518: устойчивое имя отказа (`open_by_hand`) — строка карты пишет
    #: простой текст вместо «countries failed».
    error_code: str = ""
    shot: str = ""
    #: Когда чтение началось и когда закончилось (ACL-1282).
    started_at: str = ""
    finished_at: str = ""
    #: Какой спенд эта разбивка объясняет: время того показания и сами числа.
    #: Первая версия ACL-1282 сравнивала часы — «разбивка старше спенда» — и
    #: не успокаивалась никогда: нажатие без разбивки всё равно обновляло
    #: время спенда, и следующее нажатие объявляло разбивку устаревшей.
    #: Отчёт в кабинете строился через раз на ровном месте (ревью 23.09,
    #: замер: 3 отчёта на 6 нажатий). Устаревание — это про числа: пока
    #: сумма та же, ответ «куда она ушла» тот же, сколько бы раз спенд ни
    #: перечитывали. Записи, снятые до ACL-1282, спенда не помнят — они
    #: считаются устаревшими, а не свежими, и перечитываются один раз.
    spend_at: str = ""
    spend: float | None = None
    spend_currency: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ── Хранилище: своя копилка, тот же принцип, что у показаний спенда ────
_LOCK = threading.RLock()


def _now() -> str:
    """То же время и тем же видом, что у показаний спенда.

    Один формат — условие сравнения: ACL-1282 спрашивает, не старше ли
    разбивка спенда за те же даты, и сравнивает эти строки как строки.
    """
    from datetime import datetime

    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _spend_now(account: str, date_from: str, date_to: str) -> dict:
    """Показание спенда за эти даты — временем и числами, без падений."""
    try:
        from src.analysis_agent import account_spend

        reading = account_spend.get_reading(account, date_from, date_to)
        if getattr(reading, "state", "") != "ok":
            return {}
        return {"spend_at": reading.finished_at or "",
                "spend": reading.spend,
                "spend_currency": reading.currency or ""}
    except Exception:  # pragma: no cover - запись не должна ронять чтение
        return {}


def _as_split(stored: dict) -> "CountrySplit":
    """Запись с диска — в объект, не спотыкаясь о чужие поля.

    В хранилище лежат записи, снятые прежними версиями: у них нет новых
    полей, а когда-нибудь может не остаться старого. `CountrySplit(**stored)`
    падает и на том, и на другом, а падать здесь — значит потерять чтение,
    которое уже состоялось.
    """
    known = {f for f in CountrySplit.__dataclass_fields__}
    return CountrySplit(**{k: v for k, v in stored.items() if k in known})


def _store_path() -> Path:
    return Path(paths.get_local_config_dir()) / "country_splits.json"


def _key(account: str, date_from: str, date_to: str) -> str:
    return f"{account}|{date_from}|{date_to}"


def save_split(split: "CountrySplit") -> None:
    with _LOCK:
        path = _store_path()
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(data, dict):
            raise ValueError("Country readings store is not an object; original file preserved")
        data[_key(split.account, split.date_from, split.date_to)] = split.to_dict()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1, allow_nan=False), encoding="utf-8")
        tmp.replace(path)


#: Чтения, которые идут прямо сейчас. Ключ тот же, что у хранилища.
_RUNNING: set[str] = set()


def running_now() -> list[str]:
    """Какие аккаунты читаются в эту минуту — для экрана, а не для решений.

    Имена аккаунтов, а не ключи хранилища: тем же, чем отвечает
    `account_spend.running_now`, иначе на экране рядом окажутся две разные
    вещи под одним словом.
    """
    with _LOCK:
        return sorted({k.split("|")[0] for k in _RUNNING})


def default_reader():
    """Чем читать страны: `insights?breakdowns=country` (ACL-1760) или отчёт Ads Manager."""
    from src.analysis_agent import account_spend_api

    return account_spend_api.read_country_split_via_api if account_spend_api.api_enabled() else read_country_split


def start_split(
    account: str,
    date_from: str,
    date_to: str = "",
    *,
    reader=None,
) -> "CountrySplit":
    """Поставить пострановое чтение в работу и сразу вернуть его состояние.

    Тем же порядком, что и чтение спенда: профиль отвечает десятки секунд, и
    держать всё это время страницу — значит показать человеку зависший
    дешборд вместо ответа.
    """
    date_to = date_to or date_from
    key = _key(account, date_from, date_to)
    with _LOCK:
        if key in _RUNNING:
            stored = get_split(account, date_from, date_to)
            return _as_split(stored) if stored else CountrySplit(
                account=account, date_from=date_from, date_to=date_to, state="running")
        # Спенд, который эта разбивка объясняет, снимается в начале чтения:
        # к концу его уже могли перечитать, и записанное «объясняю вот это»
        # относилось бы к числу, которого мы не видели.
        spend = _spend_now(account, date_from, date_to)
        started = CountrySplit(account=account, date_from=date_from,
                               date_to=date_to, state="running",
                               started_at=_now(), **spend)
        _RUNNING.add(key)
        try:
            save_split(started)
        except Exception:
            _RUNNING.discard(key)
            raise

    fn = reader or default_reader()

    def run() -> None:
        try:
            done = fn(account, date_from, date_to)
        except Exception as exc:  # падение чтения не должно ронять сервер
            done = CountrySplit(account=account, date_from=date_from, date_to=date_to,
                                state="failed", error=f"{type(exc).__name__}: {exc}")
        done.started_at = started.started_at
        done.finished_at = _now()
        done.spend_at = started.spend_at
        done.spend = started.spend
        done.spend_currency = started.spend_currency
        with _LOCK:
            try:
                save_split(done)
            except Exception:
                # Результат потерян, но ронять поток нечем и некуда. Важно, что
                # после этого на диске остаётся «running» от начала чтения —
                # его разбирает `_honest`, иначе экран ждал бы вечно.
                pass
            finally:
                _RUNNING.discard(key)

    try:
        threading.Thread(target=run, name=f"split:{account}", daemon=True).start()
    except Exception:
        with _LOCK:
            _RUNNING.discard(key)
        raise
    return started


def all_splits(date_from: str, date_to: str = "") -> dict[str, dict]:
    """Все сохранённые пострановые чтения за период: аккаунт → чтение."""
    date_to = date_to or date_from
    with _LOCK:
        path = _store_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except Exception:
            return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, dict] = {}
    for value in data.values():
        if not isinstance(value, dict):
            continue
        if value.get("date_from") != date_from or value.get("date_to") != date_to:
            continue
        account = str(value.get("account") or "")
        if account:
            # Тот же разбор осиротевших записей, что и в `get_split`: экран
            # берёт аккаунты отсюда, и вечное «читаю» попало бы прямо в него.
            with _LOCK:
                out[account] = _honest(_key(account, date_from, date_to), value)
    return out


def totals_by_country(date_from: str, date_to: str = "") -> list[dict]:
    """Сколько потрачено на каждую страну — по всем прочитанным аккаунтам.

    **Список строится из показаний, а не из карты аккаунтов.** Это и есть
    смысл задачи: деньги нередко уходят на страну, к которой аккаунт не
    привязан, и такая страна обязана появиться сама, а не как особый случай.
    `account_country` уже зафиксировал замером, что у аккаунта не одна
    страна: пять из четырнадцати стоят сразу под двумя.

    Валюты **не складываются между собой**: ключ — «страна и валюта», поэтому
    кабинет в шекелях и кабинет в долларах дадут по стране две строки, а не
    одну выдуманную сумму.

    Больше того: **поперёк аккаунтов складываем только код** — «KWD», «EUR».
    Знак валютой не является. Два кабинета, оба показывающие `$`, могут вести
    учёт в песо и в долларах, и сложенное число было бы бессмысленным, хотя и
    выглядело бы обычным. Такие строки остаются по одной на аккаунт, и это
    видно по полю `summed`.

    Считаются только удавшиеся чтения. Отказ — это «не знаем», и подмешивать
    его нулём значило бы показать неполную картину полной.
    """
    def _is_code(currency: str) -> bool:
        """Названа ли валюта кодом. Знак и пустота — не названа."""
        return len(currency) == 3 and currency.isalpha()

    buckets: dict[tuple, dict] = {}
    for account, split in sorted(all_splits(date_from, date_to).items()):
        if split.get("state") not in ("ok", "empty"):
            continue
        for row in split.get("rows") or []:
            country = str(row.get("country") or "").strip()
            if not country:
                continue
            currency = str(row.get("currency") or "")
            try:
                spend = Decimal(str(row.get("spend")))
            except Exception:
                continue
            summed = _is_code(currency)
            key = (country, currency) if summed else (country, currency, account)
            bucket = buckets.setdefault(
                key,
                {"country": country, "currency": currency, "spend": Decimal(0),
                 "accounts": [], "summed": summed},
            )
            bucket["spend"] += spend
            if account not in bucket["accounts"]:
                bucket["accounts"].append(account)
    out = [
        {**b, "spend": float(b["spend"])}
        for b in buckets.values()
    ]
    out.sort(key=lambda b: (b["country"], -b["spend"], b["accounts"][0] if b["accounts"] else ""))
    return out


#: Что показывать вместо «running», за которым никого нет.
_ORPHANED = "the reading stopped before it said anything"


def _honest(key: str, value: dict | None) -> dict | None:
    """Снять «running» с записи, которую не держит ни один живой читатель.

    Хранилище говорит «читаю» с того момента, как чтение началось, и до того,
    как результат записан. Между этими двумя записями есть два способа
    оборваться, и оба оставляли на экране вечное «читаю»:

    * запись результата не удалась (диск занят, файл перечитывают) — флаг в
      памяти снимался в `finally`, а на диске оставалось «running», и экран
      читает именно диск;
    * дешборд перезапустили посреди чтения — поток умер вместе с процессом,
      а запись пережила его.

    Правда здесь у памяти: `_RUNNING` знает, кого действительно читают
    **сейчас**. Если записи там нет, а на диске «running» — это осиротевшая
    запись, и честнее показать отказ, чем бесконечное ожидание. Человек
    нажмёт ещё раз; вечное «читаю» не даёт даже этого.
    """
    if not isinstance(value, dict) or value.get("state") != "running":
        return value
    if key in _RUNNING:
        return value
    orphan = dict(value)
    orphan["state"] = "failed"
    orphan["error"] = orphan.get("error") or _ORPHANED
    return orphan


def why_split_is_needed(
    *, countries: int, split: dict | None,
    spend_finished_at: str = "", spend: float | None = None, currency: str = "",
) -> str:
    """Зачем читать разбивку по странам сейчас — или пусто, если незачем.

    Правило владельца (ACL-1282). До этой правки разбивка читалась отдельной
    кнопкой, то есть ровно тогда, когда человек её нажал. Кнопка теперь одна,
    и цену чтения — отчёт, созданный внутри кабинета, — нельзя платить каждый
    раз, когда нужна одна цифра спенда.

    Разбивка отвечает на вопрос «куда ушли вот эти деньги». Значит она
    устаревает вместе с числом, которое объясняет, а не сама по себе:

    * разбивки за эти даты нет, она не удалась или ещё идёт;
    * разбивка не помнит, какой спенд объясняла (записи до ACL-1282);
    * спенд за эти даты изменился с тех пор — деньги куда-то двинулись.

    Сумма та же — ответ тот же, и перечитывать нечего, сколько бы раз спенд
    ни снимали заново. Первая версия сравнивала время («разбивка старше
    спенда») и строила отчёт через раз: чтение спенда само обновляло время,
    с которым сравнивали. Это ровно та цена, ради которой задача и затевалась.

    Число стран решает, что делать, когда сравнить не с чем. Аккаунт в одной
    стране без прочитанного спенда подождёт; у аккаунта в нескольких странах
    общая сумма не говорит ничего, и разбивку берут сразу.

    Возвращает причину словами (её показывают человеку) или пустую строку.
    Причина — не украшение: «ничего не произошло» и «мы решили не читать»
    выглядят на экране одинаково, и отличить их можно только текстом.
    """
    state = (split or {}).get("state")
    if state not in ("ok", "empty"):
        return "no country split for these dates"
    if not str((split or {}).get("spend_at") or ""):
        return "the stored country split does not say which spend it explains"
    if not spend_finished_at:
        # Спенда за эти даты нет (или он не удался): сравнивать не с чем.
        return ("more than one country is linked to this account"
                if countries > 1 else "")
    same = (_same_money((split or {}).get("spend"), spend)
            and str((split or {}).get("spend_currency") or "").strip().upper()
            == str(currency or "").strip().upper())
    if not same:
        return "the spend has changed since the country split was read"
    return ""


def _same_money(a, b) -> bool:
    """Две суммы — одно и то же число? Пустое равно только пустому."""
    if a is None or b is None:
        return a is None and b is None
    try:
        return abs(float(a) - float(b)) < 0.005
    except (TypeError, ValueError):
        return False


def get_split(account: str, date_from: str, date_to: str) -> dict | None:
    key = _key(account, date_from, date_to or date_from)
    with _LOCK:
        path = _store_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        value = data.get(key)
        return _honest(key, value) if isinstance(value, dict) else None


# ── Разбор ячеек: чистый Python, тестируется без браузера ──────────────

def money_and_currency(text: str) -> tuple[float | None, str]:
    """Parse a complete English-locale amount cell, never a matching substring.

    Keep signs and explicit currency codes; '$' alone stays unknown. Unsupported
    locale/precision is a refusal, not an invitation to guess the amount.
    """
    flat = " ".join((text or "").split()).replace("−", "-")
    number = r"(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]{1,3}(?: [0-9]{3})+|[0-9]+)(?:\.[0-9]{1,3})?"
    match = re.fullmatch(
        rf"(?P<sign1>[+-]?)\s*(?P<prefix>[A-Z]{{3}}|[$€£₪])?\s*"
        rf"(?P<sign2>[+-]?)\s*(?P<number>{number})\s*(?P<suffix>[A-Z]{{3}}|[$€£₪])?", flat
    )
    if not match or (match['sign1'] and match['sign2']):
        return None, ""
    # Передаём то, что написала Meta, и ничего не переводим: `$` остаётся
    # `$`, `₪` остаётся `₪`. Прежде знак превращали в код, а `$` отбрасывали
    # как неоднозначный — решение владельца 15.09: показывать знак кабинета
    # как есть. Это снимает и выдуманное знание («$» = USD), и охоту за
    # валютой кабинета по страницам Ads Manager.
    #
    # Таблица знаков осталась, но только для сверки: «€12.00 EUR» — одна
    # валюта, записанная дважды, а «€1.00 GBP» — противоречие и отказ.
    tokens = [t for t in (match['prefix'], match['suffix']) if t]
    if len({_CURRENCY_SIGNS.get(t, t) for t in tokens}) > 1:
        return None, ""
    value = float(match['number'].replace(",", "").replace(" ", ""))
    if not math.isfinite(value):
        return None, ""
    if (match['sign1'] or match['sign2']) == "-":
        value = -value
    # Если Meta написала и знак, и код («€12.00 EUR»), берём код: он точнее,
    # и он тоже написан ею, а не выдуман нами.
    written = next((t for t in tokens if len(t) == 3), "")
    return value, written or (tokens[0] if tokens else "")


_EMPTY_MARKERS = {"no data", "no results", "no data available", "no results found"}
#: Итоговая строка и, если Meta её написала, сколько строк показано.
#:
#: Пробелы вокруг дроби и единственное число — не придирка: живьём 15.09
#: Reporting написал «Total results 1 / 1 row displayed», и прежний шаблон,
#: ждавший «1/1 rows displayed», счёт не разобрал бы. Счёт нужен для проверки
#: полноты, так что промах здесь стоил бы отказа на исправном отчёте.
_TOTAL_LABEL = re.compile(
    r"(?:total(?: results)?|all)(?:\s+([0-9]+)\s*/\s*([0-9]+) rows? displayed)?", re.I)
#: Колонки, которые не являются разрезом данных и потому строк не множат.
#:
#: Список остаётся **белым**: неизвестная колонка — отказ, потому что лишний
#: разрез (скажем, «Age») дал бы по стране несколько строк, и каждая
#: выглядела бы правильной. Пополняется только тем, что видели живьём.
#:
#: 15.09, первый прогон до таблицы: со снятыми уровнями и включённой
#: «Country» Meta всё равно оставляет «Delivery», «Attribution setting» и
#: «Schedule». Строк при этом ровно столько, сколько стран, и итог сходится —
#: настоящая защита от неполноты не здесь, а в сверке с итоговой строкой.
_METRIC_COLUMNS = {
    "amount spent", "reach", "impressions", "cost per result", "results",
    "frequency", "delivery", "attribution setting", "schedule",
}


def _parse_cells(matrix: list[list[str]]) -> tuple[list[dict], dict | None, str]:
    """Reject incomplete rows instead of silently dropping countries."""
    headers = []
    for i, row in enumerate(matrix):
        names = [" ".join(c.lower().split()) for c in row]
        if "country" in names and "amount spent" in names:
            headers.append((i, names))
    if len(headers) != 1:
        return [], None, "refused: unique Country and Amount spent header not found"
    header_i, names = headers[0]
    if names.count("country") != 1 or names.count("amount spent") != 1:
        return [], None, "refused: ambiguous report columns"
    # An unrecognized dimension can still yield unique country names (e.g. only
    # one age bucket happened to deliver). Duplicate detection alone is unsafe.
    if any(n != "country" and n not in _METRIC_COLUMNS for n in names):
        return [], None, "refused: additional breakdown or unsupported report column"
    country_col, amount_col = names.index("country"), names.index("amount spent")
    rows, seen, total, empty = [], set(), None, False
    for cells in matrix[header_i + 1:]:
        cells = [" ".join(c.split()) for c in cells]
        if not any(cells):
            continue
        if " ".join(c for c in cells if c).lower() in _EMPTY_MARKERS:
            empty = True
            continue
        if len(cells) != len(names):
            return [], None, "refused: incomplete report row"
        country = cells[country_col]
        amount, currency = money_and_currency(cells[amount_col])
        if not country or amount is None:
            return [], None, "refused: unreadable country or amount"
        total_label = _TOTAL_LABEL.fullmatch(country)
        if total_label:
            if total is not None:
                return [], None, "refused: multiple totals"
            total = {"spend": amount, "currency": currency, "count": None}
            if total_label[1] is not None:
                displayed, count = int(total_label[1]), int(total_label[2])
                if displayed != count:
                    return [], None, "refused: only some report rows are displayed"
                total["count"] = count
            continue
        if country.lower() in seen:
            return [], None, "refused: duplicate country or unresolved breakdown"
        seen.add(country.lower())
        rows.append({"country": country, "spend": amount, "currency": currency})
    if empty:
        if rows or (total and (total["spend"] != 0 or total["count"] not in (None, 0))):
            return [], None, "refused: no-data marker conflicts with report rows"
        return [], total, "empty: explicit no-data marker"
    if not rows:
        return [], total, "refused: report has no readable country rows yet"
    return rows, total, f"pivot rows from cells, header row {header_i}, amount col {amount_col}"


def rows_from_cells(matrix: list[list[str]]) -> tuple[list[dict], str]:
    """Parse rows; this alone does not establish report completeness."""
    rows, _, how = _parse_cells(matrix)
    return rows, how


def _verified_rows(matrix: list[list[str]]) -> tuple[list[dict], str]:
    rows, total, how = _parse_cells(matrix)
    if how.startswith(("refused:", "empty:")):
        return rows, how
    # Reporting virtualizes rows. A matching total alone can hide omitted zero
    # rows or cancelling adjustments. Require the displayed/full row count too.
    if not total or total["count"] != len(rows):
        return [], "refused: complete country row count and total are not confirmed"
    if any(row["currency"] != total["currency"] for row in rows):
        return [], "refused: currencies differ between country rows and total"
    summed = sum((Decimal(str(row["spend"])) for row in rows), Decimal(0))
    if summed != Decimal(str(total["spend"])):
        return [], "refused: country rows do not reconcile with report total"
    return rows, how + "; complete row count and total reconciled"


# ── Браузерная часть ───────────────────────────────────────────────────

# Only one visible Country report may supply the matrix, ARIA or native HTML.
_VISIBLE_JS = """
const visible = el => {
  const style = getComputedStyle(el), box = el.getBoundingClientRect();
  return !!(box.width && box.height) && style.visibility === 'visible'
    && !el.closest('[hidden],[aria-hidden="true"]');
};
const norm = text => (text || '').trim().replace(/\\s+/g, ' ').toLowerCase();
"""

_GRAB_CELLS_JS = "() => {" + _VISIBLE_JS + """
  const rootSelector = 'table,[role="table"],[role="grid"],[role="treegrid"]';
  const candidates = [...document.querySelectorAll(rootSelector)].filter(visible).map(root => {
    const rows = [...root.querySelectorAll('tr,[role="row"]')]
      .filter(row => row.closest(rootSelector) === root && visible(row))
      .map(row => [...row.querySelectorAll('th,td,[role="cell"],[role="gridcell"],[role="columnheader"],[role="rowheader"]')]
        .filter(cell => cell.closest('tr,[role="row"]') === row && visible(cell))
        .map(cell => (cell.innerText || '').trim()));
    return {root, rows};
  }).filter(({rows}) => rows.some(row => row.map(norm).includes('country') && row.map(norm).includes('amount spent')));
  if (candidates.length !== 1) return {matrix: [], error: 'unique visible Country report not found'};
  let {root, rows} = candidates[0];

  // Разметку имеет только заголовок. Строки с данными — обычные `div`
  // (живое наблюдение 15.09: «Chile» лежит в span < div < div < div, без
  // role="row" и role="cell", и таких строк на странице ровно одна — сам
  // заголовок). Поэтому, если по разметке пришёл только заголовок, читаем
  // по расположению: колонки берём у заголовка, а строки — по якорям
  // первой колонки, к которым притягивается всё, что ниже и правее.
  if (rows.length === 1 && rows[0].length > 1) {
    const header = [...root.querySelectorAll('tr,[role="row"]')].filter(visible)[0];
    const heads = [...header.querySelectorAll('th,[role="columnheader"],[role="cell"],[role="gridcell"]')]
      .filter(visible);
    const bands = heads.map(h => { const r = h.getBoundingClientRect(); return {l: r.left, r: r.right}; });
    const headerBottom = header.getBoundingClientRect().bottom;
    const leaves = [...root.querySelectorAll('*')].filter(e =>
      visible(e) && e.children.length === 0 && (e.innerText || '').trim()
      && !e.closest('button,[role="button"]')
      && e.getBoundingClientRect().top >= headerBottom - 1);
    const assigned = leaves.map(e => {
      const r = e.getBoundingClientRect();
      const mid = (r.left + r.right) / 2;
      let col = bands.findIndex(b => mid >= b.l && mid <= b.r);
      if (col < 0) col = bands.findIndex(b => r.left < b.r && r.right > b.l);
      if (col < 0) {
        // Ни одна полоса не накрыла: числа выровнены по правому краю и
        // попадают в зазор между колонками заголовка. 15.09 так выпала вся
        // правая часть итоговой строки, включая сам итог «$44.17» — а без
        // него сверка полноты невозможна. Берём ближайшую колонку.
        let best = -1, bestGap = Infinity;
        bands.forEach((b, i) => {
          const gap = mid < b.l ? b.l - mid : (mid > b.r ? mid - b.r : 0);
          if (gap < bestGap) { bestGap = gap; best = i; }
        });
        col = best;
      }
      return {
        text: (e.innerText || '').trim(), top: r.top, col: col,
        size: parseFloat(getComputedStyle(e).fontSize) || 0,
      };
    });
    // Мелкие подписи под значениями («Total spent» под суммой, «All
    // conversions» под настройкой) — пояснения, а не данные: с ними
    // «$44.17 Total spent» не разбирается как деньги. В первой колонке
    // подпись, наоборот, нужна: в ней Meta пишет счёт строк.
    const main = Math.max(...assigned.map(x => x.size), 0);
    const placed = assigned.filter(x =>
      x.col === 0 || x.size >= main - 0.5).filter(x => x.col >= 0);
    // Что не легло ни в одну колонку. Без этого «пустая ячейка» в отчёте
    // неотличима от «надпись выпала при разборе», а разница решающая.
    window.__aclDropped = assigned.filter(x => x.col < 0).map(x => x.text).slice(0, 12);
    // Якоря строк — ячейки первой колонки, а не отдельные надписи в них.
    // Итоговая ячейка состоит из двух строк («Total results» и «1 / 1 row
    // displayed»), и по надписям она разъезжалась на две строки таблицы.
    // Поэтому от надписи поднимаемся, пока предок не выходит за колонку.
    const cellOf = (el) => {
      let node = el;
      for (let i = 0; i < 5 && node.parentElement; i++) {
        const p = node.parentElement.getBoundingClientRect();
        if (p.left < bands[0].l - 2 || p.right > bands[0].r + 2) break;
        node = node.parentElement;
      }
      return node;
    };
    // Подпись под значением («1 / 1 row displayed» под «Total results»)
    // мельче основного текста — по этому её и отличаем. Иначе она заводит
    // собственную строку, и итог разъезжается надвое.
    const firstCol = leaves.filter(e => {
      const r = e.getBoundingClientRect();
      const mid = (r.left + r.right) / 2;
      return mid >= bands[0].l && mid <= bands[0].r;
    }).map(e => ({
      top: e.getBoundingClientRect().top,
      size: parseFloat(getComputedStyle(e).fontSize) || 0,
    })).sort((a, b) => a.top - b.top);
    const anchors = [];
    for (const item of firstCol) {
      const prev = anchors[anchors.length - 1];
      if (prev && item.size < prev.size - 0.5) continue;  // подпись, не строка
      anchors.push(item);
    }
    if (anchors.length) {
      const built = anchors.map(() => bands.map(() => ''));
      for (const item of placed) {
        let idx = 0;
        for (let i = 0; i < anchors.length; i++) if (item.top >= anchors[i].top - 2) idx = i;
        built[idx][item.col] = built[idx][item.col]
          ? built[idx][item.col] + ' ' + item.text : item.text;
      }
      rows = [heads.map(h => (h.innerText || '').trim())].concat(built);
    }
  }
  const busy = root.closest('[aria-busy="true"]') ||
    [...root.querySelectorAll('[aria-busy="true"],[role="progressbar"]')].some(visible);
  return {matrix: rows, error: busy ? 'report is still loading' : ''};
}
"""

#: Клик по подписи: найти видимый элемент, чей текст по словам совпадает с
#: искомым, и кликнуть ближайший кликабельный. Подписи сравниваются по
#: словам — Meta переносит их как хочет.
_CLICK_LABEL_JS = "(args) => {" + _VISIBLE_JS + """
  const wanted = typeof args === 'string' ? args : args.label;
  const allowMany = typeof args === 'object' && !!args.allowMany;
  let matches = [...document.querySelectorAll('a,button,[role="button"],[role="menuitem"]')]
    .filter(el => visible(el) && norm(el.innerText) === norm(wanted));
  if (!matches.length) return false;
  if (matches.length > 1) {
    if (!allowMany) return false;
    // Из одинаково подписанных берём самый внутренний: обёртка с тем же
    // текстом не несёт обработчика, и клик по ней никуда не долетит.
    matches = matches.sort((a, b) => a.querySelectorAll('*').length - b.querySelectorAll('*').length);
  }
  const el = matches[0];
  if (el.disabled || el.getAttribute('aria-disabled') === 'true') return false;
  el.click();
  return true;
}
"""

# Resolve and operate on the same unique, visible checkbox. A matching span
# elsewhere on the page must never toggle an unrelated control.
_CHECKBOX_JS = "(args) => {" + _VISIBLE_JS + """
  const wanted = typeof args === 'string' ? args : args.label;
  const name = el => {
    if (el.getAttribute('aria-label')) return norm(el.getAttribute('aria-label'));
    const ids = (el.getAttribute('aria-labelledby') || '').split(/\\s+/).filter(Boolean);
    if (ids.length) return norm(ids.map(id => document.getElementById(id)?.innerText || '').join(' '));
    if (el.labels?.length) return norm([...el.labels].map(label => label.innerText).join(' '));
    if (norm(el.innerText)) return norm(el.innerText);
    return norm((el.closest('label') || el.parentElement)?.innerText);
  };
  const matches = [...document.querySelectorAll('[role="checkbox"],input[type="checkbox"]')]
    .filter(el => visible(el) && name(el) === norm(wanted));
  if (matches.length !== 1) return null;
  const el = matches[0], aria = el.getAttribute('aria-checked');
  if (el.disabled || el.getAttribute('aria-disabled') === 'true' || el.indeterminate) return null;
  const state = aria !== null ? (aria === 'true' ? true : aria === 'false' ? false : null)
    : typeof el.checked === 'boolean' ? el.checked : null;
  if (state !== null && typeof args === 'object' && state !== args.wanted) el.click();
  return state;
}
"""
_CHECKED_JS = _CHECKBOX_JS


#: Сколько подписанных кнопок видно. Признак того, что приложение
#: отрисовалось: сразу после перехода их одна («Ads Manager»), а не ноль, так
#: что ждать появления DOM бессмысленно — ждём появления интерфейса.
_MOUNTED_JS = "() => {" + _VISIBLE_JS + """
  return [...document.querySelectorAll('a,button,[role="button"]')]
    .filter(el => visible(el) && norm(el.innerText)).length;
}
"""

#: Открыт ли запрос «сохранить изменения». Уход из изменённого отчёта
#: перехватывается им, и без ответа страница никуда не уйдёт.
_UNSAVED_DIALOG_JS = "() => {" + _VISIBLE_JS + """
  return [...document.querySelectorAll('[role="dialog"]')].filter(visible)
    .some(d => norm(d.innerText).includes('unsaved changes'));
}
"""

#: Чем на странице задаётся период — для отказа, а не для клика. Параметр
#: `date` в адресе Meta выбрасывает (15.09: просили `2026-08-16_2026-09-16`,
#: приехали на адрес без него), так что период придётся задавать
#: интерфейсом. Пока этого шага нет, отказ должен хотя бы называть, чем его
#: задавать, а не только «период не тот».
_DATE_CONTROLS_JS = "() => {" + _VISIBLE_JS + """
  return [...document.querySelectorAll('button,[role="button"]')].filter(visible)
    .map(el => (el.innerText || '').trim())
    .filter(t => t && t.length < 60 && /\\d{4}|month|week|today|yesterday|lifetime|custom/i.test(t))
    .filter((t, i, all) => all.indexOf(t) === i).slice(0, 8);
}
"""


#: Выбор вида отчёта в диалоге «Create your report». Живой прогон 15.09:
#: «Pivot table» здесь не кнопка, а вариант с описанием во второй строке
#: («Cut and group data to find meaningful insights»), поэтому точное
#: совпадение подписи его не находит. Вариант уже выбран по умолчанию —
#: но полагаться на умолчание Meta нельзя, поэтому состояние проверяется.
_PICK_PIVOT_JS = "() => {" + _VISIBLE_JS + """
  const dlg = [...document.querySelectorAll('[role="dialog"]')].filter(visible)
    .find(d => norm(d.innerText).includes('create your report'));
  if (!dlg) return { ok: false, why: 'report type dialog not found' };
  const rows = [...dlg.querySelectorAll('div,li,label')].filter(visible)
    .filter(e => norm(e.innerText).startsWith('pivot table'))
    .sort((a, b) => a.querySelectorAll('*').length - b.querySelectorAll('*').length);
  if (!rows.length) return { ok: false, why: 'no "Pivot table" option in the dialog' };
  let radio = null, node = rows[0];
  for (let i = 0; i < 4 && node && node !== dlg && !radio; i++) {
    radio = node.querySelector('[role="radio"],input[type="radio"]');
    node = node.parentElement;
  }
  if (!radio) return { ok: false, why: 'the "Pivot table" option has no radio button' };
  const aria = radio.getAttribute('aria-checked');
  const on = aria !== null ? aria === 'true' : !!radio.checked;
  if (!on) radio.click();
  return { ok: true, already: on };
}
"""


#: Открыта ли панель настройки сводной. Признак — её собственные разделы,
#: а не положение кнопки: «Customize» переключатель, и на свежем отчёте
#: панель уже открыта. Живой прогон 15.09: клик по ней панель **закрыл**, и
#: галочки после этого искать было негде — ровно та же ошибка, что клик по
#: «Show more settings» на уже раскрытом разделе площадок.
_CUSTOMIZE_OPEN_JS = "() => {" + _VISIBLE_JS + """
  return [...document.querySelectorAll('*')].filter(visible)
    .some(e => e.children.length === 0 && norm(e.innerText) === 'popular breakdowns');
}
"""


def _ensure_customize_open(page, tries: int = 6) -> bool:
    """Открыть панель настройки сводной, если она закрыта, и убедиться.

    Успехом считается не нажатие, а открытая панель: у переключателя клик и
    результат — разные вещи, и путать их мы уже научены.
    """
    for attempt in range(tries):
        try:
            if bool(page.evaluate(_CUSTOMIZE_OPEN_JS)):
                return True
        except Exception:
            pass
        if attempt == 0 or attempt % 2 == 1:
            _click_label(page, "Customize", tries=2)
        _sleep(1.5)
    try:
        return bool(page.evaluate(_CUSTOMIZE_OPEN_JS))
    except Exception:
        return False


#: Галочка внутри раздела панели, а не по всей панели.
#:
#: Живое наблюдение 15.09: «Campaign name» в панели **две** — в «Popular
#: breakdowns» и в «Level», — и строгое правило единственности честно
#: отказывало («could not verify breakdown»). Разделять их по разделу
#: надёжнее, чем разрешать двойников: это разные настройки, и нужная нам —
#: та, что в своём разделе.
#:
#: Раздел определяется по вертикали: заголовок и всё, что ниже него до
#: следующего заголовка. Вложенности, по которой можно было бы спросить
#: «внутри какого блока», в разметке панели нет.
_SECTION_CHECKBOX_JS = "(args) => {" + _VISIBLE_JS + """
  const top = el => el.getBoundingClientRect().top + window.scrollY;
  // Искать заголовки по всей странице нельзя: «Delivery» — ещё и колонка
  // таблицы отчёта (живой прогон 15.09 отказал именно так). Но и «корень
  // панели» опорой не стал: подъём от «Popular breakdowns» давал контейнер,
  // в котором из разделов видно только его самого. Поэтому исключаем не всё
  // кроме панели, а именно таблицу — единственное подтверждённое место, где
  // те же слова значат другое.
  const inTable = el => !!el.closest(
    'table,[role="table"],[role="grid"],[role="treegrid"],[role="columnheader"],th');
  const leaves = [...document.querySelectorAll('*')]
    .filter(e => visible(e) && e.children.length === 0 && !inTable(e));
  const heads = leaves.filter(e => norm(e.textContent) === norm(args.section));
  if (heads.length !== 1) {
    // Не «не нашли», а «что вместо»: разделы, которые в корне всё-таки есть.
    // Без этого следующий разбор снова начнётся с живого прогона.
    const seen = [...new Set(leaves.map(e => norm(e.textContent))
      .filter(t => args.sections.some(s => norm(s) === t)))];
    return { ok: false, why: 'section "' + args.section + '" found ' + heads.length
      + ' time(s); the panel shows: ' + (seen.join(', ') || 'no sections') };
  }
  const start = top(heads[0]);
  const others = leaves.filter(e => args.sections.some(s => norm(e.textContent) === norm(s)))
    .map(top).filter(y => y > start);
  const end = others.length ? Math.min(...others) : Infinity;
  const name = el => {
    if (el.getAttribute('aria-label')) return norm(el.getAttribute('aria-label'));
    if (el.labels && el.labels.length) return norm([...el.labels].map(l => l.innerText).join(' '));
    const near = el.closest('label') || el.parentElement;
    return norm(near ? near.innerText : '');
  };
  const boxes = [...document.querySelectorAll('[role="checkbox"],input[type="checkbox"]')]
    .filter(el => visible(el) && top(el) >= start && top(el) < end && name(el) === norm(args.label));
  if (boxes.length !== 1) return { ok: false, why: 'found ' + boxes.length + ' checkboxes in the section' };
  const el = boxes[0];
  if (el.disabled || el.getAttribute('aria-disabled') === 'true' || el.indeterminate)
    return { ok: false, why: 'the checkbox is disabled' };
  const aria = el.getAttribute('aria-checked');
  const state = aria !== null ? aria === 'true' : !!el.checked;
  if (state !== args.wanted) el.click();
  return { ok: true, was: state };
}
"""

#: Заголовки разделов панели — границы, по которым режутся полосы.
#: Перечислены целиком, иначе конец раздела определится не там.
_PANEL_SECTIONS = (
    "Popular breakdowns", "Custom breakdowns", "Level", "Time", "Delivery",
    "Action", "Settings", "Dynamic creative asset", "Value rules breakdowns",
    "Metrics",
)


def _set_section_checkbox(page, section: str, label: str, wanted: bool) -> tuple[bool, str]:
    """Привести галочку раздела к нужному состоянию и убедиться в этом.

    Состояние перечитывается после клика: у переключателя нажатие и
    результат — разные вещи, и на этом мы уже обжигались дважды за сутки.
    """
    why = "not tried"
    for _ in range(6):
        try:
            res = page.evaluate(
                _SECTION_CHECKBOX_JS,
                {"section": section, "label": label, "wanted": wanted,
                 "sections": list(_PANEL_SECTIONS)},
            ) or {}
        except Exception as exc:
            res, why = {}, str(exc)[:80]
        if res.get("ok"):
            if res.get("was") == wanted:
                return True, "already set"
            _sleep(1)
            try:
                again = page.evaluate(
                    _SECTION_CHECKBOX_JS,
                    {"section": section, "label": label, "wanted": wanted,
                     "sections": list(_PANEL_SECTIONS)},
                ) or {}
            except Exception:
                again = {}
            if again.get("ok") and again.get("was") == wanted:
                return True, "switched"
            why = "the click did not stick"
        else:
            why = str(res.get("why") or why)
        _sleep(1)
    return False, why


#: Месяцы так, как их пишет Reporting: «Sep 1, 2026». Список задан явно, а не
#: взят у `strftime`: тот зависит от локали машины, и на компьютере байера с
#: другой локалью подставил бы «сен» — поле молча не совпало бы.
_META_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _meta_date(iso: str) -> str:
    """`2026-08-16` → `Aug 16, 2026` — как период показан на кнопке."""
    d = date.fromisoformat(iso)
    return f"{_META_MONTHS[d.month - 1]} {d.day}, {d.year}"


def _typed_date(iso: str) -> str:
    """`2026-08-16` → `8/16/2026` — как поле периода принимает ввод.

    Показывает поле одно («Sep 1, 2026»), а принимает другое: живой прогон
    15.09 — ввели «Sep 15, 2026», поле переписало это в `9/15/2026`, а
    соседнее, получившее «Aug 16, 2026», откатилось к прежнему значению.
    Значит словесный месяц им не годится.
    """
    d = date.fromisoformat(iso)
    return f"{d.month}/{d.day}/{d.year}"


def _same_day(text: str, iso: str) -> bool:
    """Одну ли дату называют строка поля и наш ISO — в любом из двух видов."""
    want = date.fromisoformat(iso)
    flat = " ".join((text or "").split())
    slash = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", flat)
    if slash:
        m, d, y = (int(x) for x in slash.groups())
        return (y, m, d) == (want.year, want.month, want.day)
    worded = re.fullmatch(r"([A-Z][a-z]{2})[a-z]* (\d{1,2}), (\d{4})", flat)
    if worded and worded.group(1) in _META_MONTHS:
        return (int(worded.group(3)), _META_MONTHS.index(worded.group(1)) + 1,
                int(worded.group(2))) == (want.year, want.month, want.day)
    return False


#: Пометить два поля периода, чтобы заполнить их настоящим вводом.
#:
#: Живое наблюдение 15.09: выбор периода — не всплывающее окно, а встроенная
#: панель с пресетами, двумя календарями и **двумя текстовыми полями**
#: («Sep 1, 2026» / «Sep 15, 2026») и кнопкой «Update». Ходить по числам
#: календаря не нужно.
#:
#: Поля без подписей и без placeholder, узнаются только по виду значения.
#: Значение присваивается не здесь: React не заметит записи в `value` — поля
#: заполняются `page.fill`, которому нужна лишь опора для поиска.
_MARK_DATE_FIELDS_JS = "() => {" + _VISIBLE_JS + """
  const looksLikeDate = v => /^[A-Z][a-z]{2} \\d{1,2}, \\d{4}$/.test((v || '').trim());
  const fields = [...document.querySelectorAll('input')]
    .filter(el => visible(el) && looksLikeDate(el.value));
  document.querySelectorAll('[data-acl-date]').forEach(el => el.removeAttribute('data-acl-date'));
  if (fields.length !== 2) return fields.length;
  fields[0].setAttribute('data-acl-date', 'from');
  fields[1].setAttribute('data-acl-date', 'to');
  return 2;
}
"""


def _set_period(page, date_from: str, date_to: str) -> tuple[bool, str]:
    """Выставить период в интерфейсе и подтвердить его кнопкой «Update».

    Через адрес period задать нельзя: параметр `date` Meta выбрасывает
    (живой прогон 15.09 — просили один период, на экране был её умолчательный
    «This month»). Здесь — единственный работающий путь.
    """
    control = ""
    try:
        for text in page.evaluate(_DATE_CONTROLS_JS) or []:
            if re.search(r"\b\d{4}\b", text) and "," in text:
                control = text
                break
    except Exception as exc:
        return False, f"could not look for the period button ({str(exc)[:60]})"
    if not control:
        return False, "period button not found"
    if not _click_label(page, control, tries=6, allow_many=True):
        return False, f"period button \"{control}\" did not respond"
    _sleep(2)

    found = 0
    for _ in range(6):
        try:
            found = int(page.evaluate(_MARK_DATE_FIELDS_JS) or 0)
        except Exception:
            found = 0
        if found == 2:
            break
        _sleep(1)
    if found != 2:
        return False, f"found {found} period fields, expected two"

    read = "() => [...document.querySelectorAll('[data-acl-date]')].map(e => (e.value || '').trim())"
    got: tuple = ()
    # Заполняем и перечитываем: ввод в одно поле сбивал соседнее, поэтому
    # успехом считается не сам ввод, а то, что в обоих полях осталась нужная
    # дата. Два захода — второй дозаполняет то, что откатилось.
    def _type_into(selector: str, text: str) -> None:
        """Набрать как человек: встать в поле, выделить всё, набрать, уйти.

        `fill` здесь не годится: у поля маска, и программная установка
        значения **дописывалась** к прежнему («Sep 1, 2026» + «8/16/2026» →
        `Sep 1, 202688`). Живой прогон 15.09.
        """
        page.click(selector)
        page.keyboard.press("Control+A")
        page.keyboard.press("Delete")
        page.keyboard.type(text, delay=40)
        page.keyboard.press("Tab")

    for _ in range(2):
        try:
            _type_into('[data-acl-date="from"]', _typed_date(date_from))
            _sleep(0.7)
            _type_into('[data-acl-date="to"]', _typed_date(date_to))
        except Exception as exc:
            return False, f"could not type into the period fields ({str(exc)[:60]})"
        _sleep(1)
        try:
            got = tuple(page.evaluate(read) or ())
        except Exception:
            got = ()
        if len(got) == 2 and _same_day(got[0], date_from) and _same_day(got[1], date_to):
            break
    else:
        return False, (f"the period fields show {list(got)}, "
                       f"but we typed {[_typed_date(date_from), _typed_date(date_to)]}")

    if not _click_label(page, "Update", tries=6):
        return False, "\"Update\" did not respond"
    _sleep(3)
    return True, f"{_meta_date(date_from)} — {_meta_date(date_to)}"


def _page_url(page) -> str:
    """Адрес страницы — как строка, чего бы нам ни подсунули.

    Адрес здесь подсказка, а не опора: по нему решается, оказались ли мы
    внутри отчёта. Падать из-за того, что у объекта страницы адреса нет,
    нельзя — чтение важнее подсказки.
    """
    try:
        return str(getattr(page, "url", "") or "")
    except Exception:
        return ""


def _wait_mounted(page, need: int = 6, tries: int = 22) -> int:
    """Дождаться, пока Reporting отрисует интерфейс.

    15.09, первый живой прогон: код ждал четыре секунды и начинал искать
    кнопки, а на странице в этот момент была ровно одна — «Ads Manager».
    Отказ выглядел как «кнопки нет», хотя приложение ещё не поднялось.
    Ждём по признаку, а не по секундам.
    """
    seen = 0
    for _ in range(tries):
        try:
            seen = int(page.evaluate(_MOUNTED_JS) or 0)
        except Exception:
            seen = 0
        if seen >= need:
            return seen
        _sleep(2)
    return seen


def _leave_open_report(page) -> tuple[bool, str]:
    """Выйти из открытого отчёта в список — там живёт «Create report».

    Переход по нашему адресу приводит не на список отчётов: Reporting сам
    открывает последний (`/reporting/manage` → `/reporting/view`), и кнопки
    создания там нет вовсе. Первый живой прогон 15.09 упал именно на ней.

    Уход перехватывается диалогом «You have unsaved changes» с тремя
    ответами. Отвечаем **«Don't save»**: «Save» оставил бы на кабинете байера
    сохранённый отчёт, которого никто не просил, а «Cancel» вернул бы нас
    туда же, откуда уходим.

    Возвращает `(добрались, чем кончилось)`. Признак отдельно от описания
    намеренно: разбирать по словам русскую фразу — та же опора на текст, от
    которой мы уходим в самих шагах. Описание попадает в отчёт: в чужом
    отчёте могли висеть фильтры (в том же прогоне висел «Had delivery»), и
    знать, откуда мы пришли, важнее, чем кажется.
    """
    if "/reporting/view" not in _page_url(page):
        return True, "already on the reports list"
    if not _click_label(page, "All reports", tries=6):
        return False, "«All reports» not found — cannot leave the report"
    _sleep(2)
    try:
        asked = bool(page.evaluate(_UNSAVED_DIALOG_JS))
    except Exception:
        asked = False
    if asked and not _click_label(page, "Don't save", tries=6):
        return False, "the unsaved-changes prompt did not close"
    for _ in range(12):
        if "/reporting/manage" in _page_url(page):
            return True, "left the open report" + (
                ", changes not saved" if asked else "")
        _sleep(1)
    return False, "could not leave the report"


def _click_label(page, label: str, tries: int = 10, *, allow_many: bool = False) -> bool:
    """Нажать подпись. По умолчанию — только если она на экране одна.

    Единственность — защита: две разные кнопки с одинаковым текстом означают,
    что мы не знаем, куда попадём. Но бывает и наоборот: 15.09 на пустом
    списке отчётов «Create report» нарисована **дважды** — вверху панели и в
    середине пустого состояния, — и это одно и то же действие. Отказ там был
    бы на ровном месте, поэтому разрешение даётся явно и по месту, а правило
    остаётся строгим везде, где его не снимали.
    """
    args = {"label": label, "allowMany": True} if allow_many else label
    for _ in range(tries):
        try:
            if page.evaluate(_CLICK_LABEL_JS, args):
                return True
        except Exception:
            pass
        _sleep(0.6)
    return False


def _set_checkbox(page, label: str, wanted: bool) -> bool:
    """Привести чекбокс к нужному состоянию. Неизвестное состояние после
    клика — отказ, а не надежда: галка, о которой мы гадаем, даст таблицу
    не с тем брейкдауном, и число будет выглядеть правильным."""
    for _ in range(6):
        try:
            state = page.evaluate(_CHECKED_JS, label)
        except Exception:
            state = None
        if state is wanted:
            return True
        if state is None:
            return False
        page.evaluate(_CHECKBOX_JS, {"label": label, "wanted": wanted})
        _sleep(0.8)
    return False


def _reporting_url(act: str, date_from: str, date_to: str) -> str:
    act_id = act.strip().removeprefix("act_")
    if not re.fullmatch(r"[0-9]+", act_id):
        raise ValueError("ad_account_id must contain digits, optionally prefixed with act_")
    start, end = date.fromisoformat(date_from), date.fromisoformat(date_to or date_from)
    if start.isoformat() != date_from or end.isoformat() != (date_to or date_from) or start > end:
        raise ValueError("Dates must be canonical YYYY-MM-DD in ascending order")
    return f"{REPORTING}?act={act_id}&date={start.isoformat()}_{(end + timedelta(days=1)).isoformat()}"


_PERIOD_JS = "() => {" + _VISIBLE_JS + """
  const labels = [...document.querySelectorAll('button,[role="button"]')].filter(visible)
    .map(el => (el.innerText || '').trim())
    .filter(text => /\\b\\w{3} \\d{1,2}, \\d{4}|\\b\\d{4}-\\d{2}-\\d{2}\\b/.test(text));
  const unique = [...new Set(labels)];
  return unique.length === 1 ? unique[0] : '';
}
"""


def _shown_period_text(page) -> str:
    return " ".join((page.evaluate(_PERIOD_JS) or "").split())


def _lease_error(exc, account, date_from, date_to=""):
    out = CountrySplit(account=account, date_from=date_from, date_to=date_to or date_from,
                       state="failed", error=str(exc))
    # The public reader persists refusals too: an old successful report must
    # not remain current after a new attempt failed at the ownership boundary.
    try:
        save_split(out)
    except Exception as save_exc:
        out.error += f"; Could not save reading ({type(save_exc).__name__}); stored data was not replaced"
    return out


@serialized_browser(key=_profile_key, reading=True, on_lease_error=_lease_error)
def read_country_split(account: str, date_from: str, date_to: str = "") -> CountrySplit:
    """Read one fresh report, refusing unverified or partial results.

    Uses the physical-profile lease shared by ACM processes of one OS user.
    """
    date_to = date_to or date_from
    out = CountrySplit(account=account, date_from=date_from, date_to=date_to)
    conn = None
    held = _ReadOpen(account)
    try:
        entry = _entry_for(account)
        if entry is None:
            raise ValueError(f"{account} is not in the account map")
        act = (entry.ad_account_id or "").strip()
        url = _reporting_url(act, date_from, date_to)  # validate before attach
        expected_act = "act_" + act.removeprefix("act_")
        # ACL-1518: окно, открытое руками, переоткрывается путём Publish.
        # ACL-1580: закрытый профиль чтение запускает само и потом закрывает.
        with opening_closed_profile(held):
            conn = _attach_only(entry, account)
        page = conn.page
        try:
            page.goto(url, timeout=REPORT_TIMEOUT_MS, wait_until="domcontentloaded")
        except Exception as exc:
            if _profile_trouble.classify_error(exc)[0] == LOGGED_OUT_CODE:
                raise ProfileNotAttachable(logged_out_message(account), code=LOGGED_OUT_CODE) from exc
            raise
        if _logged_out(page):
            # Разлогинен — понятная строка и никаких повторов.
            raise ProfileNotAttachable(logged_out_message(account), code=LOGGED_OUT_CODE)
        _sleep(4)
        if _act_from_url(page, tries=6) != expected_act:
            if _logged_out(page, deep=True):
                raise ProfileNotAttachable(logged_out_message(account), code=LOGGED_OUT_CODE)
            raise ValueError("Ads Reporting opened a different account; reading refused")
        out.status, out.status_text = _read_status(page)
        # Дождаться интерфейса и выйти из отчёта, который Reporting открыл сам:
        # «Create report» есть только на списке отчётов (живой прогон 15.09).
        _wait_mounted(page)
        reached, out.entered = _leave_open_report(page)
        if not reached:
            raise ValueError(f"could not reach the reports list: {out.entered}")
        _wait_mounted(page, need=4)
        # Reusing a saved view could retain filters and quietly undercount spend.
        # A new report is mandatory until the live pilot proves filter handling.
        # «Create report» на пустом списке нарисована дважды — в панели и в
        # середине пустого состояния (живой прогон 15.09), и это одно и то же
        # действие. Остальные подписи остаются под строгим правилом.
        if not _click_label(page, "Create report", tries=8, allow_many=True):
            raise ValueError("could not open fresh report: Create report")
        _sleep(2)

        # Вид отчёта выбирается в диалоге: «Pivot table» — вариант с
        # описанием, а не кнопка, и подтверждается «Continue».
        pick = {}
        for _ in range(8):
            try:
                pick = page.evaluate(_PICK_PIVOT_JS) or {}
            except Exception:
                pick = {}
            if pick.get("ok"):
                break
            _sleep(1)
        if not pick.get("ok"):
            raise ValueError(
                "could not open fresh report: Pivot table — "
                + str(pick.get("why") or "the dialog did not answer")
            )
        _sleep(1)
        if not _click_label(page, "Continue", tries=8):
            raise ValueError("could not open fresh report: Continue")
        _sleep(2)

        if not _ensure_customize_open(page):
            raise ValueError("could not open fresh report: Customize")
        _sleep(1)
        # Галочки берутся в своих разделах: «Campaign name» в панели две —
        # в «Popular breakdowns» и в «Level» (живое наблюдение 15.09), а
        # «Country» лежит в «Delivery». Раздел снимает двусмысленность, не
        # ослабляя правила единственности.
        for section, label, wanted in (
            ("Level", "Campaign name", False),
            ("Level", "Ad set name", False),
            ("Delivery", "Country", True),
        ):
            done, why = _set_section_checkbox(page, section, label, wanted)
            if not done:
                raise ValueError(f"could not verify breakdown '{section} / {label}': {why}")
        _sleep(3)
        # Период задаётся интерфейсом: параметр `date` в адресе Meta
        # выбрасывает (живой прогон 15.09 — на экране оказался её
        # умолчательный «This month» вместо запрошенного периода).
        if not _period_matches(_shown_period_text(page), date_from, date_to):
            set_ok, set_note = _set_period(page, date_from, date_to)
            if not set_ok:
                raise ValueError(f"could not set the report period: {set_note}")

        if not _period_matches(_shown_period_text(page), date_from, date_to):
            # Не взялось даже после ввода — отказ обязан принести, чем период
            # задаётся, иначе следующий разбор снова начнётся с живого прогона
            # ради одного названия кнопки.
            try:
                controls = page.evaluate(_DATE_CONTROLS_JS) or []
            except Exception:
                controls = []
            shown = _shown_period_text(page) or "(period not recognised)"
            raise ValueError(
                "Displayed report period does not match the requested dates: "
                f"shown {shown!r}; period controls: {controls}"
            )

        rows, how = [], "refused: report not loaded"
        for attempt in range(10):
            snapshot = page.evaluate(_GRAB_CELLS_JS)
            if not isinstance(snapshot, dict) or snapshot.get("error"):
                how = "refused: " + (snapshot.get("error", "unreadable report") if isinstance(snapshot, dict) else "unreadable report")
            else:
                rows, how = _verified_rows(snapshot.get("matrix") or [])
                if not how.startswith("refused:"):
                    break
            if attempt < 9:
                _sleep(2)
        out.how = how
        if how.startswith("refused:"):
            # Отказ разбора без самой таблицы читается как «что-то не так».
            # Кладём то, что разбирали: строки видно сразу, и следующий заход
            # начинается с факта, а не с живого прогона ради одного взгляда.
            seen = (snapshot.get("matrix") if isinstance(snapshot, dict) else None) or []
            shown = "; ".join(" | ".join(c for c in row) for row in seen[:6])
            try:
                dropped = page.evaluate("() => window.__aclDropped || []") or []
            except Exception:
                dropped = []
            tail = f"; did not fit the columns: {dropped}" if dropped else ""
            raise ValueError(f"{how}; table: [{shown[:600]}]{tail}")
        if _act_from_url(page, tries=1) != expected_act:
            raise ValueError("Account changed during reading; result refused")
        if not _period_matches(_shown_period_text(page), date_from, date_to):
            raise ValueError("Displayed period changed during reading; result refused")
        out.rows = rows
        out.state = "ok" if rows else "empty"
        if not rows:
            out.how = "Report explicitly shows no data for the requested period"
    except Exception as exc:
        out.state, out.rows = "failed", []
        out.error = str(exc) or type(exc).__name__
        out.error_code = str(getattr(exc, "code", "") or "") if isinstance(exc, ProfileNotAttachable) else ""
        if not out.error_code:
            # ACL-1826: вход в прокси читается одной фразой.
            from src.fb_api import mlx_session as _mlx

            _text, _code = _mlx.humanize(exc, "", account)
            if _code:
                out.error, out.error_code = _text, _code
    finally:
        # Persist failures too: a failed retry must not expose an old success.
        try:
            save_split(out)
        except Exception as exc:
            out.state, out.rows = "failed", []
            out.error = (out.error + "; " if out.error else "") + f"Could not save reading ({type(exc).__name__}); stored data was not replaced"
        if conn is not None:
            if out.state == "failed":
                try:
                    out.shot = _shoot(conn.page, account, date_from)
                    # Update the persisted failure with its diagnostic path.
                    save_split(out)
                except Exception:
                    pass
            if not held.keep_tab:
                try:
                    conn.page.close()  # _attach_only creates a separate owned tab
                except Exception:
                    pass
            try:
                conn.close(keep_browser_open=True)
            except Exception:
                pass
        # ACL-1580: профиль, который запустило это чтение (или Read spend перед
        # ним), закрывается мягкой остановкой — и при отказе тоже.
        _finish_started(held)
    return out


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3:
        raise SystemExit("usage: python -m src.analysis_agent.country_split ACCOUNT DATE_FROM [DATE_TO]")
    result = read_country_split(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "")
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=1))
    raise SystemExit(1 if result.state == "failed" else 0)
