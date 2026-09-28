"""One place that decides whether a failed step means "do not publish this".

Every step of a run can fail on its own without aborting the rest — that is
deliberate, because a run that stops at the first hiccup produces nothing,
while one that carries on produces a draft plus a list of what went wrong. The
question that then matters is which entries on that list make the draft unsafe
to publish.

Before this module that question was answered in one line inside the flow, by
substring match:

    blocking = [f for f in ad_soft_fails
                if "creative_upload" in f or "destination" in f]

Two things were wrong with it. Renaming a failure in meta_dom silently turned
blocking into non-blocking, with nothing to notice — the names are produced in
one file and judged in another, and nothing tied them together. And the whole
adset level was outside the question entirely: audience, location, languages,
placements and the conversion event went to
`output/debug_audience_soft_fails.txt` and nowhere else, so a run that failed
to set the target country looked exactly like a clean one. That is the same
shape of bug as the ad with no media (run 58c3cb9b53c6), which we only found
after it reached a draft.

Unknown names are BLOCKING on purpose. The two mistakes cost different amounts:
a false block means someone opens Ads Manager and looks at a draft that turned
out fine, while a missed block means a campaign goes live wrong and spends real
money. So a failure nobody has classified yet is treated as serious, and this
table is the record of every one we have decided is not.
"""
from __future__ import annotations

import re

from src.fb_ads_rpa import profile_trouble as _profile

BLOCKING = "blocking"
WARNING = "warning"

# name -> (severity, why this severity)
#
# Names are the base part, before any ":detail" suffix. Add a new step here in
# the same commit that starts producing it, otherwise it blocks by default.
POLICY: dict[str, tuple[str, str]] = {
    "account_blocked": (
        BLOCKING,
        "Meta has restricted this ad account (\"Verification required to advertise\", "
        "\"Account disabled\" or \"Start authentication\"); the campaign was stopped and "
        "will not be retried - complete the verification in Meta, then run it again",
    ),
    "browser_closed": (
        BLOCKING,
        "the profile's browser tab was closed while the ads were being built; the "
        "draft is incomplete and was not retried (a replay would duplicate it) - "
        "check the draft in Ads Manager and finish or delete it",
    ),
    "profile_unresponsive": (
        BLOCKING,
        "the Multilogin profile's browser did not answer while the campaign was being "
        "opened (neither its tab nor a fresh one), and restarting the profile did not "
        "help; no draft was created - close the profile in Multilogin and run the "
        "campaign again",
    ),
    # ACL-1320, живой прогон 26.09: причина в профиле, не в кампании —
    # остановка всего задания аккаунта, без свежей вкладки и перезапуска.
    _profile.LOGGED_OUT: (
        BLOCKING,
        "Facebook opened its login page in this profile, so no draft was created - "
        + _profile.MESSAGES[_profile.LOGGED_OUT],
    ),
    _profile.NETWORK: (
        BLOCKING,
        "the profile could not reach Facebook (proxy/network error), so no draft was "
        "created - " + _profile.MESSAGES[_profile.NETWORK],
    ),
    _profile.OPEN_BY_HAND: (
        BLOCKING,
        "the account's Multilogin profile was open by hand (without automation), and the "
        "robot does not close such a window - it may hold a Facebook session Multilogin has "
        "not saved yet; no draft was created - " + _profile.MESSAGES[_profile.OPEN_BY_HAND],
    ),
    _profile.RESTART_FAILED: (
        BLOCKING,
        "the profile's browser stopped answering and the robot's restart of the "
        "profile through Multilogin failed; no draft was created - "
        + _profile.MESSAGES[_profile.RESTART_FAILED],
    ),
    # ACL-1320: Meta's new single-screen editor (URL .../simple_create).
    "simple_create_layout": (
        BLOCKING,
        "Meta opened its new single-screen campaign editor for this account; the robot "
        "does not support it yet - the campaign was stopped and not retried; a partial "
        "draft may be left in Ads Manager - check it and delete or finish it by hand",
    ),
    "window_too_small": (BLOCKING, "the editor viewport is too narrow or unreadable after window recovery; "
                         "restore a desktop-sized window before continuing the draft"),
    # ── Ad level ────────────────────────────────────────────────────────────
    # Keyed without the ad prefix: meta_dom emits "ad_creative_upload" for the
    # first ad and "ad2_creative_upload", "ad3_..." for the rest, so the entry
    # has to match on the suffix or every ad after the first lands in the
    # unclassified bucket — right verdict, useless explanation.
    "creative_upload": (
        BLOCKING,
        "the ad has no media — it cannot run, and the operator would only find "
        "out after publishing it",
    ),
    "destination_sweep": (
        BLOCKING,
        "leads go to the wrong place or nowhere; spend continues either way",
    ),
    # ── Adset level ─────────────────────────────────────────────────────────
    "audience_location": (
        BLOCKING,
        "the country was not set, so the adset keeps whatever Meta defaults to "
        "— the campaign spends in the wrong market",
    ),
    "conversion_event": (
        BLOCKING,
        "optimising for the wrong event spends the budget on the wrong people; "
        "this is also the step Meta's 2026-08-24 redesign broke",
    ),
    "audience_age": (
        BLOCKING,
        "the requested age range was not verified; a wider audience changes "
        "delivery and may spend outside the campaign manager's specification",
    ),
    "languages": (
        BLOCKING,
        "the requested language restriction was not verified, so delivery can "
        "reach people outside the approved audience",
    ),
    "targeting_open": (
        BLOCKING,
        "the detailed-targeting panel did not open, so requested targeting "
        "could not be applied or verified",
    ),
    "targeting_none_added": (
        BLOCKING,
        "none of the requested interests were added; broad delivery is only "
        "valid when the input specification explicitly requests broad targeting",
    ),
    "targeting": (
        BLOCKING,
        "detailed targeting failed as a whole and no longer matches the input "
        "campaign specification",
    ),
    "placements": (
        BLOCKING,
        "the requested manual placements were not verified; leaving Advantage+ "
        "changes where budget can be spent",
    ),
    "advantage_plus_audience": (
        BLOCKING,
        "the Audience card still reads 'Advantage+ on': the age range is only a "
        "suggestion and Meta may deliver outside it",
    ),
    "devices_ios": (
        BLOCKING,
        "the requested iOS-only filter did not apply, so delivery can spend on "
        "unapproved devices",
    ),
}

# Additional ads are emitted as ad2_*, ad3_* and so on. Keep their policy
# canonical instead of relying on the unknown-failure fallback: blocking is
# the same verdict, but the operator needs a useful explanation and a rename
# must be caught by tests.
PER_AD_POLICY: dict[str, tuple[str, str]] = {
    "create": (
        BLOCKING,
        "the requested ad shell was not created, so the campaign contains "
        "fewer ads than the approved input specification",
    ),
    "name": (
        BLOCKING,
        "the ad name was not verified, so the operator cannot reliably match "
        "the draft to its approved source row",
    ),
    "creative_upload": POLICY["creative_upload"],
    "copy": (
        BLOCKING,
        "the approved primary text or headline was not verified on the ad",
    ),
    "destination": (
        BLOCKING,
        "the ad destination was not verified, so leads may be sent to the "
        "wrong place or into an Instant Form",
    ),
    "extra_empty_ad": (
        BLOCKING,
        "Meta created an extra empty ad in this ad set; delete the empty "
        "\"New Leads ad\" before publishing",
    ),
    # ACL-1344: второй вариант основного текста не встал. Первый текст
    # сверен, объявление годно — предупреждение, а не блокировка.
    "second_text": (
        WARNING,
        "the optional second primary text was not added; the ad runs with its "
        "first primary text only - add the second text option in Ads Manager "
        "by hand if it is needed",
    ),
}

_PER_AD_NAME = re.compile(
    r"^ad(?:\d+)?_(create|name|creative_upload|copy|destination|extra_empty_ad|second_text)$"
)


def _lookup(name: str) -> tuple[str, str] | None:
    known = POLICY.get(name)
    if known:
        return known
    per_ad = _PER_AD_NAME.fullmatch(name)
    if per_ad:
        return PER_AD_POLICY[per_ad.group(1)]
    return None


def base_name(failure: str) -> str:
    """The step name, without the ":detail" a caller may have appended."""
    return (failure or "").split(":", 1)[0].strip()


def severity(failure: str) -> str:
    """BLOCKING or WARNING. Anything unclassified is BLOCKING — see module doc."""
    name = base_name(failure)
    if not name:
        return BLOCKING
    known = _lookup(name)
    if known:
        return known[0]
    # Ad-level steps are produced per ad and carry the ad's tag as a prefix
    # ("ad2_creative_upload"), so match on the suffix as well before giving up.
    for candidate, (level, _why) in POLICY.items():
        if name.endswith("_" + candidate):
            return level
    return BLOCKING


def reason(failure: str) -> str:
    """Why this failure has the severity it has — for the operator, not a log."""
    name = base_name(failure)
    known = _lookup(name)
    if known:
        return known[1]
    for candidate, (_level, why) in POLICY.items():
        if name.endswith("_" + candidate):
            return why
    return "unclassified step — treated as blocking until someone decides otherwise"


def split(failures: list[str]) -> tuple[list[str], list[str]]:
    """(blocking, warnings), order preserved, duplicates kept as reported."""
    blocking: list[str] = []
    warnings: list[str] = []
    for f in failures or []:
        (blocking if severity(f) == BLOCKING else warnings).append(f)
    return blocking, warnings


def describe(failures: list[str]) -> str:
    """One line per blocking failure, saying what it is and why it matters."""
    blocking, _ = split(failures)
    return "\n".join(f"  {f} — {reason(f)}" for f in blocking)
