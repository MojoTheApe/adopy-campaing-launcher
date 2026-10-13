# Official Meta HTTP access pilot — ACL-1844

This is a prepared source-level diagnostic for one explicitly selected ad account.
It does not connect accounts to the dashboard, provision an app, prove ownership,
check publishing access or replace the browser backend. No live Meta request was
part of implementation or the offline tests. Related ownership/access research:
[ACL-1788](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/788).

## Before a real read

An operator must select the exact account and have authorization to read it with
an official token for the intended Meta app. Access visible inside an Ads Manager
browser profile is not proof of access through that app. Clarify ownership or
partner access and assigned assets with the portfolio administrator; this tool
does not claim, share, transfer, unlock or repair accounts.

Supply a user or system-user token obtained through the authorized Meta app and
asset assignment process. Start with reading permissions appropriate to that
account. Do not copy cookies or extract a browser's token, and do not send a token
through a chat, issue, command argument or saved diagnostic report. A successful
read does not establish permission to create, activate or publish advertising.

Official references: [Meta's Marketing API collection](https://www.postman.com/meta/facebook-marketing-api/collection/0zr4mes/facebook-marketing-api-mapi),
[separate Marketing API access tiers](https://developers.meta.com/blog/updates-to-ads-management-standard-access-feature/).
The pilot fixes the API version to **v26.0**; it has no arbitrary URL or version
override. App review, quotas, ownership and actual token validity remain external
prerequisites, not facts inferred by this diagnostic.

## Run after the read has been authorized

From a source checkout with its Python environment:

```text
python scripts/acl_meta_access_probe.py --account act_1234567890 --since 2026-10-01 --until 2026-10-02
```

The account above is a placeholder. Choose explicit ISO calendar dates in the
account's timezone, inclusive, with a maximum span of 31 days. The tool first
validates these arguments, then prompts for a token without echo. If hidden input
is unavailable, it refuses the fallback to echoed stdin. `--help` performs no
read and needs no credential.

For a process launched by an authorized secret provider, the sole alternative is
an explicitly selected process variable:

```text
python scripts/acl_meta_access_probe.py --account act_1234567890 --since 2026-10-01 --until 2026-10-02 --token-env ACL_META_OFFICIAL_ACCESS_TOKEN
```

Arrange that variable through the provider's secret input, not a token literal in
shell history. The tool does not read `.env`, ACL machine settings,
`META_ACCESS_TOKEN`, browser profiles or stored credentials. The variable is not
used unless `--token-env` is present. It does not save the token or write telemetry.
Use of a process variable can expose the secret to tools with permission to inspect
that process; the hidden prompt is the normal interactive path.

## What is actually checked

At most two requests, both GET, to `https://graph.facebook.com/v26.0`:

1. Read the selected account's `id`, `account_id`, `currency`, `timezone_name` and
   `account_status`. Both returned identity fields must match the explicit ID.
2. Read that account's Insights at account level for the explicit date range.
   A reported row must match the account, currency and both requested dates, and
   contain a finite nonnegative decimal spend. Extra rows or a next-page link
   mean the small diagnostic is incomplete and are not accepted as success.

The token travels in the Authorization header, never the URL. The transport uses
certificate verification and does not use ambient proxies, browser cookies,
redirects, retries, pagination, writes or a browser fallback. An environment that
requires a proxy will need a separately reviewed transport option; this pilot
does not silently adopt one. Socket timeout is 5 seconds; body reading has a
10-second elapsed-time check and a 64 KiB limit. Operating-system DNS resolution
is outside the socket timeout guarantee. This is a bounded diagnostic, not a
promise of a fixed overall latency on every network.

## Interpret the JSON result

- `ok`: verified account metadata and a successful spend read. `reported` carries
  a decimal string; `no_data` carries null, because an empty response is not
  silently turned into a confirmed zero.
- `partial`: account metadata was verified, but the spend read failed. Previously
  confirmed metadata is preserved, with a typed failure and the failed step.
- `failed`: no complete verified account result. Failures distinguish invalid
  input, invalid credential, permission, rate limit, network, account mismatch and
  unexpected response. Numeric Meta codes/subcodes and HTTP status may be included;
  upstream error text, response bodies and raw exception details are excluded.

`publishing_access` always says `not_checked`. Account status is the returned
numeric field, not a prediction that advertising can run. No scopes are treated
as proof of publishing rights. The report contains the explicit account ID and
spend, so keep it local unless sharing those business data is authorized.

Exit code 0 means the read completed (including an explicitly empty report), 1
means a typed read failure or partial result, and 2 means input/credential setup
or an unexpected local failure prevented completion. The tool does not retry;
after a failure the operator decides the next step.

## Verification and delivery boundary

`tests/test_official_meta_access_probe.py` uses fake HTTP responses and covers
identity mismatch, date/currency mismatch, empty/incomplete/malformed reports,
credential/permission/rate/network errors, secret echo, request/body limits,
no writes/redirects/retries and the CLI secret-input boundary. It does not verify
the user's app, real accounts, network, permissions or parity with Ads Manager.

This script is an operator tool in the source checkout, not a new packaged UI.
Dashboard integration, credential storage, OAuth onboarding, many-account reads
and publishing need their own scope and review after an explicitly authorized
one-account pilot. Existing source review, stage, main and package delivery rules
remain in force.
