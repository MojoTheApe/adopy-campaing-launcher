from src.campaign_bridge.open_rec_mapper import open_rec_to_plan_rows, parse_creative_from_ad_title


def test_parse_creative_from_ad_title() -> None:
    assert parse_creative_from_ad_title("2046 - Text 4") == ("Image", "20464")
    assert parse_creative_from_ad_title("Video 99 promo") == ("Video", "99")


def test_open_rec_to_plan_rows_shared_campaign_name() -> None:
    rec = {
        "campaign_name": "EC_Ait_All_Video_Day_35+_08.06",
        "offer": "AI Trader",
        "targeting": "Day",
        "device": "all",
        "age": "35+",
        "creatives": [{"name": "2046 - Text 4"}],
    }
    rows = open_rec_to_plan_rows(
        rec,
        account_label="FacebookUL574 EC Fav",
        country="Ecuador",
        campaign_name="EC_Custom_Name_08.06",
        ad_titles=["2046 - Text 4", "2047 - Video 2", ""],
    )
    assert len(rows) == 2
    assert all(r["campaign_name"] == "EC_Custom_Name_08.06" for r in rows)
    assert all(r["ad_set_name"] == "EC_Custom_Name_08.06" for r in rows)
    assert rows[0]["copy"] == "2046 - Text 4"
    assert rows[0]["creative_number"] == "20464"
    assert rows[1]["creative"] == "Video"
