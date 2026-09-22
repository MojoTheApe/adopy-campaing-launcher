# System history

## Latest read shown, marked, when the dates on screen have none — ACL-1271

The account map showed a Read result only for exactly the period on screen: open
1–22 September after reading 1–21 and the row said "—", "0/1 spend read", with no
tree. Now, when an account has no reading at all for the dates on screen, the row
shows its latest successful reading for other dates: muted and italic, with those
dates under the amount and in the tooltip ("not the dates on screen"), and the
campaign/ad tree is painted from it with the same note in its heading. Such amounts
are not added to the country total and not counted in "N/M spend read". A running or
failed reading for the dates on screen is shown as before, never replaced by an old
one. Read spend / Read all spend read these accounts for the dates on screen as usual.

## STAGE badge and "not on stage" count in the header — ACL-1267

Owner's decision (2026-09-22): the process stays as it is — working branch -> stage
-> main, and campaign managers get updates only from a release tag on main. The
dashboard run from source is the version the owner works on, so its header shows a
STAGE badge with how many changes in it are not on the project's `stage` branch yet
("+119 not on stage"), and the browser tab title starts with "STAGE ·". Commits are
counted without merges and without copies already on stage by the same patch; the
project remote is found by its URL, and the count is refreshed by a background
fetch every 10 minutes, so rendering never waits for the network. If the count
cannot be determined the badge says "stage unknown" with the reason in its tooltip.
Packaged builds (what campaign managers run) show no badge.

## Read buttons say what they read — ACL-1266

Account managers read the map's "Read" buttons as "connect the account". On the
owner's decision the buttons now name the action: "Read spend", "Read all spend",
"Read all spend in <country>"; Read all while busy says "Reading spend N of M…"
(the per-account busy label stays the short "reading…" so three buttons still fit
the actions cell at 1280–1366px), and the country counter reads "N/M spend read" both from the
server and after a read in the page. "Connect" was rejected on purpose: the
button reads spend for the dates on screen, and "connected" already means the
account/profile link. Labels only; behaviour unchanged.

## Workbook numbers per campaign and ad in the account tree — ACL-1256

The account row shows spend, leads, FTD, CPL, CPA and CR from the stats workbook;
the tree under it (campaigns and ads read from Ads Manager) showed only Meta's own
spend and results, pushed to the right edge. Now every tree row stands under the
account columns: Spend (workbook), Account spend (Meta), Leads, FTD, CPL, CPA, CR
(workbook), Status (Meta delivery), Setup (Meta result). Column widths are taken
from the account row itself and follow window resizes.

Numbers come from `/account-attribution`: the same selection as the account row
(country, workbook alias, period) and the same formula, so campaigns add up to the
account row (verified on Doc20, Doc19 and Doc21). A campaign is the workbook's
`Adset Name`, an ad its `Ad Title`; names are matched with whitespace collapsed.
Rows the workbook does not know, or that share a name with another row (e.g.
several ads with one name in a Doc19 campaign), show a dash with the reason in
the tooltip, never guessed numbers.

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
