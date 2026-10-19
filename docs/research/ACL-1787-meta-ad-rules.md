# ACL-1787: Meta automatic rules (Ad Rules Engine) instead of watching accounts

Issue: #787. Date: 2026-10-16. Desk research only: official Meta documentation was read; no Meta API was called, and no credential, token, profile or account was touched.

Labels: **[V]** = verified in an official Meta page (URL given). **[I]** = inference, not stated by Meta. **[U]** = unknown, only a live test can answer.

Page-reading note: the Meta pages are JavaScript-heavy and were read through a summarising fetcher, so the exact wording of long tables (for example the evaluation-spec field lists) should be re-checked in a browser before code relies on it. This extends the 2026-10-11 desk note in the Issue comments, which had only part of the pages.

## 1. Short answer

- Meta can run "pause / notify / change budget" rules on its own side with no client polling [V for the engine, I for "no polling by us", see 3.1]. This directly addresses the checkpoint risk of fixed-interval Ads Manager polling.
- It can replace the **stop-loss and alert** part of the watcher. It cannot replace stats collection for the dashboard, identity/attribution logic (Rules T3 evidence, cohorts, FTD), or anything that needs our own goals (My Goals, "T multiple").
- Whether our in-profile page token may create rules, and whether "pause with a spend/cost condition" is accepted at all, are **unknown** and need one gated live step (section 8).
- Recommendation: do not build a rules feature yet. First run the small pilot in section 8 on one allowlisted account with an explicit owner go.

## 2. How the Ad Rules Engine works (verified)

Sources:
- Overview: https://developers.facebook.com/docs/marketing-api/ad-rules (also https://developers.facebook.com/documentation/ads-commerce/marketing-api/ad-rules)
- Evaluation spec: https://developers.facebook.com/docs/marketing-api/ad-rules/overview/evaluation-spec
- Execution spec: https://developers.facebook.com/docs/marketing-api/ad-rules/overview/execution-spec
- API calls: https://developers.facebook.com/docs/marketing-api/ad-rules/overview/api-calls
- Reference, create edge: https://developers.facebook.com/docs/marketing-api/reference/ad-account/adrules_library/
- Reference, rule object: https://developers.facebook.com/docs/marketing-api/reference/ad-rule/
- Trigger guide: https://developers.facebook.com/documentation/ads-commerce/marketing-api/ad-rules/guides/trigger-based-rules
- Schedule guide: https://developers.facebook.com/documentation/ads-commerce/marketing-api/ad-rules/guides/scheduled-based-rules

**Object and endpoints [V].** A rule has `name`, `evaluation_spec`, `execution_spec`, optional `schedule_spec`, and `status` (ENABLED, DISABLED, DELETED, HAS_ISSUES; `disable_error_code` explains HAS_ISSUES).
- Create: `POST /act_<ID>/adrules_library`. Read: `GET /act_<ID>/adrules_library`, `GET /<RULE_ID>`.
- Update: `POST /<RULE_ID>` (resend every spec, even unchanged parts; changing only status needs no specs). Delete: `DELETE /<RULE_ID>`.
- History: `GET /<RULE_ID>/history`, `GET /act_<ID>/adrules_history` (filters `object_id`, `action`, `hide_no_changes`).
- Schedule rules only: `POST /<RULE_ID>/preview` (objects that currently match) and `POST /<RULE_ID>/execute` (run now). `POST /<OBJECT_ID>/adrules_governed` lists the rules governing an object.

**Two evaluation modes [V].**
- Schedule-based: checked at an interval. `schedule_type` DAILY (midnight, account timezone), HOURLY (start of every hour), SEMI_HOURLY (start of every half-hour), CUSTOM (`schedule` entries with `start_minute`/`end_minute` in multiples of 30, `days` 0-6).
- Trigger-based: "real time". Metadata changes are usually evaluated within seconds; Insights changes within minutes (documented p99 about 7.5 minutes). Trigger types: METADATA_CREATION, METADATA_UPDATE, STATS_MILESTONE, STATS_CHANGE, DELIVERY_INSIGHTS_CHANGE (beta). STATS_CHANGE is edge-triggered: it fires once when the condition goes from false to true, not again while it stays true. No `schedule_spec`. **Trigger-based rules are API-only, not available in Ads Manager.**

**Filters [V].** `field` / `value` / `operator`, all ANDed. Every rule needs an `entity_type` (AD, ADSET, CAMPAIGN) or an `id` filter. One `time_preset` per rule (required whenever an Insights filter or trigger is used; trigger rules accept only presets that include TODAY, such as TODAY, LAST_3_DAYS, LAST_7_DAYS; LIFETIME and YESTERDAY are schedule-only). Metadata filters include `id`, `name` (EQUAL/CONTAIN/NOT_CONTAIN), `effective_status`, `daily_budget`, `created_time`, `hours_since_creation`, `objective`. Insights filters include `spent`, `results`, `cost_per`, `leadgen`, `impressions`, `clicks`, `cpa`, `cost_per_lead_fb`-style fields. Money is in the currency base unit (the page says "for USD, cents"). Without an `effective_status` filter Meta adds one itself (ACTIVE and PENDING_REVIEW for active-object actions).

**Actions [V]** (execution spec):
- Both modes: NOTIFICATION (to the rule creator or to `execution_options.user_ids`), PAUSE, UNPAUSE.
- Schedule only: CHANGE_BUDGET (ad sets), CHANGE_CAMPAIGN_BUDGET, CHANGE_BID, ROTATE, REBALANCE_BUDGET.
- Trigger only: PING_ENDPOINT (webhook to our app; needs an `ads_rules_engine` webhook subscription and a callback URL).
- The reference lists 19 `execution_type` values in total (including AD_RECOMMENDATION_APPLY); only the ones above are described in the guides.
- Schedule rules with `user_ids` send a daily summary email at 12:30 AM account time (not on days without actions).

**Error codes on create/update [V].** 200 permissions, 100 invalid parameter, 190 token, 368 "action deemed abusive or otherwise disallowed", **2703 "Rules that turn off ads can't have cost conditions."**, 80004 too many calls to this ad account (read).

**Permissions and limits.**
- [V] No Ad Rules page names a required permission or scope; they list only error 200 "Permissions error". They give no maximum number of rules per account and no rate limit specific to rules.
- [V] General Marketing API: `ads_management` reads and manages ads; `ads_read` reads. Own ad accounts need only standard access to those permissions; other people's accounts need advanced access via App Review. Source: https://developers.facebook.com/docs/marketing-api/overview/authorization
- [V] Marketing API access tier: Limited (default, development-only rate limits, 1 system user) versus Full (after upgrade; 500 successful calls in 15 days and error rate under 15%). Same page.
- [V] Rate limits: reads cost 1 point, writes 3; Limited tier max score 60, Full 9000; the ad-account quota is 300 (Limited) or 100,000 (Full) `ads_management` calls per hour plus 40 per active ad. Source: https://developers.facebook.com/docs/marketing-api/overview/rate-limiting

## 3. What the docs do NOT say

3.1 **Where rules run.** The pages never state outright that evaluation happens on Meta's servers without a client. [I] It is strongly implied: the overview describes a "central rule management service" that manages ads so you do not have to query the API yourself, trigger rules fire as soon as data changes, and `PING_ENDPOINT` calls us rather than the reverse. Very likely, but the pilot should confirm it (a rule must act while ACL and the profile are closed).

3.2 **Error 2703 versus the docs' own example.** [V] 2703 reads "Rules that turn off ads can't have cost conditions." [V] The trigger guide's own STATS_CHANGE example pauses an AD when `cost_per_purchase_fb` is greater than 1000 and `reach` is greater than 5000 over LAST_3_DAYS. These two statements conflict as written. [I] Possible readings: 2703 applies only to schedule rules, or to some fields, or some action/level combinations, or the example is outdated. [U] Not resolvable from the docs. This is the most important unknown, because "pause when spend is over X and leads equal 0" uses a spend condition, and `spent` may or may not count as a "cost condition". The pilot must test `spent` + `results` (schedule and trigger variants) and a cost-per variant.

3.3 **Token type.** [U] Whether the page token captured inside the Multilogin profile (ACL-1760 route) may create or read `adrules_library`. Meta names no scope; the nearest permission is `ads_management`. [I] A token from the Ads Manager page acts as the logged-in user, so the answer probably follows that user's role on the account.

3.4 **Parity with the Ads Manager "Automated rules" UI.** [U] The docs do not say API-created schedule rules are visible and editable in the UI. Trigger rules are API-only [V], so they are not in the UI by definition.

3.5 **Units and field semantics.** [V] Money is in the currency base unit, with "cents" as the example. [I] So `spent` 2000 probably means 20.00 in USD. `results` versus `leadgen` for a Leads ad set: [U].

3.6 **Whether rule creation on our accounts is treated as abuse.** [U] Error 368 exists as a generic block on create and delete; nothing documents account-level policy for rules.

3.7 **Rule count per account, exact run jitter, behaviour on archived or paused objects.** [U].

## 4. What Meta rules can and cannot replace in our watcher

| Need | Meta rule can do it? | Notes |
|---|---|---|
| Stop-loss: pause an ad set/campaign when spend passes X with 0 leads | Maybe | Depends on 2703 (3.2). If refused: NOTIFICATION only, or a budget cap. |
| Pause on bad cost per result | Maybe | Same 2703 question. |
| Alert the buyer (spend, CPA, budget nearly used) | Yes [V] | NOTIFICATION goes to Facebook notifications and a daily email summary, not to the ACL dashboard [I]. |
| Push an event to ACL without polling | Yes, trigger rules [V] | `PING_ENDPOINT` needs an app webhook subscription and a public callback URL. ACL runs on buyer laptops, so a relay would be needed [I]. |
| Auto-scale / budget up on good performance | Partly [V] | CHANGE_BUDGET exists (schedule, ad sets), but ACL policy forbids budget writes outside the closed edge list. |
| Spend/leads reading for the dashboard | No | Rules return no data; history shows only rule actions. |
| FTD, CPA by cohort, identity verdicts, My Goals targets (Rules T3 evidence) | No | Meta knows `results`/`leadgen`, not our FTD or goals. A Meta rule cannot express "T multiple". |
| Automatic mode with caps, grants, cooldown and audit | No | Those live in ACL (`src/analysis_agent/rules`). Meta offers `execution_count_limit` and `action_frequency` only for budget/bid changes [V]. |

Granularity [V]: the tightest schedule is every half hour; trigger Insights latency is minutes. That is acceptable for stop-loss but not a real-time stop.

## 5. Access and permissions

[V] No Ad Rules-specific permission is documented. [I] `ads_management` on the token/app plus an account role that can edit ads is the likely requirement.
- Official route (ACL-1844/1859): an app with standard access works for the app owner's own accounts [V]; managing buyers' accounts needs advanced access through App Review [V]. System users exist and have per-tier quotas [V]; that they can call `adrules_library` is [I]. The Limited tier allows 300 `ads_management` calls per hour per account [V], enough for a pilot, not a fleet.
- In-profile route (ACL-1760/1761): no app review, uses the buyer's browser-session token [I]. Suited to a pilot because nothing needs Meta approval, but 3.3 is open.
- `PING_ENDPOINT` additionally needs an app webhook subscription [V], which the in-profile route cannot provide.

## 6. Fit with ACL-1760/1761 (in-profile Graph) and ACL-1844/1859 (official access pilot)

- [V from repo code] `src/fb_api/write_policy.py` (ACL-1761) allows writes only to six edges (campaigns, adsets, adcreatives, ads, adimages, advideos), requires PAUSED on campaigns/adsets/ads, forbids DELETE, and forbids the key `execution_options` anywhere. Rule creation (`adrules_library`, `execution_spec`, `execution_options.user_ids`) is therefore **refused by the current policy**. Using rules would need an explicit, owner-approved policy change: a new narrow edge entry and a decision on DELETE for rules.
- ACL-1844 (#844) is explicitly read-only (GET of fixed account and Insights endpoints, no POST). A rules probe must not be added to it; it would be a separate task.
- ACL-1859 (#859) is blocked on an authorised administrator and credential. A rules pilot through the official route also waits for that.
- Reading rules (`GET adrules_library`, `/history`) is the cheapest first probe: read-only, one call, answers 3.3 without creating anything. [I] It still needs an allowlist addition in whichever read path is used.

## 7. Risks

1. **Actions without a human.** A Meta rule pauses or changes budgets with no ACL review, outside our caps, grants and cooldown, and ACL does not see it happen unless it reads history. A wrong filter (for example name CONTAIN) could hit other campaigns of the buyer. Mitigation: scope with explicit `id IN [...]` only, never by name or account-wide; PAUSE and NOTIFICATION only; one rule per object.
2. **PAUSED-only policy.** The owner policy is that ACL creates campaigns only PAUSED and a human enables them. PAUSE and NOTIFICATION rules are consistent with it (they only reduce delivery or inform). UNPAUSE and ROTATE activate ads [V]; CHANGE_BUDGET, CHANGE_CAMPAIGN_BUDGET, CHANGE_BID and REBALANCE_BUDGET change live spend [V]. All of these must stay out of scope. Pausing the wrong object still harms a buyer's campaign.
3. **Invisible state.** Trigger rules are API-only [V] and invisible to the buyer in Ads Manager; schedule rules may or may not show (3.4). A hidden rule that keeps pausing ads is a support hazard. ACL would have to list and reconcile rules (`adrules_library`, `adrules_governed`) before trusting them.
4. **Account safety.** Creating and updating rules are writes (3 rate points each [V]); account-level abuse handling (368) is undocumented. Do not make production buyer accounts the first test; use the pilot allowlist.
5. **Rule lifecycle.** Rules persist after ACL closes or the buyer leaves. A DISABLED rule is the safe off state [V]; DELETE is forbidden by the current write policy.
6. **Doc drift.** The fetched pages disagree in places (3.2; also `unique_clicks` and `reach` appear both as allowed and disallowed trigger fields). Pin an API version and re-read the pages in a browser before coding.
7. **Notification path.** NOTIFICATION reaches Facebook notifications/email [V], not the ACL dashboard [I]; a buyer who ignores Facebook notifications gets no stop-loss.

## 8. Recommendation and next steps

Recommendation: **promising, but not buildable on this evidence; do not start a rules feature yet.** Run a small gated pilot, then decide. The safest value is NOTIFICATION rules; the valuable one (PAUSE on spend with no leads) depends on error 2703.

Next steps (each needs an explicit owner go; none were done here):
1. Owner chooses the pilot route: in-profile token (needs a policy exception for `adrules_library`) or the official token from ACL-1859. Do not extend the read-only ACL-1844 probe.
2. Pilot on one allowlisted test account, PAUSED test ad set, one rule at a time:
   - a. `GET /act_X/adrules_library?fields=name,status,evaluation_spec,execution_spec`: answers token and permission (error 200 ends the test) and lists existing rules.
   - b. Create one NOTIFICATION rule with `status=DISABLED` scoped by `id IN [<paused ad set>]`; read back `status` and `disable_error_code`.
   - c. Create one PAUSE rule with `spent` greater than a small amount and `results` equal to 0 (SEMI_HOURLY schedule, and separately a STATS_CHANGE trigger variant) to settle 2703. PAUSE on an already paused ad set is a no-op.
   - d. Check Ads Manager visibility and `adrules_governed`; check that the rule survives with ACL and the profile closed.
   - e. Cleanup: set DISABLED; DELETE only with owner approval.
3. If it works: add one narrow edge to the write policy (`adrules_library`; keys `name`, `status`, `evaluation_spec`, `execution_spec`, `schedule_spec`; execution_type limited to PAUSE and NOTIFICATION; `id IN` scoping mandatory) and show rule state on the Rules page (T4), keeping the T3 engine as the owner of goals and evidence.
4. If it does not work: keep ACL-side evaluation fed from the stats pipeline (no Ads Manager polling) with a human-confirmed pause command.
5. In every case keep UNPAUSE, ROTATE and all budget/bid actions out of Meta-side rules.

## 9. Source status

All URLs were fetched on 2026-10-16. The older `/docs/marketing-api/ad-rules/overview/schedule-based-rules` and `.../trigger-based-rules` paths return 404; the working guides are under `/documentation/ads-commerce/marketing-api/ad-rules/guides/`. A web search found no Meta page describing error 2703 beyond the two reference pages.
