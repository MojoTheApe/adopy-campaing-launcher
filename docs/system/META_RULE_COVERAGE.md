# Meta compatibility rules

ACL-2125 adds three narrowly sourced checks to the existing registry and the
existing API pre-upload check. These are offline checks, not a complete Meta
eligibility matrix or evidence that Meta will accept an account's campaign.

The following official public references were read on 2026-10-27:

| Rule | Verified source and scope |
| --- | --- |
| Destination URL maximum: 1000 characters | [Ad Creative, Limits](https://developers.facebook.com/docs/marketing-api/reference/ad-creative/). The same table marks title/body lengths as recommended; those recommendations do not block submission. |
| Instant Form buttons: APPLY_NOW, DOWNLOAD, GET_QUOTE, LEARN_MORE, SIGN_UP, SUBSCRIBE | [Lead Ads, Create](https://developers.facebook.com/documentation/ads-commerce/marketing-api/guides/lead-ads/create), Step 4. The guide specifies OUTCOME_LEADS, LEAD_GENERATION/QUALITY_LEAD and a form. The rule requires this objective, goal and ON_AD destination. Values remain identical to `write_policy.CTA_FORM`; website buttons retain their existing policy. |
| No Facebook right-hand column for Instant Forms using LEAD_GENERATION | [Campaign, Objective and Placements](https://developers.facebook.com/documentation/ads-commerce/marketing-api/reference/ad-campaign-group#placement). The table prohibits right_hand_column for legacy LEAD_GENERATION Page post ads. This check is restricted to the current explicitly documented Instant Form objective/goal/destination combination, not every OUTCOME_LEADS campaign. It does not extrapolate to QUALITY_LEAD. |

`placements_by_objective` may contain either an exhaustive `allowed` list or a
nonempty `disallowed` list. A disallowed-only rule rejects only its named values;
it cannot prove that unlisted placements are supported. Only a generally
available prohibition is enforced. Graph position fields are normalized to
`platform.position` identifiers without treating audience or publisher fields as
positions. The existing creator adapter exposes the actual ad-set targeting.

URL validation uses the submitted creative URL, including the resolved website
URL. Image Instant Forms use Meta's fb.me placeholder; video Instant Forms carry
the form ID without a website link. An unused draft website URL does not block
either. Creative names, ownership tags and visible headlines are not conflated.

The existing ACL-2052 value-rule/account-control and limited-spend facts remain
unchanged. This work adds no account controls, placement writes, profiles or live
Meta validation. Unknown combinations remain unknown. No universal ad-copy hard
limit or complete ODAX CTA/placement table was established by these sources;
the existing asset-feed text bounds and write-policy checks remain separate.

The docs watcher includes the three sources. Future verified restrictions belong
in the same registry and translation boundary, with the full applicable
conditions, instead of a second validation handler.
