# Recorded Ads Manager bench

These are sanitized fragments captured by the original author on **2026-09-09**, imported in PR #132. They are historical selector fixtures, not a live integration test. The original capture did not record a Meta build identifier; it is **unknown** and must not be inferred from the date. The cropped HTML can begin/end inside a node. Browser error recovery and native checkbox behavior are tested separately; these fragments do not include Meta's JavaScript or its server-side save behavior.

| Fixture | Capture state | Coverage limit |
| --- | --- | --- |
| placements_2026_09_09.html | Categories visible; subcategories collapsed | Category locators and expansion controls; cannot confirm current subitem labels or saving |
| ad_copy_2026_09_09.html | Copy section visible; destination hidden | Primary text/headline lookup and visibility |
| ad_destination_2026_09_09.html | Destination section visible | Website URL lookup |
| conversion_event_2026_09_09.html | Dataset selected, event still empty | Dataset alone must not mean event selection succeeded |

## ACL-1886 permission error evidence (2026-10-13)

`fb_api_permission_denied_2026_10_13.json` reconstructs only the shared visible
fields from two owner-reported Plan check errors: Graph10, OAuthException,
Permission Denied, HTTP400 and the fixed Ads Manager Graph host/cookie mode.
The screenshot does not establish which operation failed or why access was
denied. No account labels, identifiers, token, cookie, request URL or raw capture
are included. Tests inject this response at the real GraphClient boundary for
synthetic request paths; they verify diagnostics, not actual Meta permissions.

## ACL-1313 fragments (live run 2026-09-24)

Cut from `output/rpa_pages/<run>/<step>/page.html` (and one telemetry record) of the 24.09 evening run. Class attributes and inline SVG paths were stripped, campaign/ad names, object ids, creative copy, media URLs and the account id in `cachekey` were replaced with synthetic values; structure, `data-surface`/`data-block`/`id` attributes and visible labels are kept. UI language: English. Meta build identifier: not recorded. Browser: Multilogin Chrome, version not recorded.

| Fixture | Source / capture state | Coverage limit |
| --- | --- | --- |
| ad_copy_draft_split_2026_09_24.html | 7364047ef03c/04_ad_copy — Primary text field after the failed retry: one `[data-block]`, the Draft.js leaf with the whole text plus 7 orphan paragraphs Chrome cut from a multi-line `insert_text` | No Draft.js runtime; typing behaviour is emulated in the test |
| library_tile_2026_09_24.html | telemetry `ad_creative_reuse` 20:19:29 (da86e97cbf8b), `diag.tile_shape.html` — one media-library tile | Single tile, no grid virtualisation; image removed |
| editor_tree_adset_2026_09_24.html | d3e690189a6e/04_create_additional_ad — left tree grid: campaign, ad set (with action-menu button), ad | Hover-only menu; the "Create ad" menu is injected by the test |
| editor_tree_new_shell_between_2026_10_07.html | 4e4969867d28/12_ad4_create (capture truncated at 3.98 MB, tree whole) - left tree grid with a fresh "New Leads Ad" row in the middle of four ad rows (ACL-1675). Ad names replaced by `Sample Ad N`, object ids by synthetic ones; `data-objecttype` is not kept by the cutter, rows are told apart by `aria-label` and `id` | No CSS and no Meta runtime; leftover shells above/below are made by the test renaming rows |
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

The catalogue of every layout in this folder — per robot step, with its test and expected result — is `tests/bench/CATALOGUE.md` (source `tests/bench/catalogue.json`). Steps 2–3 below are automated by `scripts/capture_to_fixture.py` (ACL-1433):

    python scripts/capture_to_fixture.py output/rpa_pages/<run>/NN_<step> --name <layout> \
        --fragment "<name>=<css>[|text=<s>][|up=<n>][|nth=<k>][|smallest]" [--fragment ...] \
        [--source block_<x>.html] [--replace "Real text=Synthetic"] [--append] --review \
        [--stub tests/test_bench_<layout>.py]

It loads a copy of the capture (a truncated 4 MB `page.html` too) in offline Chromium with page scripts off, cuts each named block, keeps only the attributes the robot reads (`role`, `aria-*` except `aria-activedescendant`, `id`, `data-surface`, `placeholder`, `type`, `value`, `tabindex`, `hidden`, `for`, `checked`, `disabled`, `name`, `min`/`max`/`step`, `typeaheadcomponentid`, `contenteditable`; `href` → `#`), drops scripts, styles, frames, comments, classes and the robot's own `data-rpa-*` marks, replaces SVG/images with `<i>`, renames Meta render ids to `syn_N` consistently across `id`/`for`/`aria-labelledby`/…, and scrubs text and attribute values: the campaign name from `context.json`, `--replace` pairs, e-mails, URLs and bare landing domains, `act=`/`act_` ids, 9+-digit object ids (stable synthetic numbers), phones, tokens, UUIDs, media file names (`sample_media_NN.ext`), the person's name in `business_scope:` surfaces, `Facebook_NNNN`-style profile aliases. The file is named `<layout>_<YYYY_MM_DD>.html` from the capture date, each block introduced by `<!-- fragment: <name> | source: <run>/<step> -->`, and the result is checked with the rules of `scripts/acl_scan_secrets.py` (a finding refuses to write). Page names, form names, ad copy and interests are ordinary words no regex can tell: read the `--review` list, add `--replace` pairs and rerun. `--stub` writes a test skeleton that fails until it runs the robot's step on the fragment. Then describe the fixture below, add it to `tests/bench/catalogue.json` and run `python scripts/bench_catalogue.py --write`.

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

## ACL-1348 empty ad creative and the wizard step row (live run 2026-09-25)

`ad_creative_media_step_2026_09_25.html` — 956d0d184138/05_ad2_creative_upload (CL_ML Video Day, second ad; step `ad2_creative_upload` failed). The whole `data-surface=".../ad_image_video_selector_section"` block of the ad editor: the "Set up creative" menu trigger (`aria-haspopup=menu`) and "Please specify the media to run with this ad." Cut in offline Chromium: scripts, images and SVG replaced by `<i>`, every attribute except `role`, `aria-*`, `data-surface`, `tabindex`, `type` removed. No account, campaign, page or creative data in the block. Capture truncated at ~3.98 MB; the block is whole, but the creative wizard's portal (end of the document) was cut off entirely in this page. UI language: English. Meta build identifier and browser version: not recorded.

`creative_wizard_steps_2026_09_25.html` — the real wizard step row, from `956d0d184138/04_library_pick_selected/block_library.html` (the same ad, 06:52:44: the robot on the Media step with the library tile picked; the block is whole, not truncated). Kept: the `role=dialog` shell, the step row `role=list` > `listitem` > `role=button` (label, `aria-current="step"` on the current step, `tabindex=-1` on the completed "Creative setup", `aria-disabled="true"` on "Translation", hidden "completed / incomplete / pending status" labels), the wizard's Close button and the Media step heading. The library grid (file names, thumbnails) was dropped; attributes other than `role`, `aria-current`, `aria-disabled`, `aria-labelledby`, `aria-level`, `tabindex`, `id` removed; Meta render ids (`js_*`) replaced with `syn_N`. No account, campaign or creative data. UI language: English. Meta build identifier and browser version: not recorded.

Coverage limit: no Meta JS or CSS (the test lays the list out as a row, like the screenshots). Only the state "current step = Media" was captured; "current step = Text" is derived in the test by moving `aria-current`. Locked, covered or click-deaf Media steps were never captured — those rows in `tests/test_media_step_reached_or_named.py` stay synthetic. Since ACL-1348 a failure on the way to Media captures the wizard as its own block (`block_creative_wizard.html`, step `ad_creative_media_step`, once per ad).

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

## Placements heading with a button group (live run 2026-09-28)

`placements_heading_group_2026_09_28.html` — 6925f39bdc67/01_placements (Facebook_6540, classic ad set editor; step `placements` failed in ~1 s with `tag_info={'tagged': False, 'headings': 1}`; the second campaign's capture 8058b54f78d0/01_placements has the same card). Capture truncated at 3.98 MB; the card is whole. Two fragments, each introduced by a `<!-- fragment: ... -->` comment: `card` — the whole Placements card, whose `role=heading` holds "Placements" plus the "Explore placement opportunities" / "More options" button group, then "Advantage+ on", "About placements", the "WhatsApp Status included" notice with its Close button, "Placement value rules", "Account controls · Excluded placements: None" and "Show more settings" (collapsed: no categories in the capture); `campaign_score_row` — the right-hand Campaign score row "Placements · You're including all available placements for all ad sets.", wrapped in 29 plain `<div>`s standing in for the real 33-level chain to the first ancestor that also holds "Account controls". Cut in offline Chromium: scripts, images and SVG replaced by `<i>`, every attribute except `role`, `aria-*`, `id`, `tabindex`, `href` (set to `#`), `for` removed; Meta render ids replaced with `syn_N`. No account, campaign or creative data in the blocks. UI language: English. Meta build identifier and browser version: not recorded.

Coverage limit: no Meta JS — "Show more settings" does not open anything, so `tests/test_rpa_placements_heading_with_buttons.py` checks which block gets marked and what is clicked, not the category checkboxes.

## ACL-1344 ad creative section and "Add text option" (live run 2026-09-25)

`ad_copy_text_options_2026_09_25.html` — the whole `data-surface=".../ad_creative_section"` block from 36a1bbf663c9/02_ad_media_ready (capture truncated at 3.98 MB, the block is whole): Primary text (Draft.js `contenteditable role=combobox`, empty), Headline, Description, "Optimize text per person", the empty `text_enhancements_inline_adoption_field`, Call to action. Classes, `data-auto-logging-id`, SVG and help links removed; Meta render ids `js_*` replaced with `syn_N`. No account, campaign or creative data in the block. UI language: English. Meta build identifier and browser version: not recorded.

Coverage limit: **the "Add text option" control is not in any capture** in `C:\acl-live-0928`, `C:\acl-stage` or `C:\acl-live-0919` `output\rpa_pages` (all ad-copy captures were taken before typing, with the creative block still hidden). Its look is known only from screenshots (`rpa_screenshots/0a81a6438251/20260922_170433_recorded_after_ad_copy.png`, `08726d544c47/20260921_205735_recorded_fail_ad3_copy.png`): label "Primary text (1 of 5)", under the box an outlined button "⊕ Add text option", then "Headline 1 of 5" / "⊕ Add headline option"; the "Advantage+ creative text generation" section sits right under the button. `tests/test_second_primary_text_option.py` injects that button and Meta's reaction (a new empty option box, a copy of the first) synthetically, plus a synthetic generation section with its own "Add text option" as a trap.

A live capture that replaces the synthetic part must show, after Primary text 1 is typed: (1) the "Add text option" element — tag, `role`, `aria-label`, where it sits relative to `data-surface$="/message_field"`; (2) the same block after one click — where the second box lives (inside the same `message_field` surface or not), its `aria-label`/placeholder, its remove (X) control, and the label text ("Primary text (2 of 5)"?); (3) the "Advantage+ creative text generation" section expanded, with any generated text options, to confirm they are outside our option boxes; (4) the "Optimize text per person" state once there are two options — the robot does not change it, it only records it (`ad_copy_second_text` → `diag.optimize_text_per_person`).

## ACL-1344 "Add text option" on the live page (Doc19, 2026-09-29)

`ad_copy_add_text_option_2026_09_29.html` — the whole `data-surface=".../ad_creative_section"` block, read off the live Doc19 page (classic editor) right after the robot failed with `ad_copy_second_text add_control_not_found {"boxes": 1, "zones": 7}`: Primary text 1 typed (a `textarea` marked `data-rpa-field="body"`), the "Add text option" control, the expanded "Advantage+ creative text generation" section with five Meta-generated variations, Headline with "Add headline option", Description, "Optimize text per person", Call to action. Cut in Chromium: classes, `data-auto-logging-id`, SVG, image URLs removed; links set to `#`; Meta render ids `js_*` replaced with `syn_N`; the ad text (primary text, headline, the five generated variations, the word-by-word mirror) replaced with neutral placeholders. No account, campaign, page or creative data in the block. UI language: English. Meta build identifier and browser version: not recorded.

What it shows: the control is `div[role=button][tabindex=0]` with `data-surface` ending `/message_field/lib:dco-add-asset-button`, inside the `message_field` surface; the generation section (`role=list`, heading "Advantage+ creative text generation", variations in a `role=region`) is also inside `message_field`, and the button and the section share a parent. The generation zone used to climb up to that parent and swallow the button — `tests/test_add_text_option_live_markup.py`. Coverage limit: what Meta draws after the click (the second option box) is still not captured; the test adds an empty `textarea` next to the first. A failure to find the control now saves this block itself (`page_capture`, step `ad_second_text_add_not_found`, `block_ad_creative_section.html`, once per ad).

## ACL-1344 "Add text option" in both editors (live runs 2026-09-30 / 2026-10-01)

`ad_copy_single_screen_2026_09_30.html` — fragment `creative`: the whole `data-surface="/am/editor:ad/ad_creative_section"` block of 3100d06ad51e/04_destination_sweep (single-screen editor `simple_create`, CO_bnc Mix, capture truncated at 3.98 MB, the block is whole), cut by `scripts/capture_to_fixture.py` with the ad copy and headline replaced (`Sample primary text line 1`, `Sample line 2..4`, `Sample headline`). Primary text 1 typed (Draft.js `contenteditable role=combobox`), `#add-bodies-option-button` > `div[role=button]` `.../message_field/lib:dco-add-asset-button` "Add text option", Headline with `#add-titles-option-button` "Add headline option", Description, "Optimize text per person · Disabled", empty `text_enhancements_inline_adoption_field`, Call to action. No account, campaign, page or creative data. UI language: English. Meta build identifier and browser version: not recorded.

With the classic-editor block `ad_copy_headline_missing_2026_10_01.html` (ACL-1433) it shows that both editors draw the control the same way; only the surface prefix differs (`/am/editor_drawer/editor:ad/` vs `/am/editor:ad/`). Neither capture has Meta's "Advantage+ creative text generation" section rendered; `tests/test_bench_add_text_option.py` checks each block as captured and with the real section from `ad_copy_add_text_option_2026_09_29.html` inserted where it stood on the live page (a sibling of the button's branch inside `message_field`) — the layout of every live `add_control_not_found` of 28.09–01.10 (telemetry: `zones 3–7`, and `text_generation_off` found the section each time). Coverage limit: what Meta draws after the click is still not captured; the test adds an empty copy of the first editor under it.

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

## ACL-1371 ad destination and ad name (live runs 2026-09-28)

Cut with BeautifulSoup from `output/rpa_pages/<run>/<step>/page.html` (classic editor, captures truncated at ~3.98 MB, the blocks are whole): the element `#adgroupAdDestinationGeneric` or `#adgroupNameEditorComponent`. Scripts, styles, SVG/images, comments removed; every attribute except `role`, `aria-*` (without `aria-activedescendant`), `id`, `data-surface`, `placeholder`, `type`, `value`, `tabindex`, `hidden`, `for`, `checked`, `disabled`, `name`, `href` (set to `#`) removed — also the robot's own `data-rpa-field` mark and its `display: block !important`; Meta render ids (`js_*`) replaced with `syn_N` (numbering restarts in each fragment); the ad name value replaced with `Example Ad`. No account, campaign, page, form or creative data in the blocks. UI language: English. Meta build identifier and browser version: not recorded.

`checked` is the attribute of the first render; `aria-checked` is the live state. Where they differ, the choice changed after the section was drawn.

`ad_destination_types_2026_09_28.html`:

| Fragment | Source | Capture state | Coverage limit |
| --- | --- | --- | --- |
| instant_experience | a90d704c892a/01_ad_destination (Facebook_6540, CO_bnc Trading, `ad_destination` failed after 8.3 s) | Destination type radios: Website `aria-checked=false` (`checked` attribute still on it), Instant Experience `aria-checked=true` with its "Select Instant Experience" combobox and "Create new"; no Website URL field, no Browser add-ons | What switched the type is not in the capture; the test emulates Meta redrawing the section as Website after a click on Website |
| website_instant_form | 7e5f7abec777/02_ad_destination (same account, CO_bnc FX_Y, failed after 4.1 s) | Website `aria-checked=true`, Instant form opt-in switch off, empty Website URL field, Browser add-ons "Instant form (suggested)" `aria-checked=true` (`checked` attribute on None), Lead Ads Terms card and forms list (form names not in the block) | No Meta JS: the None radio does not react; which exit of the step fired is not recorded (the step was silent) |

`ad_name_section_2026_09_28.html` — ad_name_section: a90d704c892a/01_ad_destination, the "Ad name" section with its `role=combobox` input `placeholder="Enter your ad name here..."`. The buyer's `ad_name` failures (Sentry PYTHON-1W, DAN-LT25) were never captured; the duplicated / hidden / disabled / covered / renamed variants in `tests/test_destination_and_ad_name_say_why.py` are made from this real section by the test.

## ACL-1353 single-screen editor: adding an ad (live runs 2026-09-29/30)

`simple_create_more_ads_2026_09_30.html` — the robot's step-1 capture `simple_create_more_ads` of run f9b9fc8d7bb3 (Facebook_1443, `simple_create`, two-ad campaign; 01 = before any press, 02 = the ad set's Action menu open, 03 = ~13 s after "Create ad"). Runs 8ce1b9985046 (30.09) and b3a333ff43ab (29.09) show the same menu and the same result. Cut with BeautifulSoup from the whole `block_*.html` files (not truncated): scripts, styles, comments removed, SVG/images replaced by `<i>`; every attribute except `role`, `aria-*` (without `aria-activedescendant`), `id`, `data-surface`, `placeholder`, `type`, `value`, `tabindex`, `hidden`, `for`, `checked`, `disabled`, `name`, `href` (set to `#`) removed; Meta render ids (`js_*`) replaced with `syn_N` (numbering restarts in each fragment); Meta object ids (row ids, `default-button-for-action-menu_<id>`) replaced with synthetic `1000000000000NN`; the copy-ID value dropped; campaign/ad set name → `Sample Campaign 01`, ad 1 name → `Sample Ad 01`, Page name → `Sample Page`. No account id, URL, email or phone. UI language: English. Meta build identifier and browser version: not recorded.

| Fragment | Source | Capture state | Coverage limit |
| --- | --- | --- | --- |
| tree_before | 01/block_ad_tree | Left tree: Campaign / Ad set / one Ad row (ad 1 already named), each row with hover-only `default` and `Action menu` buttons | Hover sizing is Meta CSS, not in the fixture |
| adset_menu | 02/block_menu | `role=menu` "Actions for this ad set": Duplicate, Copy, Paste (disabled), Delete (disabled), **Create ad** (`/am/editor_drawer/lib:campaign-structure-item-action-menu-create`), Create multiple ads (disabled), Create rule, ID | Menu position is emulated |
| tree_after_create | 03/block_ad_tree | The same tree with a second Ad row "New Leads Ad" | — |
| ad1_name | 01/block_ad_section `#adgroupNameEditorComponent` | Ad name field of ad 1 (`Sample Ad 01`) | — |
| new_ad_name | 03/block_ad_section `#adgroupNameEditorComponent` | Ad name field after Create ad: `value="New Leads Ad"` — the ad section shows the NEW ad | — |
| new_ad_media | 03/block_ad_section `#imageVideoSelectorEditorComponent` | Media of the new ad: "Set up creative", Required, no media | — |

What happened between 02 and 03 is not in any capture: 7.5 s after the press the robot saw a visible dialog (not the notifications panel) and still one Ad row; by the 03 capture the dialog was gone and nobody had closed it — most likely Meta's creation progress modal. `tests/test_simple_create_more_ads.py` emulates that transition (a 1 s `role=dialog` "Loading creation", then the captured "after" tree and ad section); the robot now records the dialog's text and surface (`simple_create_more_ads` → `diag.dialog`) so the next live run names it. The live runs of 2026-09-30 (d66a22d0fa30, fba2c3f5; not cut into a fixture: nothing new in the tree or ad section) named it: text "Creating...", surface `/am/lib:ads_progress_dialog_modal_8e9c`. While it is up, the page may stop answering the robot's liveness probe; the test page now shows that dialog, and the silence is emulated by stubbing `simple_create._read_ads`.

## ACL-1412 new-ad media (live runs 2026-09-30)

`simple_create_new_ad_media_2026_09_30.html` — two fragments, cut the same way as `simple_create_more_ads_2026_09_30.html` above (BeautifulSoup; only `role`, `aria-*` without `aria-activedescendant`, `id`, `data-surface`, `placeholder`, `type`, `value`, `tabindex`, `hidden`, `for`, `checked`, `disabled`, `name`, `href` kept; `js_*` → `syn_N`; SVG/images → `<i>`; 5+ digit numbers → `0`; media file names → `sample_video_NN.mp4`). No account id, URL, email, phone or campaign name. UI language: English. Meta build identifier and browser version: not recorded.

| Fragment | Source | Capture state | Coverage limit |
| --- | --- | --- | --- |
| media_filled | 8ce1b9985046/02_simple_create_more_ads/block_ad_section `#imageVideoSelectorEditorComponent` (Facebook_1443) | Media of ad 1 with its video attached: "Edit media" (menu trigger), "Remove media", the two placement groups; no "Please specify the media" | Thumbnails removed |
| media_picker | d1755a59e139/03_simple_create_more_ads/block_dialog (Facebook_1443) | The `role=dialog` on screen after Create ad for ad 3 (the robot's `dialog_open` capture): step row Media / Trim / Crop / Text / Creative generation / Enhancements / Translation, Account library with Search media, Upload, five tiles, "1 selected", Cancel / Next | Which tile was selected is not in the markup (it lives in Meta CSS); position is emulated |

The same run's page captures (`01_ad2_creative_upload`, `02_ad2_destination`) are truncated at 3.98 MB but still show the state that failed ad 2: the Ad section heading and the ad preview are in the DOM, the ad form (name, Media, Destination) is not — the editor had unmounted it while the page stood at the campaign and ad set. `tests/test_simple_create_new_ad_media.py` emulates that virtualisation (the Media block is mounted only while the scroll container is near it); the transition itself is synthetic.

## ACL-1426 fragments (owner's capture run 2026-10-01)

Re-built by `page_capture.scrub_section_html` (whitelisted tags and attributes: role, aria-*, data-surface, type, checked, disabled, id; no classes, svg, media URLs, names or ids) from the read-only wizard captures of phase A. UI language: English. Meta build identifier: not recorded. Browser: Multilogin Chrome, version not recorded.

| Fixture | Source / capture state | Coverage limit |
| --- | --- | --- |
| crop_step_video_simple_create_2026_10_01.html | 4121938e6dc3/07_wizard_crop/block_creative_wizard.html (Facebook_1443, single-screen editor, ad 2, video from the library) — the wizard on Crop: step row, "Select media crop for placements", cards Vertical and Square, each with native radios Original (checked) / 9:16 or 1:1 (recommended), Crop and Replace | No "Crop and expand (Meta AI)" checkbox was on screen; no media (video blobs removed); no Meta runtime |
| crop_step_image_classic_2026_10_01.html | 5edc0e7cca0a/05_wizard_crop/block_creative_wizard.html (Doc19, classic editor, ad 1, image upload) — Square Original checked, Vertical 9:16 (recommended) checked (Meta's default) | Same; the Replace picker was never captured (synthetic in the tests) |
| ad_form_gone_verifying_2026_10_01.html | 4121938e6dc3/11_ad2_creative_upload/page.html (ad 2 after the library pick and the wizard) — Ad heading with an empty `/am/editor:ad` surface (no name field, no Media block) and the "Verifying your changes" card with the preflight error `#100 lead_gen_form_id` | The right-column preview is not in the fragment |

## ACL-1551 fragments (live run 2026-10-04, Facebook_1443)

Cut by `scripts/capture_to_fixture.py` from the read-only captures of run 232d398471bd (live build c1f0e507; block captures, not truncated). Media file names → `sample_media_NN` (numbered per fragment: in `in_wizard_picker` the post of ad 1 is `sample_media_04.mp4`, in `media_busy` it is `sample_media_01.mp4`), videos and icons → `<i>`, render ids → `syn_N`; no names, ids or URLs. UI language: English. Browser: Multilogin Chrome, version not recorded.

| Fixture · fragments | Source / capture state | Coverage limit |
| --- | --- | --- |
| wizard_crop_story_missing_2026_10_04.html · crop_step | 232d398471bd/02_wizard_crop/block_creative_wizard.html — the wizard on Crop for a post/story pair after the first Next came back: step row Media ✓ · Trim (skipped) · Crop (`aria-current=step`) · …, header Close, cards Vertical/Square (Original checked), footer Cancel / Next (enabled). No Save on this step | No Meta runtime: Next -> busy -> the same Crop, and Close -> wizard gone, are wired by the test as seen live |
| … · crop_busy | 232d398471bd/04_ad_creative_upload/block_creative_wizard.html (same as 05_simple_create_more_ads/block_dialog.html) — the same Crop with Next and Cancel `aria-disabled` (Meta busy for over a minute after the second Next) | The page around it (editor, tree) is not in the fragment; `tests/test_more_ads_after_a_stuck_wizard.py` puts it over the 30.09 tree |
| … · in_wizard_picker | 232d398471bd/01_wizard_crop_replace/block_picker.html — "Replace media for Stories, Status, Reels, In-stream for Reels" inside the wizard: header Close/Save, Search media, six tiles without the story file, footer Save | As the 03.10 picker (ACL-1533) |
| … · media_busy | 232d398471bd/00_wizard_media/block_creative_wizard.html — Media step right after the library pick: "1 selected", Next and Cancel `aria-disabled` | Meta moved to Crop by itself later; the moment is wired by the test |

## ACL-1559 fragments (live runs 2026-10-04, Facebook_1443)

`wizard_media_picker_2026_10_04.html` — two wizard dialogs (`role=dialog`, whole block captures, not truncated) of the Media-first step row Media · Trim · Crop · Text · Creative generation · Enhancements · Translation, Media current (`aria-current=step`) in both. Cut by `scripts/capture_to_fixture.py` from read-only copies of the captures: media file names → `sample_media_01.mp4`, icons → `<i>`, render ids → `syn_N`; the search box value is the scrubbed file name; no names, ids, URLs or tokens. `data-non-int-surface` (`AdsWizardMediaPickerContainer`) and `data-is-placeholder` are not kept by the script, so the bench checks the reader's fallback (progressbars and controls inside the wizard root); the live surface attributes are covered by the reader's selectors only. UI language: English. Meta build identifier and browser version: not recorded.

| Fixture · fragment | Source / capture state | Coverage limit |
| --- | --- | --- |
| wizard_media_picker_2026_10_04.html · loading | 2f140fa687fa/00_ad_creative_media_step/block_creative_wizard.html (live build ec3cadd8, 11:02) — the Media step whose picker holds only three `role=progressbar aria-busy=true aria-valuetext="Loading..."` bars; no Upload, tabs, Search media or grid; footer Cancel enabled, Next (`lib:ads-omp-primary-button`) `aria-disabled`. Same layout in 02_ad_creative_media_step | No Meta runtime: whether the picker would load without clicks is unknown; the bench swaps in the `loaded` fragment to model it |
| … · loaded | 232d398471bd/00_wizard_media/block_creative_wizard.html (live build c1f0e507, 07:36; the same capture as `wizard_crop_story_missing · media_busy`) — the loaded Media step right after the library pick: tabs Account / Video URL / Page, Search media (value = the picked file), Filters 1, Upload (`role=button`, data-surface `…/lib:creative-tool-asset-picker-upload-button`), drop zone, grid with two tiles, "1 selected"; Next and Cancel `aria-disabled` | Captured after the pick, not before it; no `input[type=file]` (Meta creates it on Upload) |

## ACL-1533 fragments (live runs 2026-10-03, Facebook_1443)

Cut by `scripts/capture_to_fixture.py` from the read-only captures of run 2e98a75f252d (same layout in d54d68303e57, f35a2b31f7d8, a39407b72507). Media file names → `sample_media_NN`, videos and thumbnails → `<i>`, render ids → `syn_N`. UI language: English. Meta build identifier: not recorded. Browser: Multilogin Chrome, version not recorded.

| Fixture | Source / capture state | Coverage limit |
| --- | --- | --- |
| crop_step_pair_video_2026_10_03.html | 2e98a75f252d/06_wizard_crop/block_creative_wizard.html — the wizard on Crop for a post/story pair (video from the library): Vertical (Original checked, 9:16 enabled, "will not show up on certain placements" note) and Square (Original checked, 1:1 disabled) | The cards' `blob:` videos are removed; the test puts one back per card and re-mints it the way Meta does after Replace. No capture after Replace exists (telemetry `after` is the reference) |
| crop_replace_in_wizard_picker_2026_10_03.html | 2e98a75f252d/07_wizard_crop_replace/block_creative_wizard.html — Meta's "Replace media for Stories, Status, Reels" inside the wizard dialog: step row (Crop `aria-current=step`), header Close and Save, Account/Video URL/Page tabs, "Search media", Filters, Upload, grid of six tiles, footer Save (the primary slot that is Next on Crop) | No Meta runtime: what each button does is wired by the test as seen live (footer Save after a pick returned to Crop; the row's Crop did nothing) |

## ACL-1433 bench harvest (live runs 2026-09-29 … 10-01)

Cut with `scripts/capture_to_fixture.py` from `C:\acl-live-1001`, `C:\acl-live-0930` and `C:\acl-live-0928` `output\rpa_pages` (the oldest capture kept there is from 29.09). Scrubbed as described above; extra `--replace` pairs: the Page name in the wizard previews, the ad copy of cc8fcfa17ae9, the interests of 3100d06ad51e. UI language: English. Meta build identifier and browser version: not recorded. No Meta JS or CSS in any block: tests lay out what geometry depends on (the wizard step row) and emulate accordions where the step clicks. Every row with its test and expected result is in `tests/bench/CATALOGUE.md`.

| Fixture · fragments | Source (run/step) | Capture state | Coverage limit |
| --- | --- | --- | --- |
| creative_wizard_steps_2026_10_01.html · image_media, image_creative_setup, image_crop, image_text, image_image_generation, image_enhancements | 5edc0e7cca0a/03–08 (SA Doc19, image upload; ACL-1426 phase A `block_creative_wizard.html`, not truncated) | Each wizard step before the walk's forward press; row Media · Creative setup · Crop · Text · Image generation · Enhancements · Translation | One capture per step; the media grid's file names are synthetic, thumbnails removed |
| … · video_trim, video_crop, video_text, video_creative_generation, video_enhancements | 1125a87e6b51/02–05, 11 (GT PF, video library pick) | Row Creative setup · Media · Trim · Crop · Text · Creative generation · Enhancements · Translation; Trim forward "Skip and continue" | Video player is markup only |
| … · pick_crop | 4121938e6dc3/02 (CO_bnc Trading) | Row without Creative setup | — |
| … · pick_media_library, pick_enhancements | b47bc815bb3c/05, 07 (CO_bnc Mix) | Short row Media · Text · Creative generation · Enhancements · Translation; library with nothing selected | — |
| … · text_first_text, text_first_crop | cc8fcfa17ae9/03–04 (CO_bnc FX_Y) | Text before Crop | — |
| placements_doc19_2026_10_01.html · card | 5edc0e7cca0a/01_placements (truncated, the card whole; the robot's own `data-rpa-placements-marker` block) | Six categories incl. "Marketing messages"; In-stream expanded; Feeds and Stories collapsed; no "Messenger Stories" anywhere | Collapsed categories' rows are built by the test from the real In-stream row and `diag.subitems_seen` |
| placements_simple_create_2026_09_30.html · card | cb854b07857b/02_placements (single-screen editor, not truncated) | Placements card open: only "Advantage+ on" + "About placements" | How Meta reveals the manual controls here was not captured |
| ad_destination_layouts_2026_10_01.html · event_type, instant_form_terms, whatsapp_addon, simple_create_instant_form, sweep_addon_none | cc8fcfa17ae9/07, 5465ca61895f/01, cb854b07857b/03, d66a22d0fa30/01, 3100d06ad51e/04 | `#adgroupAdDestinationGeneric`: Facebook event; Instant form add-on + Lead Ads Terms; WhatsApp add-on; Instant form (single-screen); None (sweep) | Visibility needs Meta CSS (`section_shown` not compared) |
| … · errors_lead_gen_and_auth, errors_lead_gen_simple_create | 5465ca61895f/01, d66a22d0fa30/01 | The ad's error block: Lead Gen error, and on 5465ca also Meta's account authentication error (#3858385, "Start authentication") | — |
| ad_destination_instant_form_2026_10_03.html · simple_create_instant_form_terms, errors_lead_gen | 04509d5eed75/05_ad_destination (03.10, Facebook_1443, live build f09a6db; capture truncated at 3.98 MB, the section is whole) | `#adgroupAdDestinationGeneric` of the single-screen editor: Website selected, "Instant form" opt-in switch (off), Browser add-ons on "Instant form (suggested)" with the Lead Ads Terms card (View terms / Close / Create form, empty form list); the ad's Lead Gen error #100. Cut by `scripts/capture_to_fixture.py`; no names, ids or URLs in the blocks. UI language English; browser version not recorded | Live the section was `display: none !important` inside Meta's `data-virtualized` wrapper (off screen); the wrapper, Meta's CSS and its scripts are modelled in `tests/test_bench_destination_instant_form.py` |
| audience_controls_badge_on_2026_10_03.html · card_run_a, card_run_b, campaign_toggle, leads_row, card_recommended_collapsed | 0e86c42023da/00_advantage_plus_still_on, 04509d5eed75/00_advantage_plus_still_on, and b3a333ff43ab/00_stall_campaign_name (29.09) for card_recommended_collapsed (03.10, Facebook_1443, live build f09a6db; captures truncated at 3.98 MB, the blocks are whole) | Whole single-screen Audience card in controls mode with the badge still "Advantage+ on" (Locations United States, Age 18/65+ open, Gender, Custom audiences, Detailed targeting, Languages, "Switch to recommended setup"); the editor header's "Campaign toggle" switch ("In draft"); the Campaign score row "Advantage+ leads campaign · On" (a button, no switch); a collapsed Audience card in recommended setup (summary rows Locations … Languages as aria-level 4 headings, "Further limit the reach of your ads" and "Suggest an audience" in the `hidden` body). Cut by `scripts/capture_to_fixture.py`; no names, ids or URLs in the blocks. UI language English; browser version not recorded | The toggle is as captured after the robot's click (aria-checked false, `checked` attribute from the initial render); the three blocks are put under one wrapper in `tests/test_bench_audience_controls_badge_on.py`, as on the page |
| editor_closed_list_2026_10_01.html · table_header, notifications_panel | 68cc447bd867/03_ad_name, c6a785dddd0e/01_objective_leads | Campaigns table header where the ad editor was expected; Meta's Notifications panel where the objective dialog was expected | Table rows (other campaigns' names) not included |
| languages_chips_2026_10_01.html · sa_arabic_typed, co_spanish_two_chips | 0afc79a410c6/02, 243f23a1bc1b/01 (`languages_no_suggestion`) | "Arabic" chip + typed "Arabic"; chips "Spanish" and "Spanish (All)" | No suggestion list (none was open) |
| ad_copy_headline_missing_2026_10_01.html · creative | cc8fcfa17ae9/08_ad_copy | `ad_creative_section` with Primary text (ad copy synthetic), the real **Add text option** (`#add-bodies-option-button`, `…/message_field/lib:dco-add-asset-button`), no `headline_field` | Only the state after typing text 1 |
| conversion_event_heading_group_2026_10_01.html · card | cc8fcfa17ae9/01_conversion_event | Conversion card, heading with "Improve conversion location", Dataset set (pixel name synthetic), event empty | — |
| locations_default_only_2026_10_01.html · simple_create, classic | 0131b6fa3476/01, abd6661ea5d5/01 (`audience_location`) | Only "United States", its remove control disabled; on simple_create "Colombia" typed with no suggestion list | — |
| audience_simple_create_advantage_on_2026_09_30.html · age_step, suggestion_step | 3100d06ad51e/01_audience_age, 02_advantage_plus_audience | Single-screen Audience card "Advantage+ on"; summary rows + open fields; "Your suggestion" targeting (interests synthetic) | — |

This closes the README's earlier gap for **"Add text option"**: `ad_copy_headline_missing_2026_10_01.html` is the first capture of it (the ACL-1344 test still injects its own copy; switching it to the real markup is a separate change).

## ACL-1573 foreign windows (Doc21 live run 2026-10-04, network toast 2026-10-01)

`foreign_windows_doc21_2026_10_04.html` — 254ee3b3e0c7/00_conversion_website (Doc21 `GT_ait…PF`, step `conversion_website` failed with `menu_closed` ×8, `reopened: 0` — ACL-1572; 87d43d6202af and 18d15b75d1eb failed the same way). Capture truncated at 3.98 MB; the four blocks are whole. Cut with `scripts/capture_to_fixture.py`: `notifications_flyout` (`#globalNavNotificationsJewel`: the header jewel and Meta's `role=dialog` `#fbNotificationsFlyout` — "Notifications", the "Unread" switch, "Preferences", a `Loading...` progressbar), `adset_name_section` (the name combobox and "Create template"), `conversion_section` (`/am/editor_drawer/editor:ad_set/adset_conversion_goals_section`: the closed "Website and instant forms" combobox, Dataset, Conversion event "Lead", the "Set up the Conversions API" card) and `editor_footer` (`editor_drawer_footer`: Close / "All edits saved" / Back / Next). The campaign name became `Sample Campaign 01`, the dataset name `Sample dataset`. UI language: English. Meta build identifier and browser version: not recorded.

What the capture shows, and what it does not: the Notifications flyout carries `toggleTargetClosed` here and in **every one of the 284 captures** under `C:\acl-live-*\output\rpa_pages` (successful steps too) — it is Meta's pre-rendered, hidden panel, not a window over the page. The cutter strips classes; `tests/test_foreign_windows_are_closed_or_named.py` puts `toggleTargetClosed` back with Meta's rule `.toggleTargetClosed{display:none}`, and its open state (class removed) is synthetic. The screenshots of the ACL-1572 failures (308c4f7c8a5a 01.10, 254ee3b3e0c7 04.10) show the "Ad set name — Template" naming dialog (Add component / Choose existing template / Cancel / Save) over Conversion location; its portal is past the 4 MB cut, so its markup in the test is synthetic, as in `tests/test_naming_template_dialog_is_cancelled.py`. 87d43d6202af's screenshot shows no window at all. The open Conversion location menu and the "the window takes the click" behaviour are emulated.

`network_toast_2026_10_01.html` — 20583efb8c41/00_stall_objective_wait (capture truncated at 3.98 MB; the toast is whole): the "Your internet connection was lost. Some of your work may not be available." line with its own `role=button` "×" **without** an `aria-label` — the markup `_close_banners` (which wants a "Close" text or label) did not recognise; until now the toast was only ever synthetic in the tests. No account data. UI language: English. Meta build identifier and browser version: not recorded.

Coverage limit: no Meta JS or CSS; unknown windows (unsafe-only buttons, a safe "Not now", a footer-surface "Close" inside a window, the single-screen editor's Publish footer) are synthetic around the real Doc21 blocks.

## ACL-1611 collapsed Audience card in the editor_drawer window (live runs 2026-10-05)

`audience_card_collapsed_drawer_2026_10_05.html` — two fragments of the whole `#campaignTargetingStaticExtendedPGDSection` block: `card_run_a` (6ebd77ccaf6a/02_further_limit_reach_click, Facebook_9403A, KW_TRL_All_Video_Mix_35_05.10) and `card_run_b` (e89bda52e7a5/03_further_limit_reach_click, the same card on KW_TRL_All_Video_Day_35_05.10). Real captures, not constructed. Captures truncated at 3.98 MB; the card is whole. The card is collapsed into a read-only summary: header button (`role=toolbar` "Audience", badge "Advantage+ on", "Set up your audience using controls and suggestions." with the "About audiences" link) and summary button (Custom audiences / Locations / Age / Gender / Advantage+ detailed targeting / Languages), both `role=button aria-pressed=false` with one `aria-labelledby`; no Edit button. The hidden body holds the controls, "Suggest an audience" and the "Further limit the reach of your ads" row. Cut with `scripts/capture_to_fixture.py` (classes, inline styles, SVG, ids, `act=`, URLs, campaign name removed; render ids to `syn_N`; `--review` read, only UI strings remain). UI language: English. Meta build identifier and browser version: not recorded.

Coverage limit: no Meta JS or CSS — `tests/test_bench_audience_card_collapsed_drawer.py` emulates the card opening (click on either button presses both and un-hides the body). Only the "Advantage+ on, no custom audiences" state was captured; a collapsed card with Advantage+ off or filled audiences was not.

## ACL-1613 create-campaign dialog "Meta AI-guided setup" (live run 2026-10-06)

`ai_guided_setup_dialog_2026_10_05.html` — one fragment, `dialog`: the whole `role=dialog` "Choose how to create your new campaign" from `fb358fda8aab/00_stall_objective_wait` (campaign list, Create clicked): two radios "Meta AI-guided setup" (checked) and "Manual campaign setup", a prompt box with suggested-prompt buttons, Cancel / Continue. Real capture. Cut with `scripts/capture_to_fixture.py`; `--review` read, only UI strings remain. UI language: English. The Doc21 "Ad set name - Template" dialog has no page.html capture (only screenshots), so `tests/test_ai_rescue.py` uses the same hand-written markup as `tests/test_foreign_windows_are_closed_or_named.py`.
## ACL-1629 placements on the layouts of 2026-10-05/06 (live runs 2026-10-06)

`placements_button_expanders_2026_10_06.html` — b40ba4e8c2d6/00_placements (KLD_BFL_B90, classic ad set editor, 08:14 UTC; 34bf0d6c2262/00_placements has the same card). Capture truncated at 3.98 MB; the card is whole. One fragment, `card`: the whole Placements card with "Show more settings" open, the "Placements" node and the Feeds, "Stories, Status, Reels" and "In-stream ads for reels" category blocks (`ads_placement_expandable_list_item_*`) with their real subitem rows. The category expander is a plain `<button aria-label="Expand|Collapse">` inside the category `li`, without `aria-expanded`. The card also carries Meta's "Changes to placement settings are coming soon" notice. The capture is the page at the end of the step (all three lists open; "Ads on Facebook Reels" already unchecked).

`placements_collapsed_card_standalone_2026_10_06.html` — ab33293af6d7/05_placements (Facebook_9403A, classic standalone ad set editor, 09:33 UTC). Capture truncated at 3.98 MB; the card is whole. One fragment, `card`: the Placements card collapsed into a summary — header and summary ("Account controls: None set · Platforms · Placements: Advantage+ placements") as two `role=button aria-pressed=false`; the body (notice, "Placement value rules", "Account controls · Excluded placements: None", "Edit placement controls") under `<div hidden>`.

Both cut with `scripts/capture_to_fixture.py` (classes, inline styles, SVG, ids, `act=`, URLs, campaign names removed; render ids to `syn_N`; `--review` read, only UI strings remain). No account, campaign or creative data in the blocks. UI language: English. Meta build identifier and browser version: not recorded.

Coverage limit: no Meta JS or CSS. `tests/test_bench_placements_1629.py` emulates the category buttons (fold/unfold, a fold after the first save, a row disabled for a while after its category opens), the card opening (a click on either button presses both and un-hides the body) and the "Placement controls for this ad account" dialog, whose markup is synthetic (it was not in either capture). What the opened 9403A card offers beyond "Advantage+ placements · Edit" was not captured.


## ACL-1630 single-screen editor Locations and ad tree (live runs 2026-10-06)

`locations_open_dropdown_card_2026_10_06.html` - fragment `card`: the whole `#LOCATION` block of aa99ab40d397/00_audience_location (Facebook_5265, SA_ARC, `simple_create`; the same card in fab05371305f/00 and da07f392ff1f/00 and in all three second-pass captures; capture truncated at 3.98 MB, the block is whole). One group "United States" with a disabled Close, its row with a labelled "Open Dropdown" button and a disabled "Remove", then "Include" / "Search locations" (empty, `aria-expanded=false`), Browse, map controls. Saudi Arabia is not in the card in any of the six captures. `aria-activedescendant="SA"` on the search box was in the live DOM but is not kept by the cutter. Cut with `scripts/capture_to_fixture.py`; no account or campaign data. UI language: English. Meta build identifier and browser version: not recorded.

Coverage limit: no Meta JS or CSS and no suggestion list - `tests/test_bench_location_open_dropdown.py` adds the CSS that makes the buttons icons, emulates the suggestion, the second country group and Meta's rule that the last country cannot be removed. Why Enter did not add the country live is not in the captures.

`editor_tree_no_action_buttons_2026_10_06.html` - fragment `tree`: `block_ad_tree.html` of aa99ab40d397/07_simple_create_more_ads (step `simple_create_more_ads` failed `no_create_control`; the trees of fab05371305f/08 and da07f392ff1f/08 are the same). Three rows (Campaign, Ad set, Ad) and, where the 24.09 / 30.09 captures hold the "default" and "Action menu" buttons, an empty `<div></div>`. Campaign name replaced by the cutter, ad name by `Sample ad 1`. UI language: English. Meta build identifier and browser version: not recorded.

Coverage limit: no Meta JS - `tests/test_bench_single_screen_adset_row_hover.py` emulates the buttons appearing on `mouseover` of the ad set row, with the button markup of the 30.09 capture. That Meta draws them on hover is inferred from the empty containers; no capture was taken with the cursor over the row.

## ACL-1669 B97 media step and Crop (live runs 2026-10-07)

`wizard_media_picker_selected_2026_10_07.html` - fragment `selected_no_counter`: the whole wizard dialog of bc4302862ee7/00_wizard_media (KLD_BFL_B97, wizard open on Media, 0.1 s after the file was uploaded; one tile in the grid, wrapped in Meta's `data-test-decoration="selected"`). Cut by `C:/acl-tools/cut_nobrowser.py`, a browser-free copy of the cutting and scrubbing rules of `scripts/capture_to_fixture.py` (the laptop has no Playwright): classes, styles, SVG/images, ids and URLs removed, media file name to `sample_media_01.mp4`. The two footer counters ('1 selected', '0 selected') were emptied by hand, so this fixture models a MISSING counter; the real page showed them. Expected robot result: refusal. Coverage limit: no Meta JS or CSS.

`crop_cards_loading_2026_10_07.html` - fragment `cards_loading`: HAND-REDUCED from bc4302862ee7/03_wizard_crop (and the same layout in 10 and 16): step row on Crop, heading, the three `role=progressbar` "Loading..." placeholders, no Square/Vertical cards. Coverage limit: structure only, no Meta JS or CSS; how long Meta takes to draw the cards is not recorded.

## ACL-1638 fragments (live runs 2026-10-06)

`audience_whatsapp_suggestions_2026_10_06.html` — two fragments, each the whole Audience card (header with `#advantage-state-audience-section-header` down to the Languages row), introduced by a `<!-- fragment: <name> | source: <run>/<step> -->` comment: `whatsapp_suggestion_collapsed` (573ab2cf3646/00_advantage_plus_audience, Age collapsed to "35 - 65+ · Unknown age on WhatsApp: Excluded" with "Your suggestion") and `whatsapp_age_hard_badge_on` (d4f9d5f016c1/03_advantage_plus_audience, Age open with 35 / 65+, "Use as a suggestion" unchecked, badge still "Advantage+ on"). Cut from the capture HTML (truncated at ~3.98 MB, the card is whole) by a text-only scrub: scripts, styles, images and SVG replaced by `<i>`, every attribute except `role`, `aria-*` (not `aria-activedescendant`), `id`, `data-surface`, `placeholder`, `type`, `value`, `tabindex`, `hidden`, `for`, `checked`, `disabled`, `href` (set to `#`) removed, `value` of the search combobox dropped, Meta render ids (`js_*`, `_r_*_`) replaced by `syn_N`, detailed-targeting interests replaced by `Sample interest N`. Countries, languages and campaign names: only country/language names remain, no campaign, account or creative data. UI language: English. Meta build identifier and browser version: not recorded. Coverage limit: no Meta runtime; the collapse/expand and the badge change are emulated by the test.

`classic_editor_tree_hover_only_2026_10_07.html` - fragments `tree` (the `role=grid` of the left tree) and `breadcrumb` (the header trail above it): `page.html` of f2c513f0a22d/07_create_additional_ad (step `create_additional_ad` failed `create_menu_item_not_found`; 08/10/13 are the same layout). Three tree rows (Campaign, Ad set, Ad) with an empty `<div></div>` where the "Action menu" button belongs, and a breadcrumb that repeats the campaign, ad set and ad names. Cut without a browser; ad name replaced with `Sample Ad 01`, campaign with `Sample Campaign 01`. Used by tests/test_bench_classic_adset_row_hover.py.

## ACL-1836 transport incident (2026-10-12)

`fb_api_session_lost_2026_10_12.json` preserves the exact CDP error and the observed
sequence: campaign and ad set created PAUSED, video upload unknown, reconciliation
failed with `Runtime.evaluate: Session with given id not found.` It was manually
reduced from the local incident report; all target/session/account identifiers and
the URL use synthetic placeholders. No credentials or private captures are included.
This is a transport trace, so the HTML-only `capture_to_fixture.py` does not apply.
`tests/test_acl1836_session_recovery.py` executes the real PageTab/InPageGraph/client
against that trace. The original reason for detachment and the fate of the live
upload are not established by this offline fixture.
