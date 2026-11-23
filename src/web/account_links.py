"""Where each ad account is used: its countries and its assets.

The account map answers a different question and must keep answering it. The RPA
asks "which country is this campaign for" — one value, chosen per publish, and
`ad_account_map.csv` carries it in a single `country` column. The dashboard asks
"where is this account used", and the answer is genuinely several countries and
several assets at once. Squeezing the second question into the first column is
what makes an account disappear from one country the moment it is added to
another.

So links live here, in their own store, owned by the dashboard. Nothing in the
RPA changes: it keeps reading the column it always read.

One store, two screens. The account map on Home and the per-country account list
are not two lists kept in step — they are two views over these rows. Adding a
link in either place adds it once; removing it removes it once. Two lists that
synchronise always drift, and the drift is discovered by someone who trusted the
wrong one.

Format: one row per link, so a link can be added or removed without rewriting
anything else.

    account_name,kind,value
    FacebookUL869,country,Chile
    FacebookUL869,country,Peru
    FacebookUL869,asset,Ait
"""
from __future__ import annotations

import csv
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from config.paths import _replace_when_free, get_ad_account_map_path, get_local_config_dir

FIELDNAMES = ["account_name", "kind", "value"]
KINDS = ("country", "asset")
#: ACL-2853: необязательная четвёртая колонка — чей байер завёл пустую страну.
BUYER_COLUMN = "buyer"

_SEEDED_MARK = ".account_links.seeded"

#: ACL-1319: аккаунты, которые владелец сам убрал из Available accounts.
HIDDEN_FIELDNAMES = ["account_name", "hidden_at"]
_hidden_lock = threading.Lock()


def links_path() -> Path:
    return Path(get_local_config_dir()) / "account_links.csv"


def _norm(value: str) -> str:
    return (value or "").strip()


def _key(account: str, kind: str, value: str) -> tuple[str, str, str]:
    """Case-insensitive identity. "chile" and "Chile" are one link, not two."""
    return (_norm(account).lower(), _norm(kind).lower(), _norm(value).lower())


def load() -> list[dict[str, str]]:
    path = links_path()
    if not path.is_file():
        return []
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = []
        for row in csv.DictReader(fh):
            account = _norm(row.get("account_name"))
            kind = _norm(row.get("kind")).lower()
            value = _norm(row.get("value"))
            # Пустое имя аккаунта — не мусор, а заглушка: страна заведена,
            # аккаунтов в ней пока нет. Без неё пустой стране негде лежать, и
            # завести её можно было только вместе с первым аккаунтом.
            if kind in KINDS and value:
                item = {"account_name": account, "kind": kind, "value": value}
                # ACL-2853: метка байера есть только у пустой страны, которую
                # он завёл сам; у старых файлов колонки нет, и строка выглядит
                # как раньше.
                buyer = _norm(row.get(BUYER_COLUMN))
                if buyer:
                    item["buyer"] = buyer
                rows.append(item)
        return rows


def _write(rows: list[dict[str, str]]) -> None:
    # Колонку buyer пишем только когда есть что в ней хранить: файл без меток
    # остаётся байт в байт таким, каким его писали раньше.
    fields = FIELDNAMES + [BUYER_COLUMN] if any(r.get(BUYER_COLUMN) for r in rows) else FIELDNAMES
    _write_csv(links_path(), fields, rows)


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    """Атомарная запись CSV: временный файл рядом и замена с ожиданием.

    Одна процедура на оба файла рядом (связи и спрятанные аккаунты): второй
    способ писать в ту же папку однажды забыл бы про короткое имя или про
    ожидание Windows — а оба урока здесь уже оплачены."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Короткое служебное имя, а не `.{имя}.{32 hex}.tmp`: у Windows предел
    # пути 260 символов, и каталог рантайма — `.{sha40}.staging-{uuid32}` —
    # съедает 82 из них. Длинное имя упирало запись в FileNotFoundError, и
    # ровно на этом сборка Windows была красной. Уникальности восьми
    # шестнадцатеричных знаков внутри одного каталога довольно.
    temp = path.with_name(f".{uuid.uuid4().hex[:8]}.tmp")
    try:
        with open(temp, "w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
            fh.flush()
            os.fsync(fh.fileno())
        # Wait for Windows to let go of the target rather than fail on the
        # first refusal: `os.replace` answers PermissionError while the file
        # is merely open for a moment (the previous replace's handle,
        # Defender, the indexer). Nine links in a row hit it on 2026-09-16.
        # Same remedy the settings file already has.
        _replace_when_free(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _known_code(name: str) -> str:
    """Код страны только по настоящей таблице; без догадки «первые две буквы»."""
    from src.analysis_agent.country_names import COUNTRY_CODES

    n = _norm(name).casefold()
    if not n:
        return ""
    if n in COUNTRY_CODES:
        return COUNTRY_CODES[n]
    if len(n) == 2 and n.upper() in set(COUNTRY_CODES.values()):
        return n.upper()
    return ""


def _same_country(a: str, b: str) -> bool:
    """«KW» и «Kuwait» — одна страна; регистр не важен. UAE и Ukraine — разные."""
    if _norm(a).casefold() == _norm(b).casefold():
        return True
    ca, cb = _known_code(a), _known_code(b)
    return bool(ca) and ca == cb


def _shown_to(row: dict[str, str], buyer: str) -> bool:
    """Видна ли эта строка на карте байера: его аккаунт или его пустая страна."""
    from src.web import account_buyer

    if row["account_name"]:
        return account_buyer.is_visible(row["account_name"], selected=buyer)
    return not row.get(BUYER_COLUMN) or _norm(row[BUYER_COLUMN]).casefold() == buyer.casefold()


def country_marked_for(value: str, buyer: str) -> bool:
    """Заведена ли страна этим байером: пустая строка с его меткой.

    Метка по правилу `account_buyer.tag_for_new_row`: у байера по умолчанию она
    пустая, и тогда это безымянная строка, как была всегда."""
    from src.web import account_buyer

    tag = _norm(account_buyer.tag_for_new_row(_norm(buyer))).casefold()
    return any(
        r["kind"] == "country" and not r["account_name"]
        and _norm(r.get(BUYER_COLUMN)).casefold() == tag
        and r["value"].lower() == _norm(value).lower()
        for r in load()
    )


def empty_country_visible_to(value: str, buyer: str) -> bool:
    """Видна ли байеру пустая страна: безымянная — всем, с меткой — только её байеру."""
    b = _norm(buyer).casefold()
    return any(
        r["kind"] == "country" and not r["account_name"]
        and r["value"].lower() == _norm(value).lower()
        and _norm(r.get(BUYER_COLUMN)).casefold() in ("", b)
        for r in load()
    )


def accounts_visible_for(kind: str, value: str, buyer: str | None) -> list[str]:
    """Аккаунты страны, видимые байеру (все, если байер не задан)."""
    names = accounts_for(kind, value)
    if not buyer or _norm(kind).lower() != "country":
        return names
    from src.web import account_buyer

    return [a for a in names if account_buyer.is_visible(a, selected=buyer)]


def add_value(kind: str, value: str, buyer: str | None = None) -> bool:
    """Завести страну (или ассет) без аккаунтов. False — если уже есть.

    Пустая группа копится и забывается — довод, по которому раньше страну
    нельзя было создать без первого аккаунта. Но он стоил другого: аккаунт
    приходится выбирать из уже известных ещё до того, как его подключили из
    Multilogin, то есть порядок «сначала страна, потом аккаунт» был запрещён
    без всякой на то причины. Пустая страна видна на экране и закрывается
    одной кнопкой — этого достаточно, чтобы она не потерялась.

    ACL-2853: с ``buyer`` вопрос «уже есть?» задаётся про карту этого байера.
    Страна, где лежат только чужие аккаунты (в любом написании — KW и Kuwait),
    для него ещё не заведена: ей записывается метка с его именем. Чужие
    аккаунты и карты других байеров от этого не меняются.
    """
    kind = _norm(kind).lower()
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
    value = _norm(value)
    if not value:
        raise ValueError("value is required")
    rows = load()
    buyer = _norm(buyer)
    if kind != "country" or not buyer:
        if any(r["kind"] == kind and r["value"].lower() == value.lower() for r in rows):
            return False
        rows.append({"account_name": "", "kind": kind, "value": value})
    else:
        if any(r["kind"] == "country" and _same_country(r["value"], value) and _shown_to(r, buyer)
               for r in rows):
            return False
        from src.web import account_buyer

        row = {"account_name": "", "kind": kind, "value": value}
        tag = _norm(account_buyer.tag_for_new_row(buyer))
        if tag:  # байер по умолчанию метки не пишет: файл остаётся прежним
            row[BUYER_COLUMN] = tag
        rows.append(row)
    _write(rows)
    return True


def link(account: str, kind: str, value: str, buyer: str | None = None) -> bool:
    """Add one link. Returns False if it was already there.

    ``buyer`` (ACL-2853): пустую страну другого байера новый аккаунт не стирает."""
    kind = _norm(kind).lower()
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
    account, value = _norm(account), _norm(value)
    if not account or not value:
        raise ValueError("account and value are both required")
    rows = load()
    if any(_key(**r) == _key(account, kind, value) for r in _rows_as_kwargs(rows)):
        return False
    rows.append({"account_name": account, "kind": kind, "value": value})
    # Заглушка больше не нужна: у страны появился настоящий аккаунт.
    mine = _norm(buyer).casefold()
    rows = [
        r for r in rows
        if r["account_name"]
        or not (r["kind"] == kind and r["value"].lower() == value.lower())
        or (mine and _norm(r.get(BUYER_COLUMN)).casefold() not in ("", mine))
    ]
    _write(rows)
    return True


def unlink(account: str, kind: str, value: str) -> bool:
    """Remove one link. The account itself is never deleted here — it keeps its
    row in the account map, its profile and its history, and simply stops being
    listed under this country or asset."""
    target = _key(account, kind, value)
    rows = load()
    kept = [r for r in rows if _key(r["account_name"], r["kind"], r["value"]) != target]
    if len(kept) == len(rows):
        return False
    # Снятый последний аккаунт не должен уносить с собой страну: для этого есть
    # отдельная кнопка Close. Раньше страна исчезала молча, вместе с тем, что о
    # ней знали.
    kind_l, value_l = _norm(kind).lower(), _norm(value).lower()
    if not any(
        r["account_name"] and r["kind"] == kind_l and r["value"].lower() == value_l
        for r in kept
    ):
        kept.append({"account_name": "", "kind": kind_l, "value": _norm(value)})
    _write(kept)
    return True


def close_country(kind: str, value: str, buyer: str | None = None) -> tuple[list[str], int]:
    """Drop a country or asset entirely. Returns (freed accounts, rows removed).

    Они — то, что падает в Available accounts: настроены, подключены к профилю
    Multilogin, просто нигде не размещены. Вызывающий предупредил о тех, кто
    ещё крутит кампании.

    ACL-2853: с ``buyer`` закрывается только карта этого байера — его аккаунты
    и пустая страна, видимая ему (безымянная или с его меткой). Аккаунты и
    пустые страны других байеров остаются.
    """
    kind, value = _norm(kind).lower(), _norm(value)
    rows = load()

    def _goes(r: dict[str, str]) -> bool:
        if not (r["kind"] == kind and _norm(r["value"]).lower() == value.lower()):
            return False
        return _shown_to(r, buyer) if (buyer and kind == "country") else True

    freed = [r["account_name"] for r in rows if _goes(r) and r["account_name"]]
    kept = [r for r in rows if not _goes(r)]
    removed = len(rows) - len(kept)
    if removed:
        _write(kept)
    return sorted(set(freed)), removed


def unlink_all_for(kind: str, value: str, buyer: str | None = None) -> list[str]:
    """Тонкая обёртка над `close_country`: только список освобождённых аккаунтов."""
    return close_country(kind, value, buyer)[0]


def accounts_visible_for(kind: str, value: str, buyer: str | None) -> list[str]:
    """Аккаунты страны, видимые байеру (все, если байер не задан)."""
    names = accounts_for(kind, value)
    if not buyer or _norm(kind).lower() != "country":
        return names
    from src.web import account_buyer

    return [a for a in names if account_buyer.is_visible(a, selected=buyer)]


def _rows_as_kwargs(rows: list[dict[str, str]]):
    for r in rows:
        yield {"account": r["account_name"], "kind": r["kind"], "value": r["value"]}


def values_for(account: str, kind: str) -> list[str]:
    """Every country (or asset) this account is placed in."""
    account, kind = _norm(account).lower(), _norm(kind).lower()
    return sorted(
        {r["value"] for r in load() if r["account_name"].lower() == account and r["kind"] == kind}
    )


def all_pairs(kind: str) -> list[tuple[str, str]]:
    """Every (account, value) of this kind, in one pass.

    `values_for` and `accounts_for` each answer about one side, and asking them
    per account re-reads the file once per account. The Accounts overview needs
    the whole picture at once — it resolves a country for every account on every
    page load — and that is what this is for.
    """
    kind = _norm(kind).lower()
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for r in load():
        if r["kind"] != kind:
            continue
        pair = (r["account_name"], r["value"])
        if not pair[0] or not pair[1] or pair in seen:
            continue
        seen.add(pair)
        out.append(pair)
    return out


def accounts_for(kind: str, value: str) -> list[str]:
    kind, value = _norm(kind).lower(), _norm(value).lower()
    return sorted(
        {
            r["account_name"]
            for r in load()
            if r["kind"] == kind and r["value"].lower() == value and r["account_name"]
        }
    )


def all_values(kind: str) -> list[str]:
    kind = _norm(kind).lower()
    return sorted({r["value"] for r in load() if r["kind"] == kind}, key=str.casefold)


def available_accounts(known_accounts: list[str]) -> list[str]:
    """Accounts placed in no country — the Available accounts list.

    Deliberately keyed on country alone: an account tagged with an asset but no
    country still has nowhere to run, and hiding it from this list is how it
    stays forgotten.
    """
    placed = {r["account_name"].lower() for r in load() if r["kind"] == "country" and r["account_name"]}
    return sorted({a for a in known_accounts if _norm(a) and _norm(a).lower() not in placed})


# ── ACL-1319: убранные из Available accounts ──────────────────────────────
#
# Это настройка вида одного списка и больше ничего. Строка в карте аккаунтов,
# профиль Multilogin, размещение в странах и история в книге не трогаются:
# «убрать» значит только «не показывать среди неразмещённых на этой машине».
# Файл лежит рядом со связями, в машинной папке, и в гит не попадает — у
# каждого байера свой набор. Отдельно от `hidden_accounts.json` вкладки
# Accounts: там прячут строки общей таблицы, здесь — строки этого списка, и
# одно не должно молча гасить другое.

def hidden_path() -> Path:
    return Path(get_local_config_dir()) / "available_hidden.csv"


def hidden_key(account: str) -> str:
    """Сравнение прощающее: регистр и лишние пробелы — та же строка."""
    return " ".join(str(account or "").split()).casefold()


def _load_hidden_rows() -> list[dict[str, str]]:
    path = hidden_path()
    if not path.is_file():
        return []
    try:
        with path.open(encoding="utf-8-sig", newline="") as fh:
            rows: list[dict[str, str]] = []
            seen: set[str] = set()
            for row in csv.DictReader(fh):
                account = " ".join(str(row.get("account_name") or "").split())
                key = hidden_key(account)
                if not key or key in seen:
                    continue
                seen.add(key)
                rows.append({"account_name": account, "hidden_at": _norm(row.get("hidden_at"))})
            return rows
    except (OSError, csv.Error, UnicodeDecodeError):
        # Испорченная настройка вида не должна гасить список: лучше показать
        # лишнее, чем молча спрятать всё.
        return []


def hidden_accounts() -> list[str]:
    """Убранные аккаунты — в том написании, в каком их убирали."""
    return sorted((r["account_name"] for r in _load_hidden_rows()), key=str.casefold)


def hidden_keys() -> set[str]:
    return {hidden_key(r["account_name"]) for r in _load_hidden_rows()}


def hide_accounts(accounts: list[str]) -> list[str]:
    """Убрать из Available accounts. Отдаёт тех, кого действительно добавили."""
    wanted = [" ".join(str(a or "").split()) for a in accounts]
    with _hidden_lock:
        rows = _load_hidden_rows()
        have = {hidden_key(r["account_name"]) for r in rows}
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        added: list[str] = []
        for account in wanted:
            key = hidden_key(account)
            if not key or key in have:
                continue
            have.add(key)
            rows.append({"account_name": account, "hidden_at": stamp})
            added.append(account)
        if added:
            _write_csv(hidden_path(), HIDDEN_FIELDNAMES, rows)
        return added


def restore_accounts(accounts: list[str]) -> list[str]:
    """Вернуть в список. Отдаёт тех, кто действительно был убран."""
    keys = {hidden_key(a) for a in accounts if hidden_key(a)}
    with _hidden_lock:
        rows = _load_hidden_rows()
        kept = [r for r in rows if hidden_key(r["account_name"]) not in keys]
        restored = [r["account_name"] for r in rows if hidden_key(r["account_name"]) in keys]
        if restored:
            _write_csv(hidden_path(), HIDDEN_FIELDNAMES, kept)
        return restored


def seed_from_account_map() -> int:
    """Carry the existing single-country column into links, once.

    Every machine already has accounts placed in a country — in the one column
    the map has always had. Starting from an empty store would present a working
    install as if nothing were configured, and someone would place two dozen
    accounts by hand for no reason.

    Runs once; the marker sits next to the store so a later manual removal of a
    link is not undone on the next start.
    """
    marker = links_path().with_name(_SEEDED_MARK)
    if marker.exists() or links_path().exists():
        return 0
    map_path = Path(get_ad_account_map_path())
    if not map_path.is_file():
        return 0
    rows: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    with map_path.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            account = _norm(row.get("account_name"))
            country = _norm(row.get("country"))
            if not account or not country:
                continue
            k = _key(account, "country", country)
            if k in seen:
                continue
            seen.add(k)
            rows.append({"account_name": account, "kind": "country", "value": country})
    if rows:
        _write(rows)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("seeded from ad_account_map.csv\n", encoding="utf-8")
    return len(rows)
