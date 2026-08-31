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

from src.analysis_agent.account_country import country_from_stats, resolve_countries
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


def test_a_wider_audience_is_not_publishable():
    """Requested age/language failures change spend and must fail closed."""
    result = apply_step_failures(_reached_ad_level(), ["audience_age", "languages"])
    assert result.publishable is False
    assert result.warnings == []
    assert result.blocking_failures == ["audience_age", "languages"]


def test_the_verdict_reaches_the_dashboard():
    """The result is serialised for the job feed; a verdict that stays in the
    process helps nobody."""
    result = apply_step_failures(_reached_ad_level(), ["destination_sweep:no url"])
    job = result.to_job_dict()
    assert job["publishable"] is False
    assert job["blocking_failures"] == ["destination_sweep:no url"]


# ── Defect 2: an account attributed to the wrong country ────────────────────
#
# The tag recorded it as "FacebookElf303 помечен PA Amz, фактически Chile/BCI",
# and the obvious reading — the label is wrong, so check labels — is the wrong
# one. Accounts move between countries, and the suffix is typed by hand and
# stays put. Treating it as data produced seven "mismatches" out of twenty-four
# accounts, all of them stale labels rather than wrong accounts.
#
# The rule that actually holds: the sheet's country section wins, recent stats
# fill the gaps, the label suffix is never a source.

def test_the_sheet_decides_the_country():
    """The operator maintains the sheet; it is the only statement of intent."""
    stats = country_from_stats([("FacebookElf303 PA Amz", "Panama", "2026-08-25")])
    res = resolve_countries(stats, [
        {"account_label": "FacebookElf303 PA Amz", "country_code": "Chile", "account_status": "Active"},
    ])
    assert res["FacebookElf303"]["country"] == "Chile"


def test_the_label_suffix_is_never_the_source():
    """The recorded defect, stated as the rule that prevents it.

    The label says PA. Nothing in the resolution may reach for it: the sheet is
    silent here, so the answer must come from the stats.
    """
    stats = country_from_stats([("FacebookElf303 PA Amz", "Chile", "2026-08-23")])
    res = resolve_countries(stats, [])
    assert res["FacebookElf303"]["country"] == "Chile", "суффикс метки просочился в ответ"


def test_the_newest_day_wins_not_the_biggest_pile():
    """An account that moved has more history in the country it left."""
    rows = [("FacebookElf303 PA Amz", "Chile", f"2026-08-{d:02d}") for d in range(1, 24)]
    rows.append(("FacebookElf303 PA Amz", "Panama", "2026-08-25"))
    assert country_from_stats(rows)["FacebookElf303"] == "Panama"


def test_a_moved_account_leaves_its_old_country_behind():
    """Real case, FacebookMKTOBFLW06: Chile marked Dead, UAE marked New.

    That is what a move looks like on paper, and it must resolve to one country
    without any hesitation — the dead row is history, not an alternative.
    """
    res = resolve_countries({}, [
        {"account_label": "FacebookMKTOBFLW06 CL ML", "country_code": "Chile", "account_status": "Dead"},
        {"account_label": "FacebookMKTOBFLW06", "country_code": "UAE", "account_status": "New"},
    ])
    entry = res["FacebookMKTOBFLW06"]
    assert entry["country"] == "UAE"
    assert entry["countries"] == ["UAE"]
    assert entry["ambiguous"] is False


def test_an_account_can_run_two_countries_at_once():
    """Real case, FacebookMKTOBFLV37: Ecuador and Costa Rica, both Active.

    Five of fourteen accounts look like this. Forcing one answer and hiding the
    other is how a campaign gets aimed at the wrong market, so the single value
    is the recent one AND the pair is reported alongside it.
    """
    stats = country_from_stats([("FacebookMKTOBFLV37", "Ecuador", "2026-08-30")])
    res = resolve_countries(stats, [
        {"account_label": "FacebookMKTOBFLV37 EC Ait", "country_code": "Ecuador", "account_status": "Active"},
        {"account_label": "FacebookMKTOBFLV37 CR Ait", "country_code": "Costa Rica", "account_status": "Active"},
    ])
    entry = res["FacebookMKTOBFLV37"]
    assert entry["ambiguous"] is True
    assert set(entry["countries"]) == {"Ecuador", "Costa Rica"}
    assert entry["country"] == "Ecuador", "выбирать между живыми странами должны свежие данные"


def test_nothing_is_guessed_when_the_stats_are_silent():
    """Two live countries and no recent data: better empty than invented.

    A guess here is not a cosmetic error — it decides which market a campaign
    is built for.
    """
    res = resolve_countries({}, [
        {"account_label": "FacebookX1 EC Ait", "country_code": "Ecuador", "account_status": "Active"},
        {"account_label": "FacebookX1 CR Ait", "country_code": "Costa Rica", "account_status": "Active"},
    ])
    assert res["FacebookX1"]["country"] == ""
    assert res["FacebookX1"]["ambiguous"] is True


def test_the_account_name_is_the_identity():
    """Same account, three spellings of the annotation — one key."""
    stats = country_from_stats([("FacebookElf303", "Chile", "2026-08-25")])
    res = resolve_countries(stats, [
        {"account_label": "FacebookElf303 PA Amz", "country_code": "Panama", "account_status": "Active"},
    ])
    assert list(res) == ["FacebookElf303"]
    assert res["FacebookElf303"]["country"] == "Panama"


def test_an_account_the_sheet_does_not_mention_keeps_its_stats_answer():
    stats = country_from_stats([("FacebookMKTOBFLW72", "Kuwait", "2026-08-30")])
    res = resolve_countries(stats, [
        {"account_label": "FacebookElf303 PA Amz", "country_code": "Chile", "account_status": "Active"},
    ])
    assert res["FacebookMKTOBFLW72"]["country"] == "Kuwait"


def test_an_account_dead_everywhere_still_says_where_it_was():
    """Showing nothing would be worse than showing where it last ran."""
    res = resolve_countries({}, [
        {"account_label": "FacebookOld CL ML", "country_code": "Chile", "account_status": "Dead"},
    ])
    assert res["FacebookOld"]["country"] == "Chile"
