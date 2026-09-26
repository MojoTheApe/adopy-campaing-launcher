"""Explicit retry contract for the recorded Meta Ads Manager flow.

There are two very different recovery scopes in this flow:

* Before the editor reaches a usable ad, a failed campaign shell can be
  abandoned and rebuilt from the campaigns list.  Those stages are listed in
  ``CAMPAIGN_RETRY_STEPS`` and get at most ``MAX_CAMPAIGN_ATTEMPTS`` attempts.
* After audience or ad creation has started, replaying the whole campaign can
  create duplicate drafts or ads.  Those failures are returned for human
  review and make the draft non-publishable; the individual DOM helpers own
  any safe local retries.

Unknown stages never become retryable by accident.  They fail closed and are
blocked for review until somebody adds an explicit policy entry and test.
"""
from __future__ import annotations

from dataclasses import dataclass

RETRY_CAMPAIGN = "retry_campaign"
BLOCK_AND_REVIEW = "block_and_review"
MAX_CAMPAIGN_ATTEMPTS = 3

#: ACL-1313: шаги, на которых Meta тормозит под нагрузкой (три задания
#: параллельно, 24.09). Повтор через 1,5 с попадал в ту же нагрузку: CL FX
#: сделала 9 Create за 23 минуты. Прочим шагам пауза не нужна.
SLOW_META_STEPS = frozenset({"create", "objective_leads"})

#: ACL-1313, 25.09: пауза 90 с × попытка была нужна, пока окно цели бросали
#: через 75 с поверх крутящейся «Loading creation» и тут же жали Create снова.
#: Теперь `_wait_objective_dialog` сам ждёт загрузку до 180 с, а
#: `_create_took` повторяет промах за секунды — к отказу `objective_leads`
#: Meta уже минуты не отвечала, и ещё 90–180 с простоя ничего не добавляют.
#: 25 с — достаточно, чтобы список успел перерисоваться после перезагрузки.
SLOW_META_BACKOFF_S = 25.0


def backoff_s(step: str, attempt: int) -> float:
    """Пауза перед новой попыткой кампании после отказа на шаге `step`."""
    if (step or "").split(":", 1)[0].strip() in SLOW_META_STEPS:
        return SLOW_META_BACKOFF_S * max(1, attempt)
    return 1.5

# Every non-empty hard-failure string currently returned by
# meta_dom.run_recorded_campaign_flow.  A drift test reads that function's AST
# so a new return value cannot silently lose its retry decision.
CAMPAIGN_RETRY_STEPS = frozenset(
    {
        "create",
        "objective_leads",
        "objective_continue",
        "campaign_name",
        "daily_budget",
        "next_to_adset",
        "adset_name",
        "conversion_location_open",
        "conversion_website",
        "conversion_website_verify",
        "further_limit_reach",
        "further_limit_reach_radio",
        "further_limit_reach_continue",
        "next_to_ad",
        # ACL-1320: кампания не открылась, потому что профиль не отвечает —
        # ни вкладка, ни свежая вкладка рядом. До окна цели ничего не
        # создано; публикатор перезапускает профиль через MLX API, и
        # повтор кампании идёт уже в живом браузере.
        "profile_unresponsive",
    }
)

# These stops preserve the current draft; replaying Create cannot fix them.
# account_blocked (ACL-1313, run ecce52f330b8): Meta shows "Verification
# required to advertise" / "Account disabled" / "Start authentication (#3858385)"
# — every replay hits the same wall, only a person in Meta can lift it.
# browser_closed (25.09 20:59, GT Day 503b0c077db6): the profile's tab was
# closed mid ad level — the draft is partial, and a replay would duplicate it.
NON_RETRYABLE_FLOW_STEPS = frozenset({"window_too_small", "account_blocked", "browser_closed"})


# Stops and field failures that block publication and need review.
BLOCKING_REVIEW_STEPS = frozenset(
    {
        "window_too_small",
        "account_blocked",
        "browser_closed",
        "conversion_event",
        "audience_age",
        "audience_location",
        "languages",
        "targeting_open",
        "targeting_none_added",
        "targeting",
        "placements",
        "devices_ios",
        "ad_create",
        "ad_name",
        "ad_creative_upload",
        "ad_copy",
        "ad_destination",
        "destination_sweep",
    }
)


@dataclass(frozen=True)
class RetryDisposition:
    action: str
    max_attempts: int
    rationale: str


def disposition(step: str) -> RetryDisposition:
    """Return the recovery scope for a recorded-flow failure."""
    name = (step or "").split(":", 1)[0].strip()
    if name in CAMPAIGN_RETRY_STEPS:
        return RetryDisposition(
            RETRY_CAMPAIGN,
            MAX_CAMPAIGN_ATTEMPTS,
            "the flow has not produced a trustworthy ad yet, so rebuilding "
            "the campaign shell is bounded and safe",
        )
    return RetryDisposition(
        BLOCK_AND_REVIEW,
        1,
        "a whole-campaign replay could create duplicate drafts or ads; keep "
        "the partial draft blocked for human review",
    )


def should_retry_campaign(step: str, *, completed_attempts: int) -> bool:
    policy = disposition(step)
    return policy.action == RETRY_CAMPAIGN and completed_attempts < policy.max_attempts
