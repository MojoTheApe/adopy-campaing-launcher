# ACL-2510: Live acceptance, interest lookup on a test account

Date: 2026-11-11. Carried over from ACL-2491 (Issue #1510).

## Check

The interests "Creative real estate investing" and "Real estate investing" must
resolve in Check plan on a test account.

## Environment

- Build: TEST9095, release 2026-11-11.1534 (main 54e167d16).
- Account: allowed pilot account B97. "B97" is the label of this account in the approved pilot list; it is intentionally used here instead of any Meta, Multilogin or ad account id.
- Target: a single target containing both interest terms.
- Run by: the project owner.

## Result: passed

- Check plan was run twice with the target containing both terms; both runs passed.
- Telemetry event `api_create_step`, step "preflight: targeting resolved", was
  recorded at 2026-11-11T12:09:29Z (first run) and 2026-11-11T12:10:02Z (second run).
- The second run served the lookups from the warm cache (cache hits).
- Owner confirmation in chat: "B97 я проверил таргетинг работает" (the owner
  verified on B97 that targeting works).

Evidence comment on the Issue:
https://github.com/MojoTheApe/adopy-campaing-launcher/issues/1510#issuecomment-6795434680

## Ads Manager check

After the two Check plans the owner published the same plan on B97 as a campaign
that stays turned off (paused): "KW_TRL_All_Image_DAY|Investment_35_11.11"
(TEST9095 job started 12:10Z). The owner then opened it in Ads Manager and showed
both interests in the second AND group ("And must also match") of Detailed
targeting, so Ads Manager accepted them. Nothing was activated.

The original evidence comment on the Issue says no ads were created or activated
by this check; that sentence was inaccurate and is corrected in the latest comment
on #1510:
https://github.com/MojoTheApe/adopy-campaing-launcher/issues/1510#issuecomment-6795735087

## Scope notes

- Non-code result: this PR adds only this report and a changelog fragment.
- No account ids, tokens or profile identifiers are recorded here by design.
