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
    "page_identity": (
        BLOCKING,
        "the requested Facebook Page could not be selected and verified in Identity; "
        "the draft must not be published. Check Identity in Ads Manager before continuing",
    ),
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
        "robot could not reopen it with automation - either the graceful Multilogin stop did "
        "not close the window, or reopening is switched off on this computer; nothing was "
        "killed and no draft was created - " + _profile.MESSAGES[_profile.OPEN_BY_HAND],
    ),
    _profile.RESTART_FAILED: (
        BLOCKING,
        "the profile's browser stopped answering and the robot's restart of the "
        "profile through Multilogin failed; no draft was created - "
        + _profile.MESSAGES[_profile.RESTART_FAILED],
    ),
    # ACL-1320: Meta's new single-screen editor (URL .../simple_create).
    # ACL-1327: the robot walks this editor; the stop is left for a layout it
    # does not recognise (no Ad set / Ad section) and for FB_RPA_SIMPLE_CREATE=0.
    "simple_create_layout": (
        BLOCKING,
        "Meta opened its new single-screen campaign editor for this account, and the robot "
        "could not find its Ad set / Ad sections (or this editor is switched off with "
        "FB_RPA_SIMPLE_CREATE=0) - the campaign was stopped and not retried; a partial "
        "draft may be left in Ads Manager - check it and delete or finish it by hand",
    ),
    # ACL-1327: "Please read and accept Meta's Lead Ads Terms" stayed after Website.
    _profile.LEAD_TERMS: (
        BLOCKING,
        "the Facebook Page has not accepted Meta's Lead Ads Terms; only a person can accept "
        "them in Meta, so the campaign was stopped and not retried - a partial draft is left "
        "in Ads Manager - " + _profile.MESSAGES[_profile.LEAD_TERMS],
    ),
    # ACL-1697: Ads Manager opened another ad account than the draft's.
    _profile.WRONG_AD_ACCOUNT: (
        BLOCKING,
        "Ads Manager opened a different ad account than the one in the draft (or the opened "
        "account could not be read), so the campaign was stopped before Create and nothing "
        "was built - " + _profile.MESSAGES[_profile.WRONG_AD_ACCOUNT],
    ),
    # ACL-1327 (review of PR #340, F4): the draft already exists when the
    # objective is checked inside the single-screen editor.
    "objective_inline": (
        BLOCKING,
        "Meta's single-screen campaign editor did not switch the campaign objective to Leads; "
        "the draft was left in Ads Manager and not retried (a retry would leave another "
        "draft) - set the objective to Leads by hand or delete the draft",
    ),
    # ACL-1353: ads 2+ of Meta's single-screen editor are created by the
    # robot; this step is left only when that is switched off on the machine
    # (FB_RPA_SIMPLE_MORE_ADS_PRESS=0). A failed ad is its own adN_* step.
    "simple_create_more_ads": (
        BLOCKING,
        "Meta's single-screen campaign editor was used, and creating more than one ad there is "
        "switched off on this machine - only the first ad was built; add the other ads by hand "
        "in Ads Manager or publish the campaign again with the setting back on",
    ),
    # ACL-1354 (runs 37e397574548 and a71e4fc14aa1, 2026-09-28): time budgets of
    # the recorded flow; the draft is partial and a replay would duplicate it.
    "tab_frozen_budget_exceeded": (
        BLOCKING,
        "the profile's browser tab kept not answering while the campaign was being built, "
        "so the robot stopped it instead of waiting for an hour; the draft is incomplete and "
        "was not retried - check it in Ads Manager, then close and reopen the profile",
    ),
    "campaign_budget_exceeded": (
        BLOCKING,
        "building this campaign took longer than the robot's time limit, so it was stopped; "
        "the draft is incomplete and was not retried - check it in Ads Manager and finish or "
        "delete it",
    ),
    "tab_hung": (
        BLOCKING,
        "the profile's browser tab stopped answering inside one step for minutes, so the "
        "robot closed that tab (the profile stays open) and stopped the campaign; the draft is "
        "incomplete and was not retried - check it in Ads Manager and finish or delete it",
    ),
    "step_budget_exceeded": (
        BLOCKING,
        "one step of this campaign took longer than the robot's time limit, so the campaign "
        "was stopped; the draft is incomplete and was not retried - check it in Ads Manager "
        "and finish or delete it",
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
    # ACL-1390: страна встала только на втором проходе в конце кампании и
    # подтверждена перечитыванием Locations — черновик годен, это сведения.
    "audience_location_second_pass": (
        WARNING,
        "Meta's location search showed no suggestions when the country was first typed; "
        "the robot set it on a second pass at the end of the campaign and read it back "
        "from Locations",
    ),
    "conversion_website_name_changed": (
        BLOCKING,
        "the ad set name changed while Conversion location was being set; the draft was "
        "stopped so a changed name is not published - check the ad set name in Ads Manager",
    ),
    "next_to_ad_stuck_on_adset": (
        BLOCKING,
        "the editor stayed on the ad set after Next was pressed several times, so the ad was not "
        "built; the draft was not retried (a retry would leave another partial draft) - open it in "
        "Ads Manager and finish or delete it",
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
    "advantage_plus_off": (
        BLOCKING,
        "the Audience card stayed on the Advantage+ recommended setup before the age "
        "step (no audience controls, 'Further limit the reach of your ads' still "
        "offered): age, gender and languages are not applied as controls",
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
    # ACL-1476 (owner, 2026-10-01): the story of a post/story pair could not
    # be placed (`no_crop_step` / `not_in_library`); the ad has the post file
    # on all placements and is publishable.
    "story_not_placed": (
        WARNING,
        "the story file of the post/story pair was not placed (Meta showed no Crop "
        "step, or the story file is not in the media library); the ad runs with the "
        "post file on Stories and Reels too - replace it in Ads Manager by hand if needed",
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

# ── What the operator reads (ACL-1437) ─────────────────────────────────────
#
# name -> (title, impact, action). Text only: the verdict above is unchanged.
# The dashboard renders one item per problem — the title (plus "ad N" for
# per-ad steps), one sentence of impact and one "what to do" line — instead
# of the long `reason()` paragraph. A test fails if a step of POLICY or
# PER_AD_POLICY has no entry here, so add both in the same commit.
GUIDE: dict[str, tuple[str, str, str]] = {
    "account_blocked": (
        "Ad account restricted by Meta",
        "Meta asks for verification or has disabled this ad account, so nothing more was built.",
        "Complete the verification in Meta, then publish again.",
    ),
    "browser_closed": (
        "Browser tab closed during the build",
        "The draft is incomplete and was not retried, so a replay does not duplicate it.",
        "Open the draft in Ads Manager and finish or delete it.",
    ),
    "profile_unresponsive": (
        "Multilogin profile not responding",
        "The profile's browser did not answer even after a restart, so no draft was created.",
        "Close the profile in Multilogin and publish again.",
    ),
    _profile.LOGGED_OUT: (
        "Facebook logged out in this profile",
        "Facebook showed its login page, so no draft was created.",
        "Log in to Facebook in the profile window that was brought to the front, then publish again.",
    ),
    _profile.NETWORK: (
        "Profile cannot reach Facebook",
        "The profile's proxy or network failed, so no draft was created.",
        "Check the proxy of this profile in Multilogin, then publish again.",
    ),
    _profile.OPEN_BY_HAND: (
        "Profile window open by hand",
        "The hand-opened window could not be reopened with automation (Multilogin did not "
        "close it, or reopening is switched off on this computer), so no draft was created.",
        "Close the profile window so Multilogin saves the session, then publish again.",
    ),
    _profile.RESTART_FAILED: (
        "Profile restart failed",
        "The profile stopped answering and could not be restarted, so no draft was created.",
        "Open the profile in Multilogin, check that Ads Manager loads, close it and publish again.",
    ),
    "simple_create_layout": (
        "Unknown layout of Meta's single-screen editor",
        "The robot could not find the Ad set / Ad sections, so a partial draft may be left.",
        "Check the draft in Ads Manager and finish or delete it.",
    ),
    _profile.LEAD_TERMS: (
        "Lead Ads Terms not accepted on the Page",
        "Only a person can accept Meta's Lead Ads Terms, so the build stopped with a partial draft.",
        "Accept the Page's Lead Ads Terms in Meta, then publish again.",
    ),
    _profile.WRONG_AD_ACCOUNT: (
        "Wrong ad account opened",
        "Ads Manager showed another ad account than the draft's, so nothing was created.",
        "Check the ad account id on the Accounts page and that the account is visible in "
        "this profile, then publish again.",
    ),
    "objective_inline": (
        "Objective — not switched to Leads",
        "The draft may run with the wrong campaign objective.",
        "Set the objective to Leads in Ads Manager or delete the draft.",
    ),
    "simple_create_more_ads": (
        "Only the first ad was built",
        "Creating more ads in Meta's single-screen editor is switched off on this machine.",
        "Add the other ads by hand in Ads Manager or publish again with the setting on.",
    ),
    "tab_frozen_budget_exceeded": (
        "Browser tab kept freezing",
        "The robot stopped instead of waiting, so the draft is incomplete.",
        "Check the draft in Ads Manager, then close and reopen the profile.",
    ),
    "campaign_budget_exceeded": (
        "Campaign build took too long",
        "The build hit the robot's time limit, so the draft is incomplete.",
        "Open the draft in Ads Manager and finish or delete it.",
    ),
    "tab_hung": (
        "Browser tab hung during a step",
        "The robot closed the frozen tab and stopped, so the draft is incomplete.",
        "Open the draft in Ads Manager and finish or delete it.",
    ),
    "step_budget_exceeded": (
        "One step took too long",
        "A step hit the robot's time limit, so the draft is incomplete.",
        "Open the draft in Ads Manager and finish or delete it.",
    ),
    "window_too_small": (
        "Browser window too small",
        "The editor could not be read after window recovery, so the draft may be incomplete.",
        "Restore a desktop-sized profile window and check the draft.",
    ),
    "creative_upload": (
        "Ad media — missing",
        "The ad has no image or video, so it cannot run.",
        "Open the ad and upload the creative.",
    ),
    "destination_sweep": (
        "Ad destination — not verified",
        "Leads may go to the wrong place or nowhere while the budget is spent.",
        "Open each ad and check the website / destination.",
    ),
    "audience_location": (
        "Location — not set",
        "The ad set may spend in the wrong country.",
        "Check \"Locations\" on the ad set.",
    ),
    # ACL-1390: страна встала со второго прохода — предупреждение, не стоп.
    "audience_location_second_pass": (
        "Country set on the second pass",
        "Meta's location search stalled; the robot set the country at the end "
        "of the campaign and read it back.",
        "Check \"Locations\" on the ad set before publishing.",
    ),
    "conversion_website_name_changed": (
        "Ad set name changed",
        "The name changed while Conversion location was being set, so the build was stopped.",
        "Check the ad set name in Ads Manager, then publish again.",
    ),
    "next_to_ad_stuck_on_adset": (
        "Editor stayed on the ad set",
        "Next was pressed several times but the ad was not opened, so the draft has no ad.",
        "Open the draft in Ads Manager and finish or delete it.",
    ),
    "conversion_event": (
        "Conversion event — not verified",
        "Ads may optimise for the wrong event and waste budget.",
        "Check \"Conversion event\" on the ad set.",
    ),
    "audience_age": (
        "Age range — not verified",
        "Delivery may reach ages outside the requested range.",
        "Check \"Age\" in the ad set audience.",
    ),
    "languages": (
        "Languages — not verified",
        "Delivery may reach people outside the requested languages.",
        "Check \"Languages\" in the ad set audience.",
    ),
    "targeting_open": (
        "Detailed targeting — panel did not open",
        "The requested interests were not applied or verified.",
        "Add the interests in \"Detailed targeting\" on the ad set.",
    ),
    "targeting_none_added": (
        "Detailed targeting — no interests added",
        "The ad set runs broad although interests were requested.",
        "Add the interests in \"Detailed targeting\" on the ad set.",
    ),
    "targeting": (
        "Detailed targeting — failed",
        "The ad set targeting no longer matches the campaign specification.",
        "Check \"Detailed targeting\" on the ad set.",
    ),
    "placements": (
        "Placements — not verified",
        "Budget may be spent on placements that were not requested.",
        "Check \"Placements\" on the ad set.",
    ),
    "advantage_plus_off": (
        "Advantage+ audience — still on recommended setup",
        "Age, gender and languages are not applied as controls, so Meta may deliver outside them.",
        "Open the ad set audience, choose \"Further limit the reach of your ads\" and check Age.",
    ),
    "advantage_plus_audience": (
        "Advantage+ audience still on",
        "The age range is only a suggestion and Meta may deliver outside it.",
        "Switch the ad set audience to original audience options.",
    ),
    "devices_ios": (
        "iOS-only devices — not applied",
        "Delivery may spend on devices that were not requested.",
        "Check \"Devices\" in the ad set placements.",
    ),
}

PER_AD_GUIDE: dict[str, tuple[str, str, str]] = {
    "create": (
        "Ad not created",
        "The campaign has fewer ads than requested.",
        "Add the missing ad by hand in Ads Manager.",
    ),
    "name": (
        "Ad name",
        "The ad name was not confirmed, so the ad may not match its source row.",
        "Open the ad and check its name.",
    ),
    "creative_upload": GUIDE["creative_upload"],
    "copy": (
        "Ad text",
        "Primary text / headline not confirmed.",
        "Open the ad and check the text.",
    ),
    "destination": (
        "Ad destination",
        "Leads may be sent to the wrong place or into an Instant Form.",
        "Open the ad and check the website / destination.",
    ),
    "extra_empty_ad": (
        "Extra empty ad",
        "Meta added an empty \"New Leads ad\" to this ad set.",
        "Delete the empty \"New Leads ad\" before publishing.",
    ),
    "second_text": (
        "Second primary text",
        "The optional second text was not added; the ad runs with its first text.",
        "Add the second text option in Ads Manager if it is needed.",
    ),
    # ACL-1476: сторис пары не встала — предупреждение, объявление годно.
    "story_not_placed": (
        "Story file not placed",
        "The ad runs with the post file on Stories and Reels too.",
        "Replace the Stories/Reels file in Ads Manager by hand if needed.",
    ),
}

_PER_AD_NAME = re.compile(
    r"^ad(?:\d+)?_(create|name|creative_upload|copy|destination|extra_empty_ad|second_text"
    r"|story_not_placed)$"
)


# ── Per-ad details that change the explanation, not the verdict ────────────
#
# (per-ad step, reason at the start of the note) -> (why, title, impact, action).
# ACL-1543: `<tag>_destination:instant_form_not_switched - <ad>: ...` — Meta kept
# the ad on "Instant form (suggested)"; still BLOCKING, but the operator reads
# that it is Instant form, not a generic "destination not verified".
PER_AD_DETAIL: dict[tuple[str, str], tuple[str, str, str, str]] = {
    ("destination", "instant_form_not_switched"): (
        "Meta kept the ad on Instant form (suggested) and the robot could not switch it "
        "back to Website, so the website URL is not set; the robot never accepts Lead Ads "
        "Terms or creates an instant form",
        "Ad destination - Instant form",
        "Meta kept this ad on Instant form (suggested); the website URL is not set.",
        "Open the ad, select Website (Browser add-ons: None) and enter the URL. "
        "Do not accept Lead Ads Terms or create a form for it.",
    ),
    # ACL-1551 (live 04.10, 232d398471bd): Meta's media wizard stayed on its
    # Crop step after Next; the robot left it with the wizard's own Close and
    # went on with the other ads. Still BLOCKING: the creative is not confirmed.
    ("creative_upload", "crop_wizard_stuck"): (
        "Meta's media wizard stayed on its Crop step (Next did not move it on), so the robot "
        "closed the wizard with its own Close; the ad's media is not confirmed",
        "Ad media - Meta's media wizard stuck on Crop",
        "The media wizard did not finish, so this ad's media and crops are not confirmed.",
        "Open the ad in Ads Manager, check its media (and the Stories/Reels file) and fix it by hand.",
    ),
    # ACL-1559 (live 04.10, 2f140fa687fa): the wizard opened on its Media step and
    # Meta's media picker never left its "Loading..." placeholders; the robot
    # waited without clicking, reopened the wizard once, and gave up. Still
    # BLOCKING: the ad has no media. The file itself is not blamed.
    ("creative_upload", "media_picker_loading"): (
        "Meta's media picker on the wizard's Media step kept loading (no Upload, no media "
        "library), so the ad's media could not be attached",
        "Ad media - Meta's media picker kept loading",
        "Meta's media picker never finished loading, so this ad has no media.",
        "Open the ad in Ads Manager, add its media by hand (Set up creative - Media), "
        "or run the campaign again later.",
    ),
    # ACL-1637 (live 06.10): ads 2-4 of one campaign use the same video; the
    # robot could neither take a copy from Meta's library nor upload it again.
    # BLOCKING: the ad has no media, so the campaign is do-not-publish.
    **{("creative_upload", why): (
        reason,
        "Ad media - " + title,
        "This ad has no media, so the campaign must not be published.",
        "Open the ad in Ads Manager, add its media by hand (Set up creative - Media), "
        "or run the campaign again.",
    ) for why, reason, title in (
        ("library_has_no_copy",
         "the same video was used by an earlier ad, Meta's media library showed no copy of it "
         "and the robot's re-upload for this ad did not attach it",
         "no library copy of the shared video"),
        ("same_file_already_uploaded_this_run",
         "the same video was uploaded for an earlier ad but could not be attached to this ad",
         "shared video not attached"),
        ("same_file_already_failed_this_run",
         "the same video already failed to upload for an earlier ad and was not accepted again",
         "shared video failed earlier"),
        ("file_not_landed",
         "the browser did not hand the video file to Meta's upload dialog for this ad",
         "video file not accepted by the upload dialog"),
        ("wizard_not_opened",
         "Meta's 'Set up creative' wizard did not open for this ad",
         "creative wizard did not open"),
        # ACL-1669 (live 07.10, B97): the library grid showed no "N selected" counter.
        ("library_selection_unreadable",
         "Meta's media library showed no selection counter, so the robot could not tell what "
         "is selected and neither clicked a tile nor uploaded over it; attach the media by hand",
         "library selection unreadable"),
        ("library_not_searched_counter_unreadable",
         "Meta's media library showed no selection counter, so the robot did not search it "
         "for a copy of the video; the library was not asked, it did not say it has no copy, "
         "and the robot did not upload again - attach the media by hand",
         "library not searched, counter unreadable"),
    )},
    # ACL-1669: no media on the ad, so Meta shows no URL field. Still BLOCKING.
    ("destination", "url_field_missing_no_media"): (
        "the website URL field is missing because this ad has no media (Meta shows no "
        "destination fields without it); this follows from the media step, not from the "
        "destination itself",
        "Ad destination - no media on the ad",
        "This ad has no media, so its website URL could not be set; fix the media first.",
        "Open the ad in Ads Manager, add its media by hand, then check the website URL.",
    ),
    # ACL-1551: the wizard of the previous ad was still open and its own Close
    # did not close it, so the ad set's Action menu could not be used.
    ("create", "creative_wizard_left_open"): (
        "Meta's media wizard of the previous ad stayed open and its own Close did not close it, "
        "so the ad set's Action menu could not be used to create this ad",
        "Ad not created - media wizard left open",
        "The media wizard of the previous ad stayed on screen, so this ad was not created.",
        "Close the media wizard in Ads Manager, then add the missing ad by hand.",
    ),
}


# ── ACL-1573: шаг остановило незнакомое окно Meta ──────────────────────────
#
# `<шаг>:Unknown Meta window blocked the step: <title> (unknown_window)` —
# сторож окон (`foreign_windows.attach_block`) не смог безопасно закрыть
# окно, которого шаг не открывал. Вердикт шага не меняется (имя до «:»),
# меняется объяснение: оператор читает заголовок окна.
UNKNOWN_WINDOW_TOKEN = "(unknown_window)"
UNKNOWN_WINDOW_PREFIX = "Unknown Meta window blocked the step: "


def _window_detail(failure: str) -> tuple[str, str, str, str] | None:
    raw = str(failure or "")
    if ":" not in raw:
        return None
    note = raw.split(":", 1)[1].strip()
    if not note.endswith(UNKNOWN_WINDOW_TOKEN) or not note.startswith(UNKNOWN_WINDOW_PREFIX):
        return None
    title = note[: -len(UNKNOWN_WINDOW_TOKEN)].strip()
    return (
        f"{title}; the robot only closes such a window with its own Close / Not now / Dismiss / "
        "Cancel or Escape and never presses Publish, Confirm or Continue in it, so the step was "
        "stopped - the draft may be incomplete",
        title,
        "A Meta window the robot does not know stayed over the editor, so this step was not done.",
        "Open the draft in Ads Manager, close that window by hand and check this step.",
    )


def _detail(failure: str) -> tuple[str, str, str, str] | None:
    raw = str(failure or "")
    window = _window_detail(raw)
    if window:
        return window
    per_ad = _PER_AD_NAME.fullmatch(base_name(raw))
    if not per_ad or ":" not in raw:
        return None
    note = raw.split(":", 1)[1].strip()
    for (step, why), entry in PER_AD_DETAIL.items():
        if per_ad.group(1) == step and (note == why or note.startswith(why + " ")):
            return entry
    return None


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
    detail = _detail(failure)
    if detail:
        return detail[0]
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


# "<ad name> has no text…" / "<ad name> second primary text not added (…)" /
# "<reason> - <ad name>: <sentence>" (story_not_placed, ACL-1476) — the only
# details that name the ad (meta_dom); anything else is a note.
_AD_NAME_IN_NOTE = re.compile(
    r"^(?:(?P<name>.+?) (?:has no text|second primary text not added)\b"
    r"|[a-z_]+ - (?P<name2>.+?): )"
)
_TAG_ONLY = re.compile(r"^ad ?\d*$", re.I)
_AD_TAG = re.compile(r"^ad(\d*)_")


def _guide(name: str) -> tuple[str, str, str] | None:
    if name in GUIDE:
        return GUIDE[name]
    per_ad = _PER_AD_NAME.fullmatch(name)
    if per_ad:
        # .get: новый шаг в PER_AD_POLICY без текста не должен ронять
        # сериализацию задания (KeyError → результат кампании потерян).
        return PER_AD_GUIDE.get(per_ad.group(1))
    for candidate, entry in GUIDE.items():
        if name.endswith("_" + candidate):
            return entry
    return None


def problem(failure: str) -> dict:
    """One failure as the dashboard shows it (ACL-1437): plain title, which ad,
    one sentence of impact, one action; the raw code and its note stay for
    "Technical details"."""
    raw = str(failure or "")
    name = base_name(raw)
    note = raw.split(":", 1)[1].strip() if ":" in raw else ""
    known = _lookup(name)
    guide = _guide(name) or (
        # Шаг классифицирован, но текста для него нет: показываем причину из
        # политики, а не «неизвестный шаг — блокирует» (он может быть warning).
        (
            f"Step '{name}'",
            known[1][:1].upper() + known[1][1:],
            "Check this part of the draft in Ads Manager.",
        )
        if known
        else (
            f"Unclassified step '{name or '?'}'",
            "The robot reported a step it does not know, so it is treated as blocking.",
            "Check this part of the draft in Ads Manager.",
        )
    )
    ad_index: int | None = None
    ad_name = ""
    if name not in GUIDE and _PER_AD_NAME.fullmatch(name):
        ad_index = int(_AD_TAG.match(name).group(1) or 1)
        found = _AD_NAME_IN_NOTE.match(note)
        label = (found.group("name") or found.group("name2") or "").strip() if found else ""
        if label and not _TAG_ONLY.match(label):
            ad_name = label
    detail = _detail(raw)
    if detail:
        guide = detail[1:]
    title, impact, action = guide
    return {
        "step": name,
        "ad_index": ad_index,
        "ad_name": ad_name,
        "severity": severity(raw),
        "title": title,
        "impact": impact,
        "action": action,
        "note": note,
        "raw": raw,
    }


def problems(failures: list[str]) -> list[dict]:
    """Blocking first, then warnings; an exact repeat is listed once."""
    seen: set[str] = set()
    unique: list[str] = []
    for f in failures or []:
        key = str(f or "")
        if key and key not in seen:
            seen.add(key)
            unique.append(key)
    blocking, warnings = split(unique)
    return [problem(f) for f in blocking + warnings]
