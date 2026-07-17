from pathlib import Path

from src.fb_ads_rpa.variations.catalog import (
    default_strategies,
    load_steps_catalog,
    step_risk,
)


def test_load_repo_catalog_has_conversion_risky():
    cat = load_steps_catalog()
    assert "conversion_location_website" in cat["steps"]
    assert step_risk("conversion_location_website", cat) == "risky"
    assert step_risk("finish_draft", cat) == "safe"
    assert "Publish" not in str(cat).lower() or "not clicked" in str(cat["steps"]["finish_draft"]).lower()


def test_default_strategies_order():
    cat = load_steps_catalog()
    strats = default_strategies("conversion_location_website", cat)
    assert strats[0] == "recipe_space"
    assert "teach_mode" in strats
