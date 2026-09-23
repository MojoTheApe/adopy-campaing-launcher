# System history

## The tree name gets its own line where eleven columns do not fit — ACL-1277

The remaining half of ACL-1277: on a laptop with the Builder drawer docked open the
campaign name in the tree had about 91px at 1280 and 115px at 1366. Measured with the
real fragment: the ten columns other than the name need 660px by their content alone,
while the table gets 582px at 1280 and 668px at 1366. There is no width to
redistribute — the table does not fit its own contents, and the account row is
squeezed whatever the tree does.

Owner's decision (2026-09-23), choosing between hiding the derived columns and moving
the name: hide nothing. Where the name column is narrower than 210px — which leaves the
name under about 134px once the caret, the level mark and the on/off switch have taken
their share — the tree row puts the name on its own full-width line and the numbers on
the line below.

The numbers on that line do not keep the columns. The first attempt did, and review
caught what that means with honest cell padding: every numeric cell had about 2px of
text room and rendered as "$…", "1..", "6..". Which header such a number stands under
does not matter, because at that width the header row itself reads "S… A… L… F…". So
the second line lists the numbers in order, each with its own label — SPEND, META
SPEND, LEADS, FTD, CPL, CPA, CR, DELIVERY, META — and a cell with no value is not
shown at all, because "CPL" alone promises a number that does not exist. The workbook's
"—" counts as no value here: an ad usually has no workbook row of its own, and the line
would otherwise read "SPEND — LEADS — FTD — CPL — CPA — CR —". In the single-line
layout the "—" stays, because there it sits in its own column and costs nothing while
saying "we looked and found nothing". The indent belongs to the whole row, so an ad's
numbers sit to the right of its campaign's: it is the only thing that says which row a
numbers line belongs to. The labels
come from `data-label` through CSS and never exist in the single-line layout, where the
word is already in the table head.

A workbook that could not be read now says so in words, once per tree, instead of six
dashes per row: with the dashes hidden, a broken pipe looked exactly like "this row has
no workbook numbers", and the reason lived only in the tooltip of a cell that is no
longer there. The stacked row is variable-height by design — about 28px with nothing to
show, 40px with Meta's numbers and about 55px once the workbook numbers arrive, against
34px in the single-line layout — so an open tree reflows a beat after a reading lands.

The switch is made from the measured column, not from the window width: the width is
eaten by the sidebar, the docked drawer and the country block's own padding, none of
which the window knows about. The Builder drawer is now listened to as well
(`acl:builder-drawer`): it widens the page with its own padding and fires no `resize`,
so the tree kept the widths measured before it opened and its numbers ran past the
right edge of the table — the exact case this ticket is about. The class goes on the
whole tree, so ads drawn later — when a campaign is expanded — get the same layout.
Above that width the row stays a single line, columns and all: the extra height is not
free.

The test fixture was measuring a table that was not the product's: it put widths on the
cells and left them the browser's 1px padding, while the page gives every cell
0.42rem/0.85rem and the tree copies that padding to both sides of its own cells. Every
width in this 93-test file was therefore 26px per cell too generous — which is how the
"numbers keep their columns" version looked acceptable. The fixture now builds the
template's `<colgroup>` and applies the page's own cell padding.

Measured after the change at 582px (honest padding): the campaign name box goes from
91px to 506px and the ad's from 75px to about 490px, and every number is readable in
full. At 982px the layout is untouched, columns aligned as before. Six tests in
tests/test_the_account_tree_on_screen.py cover the switch, that every number says what
it is and that empty ones vanish, that the labels are the table head's own words, that
they stay invisible in the wide layout, the untouched wide layout itself, and ads
expanded after the layout was chosen.

## One button reads the account: spend always, countries when needed — ACL-1282

The account row had "Read spend" and "Countries". Both asked the same account about
the same period and each one opened it separately. They are now one button. It reads
the spend, and then, in the same profile session, the country split only when the
split would answer something the spend does not: the account is linked to more than
one country, there is no split for these dates, or the stored split is older than the
spend reading. Otherwise nothing else is touched — the split builds a report inside
Ads Manager, and that price must not be paid for every single spend number.

The decision is made on the server (`country_split.why_split_is_needed`, called from
`_split_plan`) and travels back with the reading, because the page has neither the
account links nor the two stores. It is taken *before* the spend read starts:
`start_read` replaces the stored reading with a "running" record that carries no
amount, so a decision taken after it has nothing to compare and would skip the split
exactly when the money moved.

Staleness is about the numbers, not the clock. Each split records which spend reading
it explains (`spend_at`, `spend`, `spend_currency`), and it is re-read only when that
amount or currency has changed. The first version of this change compared timestamps
— "the split is older than the spend reading" — and never settled: a press that read
only the spend still stamped a new time on the reading, so the next press called the
split stale and built a report. Measured by review: three Ads Reporting reports per
six presses on a single-country account with nothing changing. Re-reading the same
amount now changes nothing; a changed amount brings the split back, including an
"empty" split once money appears. Splits stored before this change do not say which
spend they explain and count as stale — read once, then settled.

The account's country count no longer decides on its own. It decides where there is
nothing to compare: with no successful spend reading for these dates, an account under
more than one country takes the split straight away, because its total says nothing
about where the money went. With a matching spend reading it is left alone — the same
report would give the same answer. (The issue's wording said "more than one country"
always needs a split; kept literally, one "Read all spend" run would build a report
for every multi-country account, every run, forever — five of fourteen accounts sit
under two countries.) Countries are counted the way the map displays them, so "KW" and
"Kuwait" are one country and not a permanent multi-country verdict.

"Read all spend" follows the same rule per account, inside the same queue: the
account is open now, and a second pass would be a second touch of a live account.

A failed split no longer discards a good spend reading: the shared button returns to
"Read spend" and the failure appears in the reading cell as "countries failed" with
the reason in its tooltip. It is rendered from the server too, so it survives a page
reload, and the country totals are refreshed even after a failure — a stale total
next to a failure message would be a number that no longer exists.

With two buttons instead of three the actions column drops from 19% to 13% and the
name column gains those 6%: 28%. In tree rows at 1280 with the Builder drawer docked
open the campaign name box goes from about 65px to 91px (the ad's from 49px to 75px);
at 1366 it is 115px. At that narrowest width the two buttons still wrap onto a second
line, exactly as the three did before. That is better, not solved — ACL-1277 keeps the
rest, and the next step there is a tree grid that drops the trailing button column,
which tree rows never use, in favour of the name.

A failed split shows the reader whatever the reading wrote, and several of those
sentences are still Russian (they pre-date this change, which is what first puts them
in the account row). Only the orphaned-reading text is translated here; the rest is
ACL-1287.

## Account tree reads cleanly: name, Meta result, column widths — ACL-1275 / ACL-1277

Three display problems in the tree the owner reported on 2026-09-22. Long campaign
and ad names wrapped onto a second line; they now stay on one line and end with an
ellipsis, with the full name in the tooltip. The "Setup" column means "is this
account ready" on the account row but carries Meta's own result on tree rows; the
tree value is now labelled "Meta" and its tooltip says whose number it is, so the
two are not read as the same thing. The trailing button column, which tree rows
never use, was the widest column (23%) while the name had 19%; widths are now name
22%, Setup 9%, buttons 19%, and "Remove" is the "×" control; no numeric column was
narrowed. Measured at 1280, 1366 and 1920 with the real fragment and the Builder
drawer closed: rows and the three buttons stay on one line, and the name column
gains 29px at 1280 and 49px at 1920.

With the Builder drawer docked open (400px) the table is 582px at 1280 and 668px at
1366; the name then shows about 7-10 characters, and the account row's buttons wrap
to a second line exactly as they did before this change. Eleven columns do not fit
that width; the case is not solved here. ACL-1282 (Read spend and Countries merged
into one button) frees the next chunk, and a tree grid of its own is the fallback if
that is not enough. With the drawer open the "Meta" source label hides itself so the
number it labels stays visible; the source remains in the tooltip.

Two ads under one campaign showing the same name is real account data, not a read
bug: Meta's own embedded structure for FacebookDoc19 lists distinct ad ids with
identical names (2 in GMBL, 3 in Day). The existing "—" with "several ads share
this name" stays as designed.

## Latest read shown, marked, when the dates on screen have none — ACL-1271

The account map showed a Read result only for exactly the period on screen: open
1–22 September after reading 1–21 and the row said "—", "0/1 spend read", with no
tree. Now, when an account has no reading at all for the dates on screen, the row
shows its latest successful reading for other dates: muted and italic, with those
dates under the amount and in the tooltip ("not the dates on screen"), and the
campaign/ad tree is painted from it with the same note in its heading. Such amounts
are not added to the country total and not counted in "N/M spend read". A failed
reading for the dates on screen keeps its "failed · retry" and shows the marked
fallback under it (owner's decision: one failed attempt must not hide the numbers and
the tree); a running reading is shown as before. Read spend / Read all spend read these accounts for the dates on screen as usual.

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
