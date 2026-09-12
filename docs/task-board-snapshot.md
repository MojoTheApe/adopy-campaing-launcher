---
board: ACL
updated: 2026-09-09
---

# ACL — Task Board

Five areas of the system. Under each area sit the capabilities it is made of,
and under those the actual tasks. **This file is the source of truth** — the
visual board, the dashboard and any agent session all read it. Edit it here.

## How to edit

One task per line:

```
- [ ] `acl-999` Short title #next @dashboard
      An optional note, indented. Free text.
```

- `[x]` means shipped, `[ ]` means not yet.
- `` `acl-999` `` is the id. Never reuse one. Write `closes acl-999` in a
  commit message and the task ticks itself — the closing word is required,
  because a commit that merely mentions a task has not finished it.
- `#status` — one of `#shipped` `#review` `#next` `#later` `#idea` `#blocked`.
  No tag means `#idea`.
- `@track` — who does it: `@dashboard` `@rpa` `@analysis` `@content` `@ops`.
- Indented lines under a task are its note.

**Subtasks** — indent a checkbox under the task. Ids get a letter:

```
- [ ] `acl-103` Read spend from the company BI #next @analysis
  - [ ] `acl-103a` Find out how the BI gets spend
  - [ ] `acl-103b` Connect ACL to the BI
```

A task whose parts are all ticked counts as done — no need to tick it twice.

**Dependencies** — `needs:acl-105` on any line. That is for work that lives
somewhere else on the board. If it is only *your* work, in *your* order, it is
a subtask, not a dependency.

Three rules for splitting, learned the hard way:

1. **A subtask is a thing you finish, not a thing you think about.** "Connect
   to the BI" is a subtask. "Improve spend reading" is a wish.
2. **If the blocker is somebody else's task, it is `needs:`, not a subtask.**
   Otherwise your task silently owns work you cannot do.
3. **When you do not know the parts yet, the first subtask is finding out.**
   That is honest, and it is usually a conversation rather than code.

Add, delete, reword freely. Rebuild the board with:

```
python scripts/acl_board.py --render
```

---

## 1 · Complete account control
> The account is read and acted on without a person opening Ads Manager.

### Check Spend & Metrics

- [x] `acl-101` Spend and leads from the stats workbook #shipped @analysis
      The morning report, per country and per account. This is where every
      number on the dashboard comes from today.
- [x] `acl-102` Account map: spend, leads, CPL, CPA, CR by country #review @dashboard
      On `feature/account-country-links`. Reviewed on screen, never run on a
      buyer's machine.
  - [x] `acl-102a` A country can be created before it has an account #review
        Adding a country demanded a first account from the list of already
        connected ones — so an account still sitting in Multilogin could not be
        the first. The order everyone actually works in was forbidden. A country
        now exists on its own and offers both ways to fill it.
  - [x] `acl-102c` Live countries first, ordered by spend #review
        The sidebar went alphabetically. Alphabet says nothing, and twenty
        countries are read from the top. Live means either money is moving or
        somebody opened the country in the map — the second matters on its own,
        because a country is opened before its first dollar and until then
        looked as dead as one closed six months ago.
  - [x] `acl-102d` A country's accounts live on that country's tab #review
        Under CM Comparison, the same block as on Home but for one country, and
        the same markup for both screens. Looking at a country, its accounts
        were on another page among twenty others. A country not yet on the map
        can be added from there too.
  - [x] `acl-102b` "Remove" is where it can be seen #review
        It was always there, eleventh cell in the row, and nobody saw it: the
        column widths still added to 100% across ten columns after an eleventh
        was added, so it sat past the right edge. A control nobody can see is
        the same as one that does not exist — it was asked for as a new feature.
  - [x] `acl-102e` Country blocks open the same way every time #review
        The first three countries were open, the rest closed — a rule nobody
        can see on screen, driven by the order of the list. All are collapsed
        now; the summary already carries accounts, spend, leads and FTD. What
        a person opens is remembered in that browser, because the dashboard
        reloads itself and would otherwise close the country being worked in.
  - [x] `acl-102f` Order in the account map columns #review
        Ad Asset and Also in are gone — one was noise, the other nobody could
        read. The two spends now stand side by side and each says where it came
        from: the workbook, and the ad account itself. Numbers finally sit
        under their own headings — `.amap th` forced every heading left and
        outweighed the general `th.num` at equal strength, so the label sat on
        one side of the column and the figure on the other.
        The count is now tested, not remembered: columns, colgroup shares,
        their sum, and the drill's colspan. The same class of slip once pushed
        the button column off the right edge.
  - [x] `acl-338` The state of an account is written, not ticked #review @dashboard
        Two columns this same branch had added, undone on sight.
        **To do** carried the open recommendations — "Air/GMBL/Vid Blue - 3⚠" —
        and took a sixth of the table's width. For a ready account that is
        noise rather than a task: the optimization section already says it, and
        in the account row it answers nothing.
        **The ✅/❌ tick** answered a question nobody asks out loud — "is it
        ready". People ask a different one: what is going on with this account.
        The column now says `ready`, and when the account is not ready it says
        why ("Video selfie"). That reason was the only thing worth keeping from
        To do: for a not-ready account the workbook's to-do *is* the
        explanation; for a ready one it is somebody else's list.
        Status stays separate — it is what the account page said when it was
        last read. An account can be set up and still carry a warning, so the
        two must not be merged.
  - [x] `acl-339` A day the workbook does not know must not look forbidden #review @dashboard
        "For some reason I cannot set yesterday in the date picker." Checked on
        the live dashboard: it can be set — the button is not `disabled`, the
        click applies, the address becomes `?date_from=2026-09-08`. Only the
        look was in the way.
        Days and presets with no workbook rows were painted muted with
        `opacity: .55` — the same language `:disabled` speaks two lines below.
        The reading was correct: that is what a prohibition looks like.
        This matters beyond the pixels. The workbook says what the *dashboard*
        knows, not what can be asked of the *account*: Read goes to Ads
        Manager, where yesterday and today always exist, while the workbook
        lags a day or two almost every day. Dimming the choice by the workbook
        ties two sources to one limit — the clipping itself was removed for
        that reason, and the styling kept saying otherwise.
        The colour is back; "no rows here" is now a dot under the day and a
        dashed marker on the preset. A dot says "no data", dimming said "no".
- [ ] `acl-103` Read spend from the company BI, not the workbook #next @analysis needs:acl-105
      Today every number comes from an Excel file somebody keeps up to date.
      **The BI gets its spend through Adverity** — read-only ETL that pulls
      from Meta's Marketing API on a schedule and delivers to a destination.
      So the spend ACL should read already exists, collected, harmonised and
      sitting in a database. Nothing new has to be built against Meta for it.
      Adverity cannot open or close anything — that is `acl-111`/`acl-113`,
      and no amount of ETL will ever cover it.
  - [ ] `acl-103a` Ask five questions about the Adverity setup
        Answered: the BI gets spend through Adverity. What is not known is the
        wiring, and every one of these changes what gets built —
        (1) where does it deliver: an external warehouse (Snowflake, BigQuery,
        Redshift) or Adverity's own storage?
        (2) is the data collected down to ad set and ad, or campaign only?
        (3) how often does the fetch run?
        (4) is the **Facebook Metadata** connector set up, or only Facebook Ads?
        (5) can we have a Management API key, or read access to the warehouse?
        Still a conversation, not code.
  - [ ] `acl-103b` Read the spend — warehouse first, extract download second
        **A. Query the warehouse.** One SQL statement. No new Meta permission,
        no proxy question at all: we read our own database rather than touch
        the account. Preferred.
        **B. Download the extract** when there is no warehouse access:
        `GET /api/datastreams/{id}/extracts/most-recent/download/?fileformat=json`
        with `Authorization: Bearer {key}`. Returns the whole last extract; we
        filter locally. Coarser, but it works.
  - [ ] `acl-103c` Spend follows the chosen period, not one fixed number needs:acl-105
  - [ ] `acl-103d` Show both numbers: from the data source and from the account
        Your own spec for the account map had two spend columns. They only
        agree when nothing is wrong — the gap between them is the signal.
  - [ ] `acl-103e` Carry currency and timezone with the number
        Adverity's Facebook Metadata connector reports `currency` and
        `timezone_name` per account. Without them a spend figure is not
        interpretable — which money, and which day boundary — and a wrong
        answer here looks exactly like a right one.
- [x] `acl-105` Timeframe that survives real use #review @dashboard
      **Pending review — do not merge to master yet.** The work lives on
      `feature/account-country-links` and has never run on a buyer's machine.
      Merging to master means handing it to every machine at once, so it waits
      for a person to look. Two calls belong to that review: the whole
      dashboard now defaults to month-to-date, and recommendations are
      recomputed inside whatever period is chosen.
      Defects listed 2026-09-07 against that build. The reference is the date
      picker in the company BI — presets down the left, two months, explicit
      Apply/Cancel.
  - [x] `acl-105a` Write down what is actually wrong with the picker
        Done: the five below. Read against the code, not from memory.
  - [x] `acl-105b` Apply and Cancel buttons on the picker itself #merged
        Today the second click on a date loads the period immediately —
        `amapApply` fires from inside the day handler. Nothing shows the range
        before it commits, and closing the box after one click drops that click
        without a word. Selecting dates and confirming them must be two steps.
  - [x] `acl-105c` Default the period to month-to-date #merged
        `date_from` and `date_to` default to empty, and empty means every date
        in the workbook. Nobody chose that — it is what you get when nobody
        chose anything.
  - [x] `acl-105d` Presets down the left, as in the BI #merged
        Yesterday · Last 7 days · Last week · This month · This quarter ·
        This year · Custom Range. The active one stays highlighted, so the
        picker always says which period you are looking at.
  - [x] `acl-105e` Two months side by side, with both dates shown as text #merged
        One month at a time makes a cross-month range guesswork. The BI shows
        the previous and current month together and prints both ends as
        `YYYY-MM-DD` above the calendars.
  - [x] `acl-105f` Days with no data struck through, not merely greyed #merged
        They are already `disabled`, but grey reads as "dim", not as "absent" —
        the BI strikes them through and the difference is unmistakable.
  - [x] `acl-105h` A close cross on the picker #merged
        Cancel means "forget what I picked". A cross means "I am done looking".
        Not the same thing, and today only one of them exists.
  - [x] `acl-105i` The period drives every number on the dashboard #merged
        It currently reloads one fragment. Rebuilding the whole report from a
        filtered workbook costs about a second — measured, not guessed — so the
        cheap way and the right way are the same way.
  - [x] `acl-105j` The picker lives in the header, not above one section #merged
        A control that changes the whole page cannot sit inside a block, or it
        reads as belonging to that block.
  - [x] `acl-105k` The drill shows the same days as the row above it #review
        The period changed the page while `/details` still read the whole file:
        the row spoke about six days and the panel under it about thirty-seven,
        both equally believable. For a row absent from the other range the
        answer came back empty — which reads as "it does not open".
        Fixed together with a second thing found there: the drill had **two**
        toggles, ours on the click and the native one on `htmx:beforeRequest`.
        They raced — on one row the second click closed, on another it closed
        and reopened. One owner now.
  - [x] `acl-105m` The end of the range was being lost #review
        Asking Ads Manager for 1–6 September showed **1–5**: the end of the
        `date=A_B` parameter is exclusive. Six days became five and the answer
        was 67.87 instead of 327.35.
        The period check let it through — it looked for "6" in the text and
        found it inside "2026". A guard written for exactly this case missed
        exactly this case. Dates are now parsed into ISO and compared as sets:
        the screen must name the days asked for, no more and no fewer.
        Proved by arithmetic rather than by the label: 67.87 for 1–5 plus
        259.48 for the 6th is 327.35, which is what now comes back for 1–6.
  - [x] `acl-105n` The picker looks like Ads Manager #review
        Presets down the left in Meta's own order and wording, week starting
        Sunday, month and year dropdowns over each calendar, Update instead of
        Apply. Reaching August is two clicks rather than five taps on an arrow.
        The workbook clipping stayed, and this note called that honesty. It
        was not: see `acl-105p`.
  - [x] `acl-105o` One day shows as one date #review
        `2026-09-06 → 2026-09-06` read as a two-day range and made the reader
        look twice to see it was one.
  - [x] `acl-105g` Seen once on a workbook with a full month in it
        Ran 2026-09-07 against 47,770 rows over 37 days, 1 Aug - 6 Sep. Every
        preset came alive for the first time; month-to-date resolved to
        1-6 September. The run found one defect — "This month" ended today
        while the default ended at the workbook's last day, so the two never
        matched and no preset lit up — fixed by clipping each preset's end to
        the workbook. That fix was wrong and was undone in `acl-105p`: it made
        the workbook the limit on both sources at once.
  - [x] `acl-105p` The workbook stopped deciding which days can be asked for #merged
        On 8 September Today, Yesterday and Today-and-yesterday were struck
        out: the statistics file ended on the 6th. But Read does not go to the
        file, it goes to Ads Manager, where yesterday always exists. One limit
        stood on two sources that know different things — the workbook says
        what the dashboard knows, not what the account can be asked. Presets
        and days with no rows are now marked, not forbidden, and month-to-date
        counts from the calendar, so the default finally ends on the current
        day and matches the preset of the same name. The guard test reads the
        emitted attribute line by line: its first version used a window around
        the word and passed on the old code, exactly as the drill guard once
        did.
  - [x] `acl-105q` An empty period says why, and keeps the account map #merged
        Choosing today produced "No country data found. Check Excel path and
        filters" — a diagnosis about a healthy file — and took the account map
        down with the report. The Read button lives on that map and reads Ads
        Manager, not the workbook: the screen hid exactly the part that still
        worked. It now names the period, says which day the workbook runs to,
        and leaves the map in place.
  - [x] `acl-105r` The page is parsed by a real JavaScript engine before it ships #merged
        A real newline inside quotes broke one string, and a broken string
        kills the whole <script> block: the calendar stopped opening, though
        the edit had nothing to do with dates. On screen it read as "the dates
        are not clickable any more" — a description that points nowhere near
        the cause. 441 tests passed on that page, because every template check
        looked for text, and the text was there. The rendered page now goes
        through node --check. Proved by breaking the line again on purpose:
        the guard fails on that exact line with the browser's own error.
- [ ] `acl-107` Read cost through the token — prove it on one test account #next @analysis
      The first of two routes. Nothing here is written yet; the point of this
      task is an answer, not a feature: does the token actually return what we
      need, and at what cost.
  - [ ] `acl-107a` Get a token that can read one test ad account
        `ads_read` is enough to read. `ads_management` is a separate ask and
        belongs to `acl-112`.
  - [ ] `acl-107b` Read spend and impressions for one day through `/insights`
  - [ ] `acl-107c` Send the request through that account's own proxy
        The rule is the account's traffic leaves from the account's proxy, and
        our own HTTP client can honour it — unlike a hosted MCP. It is also the
        easiest thing in this whole task to forget, and forgetting it is
        invisible: the call succeeds, from the office IP.
        Worth knowing while deciding: **Adverity has been calling Meta's
        Marketing API on these same ad accounts from its own cloud, daily, and
        nothing has happened.** That is this company's own evidence that a
        sanctioned server-side integration is not what the proxy rule was
        written against — it was written against browser sessions. It does not
        settle writing, which is a different weight of action.
  - [ ] `acl-107d` Compare the number against the workbook for the same day
        Two sources agreeing is the only evidence the route works.
- [ ] `acl-108` Read cost through the RPA #next @rpa
      The second route. The browser already carries the account's proxy and
      needs no permission from anyone. **Written, on
      `feature/account-country-links`, not merged.** What is finished is marked
      below; what is left is the live run.
  - [x] `acl-108a` Open the account in its Multilogin profile and read spend #merged
        Everything around it is done and waiting for review on
        `feature/account-country-links`: the button, the background job, the
        stored reading, one browser at a time, the refusal to relaunch, the
        clear failure. What has never run is the part that reads Ads Manager —
        the screen scraping itself. 34 tests cover every path that stops before
        the browser starts; none of them can cover the one that does not.
  - [x] `acl-108b` Return the same shape as the token route #review
        `SpendReading` in `src/analysis_agent/account_spend.py`: account, day,
        source, spend, currency, state, **how it was read**, error, timings.
        The token route fills the same record; whoever calls must not be able
        to tell which one answered.
  - [x] `acl-108f` Attach to the open window, never relaunch the profile #review
        The first live test on x01 logged the account out. The shared RPA path,
        failing to attach, stops the profile through the MLX API and starts it
        again with automation — right for publishing, wrong for one number, and
        the relaunch branch is also the one that can lose the account's proxy.
        Reading now attaches or refuses, and says which.
  - [x] `acl-108g` A button that starts a profile for automation #review
        Attaching only works on a profile automation started, and until now the
        only thing that started one was the publish flow: reading spend was
        impossible without publishing something. A closed profile is simply
        started — that is not a relaunch and cannot log anyone out; a
        hand-opened one is left alone with an explanation.
  - [x] `acl-351` The builder takes its accounts from the links, not the workbook #review @dashboard
        An account connected on the dashboard and absent from the stats
        workbook showed in the country's account map — spend, status, a Read
        button — and did not appear in the Campaign Builder at all. There was
        nowhere to build on it, however much it was configured. That is exactly
        a new test account: the working workbook has no row for it and should
        not.
        The builder answers "what are we launching", not "what made it into the
        report". Its source is now the account↔country links.
        This also switches on something the link store could do from the start
        and nobody could use: **one account in several countries**. Links are
        one row per link, `link()` takes nothing away, and the "already
        connected…" list offers every known account — so pulling an account
        from one country into another already worked. But in the builder it was
        visible only where the workbook knew it, and the second country stayed
        half-there.
        Ordering matters: the accounts are added before the preflight list is
        collected. Otherwise a linked account would get a builder row without a
        configuration check — it would look ready while saying nothing about
        itself.
  - [x] `acl-350` Open the profile before connecting the account #review @dashboard
        A closed circle. Connecting a profile found in Multilogin asked for the
        ad account number. The dashboard can read that number itself — but only
        from a profile opened by automation, or one this machine has opened
        before. Opening a profile was offered only for an account already in
        the map. And to get into the map, the number was required.
        The only way out was to go and find the number by hand. The order
        people actually work in — find it, open it, look inside the account,
        connect it — was forbidden by code rather than by anything real.
        Broken in three places. A profile now opens straight from the search
        results, without the map. A missing profile folder is no longer a
        refusal: Multilogin creates that folder on the first start here, so
        refusing over it meant refusing to start a profile because it had never
        been started. And an empty number no longer cancels the connect — the
        account joins the map and the builder says `⚠ missing: ad_account_id`
        until somebody fills it, which it already knew how to do.
        What did not soften: a profile opened by hand in MLX Desktop is still
        left alone. Restarting it is what logged x01 out.
  - [x] `acl-352` The `act` parameter must be digits — Meta ignores the prefix #review @dashboard
        `FacebookDQ438` refused to read spend: "Ads Manager opened a different
        account". The number stored in the map was right — the browser's own
        address bar showed it. What disagreed was our URL.
        We put `act=act_<digits>` into the query, the number exactly as the map
        stores it. Meta does not understand that parameter and **silently
        ignores it**, opening the profile's default ad account instead.
        **It looked like it worked for six weeks.** On a profile with one ad
        account the default *is* that account: Meta ignored the parameter and
        landed in the right place for the wrong reason. Seven successful reads
        of `KLD_BFL_X84` — 259.48, 1072.39, 408.74 — were all made through a
        broken address. It could only become visible on a profile with several
        accounts, and `DQ438` was the first.
        The guard that caught it was right to refuse: reading another account's
        numbers and filing them under this one is worse than not reading.
        One more reason it lived so long: our own URL parser **forgives** the
        prefix — `(?:act_)?(\d+)`. We were lenient about our mistake when
        reading and committed it when writing, so it never surfaced.
        The refusal now names both accounts. It used to say only that they
        differed; the fix is exactly those two numbers, and getting them took a
        separate probe nobody at a screen can run.
  - [x] `acl-345` The totals row has to be shown before it can be read #review @dashboard
        The totals row sits **below** the campaign list in Ads Manager, and in
        a short window Meta does not render it at all — so waiting for it was
        waiting for something that was not being drawn. On 8 September a read
        of 1–8 September failed exactly this way: the screenshot saved on
        failure ends right under the first campaign row, the dates are right,
        the Results spinner is still turning, and there is no totals row on the
        page. Twenty seconds of waiting changed nothing.
        The list is now scrolled before each look, not once — it loads as it
        scrolls. Branch `fix/spend-totals-scroll`, commit `292c78a`, carried
        whole inside `feature/manage-todo-column`.
  - [x] `acl-346` One row per account, one place to connect #review @dashboard
        Under the account map lived a second section describing the same
        account again: its own numbers (`Cost` — the same spend), its own Ad
        Asset column nobody could read, and its own connect form. That form was
        worse than the one on the map: the map finds a profile by the last
        letters of the name and reads the ad account id out of its own history,
        while this one asked for the number by hand. Unique to it were only
        "what is left to do" and "ready", which moved up into the row.
        Commit `6e61025`.
  - [x] `acl-337` The profile folder has two names and MLX uses the second #review @dashboard
        `KLD_BFL_X84` refused with "the profile was opened by hand in MLX
        Desktop" immediately after the dashboard itself had opened it with
        "open profile". The message sounded familiar and reasonable, which is
        why it drew no suspicion.
        Everything on the machine was fine: the Mimic process carried
        `--remote-debugging-port`, that same process held the listening socket,
        CDP answered. One path did not match. Multilogin launches the browser
        under the 8.3 short name
        `C:\Users\EXAMPL~1\mlx\profiles\WORKSP~1\...`, we compute the long one,
        and the ownership check compares them as strings — `abspath` and
        `normcase` do not expand short names, so two names of one folder
        disagree and no process is found.
        Checking ownership by folder is right and stays: without it a read
        could attach to *another* account's browser. Both spellings are offered
        instead, long one first — a volume with 8.3 disabled has no short name
        at all.
  - [x] `acl-108h` The read follows the period on screen #merged
        It always asked for yesterday. A number for a different stretch than
        the one shown beside it would read as two sources disagreeing while
        actually answering a different question. Readings are now kept by
        (account, from, to), so a day and a range cannot be mistaken for each
        other.
  - [x] `acl-108c` Pull yesterday's spend off a live test account #merged
        The whole point. Until this happens `acl-108` is code, not a route.
  - [x] `acl-108e` A public "attach, never relaunch" in the RPA track #merged @rpa
        PR #122, merged 11 September. Master carries the public pair and the
        gathered selectors (`acl-108j`) — the reader calls them there now.
        Reading calls `_try_attach_existing` and `_profile_process_running`
        directly — private methods of another track — because there is no
        public way to say "use the window that is open, and if there is none,
        stop". Publishing wants the opposite and gets it; reading needs this.
        **The dangerous half is closed** (11 September, `e6be77e`): a relaunch
        would log the account out and can lose its proxy, and until then that
        rule lived in a comment and two `strict_profile=True` arguments. A test
        now records every call reading makes to the provider and names any new
        one; a second test keeps every reading path going through the single
        door. What remains is the track boundary itself, and moving reading
        **Done 11 September** (`ec2edbb`, `feature/acl-108e-public-attach`):
        `attach_existing_only` and `profile_is_running` on the provider. They
        delegate to the same private methods, but from outside only the promise
        is visible — the kitchen can change while the promise holds.
        `strict_profile` is pinned inside the public path. A free port search
        suits publishing, which just started the profile itself; reading it
        would let attach to someone else's window and read someone else's
        account. Three guards: reading calls nothing private, the public path
        does nothing but delegate, and the strict flag stays.
        The move itself (`acl-108d`) is still worth doing, but this no longer
        waits on it.
  - [x] `acl-108j` Gather the reading's Meta selectors in one place #review @dashboard
        Groundwork for `acl-108d`, and useful on its own. Meta moves controls
        for everyone at once: on 10 September it rewrote wording and eight
        publishing steps fell over — reading looks at the same screens and
        breaks the same way. Selectors scattered through the file mean fixing
        them one at a time, found by search.
        Seven names now sit above the first function, with the two JavaScript
        blocks beside them. A test refuses any selector literal inside a
        function, so the block cannot quietly spread back — it caught one the
        eye had missed (`_wait_for_grid`).
        `country_split.py` is deliberately untouched: the other session is
        mid-work there (`acl-357`, `c069838`). Consolidating a file someone is
        rewriting is how two people produce one bug twice.
        Branch `feature/reading-selectors-in-one-place`.
  - [ ] `acl-108k` The same for `country_split.py` #blocked @dashboard needs:acl-357
        Waits on the other session's Ads Reporting work reaching master
        (`acl-357`, `c069838`). Gathering a file while someone rewrites it is
        how two people produce one bug twice — that already happened once on
        11 September with the language field.
        Most of it is already tidy: three named JavaScript constants
        (`_GRAB_CELLS_JS`, `_CLICK_LABEL_JS`, `_CHECKED_JS`). What is left is
        one query written inline at `_shown_period_text` — a whole page scan
        and a date pattern spelled into a Python string, escapes and all.
        Move those beside the rest and the file matches `account_spend.py`;
        the guard from `acl-108j` can then cover it too.
  - [ ] `acl-108d` Move the Ads Manager reading into the RPA track #next @rpa
        Tracked in GitHub Issues as **ACL-1121** (#121), module Data & Reporting,
        type Improvement, Backlog. Taken out of the ACL-108 checklist on 11
        September: an unfinished item there would keep ACL-108 from ever
        reaching Merged for work that is no longer part of it.
        The selectors for Meta's own screens belong beside the other ones, in
        `src/fb_ads_rpa`, owned by the session that fixes them when Meta moves
        a control. It sits in the dashboard track today only because that is
        whose hands were free.
        **Half done, parked 11 September** (`29f3e14`,
        `feature/acl-108d-reading-into-rpa`, pushed, merged nowhere). The code
        has moved: `src/fb_ads_rpa/ads_manager_read.py` holds Meta's screens,
        Multilogin profiles and the period sums; `account_spend` keeps the
        readings store, the statuses and the read queue — dashboard questions
        that never touch a Meta screen. The dependency runs one way.
        What is left is not the move but the tests: 115 failures and 13 errors,
        all from one mechanical cause. Around fifty
        `monkeypatch.setattr(account_spend, "_period_matches", …)` targets
        across thirteen files name things that have moved. Each has to be
        re-pointed and checked that it patches the name the code actually
        resolves.
        Re-exporting the moved names back into `account_spend` would turn the
        suite green and meaningless — the patch would stop reaching the real
        call. Not an option.
        Also on that branch: a copy of the `browser.py` change that shipped
        separately as `acl-108e` (`ec2edbb`). The branch forked earlier; drop
        it on rebase.
        No longer urgent — `acl-108e` closed the dangerous half. What remains
        is track ownership, and it costs a session of careful mechanical work.
  - [x] `acl-108i` Reading three days took three minutes #merged
        The retry added the day before was right and expensive: each look
        walked every div and span on the page one node at a time, thousands of
        round trips to the browser. Done once it cost a second; done twenty
        times it cost 175s for 5-7 September against 39.8s for a single day.
        The search now runs inside the page in one call, and the row it needs
        to hover is tagged there so it can still be picked up by selector. The
        fixed three-second pause after "View results" became a retry too — it
        was a bet that three seconds are always enough.
- [ ] `acl-109` The two routes must agree, and disagreement must be visible #next @analysis
      Same account, same day, both routes. A gap means one of them is wrong,
      and a system that quietly picks a favourite would hide exactly the fault
      it was built to catch.
      They **will** differ, and not always from a fault: Meta restates figures
      after the fact, the account's timezone decides where a day ends, currency
      is converted somewhere, and the attribution window can be set differently
      in the Adverity datastream than on screen. A comparison that does not
      know this reports four normal things as four bugs.
- [ ] `acl-110` Compare with another period #next @dashboard
      Asked for on 2026-09-08 alongside the Ads Manager-style picker. The
      picker part is half an hour: a checkbox and a second range beside the
      first. **The rest is not.** Comparing means every number on the page
      gains a second value and a delta — the countries overview, the account
      map, the recommendations — and the report has to be built twice and
      matched row by row.
      So the checkbox is deliberately **not** in the picker yet. A control that
      selects a period nothing compares would be the fourth thing this month
      that looks like it works and does not.
      Worth settling first: compare against what — the previous period of the
      same length, the same period a week ago, or a period the person picks?
      Ads Manager offers all three and they answer different questions.
- [ ] `acl-106` An empty period should say why it is empty #next @dashboard
      Picking 6 September shows a dashboard with nothing on it. That is true —
      this buyer has no rows that day, the report is per manager — but the page
      says it the same way it would say "everything is dead". A period that
      lands on nothing must name the reason: no rows for you, or no rows at
      all. Found while verifying `acl-105g`.

- [x] `acl-104` "Live spend" button — straight from the ad account #review @dashboard
      One account, on demand, when a person asks. Never on a timer. This is
      the Marketing API route, separate from the BI: it answers "what does the
      account say right now", which is not the same question as `acl-103`.
- [ ] `acl-353` "Read all" on the account map #next @dashboard
      **Release-critical.** Every account has its own Read button; reading a
      map of ten accounts is ten clicks and ten waits, and a buyer will do
      that every morning. One button reads every connected account for the
      period on screen.
      The browser rule stands — one profile at a time (`acl-108a`) — so this
      is a **queue, not a parallel run**: accounts are read one after another,
      each row updates as its reading lands, and the button shows progress
      ("3 of 10"). One account failing must not stop the queue: its row keeps
      the error, the next account starts. Never on a timer — the button is
      still a person asking (`никогда не поллить аккаунты` rule).
      To settle first: one global button, per-country buttons, or both — a
      country tab already shows its own accounts (`acl-102d`).
- [x] `acl-710` Connect an account where the country has none #review @dashboard
      A country shows up on the map because the workbook has money for it,
      while nothing is linked to it yet — Guatemala, $1,894 spent, zero
      accounts. Opening it gave a sentence, not a control: "connect the
      account from the country's Accounts section, or add the country above
      and place it." The person is already looking at the country and gets
      sent to look for another place.
      Nothing stood in the way: linking an account **creates** the country —
      `account_links.link` writes a row and the country starts existing. So
      the same form works, addressed by the country's name instead of a key
      it does not have yet.
      The form is now one piece (`_account_map_add.html`) used by both — a
      country from the map and a country that is only a number in the
      workbook. Two copies would have drifted: they get edited at different
      times for different reasons.
      Branch `feature/connect-empty-country`, based on `acl-356` iteration 2
      (`20375c8`) because that is what the dashboard serves today.

### Open/close ads/ad sets

- [ ] `acl-111` Open and close through the token #blocked @analysis
      One API call per action. Blocked on `acl-112` — read access cannot pause
      anything. Same proxy rule as `acl-107c`.
  - [ ] `acl-111a` Pause an ad set, and start it again
  - [ ] `acl-111b` Pause a single ad
  - [ ] `acl-111c` Refuse to touch anything outside the account being worked on
- [ ] `acl-112` Request `ads_management` on the app #next @ops
      Read access is not enough to pause anything **through the token**. That
      sentence used to read "nothing in this capability moves until this is
      granted", and on 11 September that stopped being true: the review picked
      the RPA route as the only one, and the browser carries the account's own
      proxy and needs no permission from Meta. `acl-113` does not wait on this.
      It still gates `acl-111`, if the token route is ever revived.
- [x] `acl-113` Close campaigns and ads through the RPA #review @rpa
      Not a fallback — the second route, meant to work on its own. The browser
      already carries the account's proxy, so it needs no permission from Meta.
      **Works live since 12 September.**
      **Owner's decisions.** The control is a toggle on the rows under an
      account — no separate dialog. The levels are **campaign and ad**: the
      11 September note here said ad set and ad, and that was wrong for how
      this team works. Campaigns and ad sets run one-to-one, and the campaign
      is what stops the money. The live account settled it — the same two rows
      read at both levels: `on / Campaign off` at the ad-set level, `off / Off`
      at the campaign level. A toggle on the ad set would have controlled the
      wrong thing.
      **Only turning off.** Turning off spends no money, turning on does; the
      way back is by hand in Ads Manager, where the person sees the
      consequences. Only a switched-on row is a button; a click takes two steps
      so a mis-click cannot stop a live campaign.
      Four checks before acting: the ad account, the period on screen, that the
      page is Ads Manager, and that the row under that id still carries the
      name the person saw. An id can go stale; turning off the wrong campaign
      is worse than turning off none.
      **Drawn off and staying off are different.** After the click it waits for
      `aria-checked=false`, then reloads and reads the row again — up to twelve
      looks. The first live turn-off confirmed on the **second** look, so the
      patience was needed and not a precaution. A one-glance check had already
      reported a failure for an ad that did turn off.
      Never open an ad's editor (DQ204, 2 August): Meta pre-selects "Instant
      form (suggested)" on every opening and autosaves it, so a clean Leads ad
      picks up error #100 from a look-and-leave visit.
      Every outcome is written down — `turn_off_log.jsonl` plus a launcher log
      line. That is the part of `acl-117` without which the action could not
      ship: "something changed yesterday" is not an answer.
      Branch `feature/acl-113-toggle-in-the-tree`, stacked on `acl-118`.
      GitHub: #23.
  - [x] `acl-113a` Close a campaign #review
        Starting it again is deliberately out of scope — see "only turning
        off" above.
  - [x] `acl-113b` Close a single ad #review
        First live confirmation 12 September, 15:52:38: `2025_23- Officialy`.
  - [x] `acl-113c` Never near Publish #review
        The switch's own label goes through `wizard_guard.forbidden_click_reason`
        before the click. It reads "On/off" and never matches, which is the
        point: the guard is there for the day we grab a different element.
        Until now the `^Publish$` rule sat in `rpa_forbidden_zones.yaml`
        connected only to the creation wizard.
  - [ ] `acl-113d` Reading an account takes one and a half to three minutes #next @rpa
        One visit for the campaigns, then one per campaign for its ads. At
        twenty-five campaigns it hits the ceiling and the person waits five
        minutes. Two honest ways out: read a campaign's ads when that campaign
        is expanded rather than up front, or skip campaigns that are off and
        have no spend. Neither is free — the first makes expanding slow, the
        second hides rows.
  - [ ] `acl-113e` The screen is not stolen, but the profile still is #next @rpa
        The read no longer raises the profile window (the tab is created with
        CDP's `background: true`; `context.new_page()` does not pass it). But
        the profile is busy for minutes and one profile runs at a time, so a
        second account waits. Worth knowing before ten buyers use this.
- [x] `acl-118` Read an account's ad sets and ads with ids and on/off state #review @rpa
      What the toggle stands on. The Read button already goes to that screen for
      spend: attach to the open profile, never start one, verify the ad account,
      wait for the grid. Owner's decision — this rides the same button rather
      than a new control. Expanding an account shows workbook rows at once;
      Read fills in ids and lights the toggles, so a live account is still read
      when a person asks and never on a timer.
      Per row: the name as the account spells it, the Meta id, the state in
      Meta's own words (in review, rejected, archived — not flattened into a
      boolean), the level, and which ad set an ad belongs to.
      Costs tens of seconds per account, one account at a time. The interface
      has to show it working rather than pretend the tree is already live.
      **Done 11 September**, PR #125, `check-pr` verified, 23 tests. The parse
      runs in a real browser against synthetic markup shaped like the grid —
      a JS block is code, and faking its return value would test the fake.
      State is two facts, not one: the switch (`on`/`off`/`unknown`) and the
      Delivery column verbatim. "No switch visible" and "switched off" are
      different answers, and "In review" is not flattened into a boolean.
      Columns are found by their headers — people reorder them, and a fixed
      index would read spend as a name. Caught on the way: the ads page also
      shows an "Ad set name" column, and a shared header list found it before
      "Ad name", so every ad of one ad set would have looked identical.
      **Verified live 11 September** (`fb97ab1`): 5 ad sets and 6 ads with real
      ids off KLD_BFL_X84. The first read returned zero rows with the headers
      parsed, and the self-diagnosis named three mistakes at once — the grid
      marks up only its header with roles (fifteen `columnheader`, zero
      `role="cell"`; the body is `role="presentation"`), the id heuristic was
      picking digits out of SVG path data, and the level was never checked.
      All three are answered by `data-surface`, Meta's own logging labels,
      which carry row id, cell purpose and object kind together.
      **A trap worth remembering:** that vocabulary is legacy. Ad-set rows are
      marked `CAMPAIGN`, ad rows `ADGROUP` — the names these levels had in the
      old Ads API. Reading it literally is being wrong by a whole level.
      Two facts, not one, proved right the same day: two ad sets came back with
      the switch `on` and delivery `Campaign off` — switched on, delivering
      nothing. A boolean would have shown them running.
      Open for `acl-113`: the live ad row carries no link to its ad set.
      **12 September, two corrections.** The tree walks **campaigns**, not ad
      sets (see `acl-113`), and the flat ad list turned out to be lying: it
      returned six rows worth $661 against the account's $2,117, while walking
      the parents gave twenty-one worth $1,957 — one campaign alone has six.
      The view tab honestly said "All ads"; what narrowed the list, the page
      does not say. It is no longer read.
      Ads come from `selected_campaign_ids`, so a parent is known by
      construction rather than read off the page — the "Ad set name" column
      exists in the header but has no cell in the row.
      Also fixed here: the account's own status. The search was for a phrase I
      invented ("account is restricted"); Meta writes "**We** restricted your
      ad account". With that fixed it still passed as "no warning", because the
      status was asked a moment after the grid drew and the banner arrives
      later — the wait is now bounded and only a quiet page pays for it. And a
      second layer: an unrecognised banner with "You can't run ads" reports
      `blocked` in Meta's own words rather than silence.
      GitHub: **ACL-1124** (#124), Data & Reporting, Feature, Review, High,
      Release.
        Pausing stops money; publishing spends it. The rule that the RPA does
        not press Publish does not loosen because it learned a new verb.
- [ ] `acl-116` One switch chooses the route #next @dashboard
      The rest of ACL asks for "pause this ad set" and does not learn which
      route ran. Two routes worth having are two routes worth swapping without
      touching the code that calls them.
- [ ] `acl-117` Every open and close leaves a record #next @dashboard
      Which account, what changed, which route, who asked, when. Until now ACL
      only ever built drafts a person then published. This is the first time it
      changes a live account by itself, and "something changed overnight" is
      not an acceptable answer the next morning.
- [ ] `acl-114` Stop-loss rule written at campaign creation #next @rpa
      Meta enforces it from the inside afterwards. Crosses the proxy once, at
      creation, and never again.
- [ ] `acl-115` Try Meta's official MCP on one test account #next @ops
      Open beta, needs no developer credentials. Half an hour answers whether
      API-driven management does what we want, before anyone builds it.

### Check Account Status

- [ ] `acl-121` Read `account_status` and `disable_reason` #next @analysis
      **Adverity already carries `account_status`** — its Facebook Metadata
      connector reports it per account, alongside `currency`, `timezone_name`
      and `business_id`. So account status needs neither `ads_management` nor
      a visit to the account: it is a column in the same place as the spend.
      `disable_reason` is not in the documented field list — worth checking
      before planning around it.
      That connector is account level only. Campaign, ad set and ad statuses
      are not there and still need the API or the browser (`acl-124`).
- [x] `acl-124` Read account status through the RPA #merged @rpa
      Read in the same visit as the spend — one trip, two answers. Every touch
      of a live account costs something, and a second visit for the status
      buys nothing.
      The rule that shapes it: **a quiet page is not an active account.** The
      reader reports `clear` — "no warning seen" — never `active`. A banner may
      not have rendered, Meta's markup moves, the wording may have changed
      yesterday. Writing `active` there would pass ignorance off as a check.
      Not yet run against a live account: the phrase list is a guess at Meta's
      own wording and needs one healthy account and, ideally, one dead one.
      The browser sees the banner and the reason written for a human. The API
      returns a code. When an account dies these two do not always say the same
      thing, and the difference is worth having.
- [x] `acl-122` Status colour on the account map #merged @dashboard
      A column of its own, next to the two spends. Four states, and they are
      deliberately not four shades of the same thing: blank means nobody has
      looked, `no warning` is muted because it is an absence of evidence, and
      only a real finding — disabled, restricted, under review — is red and
      carries the account's own words in the tooltip.
      `On account` became `Account spend` at the same time: the old name read
      as a state, and the state now sits in the next column.
- [ ] `acl-123` "Account Disabled" — say what a person must do #later @dashboard
      A dead account is a human errand. The dashboard should name it, not hide it.

### Update account status on ACL Dashboard & in relevant chats

- [ ] `acl-131` Status column also fed by the API route #later @dashboard
      The column exists and the RPA fills it (`acl-122`, `acl-124`). Adverity
      carries `account_status` for every account at once, without visiting any
      of them — that is the cheap route, and the two should agree. Where they
      disagree is worth knowing: the banner is written for a person, the API
      returns a code.
- [ ] `acl-132` Post status changes to WhatsApp — KLD #idea @ops
      Decide the channel first: an official API, or a person forwarding.
- [ ] `acl-133` Post Inhouse issues to the internal chat #idea @ops

---

## 2 · Create LP for account
> A new account arrives and comes out with a working landing page and a link.

### Receive account from WhatsApp (New accounts group)

- [ ] `acl-201` Read new-account messages from the group #idea @ops
      Nothing exists. The first decision is the channel, not the code.
- [ ] `acl-202` Turn the message into a row in the account map #idea @dashboard
      The account map already accepts accounts by hand — this only automates
      the typing.

### Create Limited QA — copy LP to account's domain

- [ ] `acl-211` Copy the landing page to the account's domain #idea @ops
- [ ] `acl-212` Limited QA checklist before the page goes live #idea @ops

### Create Keitaro Tracking Link (incl. tracking domain management)

- [x] `acl-221` Validate a tracking link #shipped @dashboard
      `src/campaign_bridge/link_validation.py`. Catches a broken link before
      the RPA carries it into an ad.
- [ ] `acl-222` Create the Keitaro link from ACL #idea @ops
- [ ] `acl-223` Tracking-domain management #idea @ops

---

## 3 · Campaign Upload
> The part that works today. Everything else is measured against it.

### Upload campaigns

- [x] `acl-301` Build a campaign in Ads Manager, draft only #shipped @rpa
      The RPA never clicks Publish. A person reviews and publishes.
- [x] `acl-302` Ad level: creative, text, tracking link #shipped @rpa
- [ ] `acl-303` Yotam's second supervised run #next @rpa
      The first run got partway. Not diagnosed yet.
- [x] `acl-348` The builder belongs to an account, not to a recommendation #review @dashboard
      9 September: the date was set to the 8th and the whole Campaign Builder
      for Saudi Arabia disappeared — together with `KLD_BFL_X84`, connected and
      ready, visible one row above in the account map with its spend and its
      status.
      The block was gated on the **sum of open recommendations**, and those are
      computed inside the chosen period. One day had none for anybody, so a
      working tool vanished for a reason that has nothing to do with it. Both
      gates carried it — the outer `has_open` and the per-account row.
      A campaign is built for an account, not for a stretch of days: the period
      answers "how did it go", the builder answers "what are we launching".
      Recommendations keep doing what they were for — filling the campaign
      cards when they exist.
      The empty-country message also lied: it blamed the GSheet and asked for a
      Refresh, which sounded plausible next to missing recommendations and sent
      people looking in the wrong place. It now points at the account map,
      which is where a country with no accounts is actually fixed.
- [x] `acl-347` A missing tracking link warns before upload, it does not forbid #review @dashboard
      The owner's rule: no tracking link → highlight the field and warn before
      upload, but the link is not required to upload. The builder form warns
      and points at the field; it also tells three different silences apart —
      nothing typed, typed but not saved, and a Jira address pasted where a
      tracker belongs. Commit `4ce9c91`.
      The other half of the rule lived in draft validation and did block. That
      is `acl-343`, done a day later.
- [x] `acl-342` Publishing no longer needs `Upload FB.xlsx` #review @dashboard needs:acl-343
      The workbook still sits in the publish path for four things. Checked in
      code, and two of the four turned out to be already done — the belief that
      they were not was the only thing keeping the file there.
      **Pixel — already dead weight.** `draft.pixel_id` is filled from the
      workbook, stored, printed in the preview as `Pixel: —`, and used nowhere
      else: no step types or picks it. Dropping it today costs nothing. The
      rule for when it *is* needed (ACTIVE campaigns, conversion optimisation)
      is the owner's: the account has exactly one pixel, so pick that one;
      when it burns out, a new one replaces it.
      **Creatives — already done.** `/api/creatives` scans the folder and the
      builder searches that list; the Excel lookup only fires when the filename
      is empty, which never happens for a builder draft. `image_files` and
      `video_files` in `/api/ref-data` are leftovers nobody consumes.
      **Interests — the fallback goes.** When a targeting name is not in the
      library, the workbook's Interests sheet is used instead. It should not
      be: missing interests are worth saying out loud, and a campaign can be
      raised without them.
      **Language — moves onto the dashboard**, into the setup row beside
      OS / age / budget. The rule already beats the workbook in code
      (`_geo_language_for_country(...) or excel_language`), so for GCC and
      LatAm the sheet is never consulted; only other countries use it.
      Defaults: GCC → Arabic, LatAm → Spanish (all), both clearable and
      replaceable. **Anywhere else the field stays empty and is highlighted
      when unset** — owner's decision, 9 September.
      What is left after all four: the tracking link, which already has a home
      in `ad_account_map.csv`. One thing has to flip — today the workbook wins
      wherever it has a row, and the machine map is only consulted when it does
      not. Reverse that and the file leaves the publish path entirely.
- [x] `acl-343` An empty tracking link must warn, not block #review @rpa
      Agreed with the owner: no tracking link → highlight the field and warn
      before upload, but do not require it. Half of that shipped — the builder
      form warns. The other half did not: `enrich_draft.py:151` still appends
      "Link empty in Upload FB Accounts sheet" to `draft.errors`, and
      `report.html` keeps Publish disabled while any error stands. So the link
      is optional in words and mandatory in fact.
      Two reasons this is the rule being wrong rather than caution: two lines
      below in the same function `page_id` is handled correctly — a warning,
      "required for ACTIVE campaigns only" — and the player reads the link as
      `d.link or None`, so an empty one is simply not typed. A draft without a
      destination URL is exactly what a draft is.
      One line: `errors` → `warnings`. Same for the interests fallback above.
      **RPA-track file** — `src/fb_ads_rpa/enrich_draft.py`. The dashboard
      session found it and stopped there.

### Tests that survive Meta

- [x] `acl-344` Cover the decisions a run is lost on #review @rpa
      The owner's ask: campaign runs must stop breaking every time the
      dashboard changes. Measured starting point — 18% of `src/fb_ads_rpa` and
      `src/meta_campaign_agent`, 8,502 of 10,353 statements untested, eleven
      modules at exactly zero.
      Covered: the boundary the dashboard can break a run through (one lock on
      everything that opens a browser), Multilogin profile pairing, the
      relaunch gate that keeps an account off an unproxied browser, the
      publisher's draft-only and plain-browser refusals, the Mimic version
      choice, telemetry, pacing and config. 176 checks, each broken in reverse
      first. Coverage 18% → 21%.
      **One defect found while writing them:** `pacing.begin_teach_wait`
      announced that the RPA was waiting for the human, and pacing was supposed
      to pause — `_HUMAN_TAKEOVER` was missing from the `global` declaration,
      so the flag never changed. The RPA asked the person to click and carried
      on acting in the same browser.
      Branch `feature/board-full-tests`, commits `aacb684`, `61fb88c`,
      `839ce4b`, `08752a9`, `5364787`, `c61e88b`. What is deliberately not
      covered, and why, is `acl-340` and `acl-341`.
### Navigator — an agent that reads the run and says where to look

> Project note: `Navigator` on the artifact wall. A rally navigator reads the
> pace notes and calls the corners; it never takes the wheel. The name is the
> red line: it does not drive the browser, it answers *where* and the RPA acts.

- [x] `acl-360` Stage 1 — telemetry says *why*, not only *what* #review @rpa
      The 9 September run produced six failures in a row — `placements`,
      `ad_copy`, `ad_destination` twice — and the records said nothing about any
      of them: `diag` empty on every one. Forty-three sites in the flow can
      report a failure and **not one** passed a reason.
      Worse: the detailed-targeting step sat entirely under
      `if targeting_label:`. An empty interest list left no line at all — the
      step simply did not exist in the stream. On screen it looked like "the
      interests did not get set", and nothing could tell "there was nothing to
      type" from "the field was not found": both outcomes looked identical,
      which is to say invisible.
      The context is now collected at the **single choke point** where the
      record is written, not at forty-three call sites that would drift.
      A skipped step gets its own outcome — `ok=None`, which stays out of the
      failure rate: a skip is not a failure, but it is not a success either.
      Everything is scrubbed before it is written: the context travels to the
      company-wide telemetry sheet, and ad account ids do not.
      Useful on its own — the person at the screen gets the reason — and it is
      the precondition for `acl-361` and `acl-362`.
- [x] `acl-361` Stage 2 — capture the page at a soft failure #review @rpa needs:acl-360
      Screenshot, HTML and the step's parameters into the run's folder,
      scrubbed. Same artefact as `acl-340`: one capture feeds both the test
      bench and the agent, and doing it twice would be silly.
- [ ] `acl-362` Stage 3 — a second opinion, one retry #later @rpa needs:acl-361
      On a soft failure the run opens the seat that already exists for a human
      (`pacing.begin_teach_wait`) and the agent answers with either "the control
      is here" or "ask the human". One attempt, the same step, everything
      recorded. Never chooses a value, never touches the browser, never adds a
      step — Publish stays impossible by construction.
- [x] `acl-340` A bench of recorded Ads Manager pages for `meta_dom` #review @rpa
      `meta_dom` is 3,175 statements and 6% covered — the largest untested
      thing we own, and the one that breaks a campaign run when Meta moves a
      button. 136 functions, and **103 of them take a page**: they cannot be
      exercised without a DOM.
      A fake page object is the wrong answer here. It would raise the number
      and protect nothing, because what actually breaks is the match between
      our selectors and Meta's markup — and a double agrees with whatever we
      wrote. The bench must run the real functions against real markup.
      Shape: save the Ads Manager pages at each step of a run, serve them from
      disk, drive them with Playwright as production does. Selector drift then
      fails a test instead of a buyer's run.
      Built 9 September on the pages `acl-361` saved at that day's six
      failures: `placements_2026_09_09.html` (106 KB),
      `ad_copy_2026_09_09.html` (120 KB), `ad_destination_2026_09_09.html`
      (93 KB) — cut from the 4 MB captures, scrubbed, `acl_scan_secrets.py`
      clean. Local Chromium opens them from disk: no Multilogin, no ad
      account, no proxy question. 30 checks, each broken in reverse first.
      What it does **not** check is written into the file: the snapshot is
      static and Meta's scripts are absent, so a checkbox fires no handler. It
      proves a control is **found**, not that it behaves — which is exactly
      the failure we have. It also ages on purpose: every fixture is dated in
      its filename, and a fresh capture now lands beside each live failure, so
      replacing one is cheaper than diagnosing from scratch.
      Selector drift now fails a test: `acl-704` was written against it.
  - [x] `acl-340a` Scrub what the pages carry before they touch the repo #review
        A saved page holds the account number, the account name, the balance
        and the campaigns. `acl_scan_secrets.py` will refuse the commit — as it
        already did on 8 September, when a real `act=…` reached a test file.
        Decide the scrub before recording, not after: what is replaced, by
        what, and how it stays readable enough to test against.
  - [x] `acl-340b` A stale bench is worse than none #review
        Saved pages age. When Meta changes its markup the bench keeps passing
        while production breaks — the failure we have been chasing all week,
        with a green suite on top. Each recording needs its date and the Meta
        build it came from, and a way to re-record that a person can run.
  - [x] `acl-340c` The pure helpers need no bench and can be done first #review
        `_norm_label`, `_website_only_label`, `_menu_first_line` and
        `_human_jitter` take no page at all, and everything else leans on
        them. 30 checks.
        Labels compare by words, not by spacing — Meta wraps them differently
        depending on window width. "Website" and "Website and instant forms"
        are different menu rows and different conversion locations, so the
        match is exact, not a substring. And pauses must not repeat: in the
        detectability review of 31 July identical intervals were the most
        countable tell — 33 pauses of exactly 0.5s, 24 of 0.6s, 21 of 0.3s in
        one file.
        Coverage of `meta_dom` stays at 6%: four functions against 3,175
        statements do not move the number. That is the whole point of
        `acl-340`.
- [x] `acl-704` Meta rewrote the wording; four steps were looking for the old one #review @rpa
      Five `placements` failures in a row, then six more across `ad_copy`,
      `ad_destination` and `destination_sweep`. One cause, four places: the
      code searched for words the page no longer has.
      Gone from the markup: "Advantage+ placements", "Manual placements",
      `ads_placement_expandable_list_item_{feed,story,stream}` (Meta generates
      ids now — `js_14p`, `_r_d2_` — never anchor to them again), the "Allow
      limited spending" checkbox, `placeholder="Write a short headline"`,
      `role=combobox` named "Tell people what your ad is about", and
      `placeholder="http://www.example.com/page"`.
      What is left to hold on to is the words next to the control — the way a
      person reads the page — plus `data-surface`, Meta's own markup for its
      logs: `/message_field`, `/headline_field`, `/pce_website_url_field`, one
      of each on the page. Ids and classes are regenerated; these are not.
      Three findings the bench alone could not have given, all from the run
      report `acl-360` added:
      **The categories are an accordion.** Opening the next one closes the
      last. The code opened all three and filled at the end, so it only ever
      worked on the third — the report showed two sub-items found out of
      eighteen, both children of the last category. Now: open one, fill it,
      move on; which sub-item belongs where is asked of the page, not guessed.
      **The reveal check could never say yes.** `fill_ad_copy` returned before
      the fields, on a check for a combobox that does not exist — and left no
      telemetry saying so. Six failures with not one row about a stage.
      **The read-back lied.** The "did it stick" check searched by the same
      dead placeholder, always answered no, and ran the repair loop on top of a
      correctly filled field. Four lookups now go through one helper.
      Result: run 19:24-19:44 on 9 September, both campaigns, 82 steps, both
      `ad_level_summary` = ok, no soft-fails. Branch
      `feature/bench-ads-manager`, merged into `work/dashboard` as `4e03f96`.
- [x] `acl-705` A retry is not a failed step #review @rpa
      `ad_copy_headline` wrote six `ok=False` rows in a run with six successful
      `ad_copy`. The first CDP click regularly reports success without giving
      the page focus; the read-back catches it and the second attempt always
      lands. A step that never fell had a 100% failure rate on the reliability
      board — and a board like that gets you fixing what works while missing
      what is broken.
      The attempt goes into the step's report, the report is attached to the
      step's outcome. Nothing is lost: if the step does fail, every attempt's
      stage lands beside the failure. Confirmed live 20:19-20:26 — not one such
      row. Merged as `38c84aa`.
- [ ] `acl-706` Two more clean runs before we trust the flow #blocked @rpa needs:acl-704
      One clean run is not repeatability. Counter: **1 of 3**, and stuck there
      for a reason outside the code: the proxy keeps dropping. Two attempts on
      10 September lost forty minutes each to an upload whose progress bar
      never moved, and a page reload during one of them landed on
      `ERR_SSL_PROTOCOL_ERROR` — the connection, not Meta, not the file size.
      Running again before the proxy is replaced buys nothing.
      First one landed 10 September, 12:04-12:26 — both campaigns, 76 steps, no
      failures, both `ad_level_summary` = ok, and on **video** creatives, which
      had produced ads with no media all morning. Nothing before it that day
      reached the second campaign at all.
      Before the next attempt: delete the drafts left by the morning's failed
      attempts. If the page crawls again, run the confirmation on a fresh
      account — 9-10 September put well over a dozen campaigns through this one.
- [x] `acl-707` The step believed a caption instead of a state #review @rpa
      Four fixes, one shape. Each waited on something being *written* rather
      than something *happening*, and each cost minutes per campaign.
      **`_wait_loading`** matched `re.compile(r"Loading", re.I)` — no word
      boundary. On the ad editor that finds a permanent visible sentence:
      "Send people to a **fast-loading**, mobile-optimized experience", the
      Instant Experience description. It can never hide, so `wait_for(hidden)`
      burned the full 30s — before *every* attempt of *every* click. Telemetry
      proved it: `ad3_name` had a flat floor of 31.7s across eight runs, while
      `ad_name` ranged 0.7-32.6 depending on whether that sentence was on the
      page.
      **`onboarding_modal`** made five attempts to click a one-time modal's
      button, each with an 8s visibility wait: exactly 43s, fourteen runs in a
      row, and the modal was never there once.
      **The creative upload** counted sixty passes (~70s) and the media check
      counted 150 (~2.5 min) — with a comment claiming the time was "only spent
      while work is in flight". It wasn't. A video that needed 166s was
      declared a failure at 537s with "Uploading video …" still on screen.
      **The error screen.** `ERR_SSL_PROTOCOL_ERROR` was not recognised at all,
      so the step would report "couldn't find Create" — blaming Meta's markup
      for a dead connection.
      Result: ad set 3m21s → 1m48s, and the first fully clean run.
- [x] `acl-708` Show what a creative weighs, not how long it will take #review @dashboard
      One folder holds files from 30 KB to 1.5 GB and the names say nothing.
      A buyer picked blind and found out ten minutes into an upload.
      `/api/creatives` now returns the size, the picker shows it, and files
      over 100 MB are marked.
      **No time estimate, deliberately.** Six uploads in one run, all on 24-26 MB
      files: 84, 248, 40, 68, 222, 163 seconds. Six-fold spread at identical
      size — "≈9 min" would have been a number we invented. What the size does
      buy is catching the gigabyte file before it is chosen.
- [ ] `acl-709` Two reloads are not enough to clear a dead connection #next @rpa needs:acl-707
      A run may hit `ERR_SSL_PROTOCOL_ERROR` between campaigns and stop dead.
      The flow now checks for the browser's own error screen before each
      campaign and reloads — twice — because on an error screen nothing has been
      typed and a reload costs nothing. Mid-wizard it would cost the campaign,
      so the rule is deliberately not applied everywhere; a test fails if it
      spreads past two call sites.
      **It has not been shown to work.** On 10 September at 12:00:36 it fired
      on the real thing, wrote the reason down — and did not recover. Two
      reloads were not enough; a person refreshed the tab by hand and the next
      attempt ran clean. The clean run itself carries zero `browser_error` rows.
      Open: how many reloads, how long between them, and whether waiting helps
      at all when the proxy is the thing that broke.

- [ ] `acl-349` A test that blinks teaches people not to look #next @dashboard
      `test_multiple_processes_do_not_lose_writes` failed twice on 9 September
      and passed both times when re-run on its own. It starts real processes,
      so it depends on timing rather than on the code under test.
      This is worth fixing rather than re-running: a suite that is red for no
      reason trains everyone to ignore red, and the one failure that matters
      arrives looking exactly like the noise.
- [ ] `acl-341` `recipe_player` is 1,064 statements at zero #later @rpa
      Waits on the same bench: it replays a recorded flow through `meta_dom`,
      so it cannot be tested without one. Listed separately so the size is not
      hidden inside `acl-340`.

### Targeting library

- [x] `acl-330` The registry is the library — the spreadsheet is not #merged @dashboard
      The library was a tab in Google Sheets, which made it hostage to access
      that is not ours: on 8 September the sheet began answering `403` — the
      same Workspace policy that closed service accounts — and the page said
      "The library is empty or the sheet is unavailable". One line for two
      different troubles, and no way to tell which had happened.
      Targets now live in a plain file beside the other machine data. The sheet
      is read once, only to seed an empty registry, and when it refuses the
      screen prints what it actually said.
  - [x] `acl-330a` A target is a set, not a string #merged @dashboard
        Order carries no meaning: "one, two, three" and "three, one, two" are
        the same target and the second is refused, naming the first. A value
        added or removed makes a different one, and so does a different word —
        `investment` and `investing` are different interests because Meta will
        show them to different people. Case and spacing are only ways of
        writing. Narrowing counts too: the same interests narrowed differently
        are different audiences.
  - [x] `acl-330b` Add, edit and remove from the dashboard #merged @dashboard
        The form knows whether it is adding or editing; without that, Add on a
        filled form would quietly create a second target while the person
        believed they had corrected the first.
- [x] `acl-332` A target name must not break the campaign name #merged @dashboard
      The campaign name is parts glued with an underscore — `cbUpdateName`
      builds `country_asset_os_type_TARGET_age_date`. A space inside the target
      name lands inside a part, and the name stops splitting back:
      `KW_Ait_All_Image_Investment Plus_35_0809` is no longer seven parts.
      Spaces now become underscores when a target is saved. Nothing else is
      touched: hyphens and digits do not hurt the split, and cleaning further
      would mean renaming people's targets for them.
      The 48 already in the library were brought to the rule and pushed to the
      shared sheet — seven had spaces, `Investment Plus GCC`, `Sport AR com`,
      `Current Events` and four more. No collisions came of it.
- [ ] `acl-333` Exporting to the spreadsheet can still overwrite someone #later @dashboard
      The race left the ordinary path when the tab stopped being written on
      every edit. It survives in the deliberate "publish my library" action,
      which rewrites the whole tab; a snapshot is taken first, so the cost is
      recoverable rather than silent.
      Worth remembering why the tab was dropped: Google Sheets has no
      transactions, so nothing built on top closes that window — it only
      narrows it. Saying "solved" there would have been the same kind of lie
      this board keeps catching.
- [ ] `acl-331` A target added on one machine does not reach the others #next @dashboard
      **Closed this afternoon, reopened the same evening.** For an hour the
      shared tab carried edits both ways and this said "done". Then the tab was
      dropped as a source, and with it went the only thing that carried a
      target from one machine to another. The task is open again because the
      thing it describes is true again.
      Nothing carries the library between machines now. Whatever ends up
      distributing updates to every machine (`acl-313`) has to carry this too.
      The screen says "stored on this machine" so nobody assumes otherwise.

### Ad text library

- [x] `acl-334` Ad titles are a section, not a tab in someone's spreadsheet #review @dashboard
      The texts lived in the "ad texts" tab of the same sheet as the targets,
      and were hostage to the same access: on 8 September that sheet answered
      `403` and the dashboard had nothing to show. They now live in a registry
      beside the targets, built the same way — a version in the file, a lock
      while it is written, tombstones so a removed text is not resurrected by
      the next read. The tab stays as the source of the seeding; the section
      does not write back to it.
      327 rows in, 316 saved. What did not make it is named rather than
      counted: five repeated titles, six pairs with identical content, and six
      rows with no title at all, which were named by their row number so the
      text in them would not be lost to an empty first cell.
  - [x] `acl-334a` A text is a sequence, not a set #review @dashboard
        This is where an ad text differs from a target, and copying the target
        rule would have been wrong. A target is a set: "one, two, three" and
        "three, one, two" are the same audience. A text is read in order —
        "buy now, discount" and "discount, buy now" are two different ads and a
        person tells them apart at a glance.
        Same means the headline, the body and the description all match. Case
        and stray spaces are ways of writing; a line break is not — paragraphs
        are made of them, and an ad with them reads differently.
  - [x] `acl-334b` Three hundred texts are not read top to bottom #review @dashboard
        Search across every field, a language filter, and grouping by language
        with the count in the heading. The body shows three lines and opens on
        click, keeping its paragraphs — otherwise what is on screen stops
        matching what Facebook will show, and that is the thing being judged.
        Language is filled for 214 of the 327 rows; the rest sit in "Language
        not set" rather than being guessed. Spanish and Portuguese are written
        in the same script, and a guess would label a text with a language that
        is not its own.
- [ ] `acl-335` An ad text added on one machine does not reach the others #next @dashboard
      The same hole as `acl-331`, for the same reason: the tab was the only
      thing carrying anything between machines, and it is no longer a source.
      Every machine gets the same 327 rows on first read and diverges from
      there. Whatever solves `acl-331` has to carry this too.
- [x] `acl-336` The builder takes its ad titles from the library #review @dashboard
      Filed as "the builder reads from `Upload FB.xlsx`" — that was wrong, and
      the truth was worse. `/api/ad-copy` went straight to the spreadsheet, and
      without `GSHEET_TARGETS_ID` in the environment the loader returned an
      empty dictionary. Silently: no error, no log line. On screen the builder
      said **"No ad titles found"** under the Ad Title field while the Ad titles
      section held 316 texts. An empty list and an unreachable source looked
      identical, which is the same shape of failure the targeting library had.
      The builder now reads the registry — the same list the person edits. The
      spreadsheet stays as a fallback for one case only: a fresh machine whose
      library has not been filled yet.

---

## 4 · Data Analysis
> Turning what we collect into a decision somebody acts on.

### Analyse data from telemetry to make decision/rule

- [x] `acl-401` Proposals from run history #shipped @analysis

- [ ] `acl-402` Turn a repeated failure into a rule #next @analysis
      Three machines failing the same step is a rule, not three incidents.

### Analyse data from Ad account & BI to make decision/rule

- [x] `acl-411` Morning recommendations — what to close, what to open #shipped @analysis
      Computed every morning. Nothing acts on it; that is area 1.

- [ ] `acl-412` Close the loop: recommendation becomes an action #next @analysis
      Depends on `acl-111`.

### Analyse Ad materials

- [x] `acl-421` Analyse images and video frames #shipped @content
      `creative_agent/vision_analyzer.py`.

- [x] `acl-422` Correlate creative traits with results #shipped @content
      `creative_agent/stats_correlator.py`.

- [ ] `acl-423` Transcribe video, not one still frame #next @content
      Today a single frame is pulled out and looked at. The words spoken in the video are not read at all.

- [ ] `acl-424` Everything into one prompt for new materials #next @content
      Text, images, video, results — one brief.

---

## 5 · Texts
> New material, from what already worked.

### Create new headlines

- [ ] `acl-501` Generate headlines from the winners #next @content

### Create brief for new ad materials

- [x] `acl-511` Brief generator #shipped @content
      `creative_agent/prompt_generator.py:generate_brief`.
- [ ] `acl-512` The brief carries account and country context #next @content

### Execute

- [ ] `acl-521` Produce the material #idea @content
- [ ] `acl-522` Hand the material to campaign upload #idea @content
      Closes the circle back to area 3.

---

---

## 6 · Many accounts, many machines
> The launcher itself, as something a team runs rather than one person: how it
> reaches a machine, how it stays current, what it reports back, who may change
> what, and how an account gets attached in the first place.

### Getting the launcher onto a machine

- [ ] `acl-304` Install in one file #review @ops
      On `feature/one-file-install`. Never run on a clean machine — that is
      the whole risk.


### Keeping every machine current

- [x] `acl-321` Release channel: tagged releases and an Update button #shipped @dashboard
      Knowledge travels out. Nothing travels back yet.
- [x] `acl-323` The Windows build goes green again #merged @ops
      CI runs on Windows on purpose, and it has been failing on every push:
      `_atomic_json` writes `.{name}.{32 hex}.tmp` inside a runtime directory
      named `.{sha40}.staging-{uuid32}` — 82 characters — and the whole path
      crosses the 260-character limit. `FileNotFoundError`, every time.
      Called "the known MAX_PATH failure" all through 7 September and skipped
      over in every test report. A red build nobody expects to be green is the
      same as no build: it stops being read, and the next real failure hides
      behind it.
- [ ] `acl-322` Selector fixes travel back automatically #later @rpa


### Learning from every machine

- [x] `acl-311` Telemetry: runs, steps, failures #shipped @ops
- [ ] `acl-312` Telemetry on by default #review @ops
      On `feature/telemetry-by-default`. Today a fresh install arrives with
      collection off and nobody knows.
- [ ] `acl-313` A changed account layout teaches every machine #next @rpa
      Meta moves a control, one machine finds out, all of them learn. This is
      the actual point of telemetry.


### The plan itself

- [x] `acl-650` One store, two views: Obsidian and a wall #review @dashboard
      The file in Obsidian is the source; the wall is built from it and never
      remembers anything of its own. A commit that names a task ticks it.
- [x] `acl-651` The wall opens folded — heading, blurb, arrow #merged @dashboard
      Six areas and sixty-odd tasks at once cannot be read; folded, the page
      answers in one screen what the system is made of and how far each part
      is. What a person opens is remembered. Two places where folding would
      have looked like breakage are handled: a filter opens what it finds, and
      a link in the contents opens its area.
- [x] `acl-652` A commit should not be able to tick the wrong task quietly #merged @dashboard
      Today a commit about the wall was tagged `acl-104` — the live-spend
      button — and the hook ticked it without a murmur. The id existed, so
      nothing looked wrong; it was simply the wrong task, and the log gained a
      line that was false. Unknown ids are already refused. What is missing is
      a second look at known ones: the hook could print the title it is about
      to tick, so a mismatch is read before it is committed rather than found
      afterwards.

- [x] `acl-653` Next to "In review" — the branch and the commit #review @dashboard
      A task marked "In review" said nothing about where that work is. Finding
      it meant asking, or guessing from the commit log. The wall now prints the
      branch and the short commit beside the status, so an update can be found
      and merged without a conversation. Branch `feature/board-branch-link`,
      commit `87a5ba7`.
- [x] `acl-654` "In master" is a state of its own #review @dashboard
      Merged and handed out are different things, and the board had one word
      for both. A merge into master does not reach a buyer's machine until a
      release goes out, so "shipped" on a merged-but-unreleased task was a
      claim nobody could check. Branch `feature/board-merged-state`, commit
      `113689b`.
- [x] `acl-655` The plan itself has a history #review @dashboard
      The source lives in Obsidian, which has neither git nor a bin. On
      8 September one bad edit cut a whole area — eight tasks — out of the
      board, and it came back only because a wall had been published shortly
      before and its HTML still held the content. Luck, not a backup. The
      build now writes a copy of the board into the repository, so the plan has
      the same history and the same three addresses as the code. Branch
      `feature/board-snapshot`, commit `b38c26d`.

### Who may change what

- [ ] `acl-601` The team lead's permission is a flag, not a mechanism #next @dashboard
      Editing the Goals block on Home is gated by `ACL_TEAM_LEAD`, an
      environment variable each machine sets for itself. It documents an
      agreement; it does not enforce one. Anybody who can open the dashboard can
      set it, and nothing would say that they did.
      That is fine while the team is small and the thing being edited is a
      target number. It stops being fine the moment the same idea gates
      something that spends money — and area 1 is heading exactly there.
      Decide before then: keep it an agreement and say so out loud, or give the
      launcher a real notion of who is asking.
- [ ] `acl-602` A buyer sees their own goals, the lead sees everyone's #later @dashboard
      Goals are already stored per campaign manager. What is missing is the
      lead's view over all of them in one place.

- [ ] `acl-141` Dialogs that look like the platform #next @dashboard
      Connecting an account asks for the ad account number through the
      browser's own `prompt()`: a grey box captioned "127.0.0.1:9193 says",
      pasted over the page. It reads as something that happened *to* the
      dashboard rather than something the dashboard is asking. There are
      **23 of them** — 15 `alert`, 6 `confirm`, 2 `prompt`.
  - [ ] `acl-141a` One in-page dialog, styled like everything else
        Title, body, a primary and a quiet button, Escape closes, focus lands
        in the field. One component, then the 23 call sites move onto it.
  - [ ] `acl-141b` Errors stop being alerts
        Half of the 15 are failures. A red line where the thing failed says
        more than a box the reader must dismiss before they can look.
- [ ] `acl-142` Adding and removing an account, made easy #next @dashboard
      Today: pick the Multilogin instance, type the last digits, press Search,
      pick the profile — and if the profile has never been opened on this
      machine, type the ad account number by hand. Every step is a place to
      stop.
  - [x] `acl-142a` Get the ad account number without asking the human #merged
        Done, and **run on a live account**: connecting `KLD_BFL_X84` on
        2026-09-07 read its `act_…` number straight off the Ads Manager address
        and offered it. The number itself is deliberately not written here —
        the board is shown to management and now has a copy in the repository. First time any of the browser half has worked
        outside a test.
        The dashboard asks the account before it asks the person: attach to the
        open profile, let Ads Manager redirect to its own account, take `act=`
        out of the address. Offered rather than filled in — a profile can see
        several ad accounts and the redirect picks the default one.
  - [x] `acl-142d` Finding the profile's folder stopped costing ten seconds #review
        The lookup underneath all of this walked the whole Multilogin profile
        tree: 9.9 s and 9.4 s on two consecutive calls, full Chrome directories.
        The path is documented; addressing it directly takes 0.001 s. The probe
        end to end went from **34 seconds to 0.02**. The same fix belongs in the
        RPA track, where every publish run pays it — `acl-144`.
  - [x] `acl-142e` An account is named the way Multilogin names it #review
        Decided 2026-09-07. The dashboard briefly matched the profile name
        against the stats workbook and offered the workbook's name instead;
        removed at your call — route, matcher and tests together.
        The consequence is open, not forgotten: `acl-145`.
  - [ ] `acl-142c` "Read from the open account" as a button needs:acl-141
        The dialog that asks for the ad account number gets a second way to
        answer: press instead of paste. It opens the profile, lets Ads Manager
        redirect to its own account, and takes the `act=` out of the address.
        **It cannot be added to what is there now.** `prompt()` is drawn by the
        browser; its contents are not ours to change. The in-page dialog
        (`acl-141a`) is what makes this button possible — that is the argument
        for doing it, not the looks.
        **Show the number, do not assume it.** A profile can see more than one
        ad account, and Ads Manager redirects to whichever is default. Read it,
        put it in the field with the account name beside it, and let the person
        press OK. Filling it in silently would pick the wrong account now and
        then, and nothing on screen would say so.
  - [ ] `acl-142b` Removing an account is one obvious action
        `Remove` takes an account out of a country. Taking it off the platform
        altogether has no button at all today.
- [ ] `acl-144` Finding a profile's folder costs ten seconds, every time #next @rpa
      `_find_mlx_profile_dir` in `src/fb_ads_rpa/browser.py` does `rglob` over
      all of `~/mlx/profiles`, and those are full Chrome profile directories.
      Measured on this machine: **9.9 s and 9.4 s** on two consecutive calls —
      it is not a cold-cache effect. The path is documented in that same file:
      `profiles/{ws}/{folder_id}/{profile_id}`. Addressing it directly takes
      **0.001 s** and gives the same answer.
      Every `provider.open` goes through it, so **every publish run pays it**,
      not only the dashboard. The dashboard already routes around it —
      `_mlx_profile_dir` in `src/analysis_agent/account_spend.py`, written there
      because `browser.py` belongs to the RPA track. Same fix, one file over.

- [x] `acl-145` The map joins the workbook by name, and the names are drifting #review @dashboard
      Decided 2026-09-07: an account is connected under the name Multilogin
      knows it by. The thirteen accounts already in the map carry **workbook**
      names — `FacebookElf191 SA`, `FacebookMKTOBFLW27 CR Ait` — and
      `_build_account_map_view` matches on that name.
      So the first account connected under the new rule, `KLD_BFL_X84`, has no
      counterpart in the workbook, where it is `FacebookMKTOBFLX84`. Its row
      shows no spend, no leads, no CPL — and reads as an account that spent
      nothing rather than as two names nobody joined.
      Old accounts keep working, new ones do not: a gap that widens by one
      account at a time. The way out is to stop joining on the name and join on
      `ad_account_id`, which the account states about itself and nobody types.
      Until then, every newly connected account is blank on the map.
  - [x] `acl-145a` A connected account shows its own workbook numbers #merged
        Every account on the map showed dashes. The names were not the cause:
        the map took its numbers from the report, and the report is built for
        today — on 8 September the workbook ended on the 7th, so the report was
        empty and every row was blank whatever it was called. The map now asks
        the workbook directly for the chosen period, with the same suffix-blind
        matching the rest of the page uses, so `FacebookMKTOBFLW27 CR Ait`
        finds `FacebookMKTOBFLW27` on its own.
        A Multilogin name shares nothing with the workbook name except the
        trailing code — `KLD_BFL_X84` and `FacebookMKTOBFLX84`. That match is a
        guess, so it comes last, is taken only when it is the single one in
        that country, and is always written on screen.
  - [x] `acl-145b` "No rows" and "rows under another country" stopped looking alike #merged
        Both were a dash. They are different: the second means the link points
        at the wrong country, and no amount of looking for data will help. The
        row now says where the money actually sits — W08 has $786 under El
        Salvador and $202 under Guatemala while linked to Costa Rica; V97 has
        $792 under Kuwait while linked to El Salvador.
- [ ] `acl-143` Take from Jira what it already knows about an account #next @ops
      Every account has a Jira ticket carrying the details. `src/jira_agent`
      already talks to Jira — it writes those tickets from the workbook, so
      the credentials and the auth are in place; reading is a short step.
  - [ ] `acl-143a` The two names for one account
        The ticket is `FacebookMKTOBFLX84`; the Multilogin profile is
        `KLD_BFL_X84`. Nothing in ACL knows they are the same account, and a
        person bridges it by eye every time. The ticket states both.
  - [ ] `acl-143b` Pixel id and domain
        Both sit in the ticket and both are typed in by hand today.
  - [ ] `acl-143c` Credentials stay in Jira — read nothing, store nothing
        **This is a boundary about fields, not about the API.** Reading Jira
        over its API is the whole point of `acl-143`; nothing here is typed by
        hand. What ACL must not touch is three fields: the password, the 2FA
        seed and the mailbox password. Not read, not cached, not logged.
        The reason is not git. It is that a secret held in a variable ends up
        in a log line, an error message, a telemetry row or one of the failure
        screenshots this dashboard already saves — and any of those is enough.
        And ACL does not need them: the Multilogin profile is already signed
        in. Nothing in this system ever types a Facebook password; that is what
        the antidetect profile is for. Those fields exist for a person
        recovering an account, not for the machine.
  - [ ] `acl-143e` Ask for the access — the message is written #next @ops
        Four asks, no more: a Jira API token for a dedicated user with browse
        permission **on the Accounts project only**, the project key, who
        maintains those tickets, and a yes or no on reading pixel and domain at
        all. The message says in its own words what will never be read and why
        it is not needed — that is the part management will look for.
        Text ready to send: a short version to actually send, and a long one
        underneath for when somebody asks why — [[ACL — Запрос доступа к Jira]]
  - [ ] `acl-143f` One machine reads Jira, not ten #next @ops
        The launcher runs on buyers' laptops, on home connections whose
        addresses change. Two things make per-laptop reading wrong. Atlassian's
        IP allowlisting is a Premium and Enterprise feature that **applies to
        API tokens too** — a call from an address not on the list is rejected,
        and ten changing home addresses cannot be kept on a list. And a token
        on ten machines is ten copies of a credential.
        So one machine reads, and the result rides the release channel that
        already exists (`acl-321`). One token, one owner, one address to
        allowlist if it comes to that. No buyer's laptop holds a Jira
        credential — which is also the answer to "do we need to whitelist my
        IP": not yours, and not theirs.
  - [ ] `acl-143d` Find out whether the ticket already carries the ad account id
        The screenshot shows MLA name, domain, UID, pixel and the login block —
        no `act_…` anywhere, and `UID: 1000…` is a Facebook profile id, not an
        ad account.
        **Ask whether it is there; do not ask anyone to start writing it.**
        That would be a habit to keep, it would only help new tickets while the
        accounts we already have stay unfixed, and a hand-typed number is a
        second copy of a fact the account states itself in the Ads Manager
        address. Second copies are where mismatches come from — the whole point
        of `acl-109`. `acl-142a` reads it from the account instead, once.
        Note also that `src/jira_agent` writes a *different* kind of ticket —
        landing pages and domains. The Accounts space is somebody else's.


## 7 · Design
> How the thing looks when somebody who did not build it opens it.

### The header

- [x] `acl-701` Fix the header #merged @dashboard
      The top strip carried the product name, the manager, two counters, the
      build and a permanent warning — six kinds of thing at one weight, so
      none of them read as anything.
  - [ ] `acl-701a` The mark depends on a font it may not find #later
        The logo draws "ACL" as `<text>` in Segoe UI. On a machine without that
        font the letters are substituted and the mark shifts — a fault visible
        only where nobody checks, which is every machine but this one.
        Replaced with geometry on 8 September and **put back by decision** the
        same day: the old mark stays. Recorded rather than fixed, so that when
        a buyer's screen shows a mark that does not match ours, the reason is
        already written down.
  - [x] `acl-701b` Last updated and Version, both labelled #review
        `Updated 2026-09-08 · acl-release-2026-09-03e` read as one smeared
        message. Both matter at the same moment — when an update broke the
        work and you need to know what is installed and when it landed, to
        roll back.
  - [x] `acl-701c` The manager is not part of the product name #review
        "CM Evgeni Sedin" sat under the logo as though it described the
        product. It describes where the reader stands, and it changes more
        often than anything else up there. It is its own control now, on the
        right, labelled **Logged as** instead of "Buyer".
  - [x] `acl-701d` The permanent CSV banner is gone #review
        "Added 13 account(s) from ad_account_map.csv not present in today's
        GSheet" appeared every single day, could not be dismissed, and taught
        people not to read that place at all — so a real warning standing
        there would go unread too. Accounts from the map are added as ordinary
        work; they are visible where they are needed, on the account map.
  - [x] `acl-701e` `Trend 7d · 58 combos` removed #review
        Numbers nobody comes to the header for.
  - [x] `acl-701f` Room kept for the theme switch #review
        The width is reserved now so the header will not rearrange in front of
        people when `acl-702` lands.
- [x] `acl-702` Theme — light and dark #merged @dashboard
      Built from what is known about this rather than from taste: Material 3
      dark theme, IBM Carbon g100, GitHub Primer dark. Four rules from there,
      and all four are visible in the palette.
  - [x] `acl-702a` The ground is not black, the text is not white #review
        Pure #000 haloes the letters on OLED and kills every shadow. The ground
        is `#0f1216`, slightly cool to sit under the product's blue. The text
        is `#e4e8ef`, not `#fff`, which is kinder over a long night and still
        15:1. Muted text holds 7.4:1 — AAA — and there is a lot of it in these
        tables.
  - [x] `acl-702b` Height is shown by lightness, not by shadow #review
        Cards, header and sidebar are lighter than the ground. On a dark ground
        a shadow is barely visible, so elevation has to be carried by the
        surface itself.
  - [x] `acl-702c` Every pair measured, not eyeballed #review
        Contrast is computed in the test, so a future colour cannot quietly
        drop below the threshold. It already caught one: the active sidebar
        item kept white text while the accent went light in dark mode —
        2.35:1. Now there is an `--on-accent` token that knows what belongs on
        top of the accent in each theme. Swept the live page afterwards: no
        unreadable text left, no light patches.
  - [x] `acl-702d` The Facebook ad preview stays light #review
        It is a picture of somebody else's feed, not our interface. Repainting
        it dark would show a buyer something they will never see in Ads
        Manager.
  - [x] `acl-702e` The theme is set before the page paints #review
        In `<head>`, not after load — otherwise the screen flashes white for a
        moment at night, which is the very thing dark mode is turned on to
        avoid. Without a stored choice the system preference is followed; one
        press makes the choice explicit and from then on the system is not
        consulted.
      Found along the way and fixed: `var(--danger, #a4362f)` appeared in three
      places against a token that was never declared. The fallback worked, so
      nobody noticed — and those places would have stayed light.

### The country view

- [x] `acl-703` Eleven doors into one country, and four lead to the same room #review @dashboard
      Opening a country offers eleven entries in the left bar: My Goals, CM,
      Assets, Landings, Trend, Combos, Targeting, Creatives, Accounts,
      Optimization, Campaign Builder. Four answer the same question — *how is
      this country doing and what do I do about it* — and answering it costs
      four clicks across screens that never sit side by side.
      One screen, named **Manage** (decided 8 September; `Main` was taken by
      the left bar's own first group). It reads top to bottom as the work
      actually goes:
      **monthly goal for this country → CM Comparison → the account, its
      figures and its status → click an account, its ads open → the builder.**
      That leaves seven entries for what they are: material, history, analysis.
  - [x] `acl-703a` The ad drill under an account was answering the wrong question #review
        `KLD_BFL_X01` showed $1,795 and 61 leads while the panel beneath it
        said "No matching rows". Two causes, both the same shape: a rule from
        somebody else's question applied in silence.
        The drill looked the account up by its Multilogin name while the
        workbook knows it as `FacebookMKTOBFLX01` — the translation existed
        only in the map. And `_aggregate_cm_drill_rows` keeps only ad sets with
        at least one deposit, which is right for the manager comparison and
        wrong here: X01 had 61 leads and no deposits, so 59 workbook rows
        became an empty panel. The label lied too — "7 campaigns with deposits"
        over rows with none. Now "7 ad sets".
  - [x] `acl-703b` The account is the row, its ads are underneath, the builder follows #merged
        The order is the decision: account with its figures and status, its ads
        one click down, and the builder after that. Today the builder opens
        under an account name in a screen of its own, so the same account is
        described twice in two places.
  - [x] `acl-703c` The Campaign Builder is a page, not a panel #merged
        It carries its own slots, account accordion, preview and publish flow,
        and holds its place across a refresh through the URL hash. Under a goal
        chart and a comparison table it needs a decision about what stays open
        — otherwise Manage becomes a screen nobody can see the bottom of.
  - [x] `acl-703d` Keep the old addresses working #merged
        `#country-kw-goals`, `-cm`, `-accounts`, `-open` sit in bookmarks and
        in the refresh logic. Merged screens must still answer to them, or a
        refresh in the middle of building a campaign lands on a blank view.
---

---

## Log

- 2026-09-09 · `9d82a44` · `feature/capture-page-on-fail` · `acl-361` — feat(телеметрия): разметка упавшего шага ложится на диск
- 2026-09-09 · `9cf6a41` · `feature/upload-without-workbook` · `acl-342`, `acl-343` — feat(заливка): Upload FB.xlsx уходит из пути публикации
- 2026-09-09 · `f3123eb` · `fix/calendar-days-not-disabled` · `acl-339` — fix(календарь): день без строк в книге больше не выглядит запрещённым
- 2026-09-09 · `11cd654` · `feature/manage-todo-column` · `acl-338` — feat(карта): состояние аккаунта словом, колонка To do убрана
- 2026-09-09 · `3aa45ef` · `feature/builder-ad-texts` · `acl-336` — fix(билдер): тексты берутся из библиотеки, а не из чужой таблицы
- 2026-09-09 · `8129c88` · `fix/attach-short-path` · `acl-337` — fix(спенд): у папки профиля два имени, и Multilogin берёт второе
- 2026-09-08 · `abd054e` · `feature/ad-text-library` · `acl-334`, `acl-334a`, `acl-334b` — feat(тексты): раздел Ad titles — библиотека объявлений уезжает из таблицы
- 2026-09-08 · `005ca43` · `feature/account-country-links` · `acl-142a` — fix(карта): страны свёрнуты по умолчанию, раскрытое запоминается
- 2026-09-08 · `22870e5` · `feature/account-country-links` · `acl-701` — feat(шапка): шесть разных вещей одним весом — теперь по-другому
- 2026-09-08 · `2cec60f` · `feature/account-country-links` · `acl-105r` — fix(дешборд): страница проверяется настоящим разборщиком JavaScript
- 2026-09-08 · `32dfd9f` · `feature/account-country-links` · `acl-108i` — perf(спенд): поиск итогов внутри страницы, а не обходом по узлу
- 2026-09-08 · `3c30690` · `feature/account-country-links` · `acl-332` — feat(таргеты): общая библиотека в обе стороны, имя не ломает название кампании
- 2026-09-08 · `684ae71` · `feature/account-country-links` · `acl-145a`, `acl-145b` — fix(карта): числа берутся из книги за период, а не из отчёта за сегодня
- 2026-09-08 · `92bb0b5` · `feature/account-country-links` · `acl-702` — fix(тема): переключатель вместо иконки — положение видно, а не угадывается
- 2026-09-08 · `96bc4aa` · `feature/account-country-links` · `acl-330` — feat(таргеты): реестр — единственный источник, таблица уходит из работы
- 2026-09-08 · `9d72ed5` · `feature/account-country-links` · `acl-105p` — fix(период): книга больше не решает, за какой день можно спросить спенд
- 2026-09-08 · `9f80d35` · `feature/account-country-links` · `acl-330a`, `acl-330b` — feat(таргеты): свой реестр вместо чужой таблицы, уникальность по набору
- 2026-09-08 · `b2c37ab` · `feature/account-country-links` · `acl-122`, `acl-124` — feat(статус): состояние кабинета читается в том же заходе, что и спенд
- 2026-09-08 · `c847712` · `feature/account-country-links` · `acl-108h` — fix(спенд): итоги ждём, а неудачу видно с первого взгляда
- 2026-09-08 · `dc16b4d` · `feature/account-country-links` · `acl-105q` — fix(период): пустой отчёт называет причину и не прячет карту аккаунтов
- 2026-09-08 · `e85e4ee` · `feature/account-country-links` · `acl-703b`, `acl-703c`, `acl-703d` — feat(страна): Manage — четыре экрана в один, билдер последним
- 2026-09-08 · `34bef9a` · `feature/task-board` · `acl-652` — fix(доска): дубль номера ловится и в подзадачах, и о нём говорит хук
- 2026-09-08 · `94a4af8` · `feature/task-board` · `acl-651` — feat(доска): области сворачиваются — заголовок, подзаголовок и стрелка
- 2026-09-07 · `179627b` · `feature/account-country-links` · `acl-108a`, `acl-108c` — feat(спенд): итоги раскрываются наведением — первое живое чтение
- 2026-09-07 · `281dffb` · `feature/account-country-links` · `acl-323` — fix(сборка): короткое имя временного файла — путь перестал упираться в предел
- 2026-09-07 · `6865ec9` · `feature/account-country-links` · `acl-105g` — fix(дешборд): «This month» совпадает с периодом по умолчанию
- 2026-09-07 · `0721a0e` · `acl-105h`, `acl-105i`, `acl-105j` — feat(дешборд): период выбирается в шапке и режет весь отчёт
- 2026-09-07 · `7b49e41` · `acl-105b`, `acl-105c`, `acl-105d`, `acl-105e`, `acl-105f` — feat(дешборд): выбор периода подтверждается, а не срабатывает на клике
Commits that ticked a task land here, newest first.
