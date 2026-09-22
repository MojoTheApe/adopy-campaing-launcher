# System history

## Country-attributed read part without noise labels — ACL-1264

The same cell's `in <country>: …` part (spend the accounts attribute to the
country, acl-357) now follows ACL-1258: amounts via `fmt_summary_amount`, no
`currency unknown`, no account names. Sign-only amounts from different accounts
are still not added up; the reason stays in the value's tooltip.

## Country read summary without noise labels — ACL-1258

The collapsed country row showed read totals without a currency code as
`currency unknown 1,044.82 (FacebookDoc20 - Mios Darwiti)`. On the owner's request
the country summary now shows only the amount: a 3-letter code or Meta's own sign
stays (`KWD 7.00`, `$1,044.82`), otherwise a bare number. Such amounts are still
never added together and stay separate values. The per-account cell keeps its
`currency unknown` label. Server (`fmt_summary_amount`) and page script
(`amapSummaryMoney`) format the same way.

## Ads under campaigns of an id-less account table — ACL-1255

Some ad accounts (FacebookDoc19, Saudi Arabia, since 2026-09-18) show the new Ads
Manager table without object ids. The account tree read their campaigns by name but
no ads, because ads are requested per campaign id. The campaign list page embeds
Meta's own `campaign_structure_tree` data with each campaign's id and ad names; the
tree now takes the id from there (only for names that map to exactly one id), reads
that campaign's ads through the usual filtered list, and keeps them only when every
ad name belongs to that campaign's structure. The recovered id is stored as
`data_id`, never as `id`, so turn-off stays unavailable for such rows. Accounts with
ids are unchanged. Read only: no clicks, no editor.

Live check on 2026-09-22 found the ads list of such an account still unread: the
campaign ids were recovered, but ad names in that table are plain text (not links)
and the page carries no `level=` hints. The id-less parser now takes a row's name
from the tooltip inside a row with a switch when no name links exist, and, without
`level=` hints, takes the level from the name column heading (`Campaign`, `Ad set`,
`Ad`). An unknown heading still refuses the list.

## Audience field confirmation — ACL-1250 / PR #247

Merged into master on 2026-09-21 after independent review, full Windows tests and
installer verification. A nearby budget number or saved-audience summary can no
longer falsely confirm age/country settings, and opening a collapsed field cannot
select an adjacent field's remove control. Nested text/pencil layouts stay supported.
The real Windows/Multilogin pilot remains explicitly deferred in ACL-1215. This
record describes source behavior, not a released package or completed adoption.

## Adoption checkpoint — 2026-09-21

ACL-1248 changes repository organization and delivery governance; it adds no product
feature. Current product code includes the reviewed installer (#245), update helper
(#246) and shared library (#244). Their remaining setup/pilot requirements are listed
in PROJECT.md and the original Issues. Code integration is not a released installer.

Existing detailed release history remains in [RELEASES.md](../RELEASES.md); original
specifications remain at their established locations. Preserve their dates and scope.
Add future implemented changes and reasons here, linking the actual ticket and PR.
