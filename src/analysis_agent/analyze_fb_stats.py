import argparse
import os
import re
import sys
from pathlib import Path
from typing import List, Optional, Tuple

# Ensure UTF-8 output so Cyrillic KB context doesn't crash on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# При прямом запуске скрипта из корня (python src/analysis_agent/analyze_fb_stats.py) нужен путь к проекту
_proj_root = Path(__file__).resolve().parents[2]
if str(_proj_root) not in sys.path:
    sys.path.insert(0, str(_proj_root))

import pandas as pd

from config.analysis_rules import (
    get_age_range,
    get_creative_skip_prefixes,
    get_device_keywords,
    get_text_segment_dictionary,
    get_text_segment_fallback,
)
from config.paths import get_excel_path
from src.knowledge_agent.knowledge_base import (
    resolve_targeting_text,
    log_unknown_target,
    logged_unknown_targets,
)
from src.analysis_agent.report_markdown import build_full_report_markdown

USE_LATEST_DATE_ONLY = False
_get_excel_path = get_excel_path


def normalize_text_segment(ad_title) -> str:
    """Классификация текста объявления: правила в config/analysis_reading_rules.json (ключ text_segments)."""
    # FIX A: guard against float/None input (NaN in Ad Title column)
    if ad_title is None or (isinstance(ad_title, float) and pd.isna(ad_title)):
        return get_text_segment_fallback()
    text = str(ad_title).lower()
    for segment, variants in get_text_segment_dictionary().items():
        for v in variants:
            if v in text:
                return segment.upper()
    return get_text_segment_fallback()


def normalize_account_alias(alias) -> str:
    """The account's identity: the first whitespace-delimited token.

    The account sheet writes a label with annotations after the name —
    "FacebookDQ365 EC", "FacebookMKTOBFLV37 EC Ait" (country code plus asset) —
    while the stats export writes the bare "FacebookMKTOBFLV37". Everything
    after the first token is annotation, never identity.

    This used to strip only a single trailing TWO-letter token, so
    "FacebookDQ365 EC" worked but "FacebookMKTOBFLV37 EC Ait" did not reduce at
    all and never matched its own stats rows — measured 2026-08-29: only 6 of
    20 accounts could be joined to the stats, which silently emptied the owner
    and country lookups built on top.

    Verified against the real data the same day: of 859 distinct stats aliases
    NONE contains a space, first tokens collide for zero accounts, and all 14
    sheet labels find their stats counterpart this way.
    """
    s = "" if alias is None else str(alias).strip()
    parts = s.split()
    return parts[0] if parts else s


def alias_matches(series, alias):
    """Boolean mask: rows of `series` whose alias matches `alias`, suffix-blind."""
    # Сравнение — единственное место, где работает явное правило имён
    # (config/account_name_rules.json) и где регистр не важен.
    from src.analysis_agent.account_names import account_key

    target = account_key(alias)
    # ACL-1729: в книге десятки тысяч строк, но алиасов — сотни. Ключ считался
    # на каждую строку (и каждый раз stat файла правил) — страница Home
    # собиралась 90 с. Считаем один раз на уникальное значение.
    text = series.fillna("").astype(str)
    hits = {v for v in text.unique() if account_key(v) == target}
    return text.isin(hits)


# A creative is identified by a 3-4 digit number; everything else in the
# title is naming noise around it (prefixes like "Vid", variant digits,
# and separators typed four different ways).
_CREATIVE_NUM_RE = re.compile(r"(?<!\d)(\d{3,4})(?!\d)")


def split_ad_title_variant(ad_title) -> tuple[str, str, str]:
    """Split an Ad Title into (creative id, variant, text name).

    ACL-1593: the variant digit is kept instead of dropped; "" when absent.

    The sheet stores both halves in one cell. This used to split on a dash
    with whitespace on at least one side, which is how most titles are
    written — but the dash is NOT reliable on its own. Buyers type these by hand and a
    large minority omit it — measured on real El Salvador data 2026-08-26,
    where "2257 Story3", "2257 Story14" and "2046 StoryYA 4" all carry a
    text name with no separator at all. Keying on the dash put the text into
    the creative half and left the Text column showing "—".

    So anchor on what the creative actually is: a 3–4 digit number. Anything
    after it is the text, minus a variant digit ("2046 1 - Story14" is
    creative 2046) and any separators:

        "2046 1 - Story14"  -> ("2046", "1", "Story14")
        "2257 Story3"       -> ("2257", "", "Story3")
        "2307_2- 6"         -> ("2307", "2", "6")
        "Vid 2257 - Story13"-> ("2257", "", "Story13")
        "2046- Powered"     -> ("2046", "", "Powered")

    Titles with no number at all fall back to the dash rule, which is all
    there is to go on ("Blue - modern" -> ("Blue", "", "modern")). Titles with
    neither ("New Engagement Ad", "-") return an empty text.
    """
    s = "" if ad_title is None else str(ad_title).strip()
    if not s or s == "-":
        return "", "", ""

    m = _CREATIVE_NUM_RE.search(s)
    if m:
        rest = re.sub(r"^[\s_\-–—]+", "", s[m.end():])
        # A digit right after the creative number is a variant, not the text —
        # unless dropping it would leave nothing, as in "2046 - 6" where the
        # text really is "6".
        vm = re.match(r"^(\d+)(?!\d)[\s_\-–—]*", rest)
        variant = ""
        if vm:
            trimmed = rest[vm.end():]
            if trimmed.strip(" -_–—"):
                rest = trimmed
                variant = str(int(vm.group(1)))  # «01» и «1» — один вариант
        return m.group(1), variant, rest.strip(" -_–—")

    m = re.search(r"\s-|-\s", s)
    if not m:
        return s, "", ""
    creative = s[: m.start()].strip(" -")
    text = s[m.end():].strip(" -")
    return (creative or s), "", text


def split_ad_title(ad_title) -> tuple[str, str]:
    """(creative id, text name) — the group contract; the variant is dropped.

    Every caller that groups by creative + text uses this; the ones that need
    the variant too call `split_ad_title_variant`.
    """
    creative, _variant, text = split_ad_title_variant(ad_title)
    return creative, text


def creative_identity(ad_title) -> dict:
    """Идентичность креатива с сохранением версии (ACL-1799).

    `split_ad_title` намеренно сводит заголовок к паре (номер пакета, текст) и
    этим склеивает версии одного пакета. Здесь версия остаётся: «1121 1» и
    «1121 - 2» — один пакет 1121, но две разные версии. Старая функция и её
    потребители не тронуты; эта живёт рядом и ничего у них не отнимает.

    Версия берётся из того, что `split_ad_title_variant` уже умеет («2046 1 -
    Story14» -> версия 1). Кроме того, если после номера пакета стоит только
    число («1121 1», «1121 - 2»), это тоже версия, но помечается `ambiguous`:
    такая запись может быть и текстовым названием. Сырая строка сохраняется,
    чтобы разбор можно было поправить позже, ничего не потеряв.

    status:
      exact      — есть пакет и версия;
      pack_only  — есть пакет, версии в заголовке нет;
      unresolved — номера пакета нет (разбору нечего сказать).
    """
    raw = "" if ad_title is None else str(ad_title).strip()
    if not raw or raw == "-":
        return {"raw": raw, "pack_id": "", "version_id": "", "text_name": "",
                "key": "", "status": "unresolved", "ambiguous": False}
    if not _CREATIVE_NUM_RE.search(raw):
        return {"raw": raw, "pack_id": "", "version_id": "", "text_name": "",
                "key": "raw:" + raw.casefold(), "status": "unresolved", "ambiguous": False}

    pack, version, text = split_ad_title_variant(raw)
    ambiguous = False
    if not version and re.fullmatch(r"\d+", text or ""):
        version, text, ambiguous = str(int(text)), "", True  # «01» и «1» — одна версия
    key = f"{pack}|{version}|{(text or '').casefold()}"
    return {"raw": raw, "pack_id": pack, "version_id": version, "text_name": text,
            "key": key, "status": "exact" if version else "pack_only", "ambiguous": ambiguous}


_MEDIA_TYPE_TOKENS = frozenset(
    {"img", "image", "images", "vid", "video", "videos", "photo", "mix"}
)


def _normalize_targeting_sep(s: str) -> str:
    """Normalize AND-group separators: `\\`, `/` and `|` all mean the same; display as `|`."""
    return s.replace("\\", "|").replace("/", "|")


def _is_compound_targeting_token(token: str) -> bool:
    """`|`, `\\` or `/` marks two+ interests in one campaign (define further / narrow further)."""
    return "|" in token or "\\" in token or "/" in token


def _is_creative_skip_token(token: str, creative_prefixes: list) -> bool:
    """Return True when an adset token is creative/media, not targeting."""
    if _is_compound_targeting_token(token):
        return False

    p_lower = token.lower()
    if p_lower in _MEDIA_TYPE_TOKENS:
        return True
    if token.isdigit():
        return True

    for pfx in creative_prefixes:
        if p_lower == pfx:
            return True
        if p_lower.startswith(pfx) and len(p_lower) == len(pfx):
            return True
        # Longer tokens like MixReading / Mix1 are unclear targeting — keep visible.
        if p_lower.startswith(pfx) and len(p_lower) > len(pfx):
            return False
    return False


def _fallback_interest(parts_before_age: list, adset_name: str) -> Optional[str]:
    """When normal parsing finds nothing, keep raw tokens for manual cleanup."""
    raw_parts = [p for p in parts_before_age if "." not in p]
    while raw_parts and raw_parts[-1].lower() in _MEDIA_TYPE_TOKENS:
        raw_parts.pop()
    if raw_parts:
        return "_".join(raw_parts)

    name = str(adset_name or "").strip()
    if name and name.lower() not in ("nan", "none"):
        return name
    return None


def parse_adset_name(
    adset_name: str,
    device_kw: dict,
    age_min: int,
    age_max: int,
    creative_prefixes: list,
) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """Parse Adset Name to extract Country Code (display), Device, Interest, Age.

    Format: CountryCode_OfferName_Device_Creative[_CreativeNum]_Interest_Age[_Date]
    Country (full name) and Offer are taken from dedicated columns.
    Creative is taken from Ad Title column as-is.
    CountryCode (position 0) is extracted only for display purposes (e.g. MX, GCC, CL).

    Algorithm:
    1. Country code: parts[0] (2-3 letter code for display only).
    2. Find Device: first part matching a device keyword (e.g. "all", "ios").
    3. After Device, find Age: first numeric part in [age_min, age_max] (optionally with "+").
    4. Interest: non-creative, non-numeric parts between Device and Age.
       Creative parts = those starting with a creative prefix or being purely numeric.
       Tokens with `|` are compound targeting (define further) and are never skipped.
       Unclear leftover tokens are kept as-is instead of becoming empty/unknown.

    Returns: (country_code, device, interest, age)
    """
    if not isinstance(adset_name, str):
        return None, None, None, None

    parts = [p.strip() for p in adset_name.split("_") if p.strip()]

    # Country code is always the first token (2-3 letters like MX, CL, GCC, UAE)
    country_code: Optional[str] = parts[0].upper() if parts else None

    # Build a flat lookup: keyword → canonical label
    device_lookup: dict = {}
    for label, keywords in device_kw.items():
        for kw in keywords:
            device_lookup[kw.lower()] = label

    # Step 1: find Device
    device: Optional[str] = None
    device_idx: int = -1
    for i, p in enumerate(parts):
        canonical = device_lookup.get(p.lower())
        if canonical is not None:
            device = canonical
            device_idx = i
            break

    if device_idx == -1:
        # Fallback: no explicit Device keyword found — assume "all" and parse from
        # after the offer token (position 1), so creative/interest/age can still be extracted.
        device = "all"
        device_idx = 1  # skip CountryCode (0) and OfferName (1)

    remaining = parts[device_idx + 1:]

    # Step 2: find Age — first part whose digit content falls in [age_min, age_max].
    # Handles plain "25", "35+", and prefixed forms like "I35" or "A40".
    age: Optional[str] = None
    age_idx: int = -1
    for i, p in enumerate(remaining):
        if "." in p:
            continue
        p_digits = "".join(ch for ch in p if ch.isdigit())
        if p_digits:
            n = int(p_digits)
            if age_min <= n <= age_max:
                # Keep the original token as Age label (e.g. "35+"), normalise prefixed forms
                age = p_digits if not p.rstrip("+").isdigit() else p
                age_idx = i
                break

    # Step 3: find Interest — parts between creative section and Age
    parts_before_age = remaining[:age_idx] if age_idx != -1 else remaining

    interest_parts: list = []
    in_creative_phase = True
    for p in parts_before_age:
        # Skip date-like tokens anywhere (e.g. "28.03", "15.01.2024")
        if "." in p:
            continue
        if in_creative_phase:
            if _is_creative_skip_token(p, creative_prefixes):
                continue
            in_creative_phase = False
        interest_parts.append(p)

    while interest_parts and interest_parts[-1].lower() in _MEDIA_TYPE_TOKENS:
        interest_parts.pop()

    interest = "_".join(interest_parts) if interest_parts else None
    if interest is None:
        interest = _fallback_interest(parts_before_age, adset_name)
    # Normalize AND-separator: `\` → `|` for consistent display
    if interest:
        interest = _normalize_targeting_sep(interest)
    return country_code, device, interest, age


def compute_metrics(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    if USE_LATEST_DATE_ONLY and "Date" in df.columns:
        df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
        latest_date = df["Date"].max()
        if pd.notna(latest_date):
            df = df[df["Date"] == latest_date]

    df["# Leads"] = pd.to_numeric(df["# Leads"], errors="coerce").fillna(0)
    df["# FTDs"] = pd.to_numeric(df["# FTDs"], errors="coerce").fillna(0)
    df["$ Spend"] = pd.to_numeric(df["$ Spend"], errors="coerce").fillna(0.0)
    df["$ Net"] = pd.to_numeric(df["$ Net"], errors="coerce").fillna(0.0)

    df["CPL"] = df["$ Spend"] / df["# Leads"].replace(0, pd.NA)
    df["CPA"] = df["$ Spend"] / df["# FTDs"].replace(0, pd.NA)
    df["CR"] = (df["# FTDs"] / df["# Leads"].replace(0, pd.NA)) * 100.0
    df["Profit"] = df["$ Net"] - df["$ Spend"]
    df["ROI"] = df["$ Net"] / df["$ Spend"].replace(0, pd.NA)

    return df


def enrich_structure(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    # Read parsing config once (not per-row) for efficiency
    device_kw = get_device_keywords()
    age_min, age_max = get_age_range()
    creative_prefixes = get_creative_skip_prefixes()

    parsed = df["Adset Name"].apply(
        lambda x: parse_adset_name(x, device_kw, age_min, age_max, creative_prefixes)
    )
    df["Country Code"] = parsed.apply(lambda x: x[0])
    df["Device"] = parsed.apply(lambda x: x[1])
    df["Interest"] = parsed.apply(lambda x: x[2])
    df["Age"] = parsed.apply(lambda x: x[3])

    # Creative = full Ad Title as-is (includes creative identifier + ad copy text)
    df["Creative"] = df["Ad Title"].fillna("").astype(str).str.strip()

    df["TextSegment"] = df["Ad Title"].apply(normalize_text_segment)

    # Ad Title carries creative id + text name in one cell — expose both
    # halves so the dashboard can show (and group by) them separately.
    _split = df["Ad Title"].apply(split_ad_title)
    df["CreativeId"] = _split.apply(lambda t: t[0])
    df["TextName"] = _split.apply(lambda t: t[1])
    # ACL-1593: вариант креатива (ведущее число файла) — отдельной колонкой.
    df["VariantId"] = df["Ad Title"].apply(lambda t: split_ad_title_variant(t)[1])
    # ACL-1799: ключ креатива с версией рядом с пакетным CreativeId.
    df["CreativeKey"] = df["Ad Title"].apply(lambda t: creative_identity(t)["key"])

    df["Country Final"] = df["Country"]

    # Offer is always taken from Ad Asset column
    df["Offer"] = df["Ad Asset"].fillna("").astype(str).str.strip()

    # Variant = landing page name (column added 2026-08-02; tagging started
    # 2026-07-30, so historical rows are legitimately empty). "-" is used in
    # the sheet as an explicit "no landing" marker — fold it into untagged.
    if "Variant" in df.columns:
        df["Variant"] = df["Variant"].fillna("").astype(str).str.strip()
        df.loc[df["Variant"] == "-", "Variant"] = ""
    else:
        df["Variant"] = ""

    # Log any Interest values not found in Knowledge Base
    _log_unknown_interests(df)

    return df


def _log_unknown_interests(df: pd.DataFrame) -> None:
    """Проверить все уникальные значения Interest против KB и залогировать неизвестные."""
    if "Interest" not in df.columns or "Adset Name" not in df.columns:
        return
    skip_values = {"", "nan", "none", "wide"}
    # ACL-1399: раньше на каждый неизвестный код заново резалась вся книга
    # (astype+strip по 70 тысячам строк) и заново читался журнал — ~15 с на
    # холодной сборке. Пример адсета и журнал берутся один раз.
    present = df["Interest"].notna()
    interests = df.loc[present, "Interest"].astype(str).str.strip()
    first_adset = (
        df.loc[present, "Adset Name"].groupby(interests.values, sort=False).first()
    )
    known = logged_unknown_targets()
    for interest in interests.unique():
        if interest.lower() in skip_values or interest in known:
            continue
        if resolve_targeting_text(interest) is None:
            example = first_adset.get(interest)
            source = "" if example is None or pd.isna(example) else str(example)
            log_unknown_target(interest, source=source, known=known)


def aggregate_combos(df: pd.DataFrame) -> pd.DataFrame:
    group_cols = [
        "Country Final",
        "Offer",
        "Device",
        "Interest",
        "Age",
        "Creative",
        "TextSegment",
    ]

    grouped = (
        df.groupby(group_cols, dropna=False)
        .agg(
            Deposits=("# FTDs", "sum"),
            Leads=("# Leads", "sum"),
            Spend=("$ Spend", "sum"),
            Net=("$ Net", "sum"),
        )
        .reset_index()
    )

    # Attach Country Code (short display code from Adset Name position 0, e.g. MX, CL, GCC)
    if "Country Code" in df.columns:
        code_map = (
            df[["Country Final", "Country Code"]]
            .dropna(subset=["Country Code"])
            .drop_duplicates("Country Final")
            .set_index("Country Final")["Country Code"]
        )
        grouped["Country Code"] = grouped["Country Final"].map(code_map)

    detail_group_cols = group_cols + ["Ad Title"]
    detail = (
        df.groupby(detail_group_cols, dropna=False)
        .agg(TitleDeposits=("# FTDs", "sum"))
        .reset_index()
    )

    if not detail.empty:
        idx = detail.groupby(group_cols)["TitleDeposits"].idxmax()
        top_titles = detail.loc[idx, group_cols + ["Ad Title", "TitleDeposits"]]
        top_titles = top_titles.rename(
            columns={"Ad Title": "TopAdTitle", "TitleDeposits": "TopAdTitleDeposits"}
        )
        grouped = grouped.merge(top_titles, on=group_cols, how="left")

    if "CM" in df.columns and "Account Alias" in df.columns:
        by_cm_acc = (
            df.groupby(group_cols + ["CM", "Account Alias"], dropna=False)
            .agg(FTDs=("# FTDs", "sum"))
            .reset_index()
        )
        if not by_cm_acc.empty:
            idx = by_cm_acc.groupby(group_cols)["FTDs"].idxmax()
            top_ca = by_cm_acc.loc[idx, group_cols + ["CM", "Account Alias"]].rename(
                columns={"CM": "TopCM", "Account Alias": "TopAccount"}
            ).reset_index(drop=True)

            def _row_key(r: pd.Series, cols: list) -> str:
                parts = []
                for c in cols:
                    v = r.get(c)
                    if v is None or (isinstance(v, float) and pd.isna(v)) or (isinstance(v, str) and str(v).strip().lower() == "nan"):
                        parts.append("__NA__")
                    else:
                        parts.append(str(v).strip())
                return "_".join(parts)

            grouped["_mk"] = grouped[group_cols].apply(lambda r: _row_key(r, group_cols), axis=1)
            top_ca["_mk"] = top_ca[group_cols].apply(lambda r: _row_key(r, group_cols), axis=1)
            grouped = grouped.merge(
                top_ca[["_mk", "TopCM", "TopAccount"]],
                on="_mk",
                how="left",
            ).drop(columns=["_mk"], errors="ignore")

    grouped["CPL"] = grouped["Spend"] / grouped["Leads"].replace(0, pd.NA)
    grouped["CPA"] = grouped["Spend"] / grouped["Deposits"].replace(0, pd.NA)
    grouped["CR"] = (grouped["Deposits"] / grouped["Leads"].replace(0, pd.NA)) * 100.0
    grouped["Profit"] = grouped["Net"] - grouped["Spend"]
    grouped["ROI"] = grouped["Net"] / grouped["Spend"].replace(0, pd.NA)

    grouped["Category"] = "Other"
    # STRONG: 2+ deposits, sorted by best CR
    mask_strong = grouped["Deposits"] >= 2
    # INTERESTING: 1 deposit + CR above 5%
    mask_interesting = (grouped["Deposits"] == 1) & (grouped["CR"] > 5.0)
    grouped.loc[mask_strong, "Category"] = "Strong"
    grouped.loc[mask_interesting, "Category"] = "Interesting"

    grouped = grouped[grouped["Category"].isin(["Strong", "Interesting"])]

    return grouped


def print_report(combos: pd.DataFrame, with_context: bool = False) -> None:
    from src.analysis_agent.report_markdown import render_combo_report_md

    print(render_combo_report_md(combos, with_context=with_context))


def print_short_report(combos: pd.DataFrame, top_per_country: int = 2, df: Optional[pd.DataFrame] = None) -> None:
    from src.analysis_agent.report_markdown import render_short_report_md

    print(render_short_report_md(combos, top_per_country=top_per_country, df=df))




def _combo_match_mask(df: pd.DataFrame, row: pd.Series, group_cols: list) -> pd.Series:
    """Маска строк df, совпадающих с комбо row по group_cols."""
    mask = pd.Series(True, index=df.index)
    for col in group_cols:
        if col not in df.columns:
            continue
        rv = row.get(col)
        if rv is None or (isinstance(rv, float) and pd.isna(rv)):
            mask &= df[col].isna()
        else:
            mask &= (df[col] == rv)
    return mask




def _merge_account_substrings(
    accounts_csv: Optional[str], account_substr_repeat: Optional[List[str]]
) -> Optional[List[str]]:
    """Combine --accounts (comma-separated) and repeated --account-substr into one list."""
    subs: List[str] = []
    if accounts_csv:
        subs.extend(s.strip() for s in accounts_csv.split(",") if s.strip())
    if account_substr_repeat:
        subs.extend(s.strip() for s in account_substr_repeat if s and str(s).strip())
    return subs if subs else None


def _parse_focus_countries(csv: Optional[str]) -> Optional[List[str]]:
    """Comma-separated Country values from Excel (matched case-insensitively)."""
    if not csv or not str(csv).strip():
        return None
    return [s.strip() for s in csv.split(",") if s.strip()]


def filter_dataframe_by_focus_countries(df: pd.DataFrame, countries: List[str]) -> pd.DataFrame:
    want = {c.strip().lower() for c in countries if c.strip()}
    if not want:
        return df
    col = df["Country"].fillna("").astype(str).str.strip().str.lower()
    return df[col.isin(want)].copy()


def filter_dataframe_by_account_substrings(
    df: pd.DataFrame, substrings: List[str]
) -> pd.DataFrame:
    """Keep rows where Account Alias contains any substring (case-insensitive, literal match)."""
    col = df["Account Alias"].fillna("").astype(str)
    mask = pd.Series(False, index=df.index)
    for t in substrings:
        mask |= col.str.contains(t, case=False, regex=False)
    return df[mask].copy()


def print_account_breakdown_for_country(df: pd.DataFrame, country_code: str) -> None:
    from src.analysis_agent.report_markdown import render_account_breakdown_md

    text = render_account_breakdown_md(df, country_code)
    if text:
        print(text)


def print_cm_breakdown_for_country(df: pd.DataFrame, country_code: str) -> None:
    from src.analysis_agent.report_markdown import render_cm_breakdown_md

    print(render_cm_breakdown_md(df, country_code))


def print_offer_comparison_and_top_campaigns(
    df: pd.DataFrame, combos: pd.DataFrame, country_name: str, top_n: int = 2
) -> None:
    from src.analysis_agent.report_markdown import render_offer_comparison_md

    print(render_offer_comparison_md(df, combos, country_name, top_n=top_n))


def main(  # noqa: PLR0913
    excel_path: Optional[str] = None,
    country_offer: str = "all",
    top_n: int = 2,
    output_path: Optional[str] = None,
    short_report: bool = False,
    with_context: bool = False,
    asset_filter: Optional[str] = None,
    account_substrings: Optional[List[str]] = None,
    focus_countries: Optional[List[str]] = None,
) -> None:
    excel_path = excel_path or _get_excel_path()
    if not os.path.exists(excel_path):
        raise FileNotFoundError(f"Excel file not found: {excel_path}")

    filter_lines: List[str] = []

    def build_report_text() -> str:
        df = pd.read_excel(excel_path)

        if asset_filter:
            mask = df["Ad Asset"].fillna("").astype(str).str.lower() == asset_filter.lower()
            df_filtered = df[mask]
            if df_filtered.empty:
                lines = [
                    f"[!] No rows found for asset '{asset_filter}'. Check the name.",
                    f"    Available assets: {', '.join(str(x) for x in sorted(df['Ad Asset'].dropna().unique()))}",
                ]
                return build_full_report_markdown(
                    df.head(0),
                    pd.DataFrame(),
                    country_offer=country_offer,
                    top_n=top_n,
                    short_report=short_report,
                    with_context=with_context,
                    account_substrings=account_substrings,
                    filter_preamble="\n".join(filter_lines + lines),
                    source_excel_path=excel_path,
                )
            filter_lines.append(
                f"[filter] Asset='{asset_filter}': {len(df_filtered)} rows"
            )
            df = df_filtered

        if account_substrings:
            n_before = len(df)
            df = filter_dataframe_by_account_substrings(df, account_substrings)
            if df.empty:
                lines = [
                    "[!] No rows match account filter (Account Alias must contain one of: "
                    f"{', '.join(account_substrings)}).",
                ]
                return build_full_report_markdown(
                    df,
                    pd.DataFrame(),
                    country_offer=country_offer,
                    top_n=top_n,
                    short_report=short_report,
                    with_context=with_context,
                    account_substrings=account_substrings,
                    filter_preamble="\n".join(filter_lines + lines),
                    source_excel_path=excel_path,
                )
            filter_lines.append(
                f"[filter] Account substrings ({len(account_substrings)} patterns): "
                f"{n_before} -> {len(df)} rows"
            )

        if focus_countries:
            n_before = len(df)
            df = filter_dataframe_by_focus_countries(df, focus_countries)
            if df.empty:
                lines = [
                    "[!] No rows left after --focus-countries filter "
                    f"(expected Country in: {', '.join(focus_countries)}).",
                ]
                return build_full_report_markdown(
                    df,
                    pd.DataFrame(),
                    country_offer=country_offer,
                    top_n=top_n,
                    short_report=short_report,
                    with_context=with_context,
                    account_substrings=account_substrings,
                    filter_preamble="\n".join(filter_lines + lines),
                    source_excel_path=excel_path,
                )
            filter_lines.append(
                f"[filter] Focus countries ({len(focus_countries)}): "
                f"{n_before} -> {len(df)} rows"
            )

        required_cols = [
            "Country",
            "Account Alias",
            "Adset Name",
            "Ad Title",
            "# Leads",
            "# FTDs",
            "$ Spend",
            "CM",
            "Ad Asset",
            "$ Net",
        ]
        missing = [c for c in required_cols if c not in df.columns]
        if missing:
            raise ValueError(f"Missing expected columns in Excel: {missing}")

        df = compute_metrics(df)
        from src.creative_variant import apply_backfill

        df = apply_backfill(enrich_structure(df))
        combos = aggregate_combos(df)
        preamble = "\n".join(filter_lines)
        return build_full_report_markdown(
            df,
            combos,
            country_offer=country_offer,
            top_n=top_n,
            short_report=short_report,
            with_context=with_context,
            account_substrings=account_substrings,
            filter_preamble=preamble,
            source_excel_path=excel_path,
        )

    text = build_report_text()
    if output_path:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"Report saved to {output_path}", file=sys.stderr)
    else:
        print(text)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="FB campaigns analysis: combos by country, CM breakdown, offer comparison."
    )
    parser.add_argument(
        "--excel", "-e",
        type=str,
        default=None,
        help="Path to Excel file (overrides FB_STATS_EXCEL env and default)",
    )
    parser.add_argument(
        "--country", "-c",
        type=str,
        default="all",
        help="Country for offer comparison and top campaigns, or 'all' for every country (default: all)",
    )
    parser.add_argument(
        "--top", "-t",
        type=int,
        default=2,
        help="Number of top campaigns per offer to recommend (default: 2)",
    )
    parser.add_argument(
        "--output", "-o",
        type=str,
        default=None,
        metavar="FILE",
        help="Save full report to FILE instead of printing to console",
    )
    parser.add_argument(
        "--short", "-s",
        action="store_true",
        help="Short report: top N combos per country only (N from --top, default 2)",
    )
    parser.add_argument(
        "--with-context",
        action="store_true",
        dest="with_context",
        help="Show target context from Knowledge Base below each combo stats line",
    )
    parser.add_argument(
        "--asset", "-a",
        type=str,
        default=None,
        help="Filter by Ad Asset name (e.g. 'Adnoc', 'Amazon'). Case-insensitive.",
    )
    parser.add_argument(
        "--accounts",
        type=str,
        default=None,
        help="Comma-separated substrings: keep rows where Account Alias contains any (case-insensitive).",
    )
    parser.add_argument(
        "--account-substr",
        action="append",
        default=None,
        dest="account_substr",
        metavar="SUBSTR",
        help="Same as substring in --accounts; repeat flag for multiple patterns.",
    )
    parser.add_argument(
        "--focus-countries",
        type=str,
        default=None,
        help=(
            "Comma-separated values of Excel column Country to keep (case-insensitive). "
            "Use when the same Account Alias appears in multiple geos."
        ),
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    acc_subs = _merge_account_substrings(
        getattr(args, "accounts", None),
        getattr(args, "account_substr", None),
    )
    fc = _parse_focus_countries(getattr(args, "focus_countries", None))
    main(
        excel_path=args.excel,
        country_offer=args.country,
        top_n=args.top,
        output_path=args.output,
        short_report=getattr(args, "short", False),
        with_context=getattr(args, "with_context", False),
        asset_filter=getattr(args, "asset", None),
        account_substrings=acc_subs,
        focus_countries=fc,
    )
