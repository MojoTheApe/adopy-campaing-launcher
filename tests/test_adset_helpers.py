"""Send-to-Builder alias resolution: 'trade'/'trading_target'/'trader' -> canonical 'Trading'."""

from __future__ import annotations

from src.web.adset_helpers import parse_adset_for_builder, resolve_interest_alias


def test_resolve_interest_alias_matches_known_alias():
    result = resolve_interest_alias("trade")
    assert result["matched"] is True
    assert result["code"] == "Trading"
    assert result["label"] == "Trading (Generic)"
    assert "Day trading" in result["fb_interest"]


def test_resolve_interest_alias_is_case_insensitive():
    result = resolve_interest_alias("Trader")
    assert result["matched"] is True
    assert result["code"] == "Trading"


def test_resolve_interest_alias_canonical_key_passthrough():
    result = resolve_interest_alias("Trading")
    assert result["matched"] is True
    assert result["code"] == "Trading"


def test_resolve_interest_alias_unknown_text():
    result = resolve_interest_alias("nonexistent_xyz_target")
    assert result["matched"] is False
    assert result["code"] is None
    assert result["raw"] == "nonexistent_xyz_target"


def test_resolve_interest_alias_empty_text():
    result = resolve_interest_alias("")
    assert result["matched"] is False
    assert result["raw"] == ""


def test_resolve_interest_alias_compound_pipe_resolves_both_parts():
    # "Day" and "SM" are both real KB keys (Day Trading / Stock Market) —
    # a bare single-key lookup of "Day|SM" always failed before, since
    # compound AND-groups are never modeled as their own KB entries.
    result = resolve_interest_alias("Day|SM")
    assert result["matched"] is True
    assert result["code"] == "Day|SM"


def test_resolve_interest_alias_compound_slash_same_as_pipe():
    # `/` is a third separator seen in real ad set names, equivalent to `|`.
    result = resolve_interest_alias("Day/SM")
    assert result["matched"] is True
    assert result["code"] == "Day|SM"


def test_resolve_interest_alias_compound_backslash_same_as_pipe():
    result = resolve_interest_alias("Day\\SM")  # linux-ok: a label with a backslash as plain text
    assert result["matched"] is True
    assert result["code"] == "Day|SM"


def test_resolve_interest_alias_compound_fails_if_any_part_unresolved():
    result = resolve_interest_alias("Day|nonexistent_xyz")
    assert result["matched"] is False


# ── Д4: словарный fallback (KB-ключ внутри мусора парсинга) ────────────────


def test_fuzzy_prefix_noise_is_stripped():
    # "AAY_" — префикс аккаунта/оффера, приклеенный к таргетингу; позиционный
    # парс отдаёт весь токен целиком, точный резолв всегда мимо.
    result = resolve_interest_alias("AAY_Day")
    assert result["matched"] is True
    assert result["code"] == "Day"


def test_fuzzy_space_separated_prefix():
    result = resolve_interest_alias("2046 Day")
    assert result["matched"] is True
    assert result["code"] == "Day"


def test_fuzzy_compound_with_age_and_variant_noise():
    # Реальный кейс из аудита (76 строк): возраст и суффикс внутри части.
    result = resolve_interest_alias("HV|Trading_40-64_Nokit")
    assert result["matched"] is True
    assert result["code"] == "HV|Trading"


def test_fuzzy_device_part_is_dropped_not_failed():
    # "/iOS" делает девайс отдельной частью компаунда; она отбрасывается как
    # мусор, а не проваливает значение.
    result = resolve_interest_alias("FX|Day/iOS")
    assert result["matched"] is True
    assert result["code"] == "FX|Day"


def test_fuzzy_pure_noise_value_stays_unmatched():
    result = resolve_interest_alias("iOS")
    assert result["matched"] is False


def test_fuzzy_variant_suffix_resolves_to_sheet_profile():
    # TradingYB добавлен пользователем в шит как самостоятельный профиль
    # (2026-07-30) — суффикс варианта "_V1" отбрасывается границей слова.
    result = resolve_interest_alias("TradingYB_V1")
    assert result["matched"] is True
    assert result["code"] == "TradingYB"


def test_fuzzy_boundary_guard_no_match_inside_word():
    # "Trading"/"trade" внутри "TradeYB" НЕ на границе слова — TradeYB
    # остаётся нерезолвнутым (такого профиля нет), автоматом не приравнивается.
    result = resolve_interest_alias("TradeYB")
    assert result["matched"] is False


def test_fuzzy_real_kb_gap_stays_unmatched():
    # Investment/TRD — реальные пробелы KB (открытый вопрос), не мусор.
    result = resolve_interest_alias("TRD|Investment/iOS")
    assert result["matched"] is False


def test_fuzzy_two_char_keys_not_scanned_inside_noise():
    # "ES" в имени — языковой маркер; ключ ES (Engaged Shoppers) не должен
    # выуживаться сканом из "ES_Lg_CL" (2-буквенные — только целой частью).
    result = resolve_interest_alias("ES_Lg_CL")
    assert result["matched"] is False


def test_fuzzy_compound_dedupes_repeated_code():
    result = resolve_interest_alias("Day|daytrading")
    assert result["matched"] is True
    assert result["code"] == "Day"


def test_parse_adset_for_builder_resolves_alias_from_full_name():
    # Format: CountryCode_OfferName_Device_Creative_Interest_Age[_Date]
    # "video" (bare media-type token, no numeric suffix) is stripped as
    # creative/not-targeting per _MEDIA_TYPE_TOKENS, leaving "Trade" as the
    # isolated interest token — see analyze_fb_stats._is_creative_skip_token.
    result = parse_adset_for_builder("KW_Casino_iOS_video_Trade_35_15.07")
    assert result["country_code"] == "KW"
    assert result["device"] == "ios"
    assert result["age"] == "35"
    assert result["targeting"]["matched"] is True
    assert result["targeting"]["code"] == "Trading"


def test_parse_adset_for_builder_empty_name():
    result = parse_adset_for_builder("")
    assert result["targeting"]["matched"] is False
