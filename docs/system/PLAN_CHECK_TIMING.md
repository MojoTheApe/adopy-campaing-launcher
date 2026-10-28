# Where the time of a pre-publish check goes — ACL-2003

A check (`/publish-plan`, event `api_plan_check` in the telemetry) took 141, 150 and 168 s
on KLD_BFL_B97 (TEST9095w, 2026-10-18). The server does not time the stages separately
today, so this is the structure read from the code plus the one measured total:

1. Waiting for the profile. The read lease and the single shared reader
   (`account_spend_api._session`) are taken first; a check behind another operation on the
   same profile waits here, and the wait counts into the total.
2. Starting the profile. If it is not already open, Multilogin starts it (proxy handshake,
   browser start). This is the largest and most variable part when the profile was closed
   after the previous read. Warm profiles (`ACL_PLAN_KEEP_OPEN_S`, default 600 s, ACL-2117)
   skip it.
3. Ads Manager. The tab navigates to the ad account and waits for a Graph session in the
   page (`READY_TIMEOUT_S` = 90 s upper bound).
4. Graph reads. `GraphClient` spaces calls by `DEFAULT_MIN_INTERVAL_S` = 1 s. One check
   reads the account (`preflight_account`: account status, page, pixel, ...) and, per
   campaign, targeting lookups and one validate-only creative; the call budget is
   `PLAN_PREFLIGHT_MAX_CALLS` (30 + 90). Every call costs at least the 1 s spacing, so
   a plan with many interests or campaigns adds tens of seconds linearly.
5. Closing. After a clean check the window is kept (ACL-2117); otherwise the profile is
   stopped softly and Multilogin saves it.

What changed in ACL-2003: identical requests no longer repeat these stages. The page shares
one in-flight check per form, the server merges identical concurrent requests, and a draft
that was not edited is shown from its stored verdict for 10 minutes ("Checked N min ago")
instead of reading the account again. A single check is not faster.

Not measured yet: the split between steps 1-5. Adding per-stage durations to `api_plan_check`
would answer it from real runs.
