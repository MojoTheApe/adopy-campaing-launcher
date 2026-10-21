"""Country Workbench (ACL-1799): данные страницы «Страна -> Ассет».

Одна страна режется по ассету (Ad Asset / Offer) и отвечает на два вопроса:
как я иду относительно остальных байеров и что из увиденного можно взять в
билдер. Всё считается из той же таблицы статистики, что и остальной отчёт;
источник цифр не меняется, обращений к Meta нет.

Правила (dashboard-rebuild-for-claude.md §6, §9):

* «Другие» — тот же срез без выбранного байера. Средние арифметические по
  байерам запрещены: пир-CPA = сумма расходов пиров / сумма их FTD, то же для
  CPL и CR.
* CPL = Spend/Leads, CPA = Spend/FTDs, CR = FTDs/Leads (в процентах).
  Нулевой знаменатель — «нет значения», а не ноль.
* Мало данных — так и говорим: значение не сравнивается и не красится.
* Цель (My Goals), порог (benchmarks) и результат пиров — три разных числа;
  здесь они лежат в разных полях и никогда не подменяют друг друга.
* Строки без CM не попадают ни в «Вы», ни в «Другие»: принадлежность по
  названию аккаунта не угадывается. Их объём показывается отдельно.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pandas as pd

from src.analysis_agent.analyze_fb_stats import creative_identity

#: Сколько лидов нужно когорте, чтобы CPL и CR считались сравнимыми.
MIN_LEADS = 30
#: Сколько FTD нужно когорте, чтобы CPA считался сравнимым.
MIN_FTDS = 3

#: Сколько строк на вкладку и сколько ассетов на страну уходит в страницу.
ROWS_PER_TAB = 15
MAX_ASSETS = 6

#: Меньше — лучше (стоимость) или больше — лучше (конверсия).
_LOWER_IS_BETTER = {"cpl": True, "cpa": True, "cr": False}


def weighted_kpis(spend: float, leads: float, ftds: float) -> Dict[str, Any]:
    """Взвешенные CPL / CPA / CR из сумм. Нулевой знаменатель -> None."""
    spend, leads, ftds = float(spend or 0), float(leads or 0), float(ftds or 0)
    return {
        "spend": spend,
        "leads": int(leads),
        "ftds": int(ftds),
        "cpl": (spend / leads) if leads > 0 else None,
        "cpa": (spend / ftds) if ftds > 0 else None,
        "cr": (ftds / leads * 100.0) if leads > 0 else None,
    }


def metric_state(name: str, k: Dict[str, Any]) -> str:
    """ok / unavailable / insufficient для одной метрики когорты."""
    if k.get(name) is None:
        return "unavailable"
    basis, need = (k["ftds"], MIN_FTDS) if name == "cpa" else (k["leads"], MIN_LEADS)
    return "ok" if basis >= need else "insufficient"


def _sum(frame: pd.DataFrame) -> Dict[str, float]:
    return {
        "spend": float(frame["Spend"].sum()),
        "leads": float(frame["Leads"].sum()),
        "ftds": float(frame["FTDs"].sum()),
    }


def you_vs_others(cube: pd.DataFrame, my_cm: str) -> Dict[str, Any]:
    """Сравнение «Вы / Другие» на срезе `cube` (колонки CM, Spend, Leads, FTDs)."""
    me = (my_cm or "").strip().casefold()
    cm = cube["CM"].fillna("").astype(str).str.strip()
    mine = (cm.str.casefold() == me) if me else pd.Series(False, index=cube.index)
    peers = (~mine) & (cm != "")
    unattributed = cm == ""

    you_k = weighted_kpis(**_sum(cube[mine]))
    oth_k = weighted_kpis(**_sum(cube[peers]))
    you_k["buyers"] = int(cm[mine].nunique())
    oth_k["buyers"] = int(cm[peers].nunique())

    if not me:
        status = "no_buyer"
    elif oth_k["buyers"] == 0:
        status = "no_peers"
    elif you_k["buyers"] == 0:
        status = "no_you"
    else:
        status = "ok"

    metrics: Dict[str, Any] = {}
    for name in ("cpl", "cpa", "cr"):
        ys, os_ = metric_state(name, you_k), metric_state(name, oth_k)
        row: Dict[str, Any] = {
            "you": you_k[name], "you_state": ys,
            "others": oth_k[name], "others_state": os_,
            "delta_pct": None, "verdict": None,
        }
        if status == "ok" and ys == "ok" and os_ == "ok" and oth_k[name]:
            delta = (you_k[name] - oth_k[name]) / oth_k[name] * 100.0
            row["delta_pct"] = delta
            better = delta < 0 if _LOWER_IS_BETTER[name] else delta > 0
            row["verdict"] = "better" if (better and abs(delta) >= 0.05) else (
                "worse" if abs(delta) >= 0.05 else "same")
        metrics[name] = row

    return {
        "status": status,
        "you": you_k,
        "others": oth_k,
        "unattributed": weighted_kpis(**_sum(cube[unattributed])),
        "metrics": metrics,
        "thresholds": {"min_leads": MIN_LEADS, "min_ftds": MIN_FTDS},
    }


# ── Куб: одна группировка на страну, дальше всё считается по нему ──────────

_CUBE_KEYS = ["Asset", "CM", "Account", "Adset", "Device", "Age", "Target",
              "Pack", "Version", "CKey", "Text", "Raw", "Landing"]


def _text(df: pd.DataFrame, col: str) -> pd.Series:
    if col not in df.columns:
        return pd.Series("", index=df.index)
    return df[col].fillna("").astype(str).str.strip()


def _mapped(series: pd.Series, fn) -> pd.Series:
    """Функция считается один раз на уникальное значение (в книге десятки тысяч строк)."""
    cache = {v: fn(v) for v in series.unique()}
    return series.map(cache)


def build_cube(df: pd.DataFrame, country: str) -> pd.DataFrame:
    """Страна в мелком разрезе: сумма Spend/Leads/FTDs на комбинацию измерений."""
    from src.analysis_agent.morning_report import _display_offer
    from src.analysis_agent.section_filters import interest_bucket_display, interest_bucket_key

    df_c = df[df["Country Final"] == country]
    if df_c.empty:
        return pd.DataFrame(columns=_CUBE_KEYS + ["Spend", "Leads", "FTDs"])

    ident = _mapped(_text(df_c, "Ad Title"), creative_identity)
    frame = pd.DataFrame({
        "Asset": _mapped(_text(df_c, "Offer"), lambda v: _display_offer(v, country)),
        "CM": _text(df_c, "CM"),
        "Account": _text(df_c, "Account Alias"),
        "Adset": _text(df_c, "Adset Name"),
        "Device": _text(df_c, "Device"),
        "Age": _text(df_c, "Age"),
        "Target": _mapped(_text(df_c, "Interest"),
                          lambda v: interest_bucket_display(interest_bucket_key(v)) if v else ""),
        "Pack": ident.map(lambda i: i["pack_id"]),
        "Version": ident.map(lambda i: i["version_id"]),
        "CKey": ident.map(lambda i: i["key"]),
        "Text": ident.map(lambda i: i["text_name"]),
        "Raw": _text(df_c, "Ad Title"),
        "Landing": _text(df_c, "Variant"),
        "Spend": pd.to_numeric(df_c["$ Spend"], errors="coerce").fillna(0.0),
        "Leads": pd.to_numeric(df_c["# Leads"], errors="coerce").fillna(0.0),
        "FTDs": pd.to_numeric(df_c["# FTDs"], errors="coerce").fillna(0.0),
    })
    frame = frame[frame["Asset"] != ""]
    return frame.groupby(_CUBE_KEYS, dropna=False, sort=False)[["Spend", "Leads", "FTDs"]].sum().reset_index()


# ── Вкладки ────────────────────────────────────────────────────────────────

def _row(keys: Dict[str, Any], part: pd.DataFrame, me: str) -> Dict[str, Any]:
    cm = part["CM"].str.strip()
    mine = (cm.str.casefold() == me) if me else pd.Series(False, index=part.index)
    k = weighted_kpis(**_sum(part))
    k["you"] = {key: int(v) if key != "spend" else float(v)
                for key, v in _sum(part[mine]).items()}
    k["buyers"] = int(cm[cm != ""].nunique())
    k.update(keys)
    return k


def _rank(rows: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    rows.sort(key=lambda r: (-r["ftds"], -r["spend"]))
    return rows[:limit]


def _aggregate(part: pd.DataFrame, keys: List[str], me: str) -> List[tuple]:
    """Строки `_row` для каждой группы `keys` одной группировкой.

    Год статистики — это десятки тысяч групп на страну; `_row` на каждую
    группу (фильтры pandas на кусок) занимал 700 с из 820 холодной сборки.
    Здесь те же суммы считаются за один проход. Порядок групп — порядок
    первого появления, как у ``groupby(sort=False)``. Возвращает пары
    (значения ключей, строка без полей ключей).
    """
    if part.empty:
        return []
    if "_mine" in part.columns:
        cm, mine = part["_cm"], part["_mine"]
    else:
        cm, mine = _cm_columns(part, me)
    work = part[keys].copy()
    work["_s"] = part["Spend"]
    work["_l"] = part["Leads"]
    work["_f"] = part["FTDs"]
    work["_ys"] = part["Spend"].where(mine, 0.0)
    work["_yl"] = part["Leads"].where(mine, 0.0)
    work["_yf"] = part["FTDs"].where(mine, 0.0)
    work["_cm"] = cm.where(cm != "")
    grouped = work.groupby(keys, dropna=False, sort=False)
    agg = grouped[["_s", "_l", "_f", "_ys", "_yl", "_yf"]].sum()
    buyers = grouped["_cm"].nunique()
    out = []
    for vals, s, l, f, ys, yl, yf, b in zip(
        agg.index, agg["_s"], agg["_l"], agg["_f"], agg["_ys"], agg["_yl"], agg["_yf"], buyers,
    ):
        k = weighted_kpis(s, l, f)
        k["you"] = {"spend": float(ys), "leads": int(yl), "ftds": int(yf)}
        k["buyers"] = int(b)
        out.append((vals if isinstance(vals, tuple) else (vals,), k))
    return out


def _cm_columns(part: pd.DataFrame, me: str) -> tuple:
    """(байер без пробелов, «строка моя») — как в `_row`."""
    cm = part["CM"].str.strip()
    mine = (cm.str.casefold() == me) if me else pd.Series(False, index=part.index)
    return cm, mine


def _group(part: pd.DataFrame, keys: List[str], make, me: str, limit: int) -> List[Dict[str, Any]]:
    pairs = _aggregate(part, keys, me)
    # Сортировка устойчивая и не зависит от полей ключей, поэтому `make`
    # нужен только строкам, которые попадут на страницу.
    order = sorted(range(len(pairs)), key=lambda i: (-pairs[i][1]["ftds"], -pairs[i][1]["spend"]))
    rows = []
    for i in order[:limit]:
        vals, k = pairs[i]
        k.update(make(vals))
        rows.append(k)
    return rows


def _creative_rows(part: pd.DataFrame, me: str, limit: int) -> List[Dict[str, Any]]:
    """Пакеты с точными версиями внутри; сумма версий равна строке пакета."""
    known = part[part["Pack"] != ""]
    packs: List[Dict[str, Any]] = []
    for (pack,), row in _aggregate(known, ["Pack"], me):
        row["pack"] = pack
        packs.append(row)
    packs = _rank(packs, limit)
    if packs:
        # Версии и первая строка версии внутри пакета — тоже одним проходом.
        by_pack: Dict[Any, List[tuple]] = {}
        for (pack, ckey), k in _aggregate(known, ["Pack", "CKey"], me):
            by_pack.setdefault(pack, []).append((ckey, k))
        first = known.drop_duplicates(["Pack", "CKey"]).set_index(["Pack", "CKey"])
        for row in packs:
            pack = row["pack"]
            items = by_pack.get(pack, [])
            order = sorted(range(len(items)),
                           key=lambda i: (-items[i][1]["ftds"], -items[i][1]["spend"]))
            versions = []
            for i in order[:50]:
                ckey, k = items[i]
                first_row = first.loc[(pack, ckey)]
                k.update(_creative_keys(first_row))
                versions.append(k)
            row["versions"] = versions
    unresolved = part[part["Pack"] == ""]
    if not unresolved.empty and unresolved["Spend"].sum() > 0:
        row = _row({"pack": "", "unresolved": True}, unresolved, me)
        row["versions"] = []
        packs.append(row)
    return packs


def _creative_keys(first: pd.Series) -> Dict[str, Any]:
    ident = creative_identity(first["Raw"])
    return {
        "key": ident["key"], "pack": ident["pack_id"], "version": ident["version_id"],
        "text": ident["text_name"], "raw": first["Raw"], "status": ident["status"],
        "ambiguous": ident["ambiguous"],
    }


def build_slice(cube: pd.DataFrame, country: str, asset: str, my_cm: str) -> Dict[str, Any]:
    """Всё, что показывает страница для (страна, ассет); asset == '' — все ассеты."""
    from config.benchmarks import get_benchmark

    part = cube if not asset else cube[cube["Asset"] == asset]
    me = (my_cm or "").strip().casefold()
    cmp_ = you_vs_others(part, my_cm)

    bench = get_benchmark(country, asset or None)
    peer = cmp_["others"]
    reference = {
        # Цель задаётся страницей из My Goals (get_goal), здесь её нет намеренно.
        "benchmark": {
            "max_cpl": bench.get("max_cpl"), "max_cpa": bench.get("max_cpa"),
            "min_cr": bench.get("min_cr"),
            "source": "offer override" if asset else "country / global default",
        },
        "peer": {"cpl": peer["cpl"], "cpa": peer["cpa"], "cr": peer["cr"],
                 "buyers": peer["buyers"]},
    }

    def _setup(vals):
        device, age, target, pack, landing = vals
        return {"device": device, "age": age, "target": target, "pack": pack, "landing": landing}

    winning = part[part["FTDs"] > 0]
    setups = _group(winning, ["Device", "Age", "Target", "Pack", "Landing"], _setup, me, ROWS_PER_TAB)
    # Лучшая версия креатива внутри комбинации — то, что можно взять в билдер.
    for s in setups:
        sub = winning[(winning["Device"] == s["device"]) & (winning["Age"] == s["age"])
                      & (winning["Target"] == s["target"]) & (winning["Pack"] == s["pack"])
                      & (winning["Landing"] == s["landing"])]
        best = sub.groupby("CKey", sort=False)["FTDs"].sum().sort_values(ascending=False)
        s["creative"] = _creative_keys(sub[sub["CKey"] == best.index[0]].iloc[0]) if len(best) else None

    return {
        "asset": asset,
        "totals": weighted_kpis(**_sum(part)),
        "compare": cmp_,
        "reference": reference,
        "campaigns": _group(part, ["Account", "Adset"],
                            lambda v: {"account": v[0], "name": v[1]}, me, ROWS_PER_TAB),
        "setups": setups,
        "landings": _group(part, ["Landing"], lambda v: {"landing": v[0]}, me, ROWS_PER_TAB),
        "targets": _group(part[part["Target"] != ""], ["Target"],
                          lambda v: {"target": v[0]}, me, ROWS_PER_TAB),
        "creatives": _creative_rows(part, me, ROWS_PER_TAB),
        "buyers": _group(part[part["CM"].str.strip() != ""], ["CM"],
                         lambda v: {"cm": v[0], "is_you": bool(me) and v[0].strip().casefold() == me},
                         me, 30),
    }


def build_country_workbench(df: pd.DataFrame, country: str, my_cm: str) -> Dict[str, Any]:
    """Данные Workbench одной страны: ассеты и срез на каждый (плюс «все»)."""
    cube = build_cube(df, country)
    if cube.empty:
        return {}
    me = (my_cm or "").strip().casefold()
    # Срезы — подмножества куба: байер считается один раз, а не в каждой группировке.
    cube["_cm"], cube["_mine"] = _cm_columns(cube, me)
    by_asset = cube.groupby("Asset", sort=False).agg(
        Spend=("Spend", "sum"), FTDs=("FTDs", "sum")).reset_index()
    mine = cube[cube["CM"].str.strip().str.casefold() == me] if me else cube.iloc[0:0]
    my_ftds = mine.groupby("Asset")["FTDs"].sum()
    by_asset = by_asset.sort_values(["FTDs", "Spend"], ascending=[False, False])
    shown = by_asset.head(MAX_ASSETS)

    assets = [{"name": "", "label": "All assets", "ftds": int(cube["FTDs"].sum()),
               "you_ftds": int(mine["FTDs"].sum())}]
    for _, r in shown.iterrows():
        assets.append({"name": r["Asset"], "label": r["Asset"], "ftds": int(r["FTDs"]),
                       "you_ftds": int(my_ftds.get(r["Asset"], 0))})
    return {
        "country": country,
        "my_cm": my_cm or "",
        "assets": assets,
        "assets_total": int(len(by_asset)),
        "slices": {a["name"]: build_slice(cube, country, a["name"], my_cm) for a in assets},
    }
