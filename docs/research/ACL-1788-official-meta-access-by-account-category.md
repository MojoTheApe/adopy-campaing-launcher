# ACL-1788 - Which ad accounts can use Meta's official path

Status: desk research, 2026-10-16 (extends the 2026-10-11 result on Issue #788).
Scope: general and per category only. No account, app, token, login or Meta API
call was touched; the real account map and secrets were not read.
Related: ACL-1844 (read-only diagnostic pilot), ACL-1858/1859/1860/1883
(official-access chain), ACL-1872 (isolated per-account app credentials),
ACL-1760 (in-profile Graph route).

"Official path" here means: a Meta developer app plus a token (system user or
user) calling the Marketing API over HTTPS, instead of a browser session in a
Multilogin profile.

## 1. Verified facts (read from the page by WebFetch; summaries, not raw text)

| # | Fact | Source |
|---|---|---|
| F1 | `ads_read` reads ads data/reports, `ads_management` manages ads. Standard access to them is enough if the app manages only your own ad account; managing other people's accounts needs Advanced access. | https://developers.facebook.com/documentation/ads-commerce/marketing-api/get-started/authorization |
| F2 | Standard access needs no App Review and reaches only data of people with a role on the app (Admin/Developer/Tester). Advanced access is approved per permission via App Review and reaches non-role users. Business Verification is required for Advanced access (since 2023-02-01 apps requesting it may need a verified business). | https://developers.facebook.com/docs/graph-api/overview/access-levels and https://developers.facebook.com/docs/development/release/business-verification |
| F3 | Apps used only by people with a role on the app do not need Business Verification. For apps serving other businesses, until verification is done those users cannot grant permissions and features are inactive. | https://developers.facebook.com/docs/development/release/business-verification |
| F4 | Separate "Marketing API Access Tier": Limited (default, "for development only", heavily rate limited, 1 system user + 1 admin system user) and Full (after App Review; 10 + 1; lightly rate limited). Full needs 500+ calls in 15 days and under 15% errors over the last 500 calls (threshold lowered from 1,500). Names changed from Standard/Advanced; older docs use the old names. | https://developers.facebook.com/docs/marketing-api/overview/authorization and the authorization page above |
| F5 | Both tiers allow an unlimited number of ad accounts. Full access is "with `ads_read` or `ads_management` permission from the account". | authorization page above |
| F6 | System users are servers/software calling APIs on assets "owned or managed by a Business Manager". The admin system user can create system users/ad accounts and assign permissions (Meta: use only for admin actions); a plain system user reaches only assets granted to it. Limits depend on the access level (F4). Needs a real person as admin of the business and an app claimed by the same business. | https://developers.facebook.com/docs/marketing-api/system-users and https://developers.facebook.com/docs/business-management-apis/system-users/overview |
| F7 | A system user token needs the system user and the app in the same Business Manager; the app must have Ads Management Standard access or higher. The token is either non-expiring or expiring (60 days, refreshable); Meta calls expiring a best practice and some businesses are forced to use it. | https://developers.facebook.com/docs/business-management-apis/system-users/install-apps-and-generate-tokens |
| F8 | System user tokens cannot tie an action to a specific person; for that Meta points to OAuth with a user token. | https://developers.facebook.com/docs/marketing-api/system-users |
| F9 | For an ad account an owning business appears as `OWNER` or `AGENCY` in `owned_ad_accounts`. Claiming is a one-time procedure by a business admin; if you are not the account admin an ownership request goes to its admins. "Once claimed, you can only manage the ad accounts in that Business Manager." `OWNER`+`CONFIRMED` accounts cannot be removed via API; `AGENCY` access can. | https://developers.facebook.com/docs/business-management-apis/business-asset-management/guides/ad-accounts |
| F10 | Roles on an ad account: `ANALYZE` (reports only), `ADVERTISE`+`ANALYZE` (general user), `MANAGE`+`ADVERTISE`+`ANALYZE` (admin). | same page |
| F11 | Agency/partner access: the requesting business calls `client_ad_accounts` with `permitted_tasks`; the account's admins approve; the owning business can grant via `act_<id>/agencies`. A business that is only an `AGENCY` cannot grant access to assets it does not own. Some grants need another admin's approval. Meta says access to these endpoints has been limited since 2021-06-08 (apps without access get an error). Whether the partner's system user can then use the granted account is not stated. | https://developers.facebook.com/docs/business-management-apis/business-asset-management/guides/business-to-business |
| F12 | Rate limits per ad account: score-based (read = 1, write = 3); development tier max 60, 300 s decay and 300 s block; Full max 9000, 60 s block. Per-hour BUC: `ads_insights` 600 (dev) vs 190,000 (full) + 400 x active ads; `ads_management` 300 (dev) vs 100,000 (full) + 40 x active ads. Insights also has app-level limits. Error codes 17, 613, 80000/80003/80004/80014, 4. Headers `X-Ad-Account-Usage`, `X-Business-Use-Case`, `X-FB-Ads-Insights-Throttle`. | https://developers.facebook.com/docs/marketing-api/overview/rate-limiting |
| F13 | Ads MCP (`https://mcp.facebook.com/ads`): OAuth sign-in for a Facebook account or Meta Managed Account, or a user token as Bearer with `ads_mcp_management`, `ads_read`, `ads_management`, `catalog_management`, `business_management`, `pages_show_list`, `instagram_basic`. Using your own app needs the "Create & manage ads with ads MCP server" use case. System-user token use and limits are not covered on the page. | https://developers.facebook.com/documentation/ads-commerce/ads-ai-connectors/ads-mcp-server/ads-mcp-server-get-started |
| F14 | Webhooks need an app secret, an HTTPS callback with `hub.verify_token` and an HMAC-SHA256 check, and an admin or system-user token with `ads_management` to connect an ad account. | https://developers.facebook.com/documentation/ads-commerce/marketing-api/ads-webhooks/setup/get-started (read on 2026-10-11) |

Refinement of the 2026-10-11 note: system user tokens are not simply
"non-expiring"; F7 says you choose non-expiring or 60-day expiring, and some
businesses are required to use expiring.

## 2. Weakly sourced (search snippets only; page body unreadable)

The Meta Business Help Center pages return only a title to our fetcher (JS
shell). The statements below come from a search-engine summary of those pages
and must be re-confirmed by a human in the UI:

- W1: Partner access can be full control or task-based; only someone with full
  control of the owning portfolio can share; a partner cannot reshare the asset.
  https://www.facebook.com/business/help/1717412048538897
- W2: An ad account created in a portfolio cannot be transferred to another
  business's portfolio. (F9 documents a claim path via API; treat any claim as a
  one-way commitment either way.)
- W3: Unless an agency's portfolio is verified, it can only manage accounts
  owned by the same company that owns it.
  https://www.facebook.com/business/help/563249781254715

## 3. Account categories

Categories are by the relationship between ACL's own business portfolio and the
ad account. Which accounts fall in which category is unknown to us: the account
map does not record the owning portfolio (see the Issue #788 comment).

| Cat | Situation | Official path? | What it takes | Main risks |
|---|---|---|---|---|
| A | Owned by a portfolio we administer (`OWNER`, a real admin person of ours) | Yes - the intended case (F1, F6) | Our app (Business type, Standard access is automatic) owned by the same portfolio; a system user in it; the account assigned to that system user with `ANALYZE` (read) or more; token with `ads_read` (add `ads_management` only for a later write pilot). Limited tier is fine for a one-account read pilot; Full tier for production volume. No App Review or Business Verification while only role holders use the app (F2, F3). | Token is a bearer secret with no per-person attribution (F8). Limited-tier read quota is small (F12: 600 insights/hour). One portfolio/app over many accounts is a single point of failure (inference). A new app/system user may be a new automation signal (docs silent). |
| B | Owned by another portfolio, shared to ours as agency/partner access (`AGENCY`) | Possible, not proven | Per F1/F2 another business's accounts need Advanced `ads_read`/`ads_management` = App Review plus Business Verification of our portfolio. The owner must approve the share (F11). Whether our system user can be assigned a partner-shared account is not stated (F11): test on one account. Alternative: a user token from a person who has the access, which stays person-bound (F8). | Owner can revoke at any time (F9: AGENCY access is removable). Verification and review are slow and can be denied. App Review puts a tool acting for other advertisers under Meta's use-case review. |
| C | Rented / purchased / vendor-owned account, used by logging in as a person (browser profile), no portfolio-level share | Not on the documented path. We have no role on the portfolio, so we cannot create a system user or assign the asset (F6, F9) | Only if the vendor shares it (becomes B), or a user token from the logged-in person for our own app, which for a non-role person needs Advanced access (F2). A token used inside the person's browser profile is the in-profile route (ACL-1760), not the official path. | Not fixable by us. The vendor may refuse, or may itself be the restricted party: project notes say the July 2026 bans cascaded from restricted purchased/rented portfolios. Linking our app to such an account ties it to that portfolio's standing (inference). Claiming (F9) is a permanent commitment to a portfolio of unknown quality - do not. |
| D | Owned by a client business that is our customer and will add us | Same as B, plus consent | Same as B; their admin approves; contract/data terms are outside this research. | Same as B; customer revocation; their portfolio's restrictions affect us. |
| E | Owned by a portfolio we control, but no real admin person or admin access lost | No, until recovered (F6 needs a real person as admin) | Recover admin first. | Locked portfolio cannot create system users. |
| F | Account or portfolio restricted, disabled or in review | Do not connect anything new | Resolve the restriction first. | Connecting a new app to a restricted portfolio may extend the restriction (inference from July 2026). |

Routes by product, same categories:
- System user + Marketing API: A yes, B test first, C/D/E/F no or only after the changes above.
- User token (OAuth, person-bound): works for whatever accounts that person can
  see; for people without a role on our app it needs Advanced access (F2).
- Webhooks (F14): need an admin or system-user token with `ads_management`, so
  they inherit the category result and also need a public HTTPS endpoint; they
  are not a read-only first step.
- Ads MCP (F13): user-token/OAuth based; not a way around ownership; the
  system-user rule is unconfirmed, keep optional.

## 4. Risks that apply to all categories

1. Ownership commitment: claiming is one-time (F9). Never claim an account whose
   portfolio is not ours or is unclear.
2. Single point of failure: one app/portfolio/system user over many accounts
   (inference). ACL-1858/ACL-1872 choose one app per connection; that limits the
   blast radius locally, not on Meta's side.
3. Review dependency: Advanced access, Business Verification and the Full tier
   are decided by Meta and take elapsed time (F2, F4); Full also needs steady
   traffic to be kept (500 calls per 15 days).
4. Tier mismatch: Limited is "for development only" (F4). A pilot is fine; a
   production dashboard on it can hit the 60-point or 600/hour limits (F12).
5. Token handling: non-expiring system tokens are high-value bearer secrets;
   expiring tokens need refresh logic (F7).
6. Terms: calls at any level run against production data and must follow the
   Platform Terms (authorization page).
7. A successful read does not prove ability to publish (ACL-1844 says the same).

## 5. Per-account decision checklist (owner; no live access by us)

The owner or a manager answers these in Meta Business Settings, one row per
account. No token or login is shared with us; record only yes/no/unknown plus
the portfolio name.

1. Which business portfolio holds the account (Business Settings > Accounts >
   Ad accounts)? Is it listed as owned by ours, or as a client/partner account?
   Not owned by ours -> category B/C/D; go to step 6.
2. Is that portfolio created and administered by us (a named real person with
   full control)? No -> category C/E. Stop.
3. Is the portfolio or account restricted, in review, or part of the July 2026
   cascade? Yes -> category F. Stop.
4. Is there one test account in it that can be sacrificed and is kept separate
   from production assets (project note on ban risk)? No -> do not pilot there.
5. Category A: can a Business-type app owned by the same portfolio be used or
   created (ACL-1872 isolates one per connection)? Then: system user with
   `ANALYZE` on this one account, token with `ads_read`, run the ACL-1844
   read-only diagnostic. Record the result, Limited vs Full tier, and the token
   expiry choice.
6. Categories B/D: ask the owning business whether it will share the account
   with our portfolio as a partner (task-based, `ANALYZE` first). If yes, record
   whether our system user can be assigned the shared account (F11 leaves this
   open). If it cannot, the route needs Advanced access and Business
   Verification: decide whether that is worth it for the number of accounts.
7. Category C: do not claim, do not ask to transfer. Keep the browser route
   (ACL-1760) or ask the vendor for a share; there is no other official path.
8. Decide once per portfolio, not per account: is the Full tier needed (many
   accounts, regular insights) or is Limited enough?

Result: a table with account alias, portfolio, our role, category, decision and
tier needed. Only category A rows go on to ACL-1844/ACL-1858.

## 6. Inferences (not documented)

- I1: "Your own accounts" in F1 means accounts of people with a role on our app
  (F2), not necessarily portfolio ownership. A role-holding person who has
  access to a vendor account could use Standard access with their own user token,
  but that token is person-bound and the account remains outside our control
  (category C).
- I2: The July 2026 bans suggest adding any new automation identity to a
  restricted portfolio is unwise; Meta docs are silent on this.
- I3: Webhooks need the same ownership as the system-user route.
- I4: The Limited tier gives 1 + 1 system users (F4). Under the owner's rule of
  one app per connection, the cap probably applies per app, which would not
  block per-account apps. The docs do not say; verify when the first app exists.

## 7. Open questions for a human

- Does the vendor's portfolio allow partner sharing or system users at all?
- Can a partner's system user be assigned a shared account (F11)?
- What does the Meta Business Help Center currently say on claim vs share
  (our fetcher gets only the title)?
- Are the business-to-business endpoints (F11, access limited since 2021)
  usable by our app without extra approval?
