# Recorded Ads Manager bench

These are sanitized fragments captured by the original author on **2026-09-09**, imported in PR #132. They are historical selector fixtures, not a live integration test. The original capture did not record a Meta build identifier; it is **unknown** and must not be inferred from the date. The cropped HTML can begin/end inside a node. Browser error recovery and native checkbox behavior are tested separately; these fragments do not include Meta's JavaScript or its server-side save behavior.

| Fixture | Capture state | Coverage limit |
| --- | --- | --- |
| placements_2026_09_09.html | Categories visible; subcategories collapsed | Category locators and expansion controls; cannot confirm current subitem labels or saving |
| ad_copy_2026_09_09.html | Copy section visible; destination hidden | Primary text/headline lookup and visibility |
| ad_destination_2026_09_09.html | Destination section visible | Website URL lookup |
| conversion_event_2026_09_09.html | Dataset selected, event still empty | Dataset alone must not mean event selection succeeded |

Tests intercept network requests. Open only in a disposable offline browser. Never use a real Multilogin profile for this bench.

## Refresh procedure

1. During a separately authorized live investigation, keep the failure's local `output/rpa_pages/<run>/<step>/page.html` and `context.json`. These files are private diagnostics, not commit-ready fixtures; automatic scrubbing is incomplete. Do not send them through telemetry or commit the raw capture.
2. Work on a copy. Extract the affected section with enough ancestor markup to preserve visibility and containment. Remove executable scripts, event handlers, embedded frames and remote media references. Replace account, campaign, pixel, profile and employee names/IDs, budgets/balances, creative copy/links, tokens, form values and resource URLs with synthetic examples. Keep selector labels and the required DOM structure.
3. Inspect both source and rendered text/attributes; run `python scripts/acl_scan_secrets.py`. A green scanner is only an additional check, not proof that private names have been removed.
4. Name the fixture with its actual capture date. Record the failing step, UI language/layout, whether the capture was truncated, the browser version and the Meta build identifier if the page exposes one. If an item is unavailable, state that explicitly. Never invent version metadata.
5. Run `tests/test_ads_manager_bench.py` and `tests/test_conversion_event_is_really_set.py` in offline Chromium. Include a negative case that removes/changes a relevant selector and makes the real lookup fail. Review the fixture and product diff together.

Review these fixtures whenever a live selector fails or a Meta layout changes. Unchanged historical fixtures cannot establish that today's Ads Manager still works. The original Meta build and expanded placement subcategories remain unrecorded; a separately authorized capture is needed to close those evidence gaps.
