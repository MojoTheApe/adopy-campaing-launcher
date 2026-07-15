import json
from pathlib import Path

from src.fb_ads_rpa.variations.ranks import StrategyRanks


def test_safe_success_without_next_step_does_not_promote(tmp_path: Path):
    path = tmp_path / "ranks.json"
    path.write_text(json.dumps({"version": 1, "steps": {}, "pending_approval": []}), encoding="utf-8")
    ranks = StrategyRanks.load(path)
    ranks.log_outcome("campaign_name_budget", "fill_campaign_name", ok=True)
    ranks.save()
    data = json.loads(path.read_text(encoding="utf-8"))
    # logged but order not yet promoted to top until gate
    assert any(e["strategy_id"] == "fill_campaign_name" for e in data.get("outcome_log", [])[-5:])
    order = ranks.ordered_strategies("campaign_name_budget", ["fill_daily_budget", "fill_campaign_name"])
    # without mark_next_step_ok, keep fallback order
    assert order == ["fill_daily_budget", "fill_campaign_name"]


def test_safe_promote_after_next_step_gate(tmp_path: Path):
    path = tmp_path / "ranks.json"
    path.write_text(json.dumps({"version": 1, "steps": {}, "pending_approval": []}), encoding="utf-8")
    ranks = StrategyRanks.load(path)
    ranks.log_outcome("nav_ad_set", "sidebar_ad_set", ok=True)
    ranks.mark_next_step_ok("nav_ad_set")  # hybrid gate passed
    ranks.save()
    order = ranks.ordered_strategies("nav_ad_set", ["breadcrumb_ad_set", "sidebar_ad_set"])
    assert order[0] == "sidebar_ad_set"


def test_risky_success_goes_pending_not_promoted(tmp_path: Path):
    path = tmp_path / "ranks.json"
    path.write_text(json.dumps({"version": 1, "steps": {}, "pending_approval": []}), encoding="utf-8")
    ranks = StrategyRanks.load(path)
    ranks.log_outcome("conversion_location_website", "keyboard_space_enter", ok=True)
    ranks.mark_next_step_ok("conversion_location_website")
    ranks.save()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert any(p["strategy_id"] == "keyboard_space_enter" for p in data["pending_approval"])
    order = ranks.ordered_strategies(
        "conversion_location_website",
        ["recipe_space", "keyboard_space_enter"],
    )
    assert order[0] == "recipe_space"  # unchanged until approve


def test_approve_pending_promotes_risky(tmp_path: Path):
    path = tmp_path / "ranks.json"
    path.write_text(json.dumps({"version": 1, "steps": {}, "pending_approval": []}), encoding="utf-8")
    ranks = StrategyRanks.load(path)
    ranks.log_outcome("conversion_location_website", "keyboard_space_enter", ok=True)
    assert ranks.approve_pending("conversion_location_website", "keyboard_space_enter")
    ranks.save()
    order = ranks.ordered_strategies(
        "conversion_location_website",
        ["recipe_space", "keyboard_space_enter"],
    )
    assert order[0] == "keyboard_space_enter"


def test_browser_crash_does_not_learn(tmp_path: Path):
    path = tmp_path / "ranks.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "steps": {
                    "nav_ad_set": {
                        "strategies": [
                            {
                                "id": "sidebar_ad_set",
                                "wins": 2,
                                "fails": 1,
                                "score": 3,
                            }
                        ]
                    }
                },
                "pending_approval": [],
            }
        ),
        encoding="utf-8",
    )
    ranks = StrategyRanks.load(path)
    ranks.log_outcome("nav_ad_set", "sidebar_ad_set", ok=False, browser_crash=True)
    ranks.log_outcome("conversion_location_website", "keyboard_space_enter", ok=True, browser_crash=True)
    ranks.mark_next_step_ok("nav_ad_set")
    ranks.save()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert [
        (entry["step_id"], entry["strategy_id"], entry["ok"], entry.get("browser_crash"))
        for entry in data["outcome_log"]
    ] == [
        ("nav_ad_set", "sidebar_ad_set", False, True),
        ("conversion_location_website", "keyboard_space_enter", True, True),
    ]
    assert data["pending_safe"] == {}
    assert data["pending_approval"] == []
    assert data["steps"]["nav_ad_set"]["strategies"][0] == {
        "id": "sidebar_ad_set",
        "wins": 2,
        "fails": 1,
        "score": 3,
    }


def test_ordered_strategies_filters_publish_ranked_and_fallback(tmp_path: Path):
    path = tmp_path / "ranks.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "steps": {
                    "finish_draft": {
                        "strategies": [
                            {"id": "safe_review", "wins": 0, "fails": 0, "score": 1},
                            {"id": "ClickPublishButton", "wins": 10, "fails": 0, "score": 99},
                        ]
                    }
                },
                "pending_approval": [],
            }
        ),
        encoding="utf-8",
    )
    ranks = StrategyRanks.load(path)
    order = ranks.ordered_strategies(
        "finish_draft",
        ["fallback_publish", "safe_review", "review_only"],
    )
    assert order == ["safe_review", "review_only"]


def test_reject_publish_strategy(tmp_path: Path):
    path = tmp_path / "ranks.json"
    path.write_text(json.dumps({"version": 1, "steps": {}, "pending_approval": []}), encoding="utf-8")
    ranks = StrategyRanks.load(path)
    try:
        ranks.log_outcome("finish_draft", "click_publish", ok=True)
        raised = False
    except ValueError:
        raised = True
    assert raised
