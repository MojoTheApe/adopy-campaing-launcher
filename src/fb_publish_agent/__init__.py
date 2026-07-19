"""Agent 10: publish campaigns into Facebook Ads Manager via Playwright (browser automation).

Input: upload_plan.xlsx produced by Agent 8 (src/upload_agent) — one sheet per account.
Output: campaigns created in Ads Manager (or dry-run only), status written back into the
plan workbook (Upload Status / FB Campaign ID / Uploaded At / Error Message columns).

See docs/agent10_fb_ads_playwright_upload.md for the design rationale and the process for
recording/updating selectors when Facebook changes its UI.
"""
