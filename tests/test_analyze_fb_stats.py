"""Tests for parsing FB stats dimensions from ad set names."""
from __future__ import annotations

from src.analysis_agent.analyze_fb_stats import parse_adset_name


DEVICE_KW = {"all": ["all"], "ios": ["ios"]}
CREATIVE_PREFIXES = ["vid", "video", "image", "images", "img", "photo", "creativ", "mix"]


def test_parse_adset_name_drops_media_type_after_targeting() -> None:
    parsed = parse_adset_name(
        "SA_ARM_all_Day_img_25+",
        DEVICE_KW,
        age_min=18,
        age_max=65,
        creative_prefixes=CREATIVE_PREFIXES,
    )

    assert parsed == ("SA", "all", "Day", "25+")


def test_parse_adset_name_keeps_multi_token_targeting_without_media_suffix() -> None:
    parsed = parse_adset_name(
        "UY_AIT_All_Videos_Blue_Day_35+_23.3_1",
        DEVICE_KW,
        age_min=18,
        age_max=65,
        creative_prefixes=CREATIVE_PREFIXES,
    )

    assert parsed == ("UY", "all", "Blue_Day", "35+")


def test_parse_adset_name_drops_mix_media_type_before_targeting() -> None:
    parsed = parse_adset_name(
        "KW_Trl_All_Mix_Day_25_12.05",
        DEVICE_KW,
        age_min=18,
        age_max=65,
        creative_prefixes=CREATIVE_PREFIXES,
    )

    assert parsed == ("KW", "all", "Day", "25")


def test_parse_adset_name_drops_mix_media_type_after_targeting() -> None:
    parsed = parse_adset_name(
        "KW_Trl_All_Day_Mix_25_12.05",
        DEVICE_KW,
        age_min=18,
        age_max=65,
        creative_prefixes=CREATIVE_PREFIXES,
    )

    assert parsed == ("KW", "all", "Day", "25")
