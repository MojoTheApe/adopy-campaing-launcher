# Recover local Meta application preparation

For the local preparation behavior and its limits, see
[the system description](../system/META_CONNECTION_PREPARATION.md).

Restore `meta-connections.json` and the corresponding `protected-secrets`
directory together from the same local backup under the same Windows user.
Moving ciphertext to another machine/user is not a supported credential
transfer. Preserve an unreadable registry for recovery; do not replace it with
an empty registry to bypass permanent application ownership.

Malformed retirement metadata is refused before cleanup or reconciliation writes.
An invalid/missing retirement phase or contradictory pending state leaves the
registry and encrypted files intact for restoring the matching backup. Do not
edit the phase to force deletion or restore a retired binding.

If user-bound decryption is unavailable, reopen setup and save a new official
credential on the current Windows user account. A disconnected setup with
pending cleanup remains disabled; reopening/status retries deletion. These
operations affect local stored credentials only and do not revoke Meta access
or delete a remote application.

After an uncertain Save response, reopen saved setup before retrying. Mapping
replacement may leave the old binding disabled even if CSV replacement failed;
prepare a new separate connection rather than restoring the old binding by
editing metadata. No recovery step here verifies API access or enables the
unfinished official HTTP/onboarding scope of ACL-1858.
