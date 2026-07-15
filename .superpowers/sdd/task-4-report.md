# Task 4 Report: Wire ranks into AdsManagerFlow

## Summary

Implemented minimal StrategyRanks hooks in `src/fb_ads_rpa/ads_manager_flow.py`.
No changes were made to `src/fb_ads_rpa/meta_dom.py`; the existing Website selection stack remains intact.

## Changes

- Added `AdsManagerFlow._variations_ranks()` with cached `StrategyRanks.load()` and failure-safe `None` return.
- Added thin helper methods for:
  - logging rank outcomes without breaking RPA flow on rank errors
  - marking safe prior steps as gated by next-step success
  - inferring `nav_ad_set` strategy from `steps_completed`
  - inferring `conversion_location_website` strategy from newly completed steps
- Logged `nav_ad_set` safe success after successful ad-set navigation / ensure.
- Marked `nav_ad_set` next-step OK when the conversion location section is reached.
- Logged `conversion_location_website` success/failure around existing Website selection and teach/recovery paths.
- Called `ordered_strategies("conversion_location_website", default_strategies(...))` from the conversion Website logging hook without rewriting the existing multi-strategy stack.
- Logged Draft/dry-run finish as `log_outcome("finish_draft", "hand_off_draft", ok=True)`.
- Did not add any publish rank logging.
- Added a `finally` save in `publish_campaign` for an existing cached ranks object.

## Verification

### Import check

Command:

```bash
PYTHONPATH=/workspace python3 -c "from src.fb_ads_rpa.ads_manager_flow import AdsManagerFlow; print('ok')"
```

Result: PASS

Output:

```text
ok
```

### Exact requested pytest command

Command:

```bash
PYTHONPATH=/workspace python3 -m pytest --noconftest tests/test_rpa_variations_ranks.py tests/test_rpa_variations_catalog.py tests/test_fb_ads_rpa_browser_profile.py -v
```

Result: BLOCKED before collection

Output:

```text
ERROR: file or directory not found: tests/test_fb_ads_rpa_browser_profile.py
collected 0 items
```

Root cause: `tests/test_fb_ads_rpa_browser_profile.py` does not exist in this workspace. A repository search only found that path referenced in the Task 4/variations plan docs.

### Available targeted tests

Command:

```bash
PYTHONPATH=/workspace python3 -m pytest --noconftest tests/test_rpa_variations_ranks.py tests/test_rpa_variations_catalog.py tests/test_fb_ads_rpa_draft.py -v
```

Result: PASS

Output:

```text
13 passed in 0.07s
```

### Additional existing AdsManagerFlow tests

Command:

```bash
PYTHONPATH=/workspace python3 -m pytest --noconftest tests/test_create_wizard_flow.py tests/test_targeting_rpa.py -v
```

Result: FAIL, apparently unrelated to Task 4 hooks.

Observed failures:

- `test_is_objective_modal_open_false_when_hidden`
- several targeting tests expecting `_fill_first` per-term calls, while current implementation uses the DOM/paste path.

These failing paths were not modified for Task 4.

## Environment note

Initial import failed because the declared dependency `playwright` was not installed:

```text
ModuleNotFoundError: No module named 'playwright'
```

Installed the declared dependency with:

```bash
python3 -m pip install --upgrade playwright
```

After that, the import check passed.

## Concerns

- The exact pytest command from the brief cannot pass until `tests/test_fb_ads_rpa_browser_profile.py` is restored or the brief is corrected.
- The extra AdsManagerFlow mock tests currently fail for existing behavior outside this task's changes.
