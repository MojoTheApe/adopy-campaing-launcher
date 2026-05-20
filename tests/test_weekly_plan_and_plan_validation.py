"""Unit tests for WeeklyPlanInput and plan validation."""

from __future__ import annotations

from pathlib import Path

from src.content_machine.plan_validation import (
    validate_plan_entries,
    validate_week_offer_cap,
)
from src.content_machine.schema import AudienceSegment, CampaignGoal, ContentItem, ContentStatus, ContentType
from src.content_machine.weekly_plan_input import WeeklyPlanInput, load_weekly_plan_input


def test_weekly_plan_from_dict_nested_strings() -> None:
    w = WeeklyPlanInput.from_dict({"weekly_goals": "one goal", "forbidden_topics": "topic a"})
    assert w.weekly_goals == ["one goal"]
    assert w.forbidden_topics == ["topic a"]


def test_weekly_plan_max_caps() -> None:
    raw = {"max_offer_posts_per_day": "2", "max_offer_posts_per_week": 4}
    w = WeeklyPlanInput.from_dict(raw)
    assert w.max_offer_posts_per_day == 2
    assert w.max_offer_posts_per_week == 4


def test_weekly_plan_prompt_block_es() -> None:
    w = WeeklyPlanInput(
        weekly_goals=["g1"],
        forbidden_topics=["f1"],
        exclude_dates=["2099-01-01"],
        notes="n",
    )
    blk = w.to_prompt_block(lang_es=True, lang_ru=False)
    assert "g1" in blk
    assert "f1" in blk
    assert "2099-01-01" in blk


def test_load_weekly_plan_example_json(tmp_path: Path) -> None:
    p = Path(__file__).resolve().parents[1] / "config" / "content_plan_week.example.json"
    assert p.is_file()
    w = load_weekly_plan_input(p)
    assert len(w.weekly_goals) >= 1


def test_validate_consecutive_offers() -> None:
    day = "2026-05-10"
    items = [
        _item(day, ContentType.SALES.value),
        _item(day, ContentType.COPYTRADING_OFFER.value),
    ]
    issues = validate_plan_entries(day, items, max_offer_posts_per_day=None)
    assert any("consecutive offer" in x for x in issues)


def test_validate_max_per_day() -> None:
    day = "2026-05-10"
    items = [
        _item(day, ContentType.SALES.value),
        _item(day, ContentType.EDUCATION.value),
        _item(day, ContentType.SALES.value),
    ]
    issues = validate_plan_entries(day, items, max_offer_posts_per_day=1)
    assert any("too many commercial" in x for x in issues)


def test_validate_week_cap_warning() -> None:
    items = [_item("2026-05-10", ContentType.SALES.value) for _ in range(6)]
    msgs = validate_week_offer_cap(items, 3)
    assert len(msgs) == 1


def _item(day: str, ctype: str) -> ContentItem:
    return ContentItem(
        status=ContentStatus.BRIEF.value,
        content_type=ctype,
        campaign_goal=CampaignGoal.CONVERSION.value,
        audience_segment=AudienceSegment.FREE_SUBSCRIBERS.value,
        topic="t",
        brief="b",
        calendar_slot=day,
    )
