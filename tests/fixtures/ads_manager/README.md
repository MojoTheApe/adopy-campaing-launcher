# Recorded Ads Manager bench

These are sanitized fragments captured by the original author on **2026-09-09**, imported in PR #132. They are historical selector fixtures, not a live integration test. The original capture did not record a Meta build identifier; it is **unknown** and must not be inferred from the date. The cropped HTML can begin/end inside a node. Browser error recovery and native checkbox behavior are tested separately; these fragments do not include Meta's JavaScript or its server-side save behavior.

| Fixture | Capture state | Coverage limit |
| --- | --- | --- |
| placements_2026_09_09.html | Categories visible; subcategories collapsed | Category locators and expansion controls; cannot confirm current subitem labels or saving |
| ad_copy_2026_09_09.html | Copy section visible; destination hidden | Primary text/headline lookup and visibility |
| ad_destination_2026_09_09.html | Destination section visible | Website URL lookup |
| conversion_event_2026_09_09.html | Dataset selected, event still empty | Dataset alone must not mean event selection succeeded |

## ACL-1313 fragments (live run 2026-09-24)

Cut from `output/rpa_pages/<run>/<step>/page.html` (and one telemetry record) of the 24.09 evening run. Class attributes and inline SVG paths were stripped, campaign/ad names, object ids, creative copy, media URLs and the account id in `cachekey` were replaced with synthetic values; structure, `data-surface`/`data-block`/`id` attributes and visible labels are kept. UI language: English. Meta build identifier: not recorded. Browser: Multilogin Chrome, version not recorded.

| Fixture | Source / capture state | Coverage limit |
| --- | --- | --- |
| ad_copy_draft_split_2026_09_24.html | 7364047ef03c/04_ad_copy — Primary text field after the failed retry: one `[data-block]`, the Draft.js leaf with the whole text plus 7 orphan paragraphs Chrome cut from a multi-line `insert_text` | No Draft.js runtime; typing behaviour is emulated in the test |
| library_tile_2026_09_24.html | telemetry `ad_creative_reuse` 20:19:29 (da86e97cbf8b), `diag.tile_shape.html` — one media-library tile | Single tile, no grid virtualisation; image removed |
| editor_tree_adset_2026_09_24.html | d3e690189a6e/04_create_additional_ad — left tree grid: campaign, ad set (with action-menu button), ad | Hover-only menu; the "Create ad" menu is injected by the test |
| route_placeholder_2026_09_24.html | 7364047ef03c/05_create_additional_ad — `AdsPERouteLoadPlaceholder` spinner instead of the editor | Spinner only |
| breadcrumb_only_2026_09_24.html | 7d2ce3ab1161/05_create_additional_ad — `nav-header-*` breadcrumb and header action-items selector, no tree rows | Header only |
| audience_advantage_on_2026_09_24.html | b30c3b2b6be9/00_advantage_plus_before — Audience and Placements section-header badges, both "Advantage+ on" | Badges only, no audience controls |
| audience_off_placements_on_2026_09_24.html | 8587f7668f86/02_languages — Audience "Advantage+ off", Placements "Advantage+ on" | Badges only |

## ACL-1313 fragments (live run 2026-09-25)

Same cutting rules as above. UI language: English. Meta build identifier: not recorded. Browser: Multilogin Chrome, version not recorded.

| Fixture | Source / capture state | Coverage limit |
| --- | --- | --- |
| languages_preselected_2026_09_25.html | 79d4d978f8c7/02_languages (SA, capture truncated at 3.98 MB but the block is whole) — Audience → Languages expanded, the chip "Arabic" preselected by Meta for the country (no "(All)"), empty "Search languages" combobox. Classes, svg, sprite `<i>` and React props dumped as attributes removed; no names or ids in the block | No suggestion list; no Meta runtime |

## ACL-1284 port fragments (live run 2026-09-24)

Same cutting rules as above. UI language: English. Meta build identifier: not recorded. Browser: Multilogin Chrome, version not recorded.

| Fixture | Source / capture state | Coverage limit |
| --- | --- | --- |
| audience_age_open_2026_09_24.html | d3e690189a6e/01_advantage_plus_control (GT, capture truncated at 3.98 MB but the block is whole) — Audience → Age row expanded: the row header "Age" (`role=button`, `aria-expanded=true`) plus the field's own `<label>` "Age", `<label>` "Minimum age" / "Maximum age" and the two comboboxes showing 18 and 65+. Classes and svg paths removed; no names or ids of the account in the block | No option list; no Meta runtime; no CSS (everything renders visible) |

Tests intercept network requests. Open only in a disposable offline browser. Never use a real Multilogin profile for this bench.

## Refresh procedure

1. During a separately authorized live investigation, keep the failure's local `output/rpa_pages/<run>/<step>/page.html` and `context.json`. These files are private diagnostics, not commit-ready fixtures; automatic scrubbing is incomplete. Do not send them through telemetry or commit the raw capture.
2. Work on a copy. Extract the affected section with enough ancestor markup to preserve visibility and containment. Remove executable scripts, event handlers, embedded frames and remote media references. Replace account, campaign, pixel, profile and employee names/IDs, budgets/balances, creative copy/links, tokens, form values and resource URLs with synthetic examples. Keep selector labels and the required DOM structure.
3. Inspect both source and rendered text/attributes; run `python scripts/acl_scan_secrets.py`. A green scanner is only an additional check, not proof that private names have been removed.
4. Name the fixture with its actual capture date. Record the failing step, UI language/layout, whether the capture was truncated, the browser version and the Meta build identifier if the page exposes one. If an item is unavailable, state that explicitly. Never invent version metadata.
5. Run `tests/test_ads_manager_bench.py` and `tests/test_conversion_event_is_really_set.py` in offline Chromium. Include a negative case that removes/changes a relevant selector and makes the real lookup fail. Review the fixture and product diff together.

Review these fixtures whenever a live selector fails or a Meta layout changes. Unchanged historical fixtures cannot establish that today's Ads Manager still works. The original Meta build and expanded placement subcategories remain unrecorded; a separately authorized capture is needed to close those evidence gaps.

## ACL-1313 banners (live runs 2026-09-24/25)

`banners_2026_09_25.html` holds several fragments, each introduced by a `<!-- fragment: <name> | source: <run>/<step> -->` comment. Cut the same way as above: classes, `data-auto-logging-id`, SVG paths and help-centre links stripped; message ids in `data-surface` replaced with `0`. UI language: English. Meta build identifier and browser version: not recorded. In the live DOM the hidden `Close` text of the banner X is visually hidden by a class; without Meta's CSS it renders as plain text.

| Fragment | Source | Coverage limit |
| --- | --- | --- |
| aymt_contact | 279ead80503a/00_objective_leads — blue top bar "Add your contact information" | One message, no stacking of several AYMT messages |
| aymt_phone | 8814247ec92e/00_objective_leads — blue top bar "Add your phone number" | Same |
| card_whatsapp_status, card_conversions_api | 5b223701b271/00_advantage_plus_before — ad set cards with X | Card only; the editor around it is the test's own wrapper |
| card_instant_form_filters | 90a8987edad9/00_conversion_website | Card only |
| card_site_links | 393bfda8b0db/02_ad_media_ready | Card only |
| card_show_offerings_no_close | 5b223701b271/06_ad3_creative_upload — card without X ("Remind me later" opens a new tab) | Card only |
| editor_drawer_header | 5b223701b271/00_advantage_plus_before — `data-surface="/am/editor_drawer"` header with the editor's own `aria-label="Close"` | Header only |

"Updated business terms", "Act now — Verification may be required soon", the "Turn off ad blocker" modal and the "Your internet connection was lost" toast were never captured in `rpa_pages`; their markup in the tests is synthetic, modelled on the real AYMT bar / cards and on the screenshots of run 279ead80503a.

## ACL-1313 placements node (live run 2026-09-25)

`placements_node_2026_09_25.html` — 10402d9c2daf/01_placements (CL_ML, step `placements` failed: "Messenger Stories"/"WhatsApp Status" — "строка не найдена"; capture truncated at 3.98 MB, the block is whole). The whole `data-rpa-placements-marker` card: "Placements" node expanded (`Included: 16`), five categories with the expander above the row (`role=button[aria-expanded]`), "In-stream ads for reels" expanded with its two real subitem rows; "Feeds" and "Stories, Status, Reels" collapsed, so their rows are not in the capture. The marker attribute, classes, `data-*`, inline styles, links, images and SVG paths were removed; Meta render ids (`js_*`) replaced with `syn_N`. No account, campaign or creative data in the block. UI language: English. Meta build identifier and browser version: not recorded.

Coverage limit: no Meta JS. `tests/test_rpa_placements_category_recollapse.py` builds the collapsed categories' rows by cloning the real In-stream row and emulates the accordion, the delayed `aria-checked` and the collapse after a save.

## ACL-1284 collapsed Age row (live run 2026-09-25)

`audience_age_collapsed_2026_09_25.html` — run 5e29e66da497 (CL_ML_All_Video_FX, step `audience_age` failed after 318 s). Three fragments: `collapsed` (01_audience_age — the whole Audience card in the summary layout: every row is a `role=button aria-expanded=false`, "Age 18 - 65+", the pencil is an unnamed `role=presentation` icon), `open_card` (00_advantage_plus_still_on — the same card a minute earlier with the Age field open: header row `aria-expanded=true`, two `role=combobox` triggers "18"/"65+") and `open_age` (the open Age field alone). Classes, ids, `data-auto-logging-id` and SVG paths removed; Meta render ids (`js_*`) replaced with `syn_N`. No account or campaign data in the block. UI language: English. Meta build identifier and browser version: not recorded.

Coverage limit: no Meta JS. `tests/test_collapsed_age_row_is_opened.py` emulates the row toggle (click on a collapsed row opens it, click on the open field's header collapses it).
