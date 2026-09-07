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
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

from config import paths

ADS_MANAGER = "https://adsmanager.facebook.com/adsmanager/manage/campaigns"

#: сколько ждать таблицу — профиль поднимается небыстро, а спешка тут ничего
#: не экономит: читаем мы по одному аккаунту и по просьбе человека
TABLE_TIMEOUT_MS = 90_000


def yesterday(today: date | None = None) -> str:
    return ((today or date.today()) - timedelta(days=1)).isoformat()


@dataclass
class SpendReading:
    """Один ответ на вопрос «сколько потратил этот аккаунт за этот день»."""

    account: str
    day: str
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
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def _key(account: str, day: str) -> str:
    return f"{account}|{day}"


def get_reading(account: str, day: str) -> SpendReading:
    raw = _load_all().get(_key(account, day))
    if not raw:
        return SpendReading(account=account, day=day)
    known = {f.name for f in SpendReading.__dataclass_fields__.values()}
    return SpendReading(**{k: v for k, v in raw.items() if k in known})


def all_readings(day: str) -> dict[str, SpendReading]:
    out: dict[str, SpendReading] = {}
    known = {f.name for f in SpendReading.__dataclass_fields__.values()}
    for key, raw in _load_all().items():
        acc, _, d = key.partition("|")
        if d == day:
            out[acc] = SpendReading(**{k: v for k, v in raw.items() if k in known})
    return out


def _put(reading: SpendReading) -> None:
    data = _load_all()
    data[_key(reading.account, reading.day)] = reading.to_dict()
    _save_all(data)


# ─────────────────────────── очередь ───────────────────────────

#: один браузер за раз. Два профиля, поднятых одновременно с одной машины,
#: это не «быстрее», это две сессии подряд с одного устройства.
_LOCK = threading.Lock()
_RUNNING: set[str] = set()


def is_running(account: str, day: str) -> bool:
    return _key(account, day) in _RUNNING


def running_now() -> list[str]:
    return sorted(k.split("|")[0] for k in _RUNNING)


def start_read(
    account: str,
    day: str = "",
    *,
    reader: Callable[[str, str], SpendReading] | None = None,
) -> SpendReading:
    """Поставить чтение в работу и сразу вернуть его состояние.

    Чтение идёт в отдельном потоке: профиль поднимается десятки секунд, и
    держать всё это время страницу — значит показать человеку зависший
    дешборд вместо ответа.
    """
    day = day or yesterday()
    key = _key(account, day)
    if key in _RUNNING:
        return get_reading(account, day)

    reading = SpendReading(
        account=account, day=day, state="running", started_at=_now()
    )
    _RUNNING.add(key)
    _put(reading)

    fn = reader or read_via_rpa

    def run() -> None:
        started = time.time()
        try:
            with _LOCK:
                done = fn(account, day)
        except Exception as exc:  # noqa: BLE001 — падение чтения не должно ронять сервер
            done = SpendReading(
                account=account, day=day, state="failed", error=f"{type(exc).__name__}: {exc}"
            )
        done.started_at = reading.started_at
        done.finished_at = _now()
        done.took_s = round(time.time() - started, 1)
        _RUNNING.discard(key)
        _put(done)

    threading.Thread(target=run, name=f"spend:{account}", daemon=True).start()
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
    conn = direct._try_attach_existing(profile_id, str(udd))
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
    if direct._try_attach_existing(profile_id, str(udd)) is not None:
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


def read_via_rpa(account: str, day: str) -> SpendReading:
    """Открыть аккаунт в его профиле и прочитать спенд за день."""
    out = SpendReading(account=account, day=day, source="rpa", state="failed")

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
        page.goto(_url(act, day), timeout=TABLE_TIMEOUT_MS, wait_until="domcontentloaded")

        # Ads Manager рисуется скриптом, и `domcontentloaded` на нём не значит
        # почти ничего: первый прогон 2026-09-07 проверил период через четыре
        # секунды после перехода, когда на экране был спиннер и пустой скелет.
        # Ждём саму таблицу, и только потом спрашиваем, какой период показан.
        if not _wait_for_grid(page):
            out.error = "Ads Manager не отрисовался за отведённое время"
            out.shot = _shoot(page, account, day)
            return out

        shown = _shown_period(page)
        if not _period_matches(shown, day):
            out.error = (
                f"на экране период «{shown or 'не найден'}», а спрашивали {day} — "
                "число не берём"
            )
            out.shot = _shoot(page, account, day)
            return out

        spend, how = _read_total(page)
        if spend is None:
            out.error = "не нашёл итоговую строку со спендом"
            out.how = how
            out.shot = _shoot(page, account, day)
            return out

        out.spend = spend
        out.currency = _currency(page)
        out.how = how
        out.state = "ok"
        return out
    finally:
        if conn is not None:
            # Окно остаётся открытым: профиль, закрытый сразу после захода,
            # выглядит хуже, чем человек, который посмотрел и ушёл.
            try:
                conn.close(keep_browser_open=True)
            except Exception:
                pass


def _url(act: str, day: str) -> str:
    act_id = act if act.startswith("act_") else f"act_{act}"
    return f"{ADS_MANAGER}?act={act_id}&date={day}_{day}"


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


def _period_matches(shown: str, day: str) -> bool:
    """Совпадает ли показанный период с запрошенным днём.

    Сверка нарочно грубая: нам нужно поймать «страница показывает не то»,
    а не разобрать все формы записи дат в интерфейсе Meta.
    """
    if not shown:
        return False
    y, m, d = day.split("-")
    months = (
        "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
    )
    month = months[int(m) - 1]
    day_no_zero = str(int(d))
    lowered = shown.lower()
    if day in lowered:
        return True
    if month.lower() in lowered and day_no_zero in lowered and y in lowered:
        return True
    # «Yesterday» — ровно то, что мы и просим, когда day это вчера
    return "yesterday" in lowered and day == yesterday()


#: сумма, которую страница сама подписывает «Total spent». Берём именно её, а
#: не первое число со знаком валюты: рядом стоят дневные бюджеты и цены за
#: результат, и первое попавшееся оказалось бы бюджетом одной кампании.
_TOTAL_SPENT = None


def _total_spent_re():
    global _TOTAL_SPENT
    if _TOTAL_SPENT is None:
        import re

        _TOTAL_SPENT = re.compile(r"([$€£₪]\s*[\d,]+(?:\.\d{1,2})?)\s*Total spent")
    return _TOTAL_SPENT


def _spend_on_screen(page) -> float | None:
    """Найти на странице сумму, подписанную «Total spent».

    Ищем по всей строке итогов, а не по одному узлу: копий надписи «Results
    from N campaigns» полтора десятка, часть невидима, а часть занимает только
    левую половину строки и суммы в себе не содержит. Один прогон уже сорвался
    именно на этом — узел был выбран правильный по тексту и бесполезный по
    ширине.
    """
    pat = _total_spent_re()
    try:
        for cand in page.query_selector_all("div, span"):
            try:
                text = " ".join((cand.inner_text() or "").split())
            except Exception:
                continue
            if "Total spent" not in text or len(text) > 600:
                continue
            m = pat.search(text)
            if m:
                return _first_money(m.group(1))
    except Exception:
        pass
    return None


def _totals_label(page):
    """Надпись «Results from N campaigns» — за неё берётся курсор.

    Число кампаний каждый раз своё, поэтому цепляемся за начало текста. Из
    полутора десятков копий годится та, у которой есть размеры на экране:
    невидимую навести курсором нельзя, и попытка стоила бы тридцати секунд
    ожидания.
    """
    found = None
    try:
        for cand in page.query_selector_all("div, span"):
            try:
                text = " ".join((cand.inner_text() or "").split())
                if not text.startswith("Results from") or "campaign" not in text:
                    continue
                if cand.is_visible() and cand.bounding_box():
                    found = cand
            except Exception:
                continue
    except Exception:
        pass
    return found


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

    value = _spend_on_screen(page)
    if value is not None:
        return value, "строка итогов, «Total spent»"

    label = _totals_label(page)
    if label is None:
        return None, "строка «Results from …» не найдена"

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

    _sleep(3)
    value = _spend_on_screen(page)
    if value is None:
        return None, "итоги раскрыты, но суммы рядом с «Total spent» нет"
    return value, "строка итогов раскрыта наведением, «Total spent»"


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

    number = r"\d{1,3}(?:[,\s]\d{3})*(?:\.\d{1,2})?|\d+\.\d{1,2}"
    signed = re.search(rf"[$€£₪]\s*({number})", text)
    m = signed or re.search(rf"(?<![\d.,])(\d{{1,3}}(?:[,\s]\d{{3}})+(?:\.\d{{1,2}})?|\d+\.\d{{1,2}})", text)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", "").replace(" ", ""))
    except ValueError:
        return None


def _currency(page) -> str:
    try:
        text = page.inner_text("body")[:4000]
        for sign, code in (("$", "USD"), ("€", "EUR"), ("£", "GBP"), ("₪", "ILS")):
            if sign in text:
                return code
    except Exception:
        pass
    return ""


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
