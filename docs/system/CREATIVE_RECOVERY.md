# Creative recovery (ACL-2231)

Meta can return a created creative without the requested ACL metadata. The creative stage now saves a machine-local intent before dispatch and the confirmed ID immediately after the response. Receipts live under ACL_CONFIG_DIR/creative_receipts and use the existing machine lock and atomic writer. Preserve this directory across installs and recovery; never delete a pending receipt to force a retry.

A receipt or legacy orphan is reusable only after reading its exact ID, account, Page, requested form, media and requested content. Name/tag matching alone is insufficient. Metadata may differ, including Meta date/hash name suffixes. Expiring video thumbnail URLs are excluded from material identity. Final ad readback checks the actual bound creative again.

Only a complete bounded listing can prove absence before the first dispatch. Unknown sends, corrupt receipts, unreadable same-ad candidates, truncated listings and multiple matching candidates stop without a new creative POST. A pending intent never expires into permission to create. The result exposes partial creative_ids for review even if a later operation fails.

This journals only creative creation. The broader operation lifecycle remains separate under ACL-1856. Existing campaign/ad-set/form guards and the PAUSED campaign delivery boundary remain required. A local test installation is separate from hosted CI, normal source approval, main integration and package delivery.
