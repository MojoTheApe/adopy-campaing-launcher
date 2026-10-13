# Local preparation of separate Meta applications

ACL-1872 extends the existing shared Connect panel. Select an account already
mapped on this machine, then enter its separate App ID, official access token
and optional App Secret. The result says **Separate app details saved. API access
is not verified.** Reopening the panel or restarting ACL resumes the same setup.
Saved credentials are never populated back into form fields.

This step makes no Meta or Multilogin request. It does not create an application,
verify the declared App ID against the token, select another transport, enable
official Read spend or grant publishing permission. ACL-1858 retains those
remaining outcomes and its ACL-1844 dependency. Automatic Developers onboarding
still requires an observed real wizard and its separate browser ownership work.
The shared secret boundary serves new Meta credentials only; broader legacy
Google and Multilogin migration remains ACL-1358.

## Identity and state

The server resolves an exact account label against the current machine map and
requires one canonical physical profile UUID/ad account pair. Labels and countries
are display references. An alias or ambiguous map cannot select a credential.
The same unfinished pair reuses its connection UUID, including pending cleanup.
Renaming a label preserves that UUID. App IDs are text containing digits and are
distinct fields from ad account IDs; their ownership is declared, not verified.

App ownership remains recorded after replacing credentials or local disconnect.
Another connection cannot reuse that App ID, including after a profile/account
mapping replacement. Changing a mapping retires the old connection permanently;
changing away and back cannot revive its credential. A newly prepared binding
needs a separate application. Local disconnect never revokes Meta access or
deletes the remote application.

`awaiting_app` requests input; `saved_unverified` records successful local
protection. `binding_changed` requests preparation for the new mapping.
`disconnected` has no active credential. `cleanup_pending` remains disconnected
and asks for another cleanup attempt; it cannot create a duplicate setup for the
same pair. Reopen/status retries cleanup. An unavailable protected credential
requests replacement on the current Windows user account.

## Persistence and interruption

Metadata lives in `meta-connections.json` under the machine configuration folder.
Encrypted envelopes live in its `protected-secrets` directory. The reusable
`config.protected_secrets` boundary uses Windows DPAPI CurrentUser, checks envelope
schema, owner and credential revision, and has no plaintext/platform fallback.
Only server-generated UUID references are accepted. Responses contain explicit
safe status fields; references and credentials are never returned.

All map/connection writers serialize in this order: process thread lock, registry
file lock, map file lock, vault operation. Existing link, patch, remove, bootstrap,
legacy copy and installer setup writers participate. Every begin/save/status
also checks the actual map. Unknown direct external edits are observed on the next
request; ACL cannot audit an external edit that changes away and back between
all its observations.

Async dashboard callers run complete synchronous map transactions in the thread
pool. Waiting for a file/thread lock therefore leaves unrelated control requests
dispatchable. Permanent App ownership is cross-checked against every accepted
save receipt as well as the current App ID; contradictory metadata fails closed.

A save writes ciphertext first, then atomically commits metadata and its request
receipt, then removes the prior encrypted envelope. A crash before metadata commit
retains the old credential and may leave an unreferenced encrypted file. Such a
file is not used automatically. A crash after commit selects the already-written
new credential. Failed encryption or metadata replacement cannot return success.
Reopen after an uncertain response to inspect durable state. Replaying the same
request ID cannot commit a second replacement; a changed revision conflicts.
Request history is bounded at 2048 accepted changes per connection and refuses
further changes visibly rather than forgetting replay protection.

Disconnect and mapping retirement commit the disabled state before deletion.
A cleanup failure therefore retains an encrypted file while operations remain
disabled. Ownership checks prevent a cleanup reference from deleting another
connection's secret. Mapping changes retire bindings before CSV replacement;
if that replacement fails, the old binding deliberately remains disabled.
This conservative outcome needs fresh preparation even if the map still shows
the old pair.

Unreadable metadata is preserved and reported before reconciliation, cleanup or
writing. A pending retirement must name `disconnected` or `binding_changed`, have
pending cleanup, no active credential and a compatible binding state. Missing or
contradictory retirement metadata preserves the registry and encrypted files;
valid pending cleanup still resumes normally. Metadata is never silently replaced
with an empty registry. Support bundles exclude the registry/vault, and existing
telemetry/support scrubbing covers App Secret and credential reference fields.
There is no browser-local secret persistence. User-bound encryption is not a
credential transfer mechanism between machines/users. These data files are
outside the installation and survive source/package updates. See the
[operational recovery procedure](../runtime/META_CONNECTION_RECOVERY.md).

Verification uses isolated synthetic configuration: real public ASGI callers,
Chromium executing the shared Connect partial and its production JavaScript,
concurrent process writers, injected persistence failures and separate actual
Windows DPAPI roundtrip/fresh-process recovery tests. An OS-boundary fake in the
cross-platform tests provides no live Meta, MLX or API-permission evidence.
