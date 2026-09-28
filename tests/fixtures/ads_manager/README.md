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

## ACL-1324 "Suggest an audience" layout (live run 2026-09-26)

`audience_suggest_layout_2026_09_26.html` — six fragments, each the whole Audience card (from the card header with `#advantage-state-audience-section-header` down to its last row), introduced by a `<!-- fragment: <name> | source: <run>/<step> -->` comment. Cut in offline Chromium from `output/rpa_pages/<run>/<step>/page.html`: scripts, images and SVG replaced by `<i>`, every attribute except `role`, `aria-*`, `id`, `data-surface`, `placeholder`, `type`, `value`, `tabindex`, `hidden`, `href` (set to `#`), `for`, `typeaheadcomponentid` removed; Meta render ids (`js_*`) replaced with `syn_N`; detailed-targeting interests replaced with `Sample interest N`. No account, campaign or creative data in the blocks. All captures were truncated at ~3.98 MB; the Audience card is whole in each. UI language: English. Meta build identifier and browser version: not recorded.

| Fragment | Source | Capture state | Coverage limit |
| --- | --- | --- | --- |
| suggest_classic | 90a8987edad9/00_conversion_website (SA, Doc19) | Pre-state: Controls (All locations) + "Suggest an audience", gear row "Further limit the reach of your ads" + "Save audience" | No dialog markup; "Reach your preferred audiences" is synthetic in the test |
| suggest_fx | 97370c743f44/00_further_limit_reach_click (Facebook_6540, CO_bnc FX) | The same layout in the failing run: custom audiences expanded, gear row + "Save audience" | Why the live Playwright click timed out is not in the capture; the test models it with a layer over the row |
| controls_suggestion_on | 7cfdc772376d/02_advantage_plus_audience (CO_bnc GMB) | After Further limit: card collapsed, "Advantage+ on", Age "35 - 65+ · Your suggestion", Languages "All languages" | Opening the Age row and the checkbox are emulated |
| languages_open_empty | 7cfdc772376d/01_languages | Languages field open, empty; `placeholder="Search languages"` on both the tokenizer `<span>` and its `<input role=combobox>` | No suggestion list |
| controls_off | 8c6496e50815/01_placements (same account, a run that passed) | "Advantage+ off", Age "35 - 65+", Languages "Spanish (All)", "Use recommended setup" recommendation card | Summary only |
| age_open_suggestion | 543dc9daad53/00_languages (GT) | Age field open (35 / 65+) with the "Use as a suggestion" checkbox (`aria-labelledby`), unchecked | The checked state is set by the test |

## ACL-1327 single-screen editor (live run 2026-09-27)

`simple_create_2026_09_27.html` — 9e7d67471c6b/00_next_to_adset (Facebook_9943, step `next_to_adset` failed: the flow did not recognise Meta's new editor `/adsmanager/manage/simple_create` and looked for Next). Capture truncated at 3.98 MB; the editor block is whole. Cut in offline Chromium (JS disabled): the smallest element holding the left tree (`/am/editor_drawer/editor_tree:*`), the three level sections (`data-surface="/am/editor:campaign" | "/am/editor:ad_set" | "/am/editor:ad"`, level-2 headings "Campaign" / "Ad set" / "Ad") and the footer Publish (`/am/lib:syd_simple_create_publish`). Scripts, styles, classes, SVG/images removed; every attribute except `role`, `aria-*`, `id`, `data-surface`, `placeholder`, `type`, `value`, `tabindex`, `hidden`, `for`, `checked`, `disabled`, `name`, `href` (set to `#`), `min`/`max`/`step` removed; Meta render ids (`js_*`) replaced with `syn_N`; campaign name, Facebook Page name/handle, dataset name, pixel id, the three object ids and the budget input's `value` (now `10.0`) replaced with synthetic ones. Meta's helper text under the budget ("$25.00 per day … $43.75 … $175.00"), the start date and the estimated audience size are left as rendered — they identify no account. UI language: English. Meta build identifier and browser version: not recorded.

State: objective Leads (chosen in the dialog before the editor), Conversion location "Instant forms" with the Page selector and the card "Please read and accept Meta's Lead Ads Terms" / "Terms of Service Not Accepted"; collapsed summary cards (`role=button aria-pressed=false` + `<div hidden>` body): Campaign details (objective radio list inside), Budget & schedule, Audience ("Suggest an audience" layout with "Further limit the reach of your ads" + "Save audience" in the hidden body), Placements, Identity; Budget, Ad setup and Destination expanded.

Coverage limit: no Meta JS — card expansion and the objective choice are emulated by `tests/test_the_single_screen_editor_is_walked.py`. The Conversion location option list, the ad-level state after Website, the creative wizard and adding a second ad were never captured in this editor. The loading shell of the same editor (c98a1c094dfc/00_simple_create_layout: footer Publish + `role=progressbar` "Loading...") is inlined in the test.

## ACL-1327 single-screen editor Locations (live run 2026-09-28)

`locations_simple_create_2026_09_28.html` — run 97314905598e (Facebook_1443, CO_bnc_All_Video_FX, `simple_create` editor), captures truncated at 3.98 MB but the blocks are whole. Three fragments: `field_target` (01_audience_location — the Locations field collapsed back into its summary `<span><strong>Inclusion:</strong> Colombia</span>` after the robot removed the default country; row `role=button aria-expanded=false`), `field_default` (00_advantage_plus_still_on — the same field before the step, `Inclusion: United States`), `card_summary` (02_ad_creative_upload — the Audience card's `role=toolbar` header (heading "Audience", badge "Advantage+ off") and the card collapsed into its `role=button aria-pressed=false` summary grid "Locations | Colombia", labelled by that toolbar). Classes, `data-auto-logging-id`, links and SVG paths removed; the toolbar id and the card's `aria-labelledby` render id replaced with `syn_1`; an empty `id` dropped. No account or campaign data in the blocks. UI language: English. Meta build identifier and browser version: not recorded.

Coverage limit: no Meta JS; `tests/test_simple_create_location_summary.py` emulates the collapse of the open field into the captured summary after the X of the extra country.

## ACL-1327 single-screen editor footer (live run 2026-09-28)

`editor_footer_simple_create_2026_09_28.html` — 97314905598e/02_ad_creative_upload (capture truncated at 3.98 MB, the block is whole): the footer of the `simple_create` editor — "By clicking **Publish**, you acknowledge…" line, `Close` button and the `Publish` button `data-surface="/am/lib:syd_simple_create_publish"`. Classes, `data-auto-logging-id`, `data-interactable`, links and SVG paths removed. No account or campaign data. UI language: English. Meta build identifier and browser version: not recorded.

Coverage limit: no Meta CSS — `tests/test_no_press_lands_on_publish.py` pins it as a full-width fixed bar over the target, the way the live screenshot shows it.

## ACL-1354 single-screen editor failures (live runs 2026-09-28)

Cut in offline Chromium (JS disabled) from `output/rpa_pages/<run>/<step>/page.html`; captures truncated at ~3.98 MB, the blocks are whole. Scripts, styles, SVG/images removed; every attribute except `role`, `aria-*`, `id`, `data-surface`, `placeholder`, `type`, `value`, `tabindex`, `hidden`, `for`, `checked`, `disabled`, `name`, `href` (set to `#`), `typeaheadcomponentid` removed; Meta render ids (`js_*`) replaced with `syn_N` (numbering restarts in each fragment). No account, campaign, page or creative data in the blocks. UI language: English. Meta build identifier and browser version: not recorded.

`objective_open_2026_09_28.html` — the dialog on the campaigns list while the robot waited for the objective dialog after Create:

| Fragment | Source | Capture state | Coverage limit |
| --- | --- | --- | --- |
| table_loading | 9d113c3d6ac3/00_stall_objective_wait (Facebook_9943) | The campaigns table's own progress modal `data-surface="/am/table/lib:ads_progress_dialog_modal_…"`, "Loading..." (also on the screenshot 60 s later) | No list around it; how long Meta keeps it is not in the capture |
| create_loading | 279b9539df43/00_stall_objective_wait (Facebook_9943) | Create's progress modal `data-surface="/am/lib:ads_progress_dialog_modal_…"`, "Loading creation" | Same |
| ad_blocker | 279b9539df43/01_objective_leads | Meta's "Turn off ad blocker" modal (heading, Close, OK) over the list at the `objective_leads` failure | Why Meta showed it is not in the capture |

`audience_fields_simple_create_2026_09_28.html` — Audience fields of the single-screen editor:

| Fragment | Source | Capture state | Coverage limit |
| --- | --- | --- | --- |
| languages_open_empty | aa1e2fbf45c9/02_languages (Facebook_1443, `simple_create`) | Languages row expanded (`aria-expanded=true`), the tokenizer with an empty `<input role=combobox aria-expanded=false aria-controls=…>`; the listbox it points to does not exist | Captured after the step cleared its input; the suggestion list was never captured — typing and suggestions are emulated/absent in the test |
| targeting_loading | aa1e2fbf45c9/03_targeting_open (same run) | Detailed targeting row expanded, its `role=region` holds only `role=progressbar aria-busy=true aria-valuetext="Loading..."` | The loaded state of this field in `simple_create` was not captured |
| targeting_search_loaded | 0749abd7482f/02_placements (classic ad set editor, same day) | The loaded field's `role=search` box with `<input placeholder="Add demographics, interests or behaviors">` (interests list not included) | Used as the loaded state of the field above; classic editor markup |

Coverage limit: no Meta JS. `tests/test_single_screen_editor_failures_are_named.py` emulates the row toggle (a click collapses/expands the row, a collapsed row does not load, a re-expanded one starts loading again) and the moment the field finishes loading.
