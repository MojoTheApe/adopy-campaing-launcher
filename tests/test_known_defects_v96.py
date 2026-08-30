"""The two defects recorded in the known-good tag, as tests.

`rpa-v96-spend-confirmed-2026-08-24` is the point we call good — it is the state
that produced the first live campaign. Its own message lists two things that
were wrong at the time:

  1. "прогон рапортует success=true даже если у объявления не встал креатив
     (ad_level_fails не доходят до RpaPublishResult)"
  2. "ad_account_map.csv строка 15: FacebookElf303 помечен PA Amz, фактически
     Chile/BCI"

A defect written into a tag message is a note. These are the tests, so that
neither can come back unnoticed (audit finding F-15). Both are written to fail
against the old behaviour: the first would pass trivially if the classification
step were removed, so it asserts `publishable` is False rather than merely that
some list is non-empty.
"""
from __future__ import annotations

from src.analysis_agent.account_label_check import disagreements, label_country_code
from src.fb_ads_rpa.ads_manager_flow import apply_step_failures
from src.fb_ads_rpa.draft import RpaPublishResult


# ── Defect 1: a run reported success with an ad that had no creative ─────────

def _reached_ad_level() -> RpaPublishResult:
    """A run that got all the way to the ad level without an outright failure.

    This is the shape the old code produced right before it reported success:
    every step "done", nothing raised.
    """
    r = RpaPublishResult(campaign_name="KW_TRL_All_IMG_x1_25_24.08")
    r.success = True
    r.steps_completed.append("recorded_flow:reached_ad")
    return r


def test_a_run_whose_creative_failed_is_not_publishable():
    """The defect itself: success=True while the ad has no media."""
    result = apply_step_failures(_reached_ad_level(), ["ad_creative_upload"])

    assert result.success is True, (
        "success должен остаться true: черновик существует, и переворот флага "
        "спровоцировал бы повторный прогон, создающий дубль"
    )
    assert result.publishable is False, (
        "объявление без медиа не должно считаться пригодным к публикации — "
        "оператор узнал бы об этом только после публикации"
    )
    assert "ad_creative_upload" in result.blocking_failures


def test_the_operator_is_told_what_failed_and_why():
    """A verdict with no reason sends someone to read the code."""
    result = apply_step_failures(_reached_ad_level(), ["ad_creative_upload"])
    assert "DO NOT PUBLISH" in (result.error or "")
    assert "no media" in (result.error or ""), "в сообщении нет причины, только имя шага"


def test_later_ads_are_covered_too():
    """The second and later ads carry a tag prefix — ad2_creative_upload."""
    result = apply_step_failures(_reached_ad_level(), ["ad2_creative_upload"])
    assert result.publishable is False


def test_a_wider_audience_is_a_warning_not_a_block():
    """Not everything that failed makes a draft unsafe.

    A test that blocked on everything would pass the two above while making the
    whole mechanism useless — every run would say "do not publish".
    """
    result = apply_step_failures(_reached_ad_level(), ["audience_age", "languages"])
    assert result.publishable is True
    assert result.warnings == ["audience_age", "languages"]
    assert result.blocking_failures == []


def test_the_verdict_reaches_the_dashboard():
    """The result is serialised for the job feed; a verdict that stays in the
    process helps nobody."""
    result = apply_step_failures(_reached_ad_level(), ["destination_sweep:no url"])
    job = result.to_job_dict()
    assert job["publishable"] is False
    assert job["blocking_failures"] == ["destination_sweep:no url"]


# ── Defect 2: an account label claiming the wrong country ────────────────────

def test_the_recorded_mislabel_is_detected():
    """FacebookElf303 was labelled PA (Panama) while its data is Chile."""
    found = disagreements([
        {"label": "FacebookElf303 PA Amz", "country_name": "Chile"},
    ])
    assert len(found) == 1
    assert found[0]["claims"] == "Panama"
    assert found[0]["actually"] == "Chile"


def test_an_agreeing_label_is_not_reported():
    assert disagreements([{"label": "FacebookUL921 CR Ait", "country_name": "Costa Rica"}]) == []


def test_a_label_without_a_country_claims_nothing():
    """Silence is not a disagreement — most labels simply do not say."""
    assert label_country_code("FacebookMKTOBFLW07") == ""
    assert disagreements([{"label": "FacebookMKTOBFLW07", "country_name": "Kuwait"}]) == []


def test_the_asset_token_is_not_read_as_a_country():
    """`Ait`, `Amz`, `ML` are assets. Reading one as a country code would make
    every annotated account look mislabelled and bury the real cases."""
    assert label_country_code("FacebookMKTOBFLV37 EC Ait") == "EC"
    assert disagreements([{"label": "FacebookMKTOBFLV37 EC Ait", "country_name": "Ecuador"}]) == []


def test_accounts_without_data_are_not_accused():
    """A newly connected account has no stats yet; that is not a mismatch."""
    assert disagreements([{"label": "FacebookElf303 PA Amz", "country_name": ""}]) == []
