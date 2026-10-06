"""ACL-1653: явное правило имён FacebookMKTOBFL<id> = KLD_BFL_<id>."""
from __future__ import annotations

import json

import pandas as pd
import pytest

from src.analysis_agent import account_names as names
from src.analysis_agent.analyze_fb_stats import alias_matches, normalize_account_alias
from src.fb_ads_rpa.mlx_profiles import profile_name_key
from src.web import account_links
from src.web import app as web_app


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(account_links, "get_local_config_dir", lambda: str(tmp_path))
    monkeypatch.setattr(account_links, "get_ad_account_map_path", lambda: str(tmp_path / "nope.csv"))
    monkeypatch.setattr(web_app, "get_ad_account_map_path", lambda: str(tmp_path / "nope.csv"))
    monkeypatch.setattr(web_app, "_account_status", lambda label: {})


def test_rule_maps_both_directions_and_any_id_length():
    assert normalize_account_alias("FacebookMKTOBFLB98") == normalize_account_alias("KLD_BFL_B98")
    assert normalize_account_alias("FacebookMKTOBFLB98") == "KLD_BFL_B98"
    assert names.account_key("facebookmktobflB12345") == names.account_key("KLD_BFL_b12345")


def test_sheet_labels_with_country_and_asset_words():
    assert normalize_account_alias("FacebookMKTOBFLW27 SV Ait") == "KLD_BFL_W27"
    s = pd.Series(["FacebookMKTOBFLW27", "FacebookMKTOBFLB98"])
    assert alias_matches(s, "KLD_BFL_W27 SV Ait").tolist() == [True, False]


def test_different_ids_never_match():
    assert not names.exact_alias("KLD_BFL_B98", ["FacebookMKTOBFLB99", "FacebookMKTOBFLB9"])
    assert normalize_account_alias("KLD_BFL_B98") != normalize_account_alias("FacebookMKTOBFLB99")
    s = pd.Series(["FacebookMKTOBFLB99"])
    assert not alias_matches(s, "KLD_BFL_B98").any()


def test_explicit_rule_beats_tail_guess():
    # у «FacebookOtherB98» тот же хвост B98, но правило называет точное имя
    known = {"FacebookOtherB98": {}, "FacebookMKTOBFLB98": {}}
    assert web_app._alias_by_tail("KLD_BFL_B98", known) == "FacebookMKTOBFLB98"
    # без правила тот же хвост был бы неоднозначным и отказал бы
    assert web_app._alias_by_tail("KLD_X_B98", {"FacebookOtherB98": {}, "FacebookTwoB98": {}}) == ""


def test_profile_key_uses_rule_suffix():
    assert profile_name_key("KLD_BFL_B98") == "B98"
    assert profile_name_key("FacebookMKTOBFLB98 SV Ait") == "B98"
    assert profile_name_key("KLD_BFL_BX1234") == "BX1234"


def test_more_rules_come_from_config(monkeypatch, tmp_path):
    cfg = tmp_path / "rules.json"
    cfg.write_text(json.dumps({"rules": [{"prefixes": ["Acme_", "FacebookACME"]}]}), encoding="utf-8")
    monkeypatch.setattr(names, "_RULES_PATH", cfg)
    names._CACHE["mtime"] = None  # без monkeypatch: откат вернул бы старый mtime к чужим правилам
    try:
        assert normalize_account_alias("FacebookACMEZ5") == "Acme_Z5"
        assert names.exact_alias("Acme_Z5", ["FacebookACMEZ5"]) == "FacebookACMEZ5"
    finally:
        names._CACHE["mtime"] = None


def test_home_row_for_b96_takes_workbook_numbers_without_guess_mark():
    df = pd.DataFrame({
        "Country Final": ["Argentina"], "Account Alias": ["FacebookMKTOBFLB96"],
        "Date": ["2026-10-02"], "Adset Name": ["a"], "Ad Title": ["t"], "Ad Asset": ["x"],
        "CM": ["c"], "# FTDs": [1], "# Leads": [3], "$ Spend": [40.0],
    })
    account_links.link("KLD_BFL_B96", "country", "Argentina")
    view = web_app._build_account_map_view({}, df, "2026-10-01", "2026-10-05")
    row = view["countries"][0]["rows"][0]
    assert row["account"] == "KLD_BFL_B96"
    assert row["has_data"] and row["spend"] == 40.0
    assert row["workbook_alias"] == "FacebookMKTOBFLB96"
    assert row["workbook_alias_exact"] is True, "точное совпадение не должно помечаться как догадка"
    assert web_app.workbook_alias_for(df, "Argentina", "KLD_BFL_B96") == "FacebookMKTOBFLB96"


def test_alias_map_for_builder_knows_both_names():
    assert names.rule_alternates("FacebookMKTOBFLB98 SV") == ["KLD_BFL_B98"]
    assert names.rule_alternates("KLD_BFL_B98") == ["FacebookMKTOBFLB98"]
