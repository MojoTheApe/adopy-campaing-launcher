# Bench catalogue — every Meta layout the robot has met

<!-- Generated from tests/bench/catalogue.json by scripts/bench_catalogue.py --write. Do not edit by hand. -->

Every Meta layout the robot has met live, per robot step, with its fixture, test and expected result (ACL-1433). Source of tests/bench/CATALOGUE.md: edit this file, then run `python scripts/bench_catalogue.py --write`. Status: pass = the robot handles it and the test checks that; xfail = a live robot bug the test exposes (strict xfail, fix belongs to the RPA track); reference = markup check only, no step function on it.

Captures read for ACL-1433: C:/acl-live-1001, C:/acl-live-0930, C:/acl-live-0928 output/rpa_pages (oldest kept capture 2026-09-29; earlier ones of 20-28.09 are no longer on disk and are represented by the fixtures cut at the time).

**27 robot steps × 125 layouts, 70 fixtures, 0 known live bugs (xfail).** Fixtures live in `tests/fixtures/ads_manager/` (how they are cut: its README and `scripts/capture_to_fixture.py`).

## Summary

| Step | Layouts | pass | xfail | reference |
| --- | --- | --- | --- | --- |
| Graph creative video identity readback | 1 | 1 | 0 | 0 |
| Graph owned-tab browser context | 1 | 1 | 0 | 0 |
| Graph reconciliation after CDP session detachment | 1 | 1 | 0 | 0 |
| objective | 5 | 5 | 0 | 0 |
| simple_create_layout | 2 | 2 | 0 | 0 |
| campaign_name / budget / adset_name | 1 | 1 | 0 | 0 |
| conversion_website (Conversion location -> Website, ACL-1572) | 2 | 2 | 0 | 0 |
| conversion_event | 3 | 3 | 0 | 0 |
| advantage_plus_audience | 8 | 8 | 0 | 0 |
| further_limit_reach (Suggest an audience) | 9 | 9 | 0 | 0 |
| audience_location | 5 | 5 | 0 | 0 |
| audience_age | 4 | 4 | 0 | 0 |
| languages | 5 | 5 | 0 | 0 |
| detailed_targeting (interests) | 2 | 2 | 0 | 0 |
| placements | 11 | 11 | 0 | 0 |
| next_to_ad / additional ad (classic) | 4 | 4 | 0 | 0 |
| ad_name | 2 | 2 | 0 | 0 |
| ad_creative (media) | 5 | 5 | 0 | 0 |
| creative_wizard | 17 | 17 | 0 | 0 |
| ad_copy | 7 | 7 | 0 | 0 |
| ad_destination / destination_sweep | 10 | 9 | 0 | 1 |
| ads 2-3 (single-screen editor) | 5 | 5 | 0 | 0 |
| editor gone under the robot | 1 | 1 | 0 | 0 |
| banners and blocks (any step) | 2 | 2 | 0 | 0 |
| foreign windows (any step, ACL-1573) | 9 | 9 | 0 | 0 |
| ai rescue summary (any step, ACL-1613) | 2 | 2 | 0 | 0 |
| Graph read permission diagnostics (ACL-1886) | 1 | 1 | 0 | 0 |

## Graph creative video identity readback

Robot: CampaignCreator readback (ACL-2224)

| Layout | Seen live | Fixture · fragment | Test | Expected | Status |
| --- | --- | --- | --- | --- | --- |
| Creative returns per-ad video copies with different IDs and matching deterministic file titles | anonymized owner creation readback 2026-10-26 | `fb_api_video_identity_2026_10_26.json` | `tests/test_acl2224_video_identity.py` | Verified content and correct placements pass despite different IDs; wrong/missing content, missing binding and swapped placement remain blocked; IDs retained as diagnostics | pass |

## Graph owned-tab browser context

Robot: PageTab.open and owned navigation (ACL-2217)

| Layout | Seen live | Fixture · fragment | Test | Expected | Status |
| --- | --- | --- | --- | --- | --- |
| Existing Ads Manager tab loads while new automatic tab reports SSL navigation failure | anonymized owner differential 2026-10-26 | `fb_api_owned_context_2026_10_26.json` | `tests/test_acl2217_owned_context.py` | real owned-tab call site preserves synthetic unique context; ambiguous discovery stops before target creation; live cause remains pending | pass |

## Graph reconciliation after CDP session detachment

Robot: PageTab / InPageGraph local helper recovery before the next Graph read (ACL-1836)

| Layout | Seen live | Fixture · fragment | Test | Expected | Status |
| --- | --- | --- | --- | --- | --- |
| Transport trace: paused campaign/ad set, uncertain video upload, lost readback session | anonymized 2026-10-12 incident, ACL-1836 | `fb_api_session_lost_2026_10_12.json` | `tests/test_acl1836_session_recovery.py` | one reattach to the same verified owned target; no replay of sent calls; unavailable/changed target becomes a typed stop | pass |

## objective

Robot: Create -> objective dialog -> Leads (`_create_took`, `_objective_missing_reason`, simple_create.ensure_leads_objective)

| Layout | Seen live | Fixture · fragment | Test | Expected | Status |
| --- | --- | --- | --- | --- | --- |
| Campaigns table's own Loading... modal after Create | 9d113c3d6ac3/00_stall_objective_wait | `objective_open_2026_09_28.html` · table_loading | `tests/test_single_screen_editor_failures_are_named.py` | table modal told from Create's modal; reason table_loading | pass |
| Create's progress modal 'Loading creation' | 279b9539df43/00_stall_objective_wait | `objective_open_2026_09_28.html` · create_loading | `tests/test_single_screen_editor_failures_are_named.py` | counts as Create taking | pass |
| Meta's 'Turn off ad blocker' modal over the list | 279b9539df43/01_objective_leads | `objective_open_2026_09_28.html` · ad_blocker | `tests/test_single_screen_editor_failures_are_named.py` | reason network_trouble | pass |
| List with only Meta's Notifications panel (no objective dialog) | c6a785dddd0e/01_objective_leads, ead714bafcc4/00_objective_leads, 20583efb8c41/02_objective_leads, 21f211e9656e/01_objective_leads, 44ad1b9fc167/00_objective_leads, f2217ff15cab/01_objective_leads | `editor_closed_list_2026_10_01.html` · notifications_panel | `tests/test_bench_list_without_editor.py` | reason no_objective_dialog:<open outcome> (radio, loading_ceiling, budget); table_loading kept | pass |
| Single-screen editor: objective radio list inside Campaign details | 9e7d67471c6b/00_next_to_adset | `simple_create_2026_09_27.html` | `tests/test_the_single_screen_editor_is_walked.py` | Leads kept or switched inline; no list = nothing to check | pass |

## simple_create_layout

Robot: single-screen editor recognised and walked (simple_create.is_simple_create, scroll_to_level, open_card)

| Layout | Seen live | Fixture · fragment | Test | Expected | Status |
| --- | --- | --- | --- | --- | --- |
| Single-screen editor: tree, Campaign / Ad set / Ad sections, footer Publish | 9e7d67471c6b/00_next_to_adset | `simple_create_2026_09_27.html` | `tests/test_the_single_screen_editor_is_walked.py` | recognised by markup; levels reached by scrolling; cards opened by their own button | pass |
| Single-screen editor footer (Publish acknowledgement bar) | 97314905598e/02_ad_creative_upload | `editor_footer_simple_create_2026_09_28.html` | `tests/test_no_press_lands_on_publish.py` | no press lands on Publish | pass |

## campaign_name / budget / adset_name

Robot: level name fields and daily budget (simple_create NAME_FIELDS, budget input)

| Layout | Seen live | Fixture · fragment | Test | Expected | Status |
| --- | --- | --- | --- | --- | --- |
| Single-screen editor: Campaign name, Budget (expanded), Ad set name | 9e7d67471c6b/00_next_to_adset | `simple_create_2026_09_27.html` | `tests/test_the_single_screen_editor_is_walked.py` | each name field and the budget input unique on the page | pass |

## conversion_website (Conversion location -> Website, ACL-1572)

Robot: _pick_website_from_open_menu / _ensure_conversion_menu_open: trigger state (aria-expanded), name-field focus release, click -> keyboard -> script openers, English failure reason

| Layout | Seen live | Fixture · fragment | Test | Expected | Status |
| --- | --- | --- | --- | --- | --- |
| Doc21 classic ad set editor, closed Conversion location trigger (combobox, aria-expanded=false); open menu synthetic, no Single/Multiple headings | 87d43d6202af/00_conversion_website, 254ee3b3e0c7/00_conversion_website, 18d15b75d1eb/00_conversion_website, 308c4f7c8a5a/00_conversion_website, 162a95a64776/00_conversion_website | `foreign_windows_doc21_2026_10_04.html` · adset_name_section, conversion_section | `tests/test_conversion_menu_opens_in_every_layout_1572.py` | menu opened by click, else keyboard, else script press; focus released from the ad set name field; Website selected and read back from the trigger; a menu closed by a method is reopened before the next one | pass |
| Same editor, menu never opens / Website row missing / Conversion location absent (ad editor on screen) | e2df3122c39f/01_conversion_website | `foreign_windows_doc21_2026_10_04.html` · adset_name_section, conversion_section | `tests/test_conversion_menu_opens_in_every_layout_1572.py` | stops after 3 failed openings; note names the reason in English | pass |

## conversion_event

Robot: select_conversion_event_lead (conversion_event_field_present, is_lead_conversion_event_selected)

| Layout | Seen live | Fixture · fragment | Test | Expected | Status |
| --- | --- | --- | --- | --- | --- |
| Classic: Dataset set, Conversion event empty | 2026-09-09 capture | `conversion_event_2026_09_09.html` | `tests/test_conversion_event_is_really_set.py` | dataset alone is not success | pass |
| Classic: heading with 'Improve conversion location' button, event empty | cc8fcfa17ae9/01_conversion_event | `conversion_event_heading_group_2026_10_01.html` · card | `tests/test_bench_conversion_event.py` | field present, Lead not selected (as live); a 'Lead' value reads selected | pass |
| Single-screen editor: Conversion location Instant forms + Lead Ads Terms card | 9e7d67471c6b/00_next_to_adset | `simple_create_2026_09_27.html` | `tests/test_the_single_screen_editor_is_walked.py` | Instant forms is not Website; Lead Ads Terms card seen, View terms never pressed | pass |

## advantage_plus_audience

Robot: turn_off_advantage_plus / _audience_badge_on / _advantage_plus_audience_on

| Layout | Seen live | Fixture · fragment | Test | Expected | Status |
| --- | --- | --- | --- | --- | --- |
| Audience and Placements badges both 'Advantage+ on' | b30c3b2b6be9/00_advantage_plus_before, every NN_advantage_plus_still_on of 29.09-01.10 | `audience_advantage_on_2026_09_24.html` | `tests/test_audience_advantage_badge.py` | audience reads on; switched off or a named failure | pass |
| Audience off, Placements on | 8587f7668f86/02_languages | `audience_off_placements_on_2026_09_24.html` | `tests/test_audience_advantage_badge.py` | placements badge does not make the audience on | pass |
| Single-screen editor Audience 'Advantage+ on' with 'Your suggestion' targeting | 3100d06ad51e/02_advantage_plus_audience | `audience_simple_create_advantage_on_2026_09_30.html` · suggestion_step | `tests/test_bench_audience_simple_create.py` | reads on (as live: the step failed with Advantage+ still on) | pass |
| Single-screen editor (1443): Audience controls mode with the badge still on, before the age | 0e86c42023da/00_advantage_plus_still_on | `audience_controls_badge_on_2026_10_03.html` · card_run_a | `tests/test_bench_audience_controls_badge_on.py` | controls mode; advantage_plus_off ok (controls_mode), nothing clicked | pass |
| Same card, second run | 04509d5eed75/00_advantage_plus_still_on | `audience_controls_badge_on_2026_10_03.html` · card_run_b | `tests/test_bench_audience_controls_badge_on.py` | controls mode; advantage_plus_off ok (controls_mode), nothing clicked | pass |
| Editor header Campaign toggle (In draft) beside the Campaign score panel | 0e86c42023da/00_advantage_plus_still_on | `audience_controls_badge_on_2026_10_03.html` · campaign_toggle | `tests/test_bench_audience_controls_badge_on.py` | never taken for the Advantage+ leads switch | pass |
| Campaign score row 'Advantage+ leads campaign · On' (button, no switch) | 0e86c42023da/00_advantage_plus_still_on | `audience_controls_badge_on_2026_10_03.html` · leads_row | `tests/test_bench_audience_controls_badge_on.py` | no switch in the row, nothing clicked | pass |
| Single-screen editor (1443): collapsed Audience card in recommended setup (summary shows Locations/Age/Gender/Detailed targeting/Languages, Further limit row in the hidden body) | b3a333ff43ab/00_stall_campaign_name, 393e372f86b3/00, cb854b07857b/00, b47bc815bb3c/02, f35a2b31f7d8/00 | `audience_controls_badge_on_2026_10_03.html` · card_recommended_collapsed | `tests/test_bench_audience_controls_badge_on.py` | reads recommended, never controls; advantage_plus_off still_on (blocking) | pass |

## further_limit_reach (Suggest an audience)

Robot: gear row 'Further limit the reach of your ads', suggestion age checkbox

| Layout | Seen live | Fixture · fragment | Test | Expected | Status |
| --- | --- | --- | --- | --- | --- |
| Classic Suggest-an-audience card (SA Doc19) | 90a8987edad9/00_conversion_website | `audience_suggest_layout_2026_09_26.html` · suggest_classic | `tests/test_suggest_an_audience_layout.py` | gear row opens the dialog | pass |
| Same card with custom audiences expanded (6540 FX) | 97370c743f44/00_further_limit_reach_click | `audience_suggest_layout_2026_09_26.html` · suggest_fx | `tests/test_suggest_an_audience_layout.py` | covered gear row still opened by its own element | pass |
| Controls with 'Your suggestion' age | 7cfdc772376d/02_advantage_plus_audience | `audience_suggest_layout_2026_09_26.html` · controls_suggestion_on | `tests/test_suggest_an_audience_layout.py` | suggestion unchecked once, badge goes off | pass |
| Controls off summary | 8c6496e50815/01_placements | `audience_suggest_layout_2026_09_26.html` · controls_off | `tests/test_suggest_an_audience_layout.py` | reads off with age and language from the summary | pass |
| WhatsApp campaign: Age collapsed to '35 - 65+ · Unknown age on WhatsApp: Excluded' with 'Your suggestion' (Doc120) | 573ab2cf3646/00_advantage_plus_audience | `audience_whatsapp_suggestions_2026_10_06.html` · whatsapp_suggestion_collapsed | `tests/test_bench_audience_whatsapp_suggestions.py` | row opened, suggestion unchecked once, badge goes off; if Meta keeps it on the step still fails | pass |
| WhatsApp campaign: hard age 35 - 65+ (suggestion unchecked), badge still 'Advantage+ on' (9403A) | d4f9d5f016c1/03_advantage_plus_audience | `audience_whatsapp_suggestions_2026_10_06.html` · whatsapp_age_hard_badge_on | `tests/test_bench_audience_whatsapp_suggestions.py` | nothing clicked; blocked still_on with age_suggestion_checked=false and mode=controls in diag (cause of the flip not known) | pass |
| Age field open with 'Use as a suggestion' | 543dc9daad53/00_languages | `audience_suggest_layout_2026_09_26.html` · age_open_suggestion | `tests/test_suggest_an_audience_layout.py` | checkbox found only inside the age field | pass |
| Editor drawer window (9403A, adsets/edit/standalone): Audience collapsed into a read-only summary card, no Edit button, Further limit row in the hidden body | 6ebd77ccaf6a/02_further_limit_reach_click | `audience_card_collapsed_drawer_2026_10_05.html` · card_run_a | `tests/test_bench_audience_card_collapsed_drawer.py` | card opened by one click on its title (never About audiences / a button inside); controls verified; simple_card ok | pass |
| Same card, second run | e89bda52e7a5/03_further_limit_reach_click | `audience_card_collapsed_drawer_2026_10_05.html` · card_run_b | `tests/test_bench_audience_card_collapsed_drawer.py` | same as above | pass |

## audience_location

Robot: fill_audience_location (_location_rows, _location_summary_countries, _wait_location_options)

| Layout | Seen live | Fixture · fragment | Test | Expected | Status |
| --- | --- | --- | --- | --- | --- |
| Classic: one country included, remove control beside its heading | 2026-09-25 capture | `locations_single_country_2026_09_25.html` | `tests/test_default_location_is_really_removed.py` | default country really removed | pass |
| Single-screen editor: field collapsed to 'Inclusion: <country>' summary | 97314905598e/01_audience_location, 97314905598e/00_advantage_plus_still_on, 97314905598e/02_ad_creative_upload | `locations_simple_create_2026_09_28.html` · field_target | `tests/test_simple_create_location_summary.py` | summary read as the wanted country | pass |
| Single-screen editor: target country typed, no suggestion list, only 'United States' with its remove control disabled | 0131b6fa3476/01_audience_location | `locations_default_only_2026_10_01.html` · simple_create | `tests/test_bench_locations.py` | one row United States, disabled; no suggestions for the typed country (as live) | pass |
| Classic: only 'United States', remove disabled, search empty | abd6661ea5d5/01_audience_location, 64cc3d5510b2/01_audience_location, 53e1f3ea8bd8/01_audience_location | `locations_default_only_2026_10_01.html` · classic | `tests/test_bench_locations.py` | one row United States, disabled | pass |
| Single-screen editor 06.10: only 'United States' (disabled Close), its row has a labelled 'Open Dropdown' button and a disabled Remove; target country not in the card | aa99ab40d397/00_audience_location, aa99ab40d397/09_audience_location_second_pass, fab05371305f/00_audience_location, da07f392ff1f/00_audience_location | `locations_open_dropdown_card_2026_10_06.html` · card | `tests/test_bench_location_open_dropdown.py` | one row United States (disabled), never 'Open Dropdown'; a country that Enter did not add is not a success (not_in_card); a highlighted city or region is never pressed and a city row under the country heading is not the country; default removed next to the target | pass |

## audience_age

Robot: fill_audience_age (_age_triggers_read, _age_range_read, _open_collapsed_age_row)

| Layout | Seen live | Fixture · fragment | Test | Expected | Status |
| --- | --- | --- | --- | --- | --- |
| Classic: Age row expanded, two comboboxes 18 / 65+ | d3e690189a6e/01_advantage_plus_control | `audience_age_open_2026_09_24.html` | `tests/test_age_is_read_on_the_live_markup.py` | both triggers read | pass |
| Audience card in summary layout, Age row collapsed | 5e29e66da497/01_audience_age | `audience_age_collapsed_2026_09_25.html` · collapsed | `tests/test_collapsed_age_row_is_opened.py` | collapsed row opened, then read | pass |
| Audience card with the Age field open | 5e29e66da497/00_advantage_plus_still_on | `audience_age_collapsed_2026_09_25.html` · open_card | `tests/test_age_list_is_picked_by_a_real_click_and_closed.py` | age picked by a real click, list closed | pass |
| Single-screen editor, Advantage+ on, age body present but no readable trigger | 3100d06ad51e/01_audience_age | `audience_simple_create_advantage_on_2026_09_30.html` · age_step | `tests/test_bench_audience_simple_create.py` | no age trigger read (as live: step failed) | pass |

## languages

Robot: fill_audience_languages (_languages_selected, _languages_read, _language_suggestions_state)

| Layout | Seen live | Fixture · fragment | Test | Expected | Status |
| --- | --- | --- | --- | --- | --- |
| Chip preselected by country ('Arabic', no '(All)') | 79d4d978f8c7/02_languages | `languages_preselected_2026_09_25.html` | `tests/test_language_preselected_by_country.py` | already selected, nothing typed | pass |
| Single-screen editor: field open and empty, listbox missing | aa1e2fbf45c9/02_languages | `audience_fields_simple_create_2026_09_28.html` · languages_open_empty | `tests/test_single_screen_editor_failures_are_named.py` | no_suggestion named, markup captured | pass |
| Classic: 'Arabic' chip and 'Arabic' still typed | 0afc79a410c6/02_languages_no_suggestion | `languages_chips_2026_10_01.html` · sa_arabic_typed | `tests/test_bench_languages.py` | fill_audience_languages succeeds without typing | pass |
| Two chips 'Spanish' + 'Spanish (All)', empty box | 243f23a1bc1b/01_languages_no_suggestion, 8ce1b9985046/01, 9993b097fb70/01, b3a333ff43ab/02, eee3be108750/03, 03086e22f5d2/01, 68cc447bd867/02, df5344da3a17/00, 2ce7fd3be6f1/01, 4121938e6dc3/01, b47bc815bb3c/04 | `languages_chips_2026_10_01.html` · co_spanish_two_chips | `tests/test_bench_languages.py` | fill_audience_languages succeeds without typing; suggestion state as recorded | pass |
| Suggest-an-audience Languages open, empty | 7cfdc772376d/01_languages | `audience_suggest_layout_2026_09_26.html` · languages_open_empty | `tests/test_suggest_an_audience_layout.py` | real input used, unmatched search cleared | pass |

## detailed_targeting (interests)

Robot: detailed targeting field wait (single-screen editor)

| Layout | Seen live | Fixture · fragment | Test | Expected | Status |
| --- | --- | --- | --- | --- | --- |
| Detailed targeting row expanded, only a Loading progressbar | aa1e2fbf45c9/03_targeting_open | `audience_fields_simple_create_2026_09_28.html` · targeting_loading | `tests/test_single_screen_editor_failures_are_named.py` | waited for, not collapsed; never-loading field named | pass |
| Loaded search box 'Add demographics, interests or behaviors' | 0749abd7482f/02_placements | `audience_fields_simple_create_2026_09_28.html` · targeting_search_loaded | `tests/test_single_screen_editor_failures_are_named.py` | loaded state accepted | pass |

## placements

Robot: fill_placements

| Layout | Seen live | Fixture · fragment | Test | Expected | Status |
| --- | --- | --- | --- | --- | --- |
| Classic, categories visible, subcategories collapsed (5 categories) | 2026-09-09 capture | `placements_2026_09_09.html` | `tests/test_ads_manager_bench.py` | five categories end in the target configuration | pass |
| 'Placements' node with In-stream expanded; category recollapses after a save | 10402d9c2daf/01_placements | `placements_node_2026_09_25.html` | `tests/test_rpa_placements_category_recollapse.py` | category reopened, every subitem set | pass |
| Heading with 'Explore placement opportunities' button group (6540) | 6925f39bdc67/01_placements, 8058b54f78d0/01_placements | `placements_heading_group_2026_09_28.html` · card | `tests/test_rpa_placements_heading_with_buttons.py` | the card, not the Campaign score row, is marked | pass |
| Doc19: six categories (+ 'Marketing messages'), no 'Messenger Stories' | 5edc0e7cca0a/01_placements | `placements_doc19_2026_10_01.html` · card | `tests/test_bench_placements.py` | every offered placement set and read back; 'Messenger Stories' named not offered | pass |
| Doc19: an OFF placement Meta does not offer | 5edc0e7cca0a/01_placements | `placements_doc19_2026_10_01.html` · card | `tests/test_bench_placements.py` | step succeeds: category open, scrolled to a proven end and settled, item never seen = not offered (ACL-1445); late rows and a tail mounting on scroll are set; a tail that never mounts or an item seen elsewhere still fails | pass |
| Single-screen editor: Placements card open, only 'Advantage+ on' + 'About placements' | cb854b07857b/02_placements | `placements_simple_create_2026_09_30.html` · card | `tests/test_bench_placements.py` | card found by its data-surface anchor; a body that never leaves Loading is named placements_card_loading (ACL-1445) | pass |
| Single-screen editor Placements card: card found | cb854b07857b/02_placements | `placements_simple_create_2026_09_30.html` · card | `tests/test_bench_placements.py` | the card is found; once its body loads, manual placements are set and read back there (ACL-1445) | pass |
| Single-screen editor: placements set, list collapsed to 'Included: 7, Allowed with limited spend: 0, Excluded: 13' (no capture, screenshot text) | SA_ARC_All_Video_IPO_35_01.10 (tester, 01.10) | `placements_simple_create_summary_2026_10_01.html` · card | `tests/test_bench_placements.py` | counts are not proof: the summary is opened and its rows set and read back; a summary that does not open, or right counts over a wrong set, fails (review of PR #449) | pass |
| Classic editor (05-06.10): category expanders are plain `button[aria-label=Expand\|Collapse]` in the category `li`, no aria-expanded | b40ba4e8c2d6/00_placements, 34bf0d6c2262/00_placements | `placements_button_expanders_2026_10_06.html` · card | `tests/test_bench_placements_1629.py` | every offered placement set and read back; 'Messenger Stories' named not offered; a category folded after a click is reopened; a row or a category box disabled while Meta saves is set once it unlocks (a box that never unlocks fails); a final read-back of every row catches a late revert; a row locked in the wrong state fails and is named (ACL-1629) | pass |
| Classic standalone editor (06.10): Placements card collapsed to a summary (aria-pressed=false), body hidden; account controls dialog | ab33293af6d7/05_placements | `placements_collapsed_card_standalone_2026_10_06.html` · card | `tests/test_bench_placements_1629.py` | the card is opened first (a card that does not open fails, named); account exclusions applied and verified by the card summary or, when it stays 'None', by reopening the dialog; a lost save or a lost switch fails; no 'Edit placement controls' is a named warning; the opened card without categories and with Meta's 'no longer available' notice is named as ACL-1336 (ACL-1629) | pass |
| Classic editor, Doc120 (07.10): the Stories list folds twice while read and its row count flickers; five rows, no 'Messenger Stories' in the DOM | 4e4969867d28/00_placements | `placements_button_expanders_2026_10_06.html` · card | `tests/test_bench_placements_1684.py` | a complete list (end, at bottom, not busy, nothing hidden) with 'Messenger Stories' absent is 'not offered' after folds and a flickering row count; a list that never proves its end, or never settles by the hard limit, still blocks; an offered but hidden row is never 'not offered' (ACL-1684) | pass |

## next_to_ad / additional ad (classic)

Robot: Next to the ad level; create_additional_ad via the tree's ad set menu

| Layout | Seen live | Fixture · fragment | Test | Expected | Status |
| --- | --- | --- | --- | --- | --- |
| Left tree: campaign, ad set (action menu), ad | d3e690189a6e/04_create_additional_ad, 0afc79a410c6/04_create_additional_ad | `editor_tree_adset_2026_09_24.html` | `tests/test_additional_ad_waits_for_tree.py` | ad set menu opens, ad created | pass |
| Left tree with a fresh "New Leads Ad" row in the middle (ACL-1675): ad rows are told apart by row id, not by place | 4e4969867d28/12_ad4_create | `editor_tree_new_shell_between_2026_10_07.html` | `tests/test_a_failed_ad_leaves_no_duplicate_shell.py` | only a row id that was not in the pre-create snapshot is adopted | pass |
| AdsPERouteLoadPlaceholder spinner instead of the editor | 7364047ef03c/05_create_additional_ad | `route_placeholder_2026_09_24.html` | `tests/test_route_placeholder_is_loading.py` | not ready; spinner wins over a stale tree | pass |
| Breadcrumb header only, no tree rows | 7d2ce3ab1161/05_create_additional_ad | `breadcrumb_only_2026_09_24.html` | `tests/test_additional_ad_waits_for_tree.py` | not ready, no blind click | pass |

## ad_name

Robot: ad_name_failure (_AD_NAME_STATE_JS)

| Layout | Seen live | Fixture · fragment | Test | Expected | Status |
| --- | --- | --- | --- | --- | --- |
| Classic 'Ad name' section | a90d704c892a/01_ad_destination | `ad_name_section_2026_09_28.html` · ad_name_section | `tests/test_destination_and_ad_name_say_why.py` | duplicated / hidden / disabled / covered / renamed variants named | pass |
| Campaigns list instead of the editor | 68cc447bd867/03_ad_name | `editor_closed_list_2026_10_01.html` · table_header | `tests/test_bench_list_without_editor.py` | why not_on_ad_level, fields 0, levels [] (as live) | pass |

## ad_creative (media)

Robot: upload_ad_creative / library reuse (_wizard_media_step, library tile click)

| Layout | Seen live | Fixture · fragment | Test | Expected | Status |
| --- | --- | --- | --- | --- | --- |
| Empty ad creative: 'Set up creative' menu trigger | 956d0d184138/05_ad2_creative_upload | `ad_creative_media_step_2026_09_25.html` · ad_creative_empty | `tests/test_media_step_reached_or_named.py` | Media step reached or named | pass |
| One media-library tile | da86e97cbf8b telemetry tile_shape | `library_tile_2026_09_24.html` | `tests/test_library_tile_click_fallback.py` | steady tile clicked; moving tile via CDP; covered tile named | pass |
| Single-screen editor new ad: 'Set up creative', Required, no media | f9b9fc8d7bb3/03_simple_create_more_ads, d7b470f4c983/10_ad2_creative_upload, 0bd520418dbe/01_ad2_creative_upload, 04c8c214d2ba/01_ad2_creative_upload, 4121938e6dc3/11_ad2_creative_upload, 0c8b122daab0/02_ad2_creative_upload, d1755a59e139/01_ad2_creative_upload | `simple_create_more_ads_2026_09_30.html` · new_ad_media | `tests/test_simple_create_more_ads.py` | the new ad's media block is the one filled | pass |
| Single-screen editor new ad, media attached: 'Edit media', 'Remove media', two placement groups; block unmounted while the page stands at campaign/ad set | 8ce1b9985046/02_simple_create_more_ads, d1755a59e139/01_ad2_creative_upload, d1755a59e139/02_ad2_destination | `simple_create_new_ad_media_2026_09_30.html` · media_filled | `tests/test_simple_create_new_ad_media.py` | unmounted block brought back (no clicks) and read as filled; media on the ad named as entry media_on_ad | pass |
| Media picker dialog after the new ad's Set up creative: Media . Trim . Crop . Text . Creative generation . Enhancements . Translation, Account library, '1 selected', Cancel / Next | d1755a59e139/03_simple_create_more_ads | `simple_create_new_ad_media_2026_09_30.html` · media_picker | `tests/test_simple_create_new_ad_media.py` | entered through the new ad's own Set up creative -> Video ad; wizard open; entry media_entry:set_up_creative | pass |

## creative_wizard

Robot: _creative_wizard_open, _wizard_media_step, _WIZARD_FORWARD_JS (walk), _creative_post_media_step

| Layout | Seen live | Fixture · fragment | Test | Expected | Status |
| --- | --- | --- | --- | --- | --- |
| Step row (25.09): Creative setup . Media . Text . Creative generation . Enhancements . Translation | 956d0d184138/04_library_pick_selected | `creative_wizard_steps_2026_09_25.html` | `tests/test_media_step_reached_or_named.py` | row and current step read | pass |
| Image: Media . Creative setup . Crop . Text . Image generation . Enhancements . Translation (6 steps captured) | 5edc0e7cca0a/03-13_wizard_* | `creative_wizard_steps_2026_10_01.html` · image_media, image_creative_setup, image_crop, image_text, image_image_generation, image_enhancements | `tests/test_bench_creative_wizard_steps.py` | open; row + current step as recorded; forward Next ... Done | pass |
| Video: Creative setup . Media . Trim . Crop . Text . Creative generation . Enhancements . Translation | 1125a87e6b51/02-11_wizard_*, 2ce7fd3be6f1/02-06_wizard_* | `creative_wizard_steps_2026_10_01.html` · video_trim, video_crop, video_text, video_creative_generation, video_enhancements | `tests/test_bench_creative_wizard_steps.py` | Trim forward 'Skip and continue' (post-media step), others Next, Enhancements Done | pass |
| Video, no Creative setup: Media . Trim . Crop . Text . Creative generation . Enhancements . Translation | 4121938e6dc3/02-10_wizard_*, d7b470f4c983/01-16_wizard_* | `creative_wizard_steps_2026_10_01.html` · pick_crop | `tests/test_bench_creative_wizard_steps.py` | row read; forward Next | pass |
| Video, short row: Media . Text . Creative generation . Enhancements . Translation; Media library without selection | b47bc815bb3c/05-07_wizard_* | `creative_wizard_steps_2026_10_01.html` · pick_media_library, pick_enhancements | `tests/test_bench_creative_wizard_steps.py` | Media: no forward button; Enhancements: Done | pass |
| Video, Text before Crop: Creative setup . Media . Text . Crop . Image generation . Enhancements . Translation | cc8fcfa17ae9/03-12_wizard_* | `creative_wizard_steps_2026_10_01.html` · text_first_text, text_first_crop | `tests/test_bench_creative_wizard_steps.py` | row read in this order; forward Next | pass |
| Crop step, video in the single-screen editor: cards Square and Vertical, Original already checked on both, Crop and Replace; no 'Crop and expand (Meta AI)' box | 4121938e6dc3/07_wizard_crop | `crop_step_video_simple_create_2026_10_01.html` · crop_step | `tests/test_crop_step_original_and_story.py` | both cards read as Original; apply makes no clicks; read-back clean (ACL-1426) | pass |
| Crop step, image in the classic editor: Square Original, Vertical on Meta's default 9:16 (recommended) | 5edc0e7cca0a/05_wizard_crop | `crop_step_image_classic_2026_10_01.html` · crop_step | `tests/test_crop_step_original_and_story.py` | one guarded click moves Vertical to Original before the walk's Next; read-back clean (ACL-1426) | pass |
| Crop step, video post/story pair (03.10): both cards Original, `blob:` video media, no thumbnail ids; Square 1:1 disabled | 2e98a75f252d/06_wizard_crop, d54d68303e57/06_wizard_crop, f35a2b31f7d8/07_wizard_crop, a39407b72507/05_wizard_crop | `crop_step_pair_video_2026_10_03.html` · crop_step | `tests/test_bench_crop_live_1003.py` | a re-minted blob src or missing ids on Square is not crop_square_changed; the picked story id on Square is (ACL-1533) | pass |
| Replace picker inside the wizard dialog: step row (Crop current), header Close/Save, Search media, tiles, footer Save; no Cancel | 2e98a75f252d/02_wizard_crop_replace, 2e98a75f252d/07_wizard_crop_replace, d54d68303e57/07_wizard_crop_replace, a39407b72507/02_wizard_crop_replace | `crop_replace_in_wizard_picker_2026_10_03.html` · in_wizard_picker | `tests/test_bench_crop_live_1003.py` | after a pick and with nothing picked the picker is left through its own footer Save (never the row's Crop, the header Save, Close or Next) (ACL-1533) | pass |
| Crop step, story not in the library (04.10): footer Cancel/Next only, header Close; busy state after Next - Next and Cancel aria-disabled; the same Crop again after the busy state | 232d398471bd/02_wizard_crop, 232d398471bd/04_ad_creative_upload, 232d398471bd/05_simple_create_more_ads | `wizard_crop_story_missing_2026_10_04.html` · crop_step, crop_busy | `tests/test_bench_wizard_exit_1004.py` | Next pressed once on Crop; busy waited out; Crop again or busy past the limit -> the wizard's own footer Save/Done if it has one, else its own Close; Close -> crop_wizard_stuck (blocking), no dialog left; never Publish or the editor footer (ACL-1551) | pass |
| Replace picker inside the wizard (04.10), story not among the tiles | 232d398471bd/01_wizard_crop_replace, 232d398471bd/03_wizard_crop_replace | `wizard_crop_story_missing_2026_10_04.html` · in_wizard_picker | `tests/test_bench_wizard_exit_1004.py` | left through the picker's own footer Save, back on Crop; post kept with story_not_placed:not_in_library when the wizard then closes (ACL-1551) | pass |
| Media step busy after the library pick (04.10): '1 selected', Next and Cancel aria-disabled | 232d398471bd/00_wizard_media | `wizard_crop_story_missing_2026_10_04.html` · media_busy | `tests/test_bench_wizard_exit_1004.py` | the walk waits while the wizard is busy (bounded) instead of four quick misses (ACL-1551) | pass |
| Media-first wizard opened on Media (04.10) with the media picker still loading: three 'Loading...' progressbars, no Upload/tabs/Search media/grid; footer Next aria-disabled, Cancel enabled | 2f140fa687fa/00_ad_creative_media_step, 2f140fa687fa/02_ad_creative_media_step | `wizard_media_picker_2026_10_04.html` · loading | `tests/test_bench_wizard_media_picker_1004.py` | picker read as loading (3 placeholders); the current Media step is not clicked and Next is not pressed; read-only wait, then media_picker_loading (not upload_button_missing); picker that loads during the wait goes on to the library (ACL-1559) | pass |
| The same Media step loaded (04.10): tabs Account/Video URL/Page, Search media, Filters 1, Upload (role=button, asset-picker data-surface), drop zone, library grid with 2 tiles | 232d398471bd/00_wizard_media | `wizard_media_picker_2026_10_04.html` · loaded | `tests/test_bench_wizard_media_picker_1004.py` | picker read as loaded (upload, search, 2 tiles); exactly one Upload, inside the wizard dialog; the step goes straight to library reuse without clicks (ACL-1559) | pass |
| Media step of the wizard opened by an upload, B97 (07.10, bc4302862ee7, 0.1 s): tabs, Search media, Upload, library grid with the one uploaded tile wrapped in data-test-decoration="selected"; the footer counters ('1 selected' / '0 selected') removed for this fixture | bc4302862ee7/00_wizard_media | `wizard_media_picker_selected_2026_10_07.html` · selected_no_counter | `tests/test_bench_b97_picker_and_crop_1007.py` | counter read as unavailable, selected tile marked -> REFUSED (library_selection_unreadable): no click, no upload over a selection the robot cannot count (ACL-1669) | pass |
| Crop step opened while Meta still draws it (07.10, B97 bc4302862ee7): step row on Crop, heading, three role=progressbar Loading placeholders, no Square/Vertical cards, no Replace | bc4302862ee7/03_wizard_crop, bc4302862ee7/10_wizard_crop, bc4302862ee7/16_wizard_crop | `crop_cards_loading_2026_10_07.html` · cards_loading | `tests/test_bench_b97_picker_and_crop_1007.py` | read as found with empty cards; the step waits for the cards instead of answering crop_replace_button_missing (ACL-1669) | pass |

## ad_copy

Robot: write_ad_copy (mark_ad_copy_field, _text_options, Draft.js typing)

| Layout | Seen live | Fixture · fragment | Test | Expected | Status |
| --- | --- | --- | --- | --- | --- |
| Copy section visible, destination hidden (bare textareas) | 2026-09-09 capture | `ad_copy_2026_09_09.html` | `tests/test_ads_manager_bench.py` | headline and primary text found | pass |
| Draft.js leaf split into orphan paragraphs after a multi-line insert | 7364047ef03c/04_ad_copy | `ad_copy_draft_split_2026_09_24.html` | `tests/test_draft_editor_text_is_typed_once.py` | text typed once | pass |
| ad_creative_section with Primary text / Headline / Description, empty | 36a1bbf663c9/02_ad_media_ready | `ad_copy_text_options_2026_09_25.html` · ad_creative_section | `tests/test_second_primary_text_option.py` | second primary text added (button synthetic there) | pass |
| ad_creative_section without headline_field; real 'Add text option' button | cc8fcfa17ae9/08_ad_copy | `ad_copy_headline_missing_2026_10_01.html` · creative | `tests/test_bench_ad_copy.py` | body field marked, headline not found (as live); real Add text option found by its surface and pointed at | pass |
| Classic editor (Doc19), text 1 typed (textarea), Add text option beside the expanded 'Advantage+ creative text generation' section | Doc19 2026-09-29 (read off the live page) | `ad_copy_add_text_option_2026_09_29.html` | `tests/test_add_text_option_live_markup.py` | button found by surface, not swallowed by the generation zone; nothing in the section pressed; Meta's variations never ours | pass |
| Classic editor, text 1 typed, real 'Add text option'; also with the real 29.09 generation section beside it | cc8fcfa17ae9/08_ad_copy, Doc19 2026-09-29 (generation section) | `ad_copy_headline_missing_2026_10_01.html` · creative | `tests/test_bench_add_text_option.py` | Add text option found by surface, pressed, text 2 typed and read back; generation untouched (Meta's new box emulated) | pass |
| Single-screen editor (simple_create), text 1 typed, real 'Add text option' and 'Add headline option' | 3100d06ad51e/04_destination_sweep | `ad_copy_single_screen_2026_09_30.html` · creative | `tests/test_bench_add_text_option.py` | same as classic, with and without the generation section; Optimize text per person recorded (Disabled) | pass |

## ad_destination / destination_sweep

Robot: set_ad_destination_website (destination_state, _destination_failed, _ensure_destination_website)

| Layout | Seen live | Fixture · fragment | Test | Expected | Status |
| --- | --- | --- | --- | --- | --- |
| Website URL field (classic, 09.09) | 2026-09-09 capture | `ad_destination_2026_09_09.html` | `tests/test_ads_manager_bench.py` | URL field found | pass |
| Instant Experience selected | a90d704c892a/01_ad_destination | `ad_destination_types_2026_09_28.html` · instant_experience | `tests/test_destination_and_ad_name_say_why.py` | switched to Website | pass |
| Website + Instant form (suggested) add-on, Lead Ads Terms | 7e5f7abec777/02_ad_destination | `ad_destination_types_2026_09_28.html` · website_instant_form | `tests/test_destination_and_ad_name_say_why.py` | reason named | pass |
| Facebook event selected | cc8fcfa17ae9/07_ad_destination | `ad_destination_layouts_2026_10_01.html` · event_type | `tests/test_bench_ad_destination.py` | dest_type event; why destination_not_website (as live) | pass |
| Website + Instant form add-on + Lead Gen error + Lead Ads Terms | 5465ca61895f/01_ad_destination | `ad_destination_layouts_2026_10_01.html` · instant_form_terms | `tests/test_bench_ad_destination.py` | state as live; why instant_form_not_switched (live: addon_none_not_set, kept as field_why) | pass |
| WhatsApp add-on selected (single-screen editor) | cb854b07857b/03_ad_destination | `ad_destination_layouts_2026_10_01.html` · whatsapp_addon | `tests/test_bench_ad_destination.py` | addon whatsapp; why addon_none_not_set | pass |
| Single-screen editor Instant form add-on + Lead Gen error | d66a22d0fa30/01_ad_destination, 3100d06ad51e/03_ad_destination | `ad_destination_layouts_2026_10_01.html` · simple_create_instant_form | `tests/test_bench_ad_destination.py` | state as live; why instant_form_not_switched (live: url_field_not_focused, kept as field_why) | pass |
| Single-screen editor, section virtualised off screen (display none), Instant form opt-in switch, add-on Instant form (suggested) + Lead Ads Terms card (View terms / Close / Create form) + Lead Gen error | 04509d5eed75/05_ad_destination, 0e86c42023da/12_ad2_destination | `ad_destination_instant_form_2026_10_03.html` · simple_create_instant_form_terms, errors_lead_gen | `tests/test_bench_destination_instant_form.py` | section scrolled on screen, add-on None (opt-in switch off when on), URL filled and read back; telemetry before/after; Instant form that comes back -> instant_form_not_switched, blocking, named in the problem; View terms / Create form / Close never pressed (press guard lead_terms) | pass |
| Add-on None already set (sweep) | 3100d06ad51e/04_destination_sweep | `ad_destination_layouts_2026_10_01.html` · sweep_addon_none | `tests/test_bench_ad_destination.py` | addon none; why url_field_not_focused | pass |
| Single-screen editor ads 2-3: no ad form in the DOM, only the preview's Ad / Destination tabs | 0bd520418dbe/02,04,05, d7b470f4c983/11,18,19, 04c8c214d2ba/03,04, 4121938e6dc3/12, d1755a59e139/02 | — | `tests/test_bench_ad_destination.py` | section false; why destination_section_missing (as live); the capture does not show why the form is not mounted | reference |

## ads 2-3 (single-screen editor)

Robot: simple_create.create_next_ad

| Layout | Seen live | Fixture · fragment | Test | Expected | Status |
| --- | --- | --- | --- | --- | --- |
| Media wizard of the previous ad left open on Crop (Next/Cancel locked) over the editor | 232d398471bd/05_simple_create_more_ads, 232d398471bd/06_ad2_create | `wizard_crop_story_missing_2026_10_04.html` · crop_busy | `tests/test_more_ads_after_a_stuck_wizard.py` | closed with the wizard's own Close before the ad set's Action menu; ads 2-3 created; Close that does not close -> creative_wizard_left_open (ACL-1551) | pass |
| Tree before, ad set Action menu, tree after Create ad, new ad name/media | f9b9fc8d7bb3/01-03, 8ce1b9985046/02-04, b3a333ff43ab/03-04, d1755a59e139/03-04, d66a22d0fa30/02-03, df5344da3a17/02 | `simple_create_more_ads_2026_09_30.html` · tree_before, adset_menu, tree_after_create, ad1_name, new_ad_name, new_ad_media | `tests/test_simple_create_more_ads.py` | Create ad from the ad set menu opens the new ad; 'Creating...' dialog waited out | pass |
| New ad's form gone after the media wizard: Ad heading with an empty /am/editor:ad surface (no name field, no Media block) and 'Verifying your changes' with the #100 lead_gen_form_id preflight error | 4121938e6dc3/11_ad2_creative_upload, 04c8c214d2ba/01_ad2_creative_upload, 0bd520418dbe/01_ad2_creative_upload | `ad_form_gone_verifying_2026_10_01.html` · ad_form_gone_verifying | `tests/test_ad_form_gone_after_media.py` | named ad_form_gone_after_media after 30 s; no second media check or wizard (ACL-1426) | pass |
| Single-screen editor 06.10: tree rows carry no Action menu / default buttons until the cursor is over them | aa99ab40d397/07_simple_create_more_ads, fab05371305f/08_simple_create_more_ads, da07f392ff1f/08_simple_create_more_ads | `editor_tree_no_action_buttons_2026_10_06.html` · tree | `tests/test_bench_single_screen_adset_row_hover.py` | no buttons as captured; after the robot hovers the ad set row the Action menu is found and ad 2 is created; a row that never shows it stays no_create_control after exactly 3 hovers; the hover never presses or focuses anything and stops once the buttons appear; a tab frozen during the hover is tab_frozen | pass |
| Classic editor 07.10: tree rows carry no Action menu until the cursor is over them; breadcrumb header above the tree repeats the ad set name | f2c513f0a22d/07_create_additional_ad, f2c513f0a22d/08_create_additional_ad, f2c513f0a22d/10_create_additional_ad, f2c513f0a22d/13_create_additional_ad | `classic_editor_tree_hover_only_2026_10_07.html` · tree, breadcrumb | `tests/test_bench_classic_adset_row_hover.py` | tree counts as ready without the button; the robot hovers the tree row (never the breadcrumb), no press and no focus, stops once the button appears; then Action menu -> Create ad are the only two clicks; a row that shows no button after 3 hovers blocks with adset_action_menu_not_shown and presses nothing | pass |

## editor gone under the robot

Robot: ad_name_failure, simple_create.page_markers, destination_state

| Layout | Seen live | Fixture · fragment | Test | Expected | Status |
| --- | --- | --- | --- | --- | --- |
| Campaigns table where an editor step expected the editor | 68cc447bd867/03_ad_name, 53e1f3ea8bd8/02_placements, e5beb2079e39/02_audience_location, e5beb2079e39/03_languages | `editor_closed_list_2026_10_01.html` · table_header | `tests/test_bench_list_without_editor.py` | no editor markers; not_on_ad_level; destination section false | pass |

## banners and blocks (any step)

Robot: _close_banners, _account_blocked_text, _review_errors_visible

| Layout | Seen live | Fixture · fragment | Test | Expected | Status |
| --- | --- | --- | --- | --- | --- |
| AYMT bars, ad set cards with X, editor drawer header | 279ead80503a/00, 8814247ec92e/00, 5b223701b271/00,06, 90a8987edad9/00, 393bfda8b0db/02 | `banners_2026_09_25.html` · aymt_contact, aymt_phone, card_whatsapp_status, card_conversions_api, card_instant_form_filters, card_site_links, card_show_offerings_no_close, editor_drawer_header | `tests/test_banners_are_closed_by_their_own_x.py` | each closed by its own X; the editor's Close never pressed | pass |
| Ad error block: Lead Gen error + account authentication (#3858385, 'Start authentication') | 5465ca61895f/01_ad_destination | `ad_destination_layouts_2026_10_01.html` · errors_lead_gen_and_auth | `tests/test_bench_ad_destination.py` | account block authentication_error recognised; lead form error visible | pass |

## foreign windows (any step, ACL-1573)

Robot: window guard before each step and after a failed one (foreign_windows.before_step / after_failure / guarded: _click, _fill, Conversion location open and pick)

| Layout | Seen live | Fixture · fragment | Test | Expected | Status |
| --- | --- | --- | --- | --- | --- |
| Doc21 ad set editor with Meta's pre-rendered, hidden Notifications panel (toggleTargetClosed; in all 284 rpa_pages captures) | 254ee3b3e0c7/00_conversion_website, 87d43d6202af/00_conversion_website | `foreign_windows_doc21_2026_10_04.html` · notifications_flyout, adset_name_section, conversion_section, editor_footer | `tests/test_foreign_windows_are_closed_or_named.py` | hidden panel is not a window; nothing pressed | pass |
| Same page with the Notifications panel shown over the conversion menu (open state synthetic) | 254ee3b3e0c7/00_conversion_website | `foreign_windows_doc21_2026_10_04.html` · notifications_flyout, conversion_section | `tests/test_foreign_windows_are_closed_or_named.py` | panel closed with Escape before the menu and between pick strategies; menu reopened; Website picked | pass |
| 'Ad set name - Template' naming dialog over Conversion location (ACL-1572; dialog portal past the 4 MB cut, markup synthetic from screenshots) | 308c4f7c8a5a/00_conversion_website, 254ee3b3e0c7/00_conversion_website | `foreign_windows_doc21_2026_10_04.html` · adset_name_section, conversion_section | `tests/test_foreign_windows_are_closed_or_named.py` | Cancel pressed (never Save) before the menu; Website picked | pass |
| 'Your internet connection was lost' toast with an unlabelled x | 20583efb8c41/00_stall_objective_wait | `network_toast_2026_10_01.html` · network_toast | `tests/test_foreign_windows_are_closed_or_named.py` | closed by its own x | pass |
| The robot's own media wizard on any screen (Creative setup, Crop, Text, Trim, Enhancements, Creative generation, media library) | 5edc0e7cca0a/04-08, 1125a87e6b51/02-11, b47bc815bb3c/05_wizard_media | `creative_wizard_steps_2026_10_01.html` · image_creative_setup, image_crop, image_text, image_enhancements, video_trim, video_text, video_creative_generation, pick_media_library | `tests/test_foreign_windows_are_closed_or_named.py` | owned (step row / media surface): never closed, no Sentry event, no capture | pass |
| The wizard's Replace picker (Replace media for Stories, Status, Reels...) | 2e98a75f252d/07_wizard_crop_replace | `crop_replace_in_wizard_picker_2026_10_03.html` · in_wizard_picker | `tests/test_foreign_windows_are_closed_or_named.py` | owned (step row / media surface): never closed, no Sentry event, no capture | pass |
| Media wizard stuck on Crop and its Replace picker | 232d398471bd/01_wizard_crop_replace, 232d398471bd/04_ad_creative_upload | `wizard_crop_story_missing_2026_10_04.html` · crop_busy, in_wizard_picker | `tests/test_foreign_windows_are_closed_or_named.py` | owned (step row / media surface): never closed, no Sentry event, no capture | pass |
| Media wizard on its Media step (picker loaded) | 232d398471bd/00_wizard_media | `wizard_media_picker_2026_10_04.html` · loaded | `tests/test_foreign_windows_are_closed_or_named.py` | owned (step row / media surface): never closed, no Sentry event, no capture | pass |
| Unknown dialogs over the Doc21 editor (unsafe-only buttons; safe Not now; footer-surface Close inside; Publish footer of the single-screen editor) | synthetic | `foreign_windows_doc21_2026_10_04.html` · editor_footer | `tests/test_foreign_windows_are_closed_or_named.py` | Publish/Confirm/Continue and footers never pressed; captured + Sentry + 'Unknown Meta window blocked the step: <title>'; a safe Not now closes it and the step is retried once | pass |

## ai rescue summary (any step, ACL-1613)

Robot: ai_rescue.build_summary: text-only page summary the model sees (dialogs first, then the anchor area), scrubbed

| Layout | Seen live | Fixture · fragment | Test | Expected | Status |
| --- | --- | --- | --- | --- | --- |
| Create-campaign dialog 'Meta AI-guided setup' / 'Manual campaign setup' (radio pair, Create new campaign) | fb358fda8aab/00_stall_objective_wait | `ai_guided_setup_dialog_2026_10_05.html` · dialog | `tests/test_ai_rescue.py` | dialog listed first with its title; both radios with checked state; summary has no ids, URLs or e-mails | pass |
| Collapsed Audience card (same capture as ACL-1611) | 6ebd77ccaf6a/02_further_limit_reach_click | `audience_card_collapsed_drawer_2026_10_05.html` · card_run_a, card_run_b | `tests/test_ai_rescue.py` | card button listed as not-pressed; a model pick of it is expanded by exactly one click | pass |

## Graph read permission diagnostics (ACL-1886)

Robot: GraphClient.get/get_all error context

| Layout | Seen live | Fixture · fragment | Test | Expected | Status |
| --- | --- | --- | --- | --- | --- |
| Plan check Graph10 Permission Denied without failed-operation context; response fields reconstructed from owner screenshots | anonymized Plan check error 2026-10-13 | `fb_api_permission_denied_2026_10_13.json` | `tests/test_acl1886_graph_error_context.py` | GET and allowlisted operation/search subtype visible, request data absent; denial remains denied with unchanged requests and stops; live permission cause unknown | pass |

## Seen but not cut

| Captures | Why not a fixture |
| --- | --- |
| NN_stall_* (objective_wait, budget, adset_name, campaign_name, create, ad_name) in 0afc79a410c6, 0c8b122daab0, 1125a87e6b51, 20583efb8c41, 21f211e9656e, 393e372f86b3, 68cc447bd867, 7afe35363ffa, ae23a32dad2c, b3a333ff43ab, b47bc815bb3c, c6a785dddd0e, cc8fcfa17ae9, cb854b07857b, e5beb2079e39, eee3be108750 | watchdog stall snapshots: the page while a step waited, not a layout the step misread; their outcomes (table_loading, network_trouble, loading_ceiling, budget) are covered by the objective rows |
| NN_ad_creative_reuse_pick in 5edc0e7cca0a, dc8cdd6a02ba, e23d7c6f196b, df5344da3a17, 0afc79a410c6, 64cc3d5510b2, 7afe35363ffa | the media library portal is past the 4 MB cut in every page (no 'Search media'/'Account images' in the file); the tile itself is covered by library_tile_2026_09_24 |
| 287b6cf6beec/01_next_to_ad | classic ad set editor with Back/Next present; the press failed, not the lookup — nothing to read offline |
| no_run/00_tree_ad_ambiguous_parents (three folders) | dashboard track (src/analysis_agent/account_spend.py), not a robot step |
| NN_advantage_plus_still_on (first capture of almost every run) | same badge layouts as audience_advantage_on_2026_09_24 / audience_suggest_layout_2026_09_26 / audience_simple_create_advantage_on_2026_09_30; the 03.10 pair is cut as audience_controls_badge_on_2026_10_03 |

## Adding a layout

Every robot fix adds the live capture that motivated it (review checklist, `.github/pull_request_template.md`):

1. `python scripts/capture_to_fixture.py output/rpa_pages/<run>/NN_<step> --name <layout> --fragment "<name>=<css>" --review --stub tests/test_bench_<layout>.py` (read the review list, add `--replace` pairs, rerun).
2. Turn the stub into a test that runs the robot's real step function or reader on the fragment and asserts the outcome; a current robot bug is a strict `xfail` with its reason.
3. Add the layout to `tests/bench/catalogue.json`, run `python scripts/bench_catalogue.py --write`, describe the fixture in the fixtures README, `git add` it and run `python scripts/acl_scan_secrets.py`.
