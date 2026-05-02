# Facebook account optimization report (MX, GT, EC, UAE)

**Data:** Excel export (`Country`, `Account Alias`, ad set metrics). Raw analysis agent output: [`_account_optimization_run.md`](_account_optimization_run.md).

**Command to reproduce** (from `fb_agents` repo root):

```powershell
python run.py --country all --top 2 --with-context `
  --accounts "FacebookMKTO1211,Facebook662,Facebook_8661,FacebookUL643,FacebookMKTO1203,Facebook_6218,FacebookMKTO1239,FacebookMKTO1235,FacebookMKTO1227,FacebookMKTO1215" `
  --focus-countries "Mexico,Guatemala,Ecuador,United Arab Emirates" `
  -o analysis_agent/_account_optimization_run.md
```

The `--focus-countries` filter is required: the same `Account Alias` values can appear with different `Country` column values (other geos) in the file.

---

## Label → `Account Alias` mapping in Excel

| Your label | Country (`Country` column) | `Account Alias` |
|------------|----------------------------|-----------------|
| FacebookMKTO1211 MX | Mexico | FacebookMKTO1211 |
| Facebook662 MX | Mexico | Facebook662 |
| Facebook_8661 GT | Guatemala | Facebook_8661 |
| FacebookUL643 GT | Guatemala | FacebookUL643 |
| FacebookMKTO1203 EC | Ecuador | FacebookMKTO1203 |
| Facebook_6218 UAE | United Arab Emirates | Facebook_6218 |
| FacebookMKTO1239 UAE | United Arab Emirates | FacebookMKTO1239 |
| FacebookMKTO1235 UAE | United Arab Emirates | FacebookMKTO1235 |
| FacebookMKTO1227 UAE | United Arab Emirates | FacebookMKTO1227 |
| FacebookMKTO1215 UAE | United Arab Emirates | FacebookMKTO1215 |

*The same **Facebook662** alias is also used in the UAE (separate from MX).*

---

## Mexico

**Accounts in scope:** FacebookMKTO1211, Facebook662.

**Close / cut back:** no clear candidates for a full shutdown — both accounts deliver FTDs and positive Net. **Facebook662** has lower profitability and higher CPA (ROI ~0.15, CPA ~$1,868 on 4 FTDs) vs **FacebookMKTO1211** (ROI ~0.24, CPA ~$818 on 3 FTDs). It is reasonable to **shift part of the budget** from 662 to 1211 if the goal is efficiency, not volume alone.

**Open / scale (Amazon offer):**

1. **ios + Day + 40 + ccblc - pro** (PRO) — 2 FTDs, strong combo; in the data the top FTD volume for this stack is on **Facebook662** (see the Recommendation block in `_account_optimization_run.md`).
2. **all + Mix4 + 40 + CCblc - pro** (PRO) — 1 FTD, best ROI among recommended lines; top account for the combo — **FacebookMKTO1211**.

**Practice:** duplicate winning combos onto the account with better economics (1211), while keeping on 662 what reliably delivers the Day stack.

---

## Guatemala

**Accounts in scope:** Facebook_8661, FacebookUL643.

**Close / cut back:**

- **Facebook_8661** — **0 FTDs**, ~$730 spend, Net $0. It makes sense to **pause or sharply cut** campaigns on this account in GT until the offer / creative / landing changes.
- **Banco Industrial** at country level: 0 FTDs on ~$1,262 spend — do not invest further in the current setup (see Offer comparison).

**Open / scale:**

- On **FacebookUL643** there is 1 FTD on **AI Trader** with positive Net at country level — **scale carefully** the ad sets that produced the FTD; run new combo tests in parallel.
- The report has **no** qualifying Strong/Interesting combos for Guatemala (`No qualifying combos …`), so new launches should come from hypotheses and tests, not from the automated combo top list.

---

## Ecuador

**Accounts in scope:** FacebookMKTO1203 (only account in the list for EC).

**Close / cut back:**

- There is only one account — **do not “close” it entirely**, but **cut** directions with no deposits: **AI Trader** and **Al Trading** (0 FTDs on ~$3.8k combined spend) until refreshed creatives / landings are in place.
- **Favorita** drives FTDs and Net; spend and results are concentrated there.

**Open / scale:**

- **Favorita:** combo **all + trading + 35 + 3509 - the app** (two aggregated “strong” lines in the combo report) — **scale** on **FacebookMKTO1203**; TopAccount in the recommendation matches this account.

---

## United Arab Emirates

**Accounts in scope:** Facebook_6218, FacebookMKTO1239, FacebookMKTO1235, FacebookMKTO1227, FacebookMKTO1215, Facebook662.

**Close / cut back:**

- **Facebook662** (UAE): **0 FTDs**, ~$1.36k spend — **pause or use a minimal test budget** until there is a new strategy (the same alias performs differently in MX — treat geos separately).
- **FacebookMKTO1215**: **0 FTDs**, ~$1.57k — **cut back / stop** current combos.
- **FacebookMKTO1239**, **FacebookMKTO1235**, **Facebook_6218**: **0 FTDs** with noticeable or test spend — **do not scale**; either pause or run only tightly capped tests.
- By offer: **AA Acady, AI Trader, ChatGPTPlatform, Finance Eagle, Trader AI** — 0 FTDs with spend; do not open wide in the current form.

**Open / scale:**

- The only stable signal on the selected accounts is **FacebookMKTO1227** with an FTD on **Adnoc**.
- Recommendation from the run: **ios + HV + 25 + 1267 2 - golden** on **FacebookMKTO1227** — **scale** within the Adnoc offer while monitoring CPA.

---

## Methodology note

- **“Close”** = pause or sharply reduce budget on an account / offer with **0 FTDs** and meaningful spend, or clearly worse economics vs paired accounts.
- **“Open”** = combos from the **Recommendation** section in `_account_optimization_run.md` (Strong / Interesting classes) plus **TopAccount** / **TopCM** fields to tie them to an account.
- When the Excel file is updated, rerun the command above and reconcile the numbers.
