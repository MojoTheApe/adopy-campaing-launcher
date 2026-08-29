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

BLOCKING = "blocking"
WARNING = "warning"

# name -> (severity, why this severity)
#
# Names are the base part, before any ":detail" suffix. Add a new step here in
# the same commit that starts producing it, otherwise it blocks by default.
POLICY: dict[str, tuple[str, str]] = {
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
        WARNING,
        "age falls back to a wider range than asked — worth knowing, but the "
        "campaign is still aimed at the right market",
    ),
    "languages": (
        WARNING,
        "no language restriction; reach is wider than intended, not wrong",
    ),
    "targeting_open": (
        WARNING,
        "the detailed-targeting panel would not open — the adset runs broad, "
        "which is a normal way to run",
    ),
    "targeting_none_added": (
        WARNING,
        "no interests were added — again broad, deliberate in many campaigns",
    ),
    "targeting": (
        WARNING,
        "detailed targeting failed as a whole; same reasoning as the two above",
    ),
    "placements": (
        WARNING,
        "placements stay on Advantage+ instead of the manual set — a difference "
        "in delivery, not a broken campaign",
    ),
    "devices_ios": (
        WARNING,
        "the iOS-only device filter did not apply; delivery is wider",
    ),
}


def base_name(failure: str) -> str:
    """The step name, without the ":detail" a caller may have appended."""
    return (failure or "").split(":", 1)[0].strip()


def severity(failure: str) -> str:
    """BLOCKING or WARNING. Anything unclassified is BLOCKING — see module doc."""
    name = base_name(failure)
    if not name:
        return BLOCKING
    known = POLICY.get(name)
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
    known = POLICY.get(name)
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
