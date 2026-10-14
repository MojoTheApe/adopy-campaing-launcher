# Official Meta token diagnostic — ACL-1879

Run from a source checkout with Python. The diagnostic inspects a token's
application identity; it does not read an ad account or prepare publication.
The command never discovers ACM configuration, dotenv, browser cookies or
credentials. It does not save the token or create a connection.

```text
python scripts/acl_meta_token_probe.py --app-id 1234567890123456 --api-version v24.0
```

The numeric ID and version above are examples. Supply the expected application
ID and a Graph API version currently supported for that application, checked
against Meta's documentation. There is no default version and no claim that the
example version was tested live. Supply the **subject token** and the inspector
**App Secret** through the two hidden prompts. An unavailable hidden-input
mechanism fails before any request; the command refuses echoed fallback.

Automation may supply `--token-env ACM_PROBE_TOKEN --app-secret-env ACM_PROBE_SECRET`
with those explicitly named variables already populated in that process by its
credential mechanism. Names alone are command arguments; never pass token/secret
values as arguments or paste them into shell history. The command reads only the
named variables and does not scan the environment. `--require-scope ads_read`
optionally compares a reported scope; it does not prove account access.

The request is one GET to `https://graph.facebook.com/<version>/debug_token`.
The inspected `input_token` is separate from the `access_token` inspector
credential formed as `App ID|App Secret`, following Meta's
[first-party diagnostic](https://raw.githubusercontent.com/facebook/agentic-tools/main/plugins/devtools/skills/debug-access-token/scripts/debug_token_probe.py).
Certificate verification is required. The transport disables ambient proxies,
cookies, redirects and retries. Request size is at most 32 KiB, response body at
most 64 KiB. Socket operations receive a 10-second timeout and body reads use the
remaining monotonic budget. Python DNS resolution and header receipt do not
provide a hard total wall-clock deadline; this is not a guarantee that every
invocation finishes within ten seconds. Credentials travel in the HTTPS query
required by this diagnostic contract; do not enable HTTP wire tracing around it.

Output is one sanitized JSON object with local outcome codes, application ID,
known token type, validity, expiry evidence, scope comparison, timestamp/version
and bounded numeric Graph error codes. Raw bodies, URLs, free-text errors,
user/target IDs and credentials are omitted. Missing expiry is `unknown`, zero
is `zero`, and a past timestamp is `expired`. Missing scopes are unknown rather
than an empty permission set. Known types are USER, PAGE, APP and SYSTEM_USER.
Top-level inspector errors are separate from an inspected `is_valid=false`;
an ambiguous OAuth error does not establish subject-token revocation.

| Exit | Meaning |
|---|---|
| 0 | Matching valid application evidence; requested scopes reported present, or none requested |
| 1 | Inspected invalid subject, wrong application, expired evidence or unsupported token type |
| 2 | Invalid local arguments/credentials or unavailable protected input; zero requests |
| 3 | Inspection/transport/response failure; no success inferred |
| 4 | Matching application evidence, but requested scope evidence missing or unknown |

Even exit zero always reports `account_access`, `read_readiness` and `publishing`
as `not_checked`. App matching with unknown expiry is application evidence only.
Future ACL-1858 integration must separately verify actual ad-account access and
read capability, and separately establish publication permissions. ACL-1844 and
ACL-1872 remain separate source children. No live token or account test is part
of this change, and this source diagnostic does not authorize deployment.
