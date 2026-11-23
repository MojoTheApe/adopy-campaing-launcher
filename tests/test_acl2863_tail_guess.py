"""ACL-2863: хвостовая догадка не отдаёт числа чужого аккаунта."""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.web import app as web_app  # noqa: E402


def _df(*aliases, country="Ecuador"):
    return pd.DataFrame({"Country Final": [country] * len(aliases), "Account Alias": list(aliases)})


def test_person_name_annotation_does_not_make_the_tail():
    known = {"Taboola_Bloomed": {}}
    assert web_app._alias_by_tail("FacebookDoc446 - Khalid Ahmed", known) == ""
    df = _df("Taboola_Bloomed", "FacebookOther111")
    label = "FacebookDoc446 - Khalid Ahmed"
    assert web_app.workbook_alias_for(df, "Ecuador", label) == label


def test_tail_comes_from_the_own_name_when_annotated():
    known = {"FacebookDoc446": {}}
    assert web_app._alias_by_tail("Doc446 - Khalid Ahmed", known) == "FacebookDoc446"


def test_letters_only_tail_never_matches():
    assert web_app._alias_by_tail("FacebookBloomed", {"Taboola_Bloomed": {}, "FacebookXBloomed": {}}) == ""
    assert web_app._alias_by_tail("Acme_Ahmed", {"Legacy_Ahmed": {}}) == ""


def test_facebook_label_never_takes_a_non_facebook_alias():
    assert web_app._alias_by_tail("FacebookDoc446", {"Taboola_Doc446": {}}) == ""
    assert web_app._alias_by_tail("KLD_BFL_X84", {"Profiles_X84": {}}) == ""


def test_existing_rules_unchanged():
    assert web_app._alias_by_tail("KLD_BFL_X84", {"FacebookMKTOBFLX84": {}}) == "FacebookMKTOBFLX84"
    df = _df("Facebook_5265")
    assert web_app.workbook_alias_for(df, "Ecuador", "Facebook_5265_61579362958966") == "Facebook_5265"
    assert web_app._alias_by_tail("KLD_BFL_X82", {"FacebookOneX82": {}, "FacebookTwoX82": {}}) == ""
