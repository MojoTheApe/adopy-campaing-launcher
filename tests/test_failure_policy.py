"""Which failed steps make a draft unsafe to publish.

This is the table the rest of the run consults, so its edges matter more than
its middle: an unclassified step must not slip through as harmless, and a
renamed step must not silently stop blocking (audit finding F-05).
"""
from __future__ import annotations

from src.fb_ads_rpa import failure_policy as fp


def test_missing_media_blocks():
    assert fp.severity("ad_creative_upload") == fp.BLOCKING


def test_detail_suffix_does_not_change_severity():
    """Callers append ':<exception text>'; the verdict must not depend on it."""
    assert fp.severity("ad_creative_upload:Timeout 30000ms exceeded") == fp.BLOCKING


def test_per_ad_prefix_is_recognised_not_merely_blocked():
    """Second and later ads carry a tag prefix — ad2_creative_upload.

    Asserting the severity alone proves nothing here: unknown names block too,
    so the test would pass while every ad after the first was in fact
    unclassified. The reason is what shows it was actually matched.
    """
    for name in ("ad_creative_upload", "ad2_creative_upload", "ad3_creative_upload:boom"):
        assert fp.severity(name) == fp.BLOCKING, name
        assert "unclassified" not in fp.reason(name), (
            f"{name} блокируется как неизвестный, а не распознан по таблице"
        )
        assert "no media" in fp.reason(name), name


def test_wrong_country_blocks():
    """A campaign aimed at the wrong market spends real money being wrong."""
    assert fp.severity("audience_location") == fp.BLOCKING
    assert fp.severity("conversion_event") == fp.BLOCKING


def test_wider_delivery_only_warns():
    """Broader than asked is worth saying, but the campaign is not broken."""
    for name in ("audience_age", "languages", "placements", "devices_ios",
                 "targeting_open", "targeting_none_added"):
        assert fp.severity(name) == fp.WARNING, name


def test_unknown_failures_block():
    """The default has to be the safe one.

    A step nobody has classified yet is more likely to be a new way for a
    campaign to be wrong than a new way for it to be fine, and the two mistakes
    do not cost the same: a false block costs a look at a draft, a missed one
    costs live spend on a broken campaign.
    """
    assert fp.severity("something_nobody_classified_yet") == fp.BLOCKING
    assert fp.severity("") == fp.BLOCKING


def test_split_keeps_everything():
    fails = ["audience_age", "ad_creative_upload:x", "languages", "audience_location"]
    blocking, warnings = fp.split(fails)
    assert blocking == ["ad_creative_upload:x", "audience_location"]
    assert warnings == ["audience_age", "languages"]
    assert len(blocking) + len(warnings) == len(fails)


def test_describe_explains_why_not_just_what():
    """The operator reads this instead of the code, so it must carry a reason."""
    text = fp.describe(["ad_creative_upload", "audience_age"])
    assert "ad_creative_upload" in text
    assert "audience_age" not in text, "не блокирующие не должны попадать в стоп-текст"
    assert len(text.split("—", 1)[1].strip()) > 20, "нет объяснения, только имя шага"


def test_every_entry_carries_a_reason():
    """A table row without a justification is how the next person guesses."""
    for name, (level, why) in fp.POLICY.items():
        assert level in (fp.BLOCKING, fp.WARNING), name
        assert why and len(why) > 20, f"{name}: severity без объяснения"
