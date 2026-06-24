# League Deals DataLoad

> ⚠️ **LEGACY (v1).** This describes the original CSV-emitter flow. The tool is now **v2** — it
> reads a Main Camera Sheet and **pushes to Salesforce directly** (confirm-gated), no manual
> dataloading. **See `RUNBOOK.md` (how to run), `CLAUDE.md` (context + guardrails), `MAPPING.md`
> (fields).** This file is kept for reference only.

Turn a **big-league-deals sheet** into Salesforce **DataLoader-ready CSVs** for
importing league/conference/federation deals.

This is a focused, self-contained port of the league flow from the *Spiideo
DataLoad Engine* (`League Focus & Territories/engine`). It reuses the engine's
proven multi-pass crosscheck + emit logic verbatim, but is **dependency-free
(Python stdlib only)** and adds a pluggable lookup layer so it reads existing
Salesforce data from **either local CSV exports or the live `sf` CLI**.

It only ever **reads** lookup data and **writes CSVs** — it never writes to
Salesforce. You dataload the CSVs yourself (DataLoader / DemandTools) after
reviewing the crosscheck.

## What it produces

`build` writes four DataLoader CSVs whose column headers match the canonical
import templates **exactly** (`Import Template NA University/College (LATEST
WORKING)`), plus a crosscheck report:

Import in dependency order: **accounts → contacts → opportunities → opp products**.

| File | Grain | Purpose |
|---|---|---|
| `account_new.csv` | one row per **unmatched** league | **CREATE these first** — leagues with no existing SF account (blank Account ID). Org type = "League Organization", EUR. SF assigns new Account IDs on import. |
| `account_existing.csv` | one row per **matched/ambiguous** league | Reference only — already in SF (Account ID filled). Don't re-create. (Usually the bulk: you normally already know the league account.) |
| `contact.csv` | one row per league **with contact data** | Contact upsert (other half of the Account/Contact split). League forecasts carry no contacts, so this is **header-only** for the league flow — structure is there for when sales adds contacts via the MCS flow. |
| `opportunity.csv` | one row per **(league, period)** | Opportunity upsert. Stage `Discover Challenges`, Forecast Category `Pipeline`, Owner = rep's SF User Id. (Template columns + `Primary Contact`; no ARR column here.) |
| `opp_product.csv` | one row per **resolved product** per league | OpportunityLineItems. **This is where the ARR lands** (`Sales Price`). The rep's free-text Product cell is resolved to canonical pricebook entries and the league's total ARR is split across them by list price. |
| `league_crosscheck.csv` | one row per **unique league** | Match report vs. existing SF/HubSpot league accounts (analysis/QA, not a DataLoader file). **Open this first** — it tells you which leagues already exist (don't re-create) and which have existing deals. |

The combined Account/Contact template is **split** into `account_new` /
`account_existing` (you only create the net-new ones) plus `contact`. Because
the template Opportunity has no ARR column, league ARR flows to the Opp Product
`Sales Price`.

## Install / run

Zero third-party dependencies. Run straight from this folder:

```bash
# the main command — emits all three CSVs
python3 -m league_dataload build --input inputs/sample_deals.csv --out outputs/run/

# just the match report
python3 -m league_dataload crosscheck --input inputs/sample_deals.csv --out outputs/run/

# convenience wrapper (same thing)
python3 run.py build --input inputs/sample_deals.csv --out outputs/run/
```

Optionally install it as a `league-dataload` command: `pip install -e .`

## Lookup source (`--source`)

The crosscheck and rep→Owner resolution need your *existing* SF Accounts and
Users. Pick where they come from:

| `--source` | Reads existing SF Accounts/Users from | Notes |
|---|---|---|
| `csv` *(default)* | `data/sf_accounts.csv`, `data/users.csv` | Fully offline. Drop SF report / Xappex exports here. Either file may be absent. |
| `sf` | live `sf data query` against the org | `--target-org` (default `spiideo`). Needs the `sf` CLI authenticated; **network required** (run with the sandbox disabled). |
| `none` | nothing | Skips crosscheck + rep resolution — every league emits as net-new. |

**The curated HubSpot league pool (`data/hubspot_leagues.csv`) is always used**
as the primary candidate source regardless of `--source`. It already links
league names → SF Account IDs, so even `--source csv` with no `sf_accounts.csv`
gives a strong crosscheck. The `--source` choice governs the *Salesforce-side*
accounts (and the Users used for rep resolution).

### `sf` field names

The live org exposes "Org Type" as `Org_Type_for_Calc__c` (not `Org_Type__c`).
That's the default; override with `SF_ACCOUNT_ORGTYPE_FIELD` in `.env` if your
org differs (`sf sobject describe --sobject Account` to check).

## Input shape

A **GTM League DB**-style CSV: a header row containing `Rep Name` (a 3-line
title preamble before it is fine — it's auto-detected within the first 15
rows), then one row per rep per league per period. Recognised columns:

`Rep Name, Territory, Submitted Date, Period, Priority Rank, League, Sport,
Tier, GTM Motion, Product, Deal Type, ARR (€), Target Close, …, SF League /
Account ID, …`

Required per row: **Rep Name, League, Sport, Product, Deal Type, ARR**. ARR
parses `50,000` / `€50000` / `25000` / `12.500,00`. Period values like
`H2 2026` / `2027` slice opportunities. See `inputs/sample_deals.csv`.

If a rep already knows the SF Account ID, they can put it in the `SF League /
Account ID` column and the matcher trusts it.

## How matching works (ported from the engine)

Multi-pass, stdlib-only, deterministic: exact → normalised → alias → acronym →
core → parenthetical → containment → fuzzy-overlap, with a composite confidence
score, a **country signal** (territory→country boost/conflict), pass-exclusivity
(one SF account claimed once), and a HubSpot ∪ Salesforce candidate merge. See
`league_dataload/matcher.py` (verbatim from the engine) and `normalize.py` /
`territory.py`.

Match provenance shows in `Match Source`:
- `HubSpot + Salesforce` — found in both
- `Salesforce only` — SF account, not in the curated HubSpot list
- `HubSpot only (no SF Account ID)` — **duplicate risk**: exists in HubSpot but
  carries no SF Account ID, so a blind import would create a fresh SF account.

## `data/` — lookup + curation files

| File | What | Refresh |
|---|---|---|
| `hubspot_leagues.csv` | Curated HubSpot League/Federation export (name → SF Account ID, deal counts, ARR). Primary candidate pool. | Re-export from HubSpot, replace file. (HubSpot caps exports at 1000 rows.) |
| `hubspot_company_ids.csv` | Broader company export — back-fills HS Record IDs on SF-only matches. | Re-export, replace. |
| `sf_accounts.csv` | *(optional)* SF Accounts export for `--source csv`. Headers: `Id, Name, Org_Type__c, Sport__c, BillingCountry` (variants accepted). | Export / `sf data query`. |
| `users.csv` | *(optional)* SF Users for rep→Owner resolution. Headers: `Id, Name, IsActive`. | Export / `sf data query`. |
| `pricebook.csv` | Product catalogue for the Opp Product line items. Headers: `Price Book Name, Product: Product Name, Product ID, Price Book Entry ID, List Price Currency, List Price, Family`. **Shipped as a USD snapshot** — `Product ID`s are currency-independent, but `Price Book Entry ID`s are not, so refresh with a **EUR** export before a real EUR import. The tool warns when the pricebook currency ≠ the opp currency. | `sf data query` on PricebookEntry (EUR) / Sheet export. |
| `league_deal_aliases.csv` | Manual league→partner/federation account aliases (`league, company_name, company_record_id, reason`). `#` rows are comments. | Hand-maintained. |
| `manual_account_ids.csv` | Canonical hand-curated league→SF-Account-ID overrides (`league, sf_account_id, hs_record_id, note`). Trusted as exact matches. Edit **here**, never the crosscheck output. | Hand-maintained. |

Rep-name → SF Full Name aliases: set `REP_NAME_ALIASES="Freddy=Federico Caini, Brana=Branislav Lazic"` in `.env`.

## Layout

```
league_deals_dataload/
├── data/            # lookup + curation CSVs (above)
├── inputs/          # drop the deals CSV here  (sample_deals.csv provided)
├── outputs/         # generated CSVs land here
├── tests/           # pytest smoke tests
├── run.py           # convenience wrapper → python3 run.py <cmd>
└── league_dataload/
    ├── cli.py             # argparse CLI (build / crosscheck)
    ├── config.py          # paths, defaults, tiny .env loader
    ├── load_forecast.py   # parse the deals CSV → ForecastRow      (ported)
    ├── normalize.py       # league name normalisation              (ported)
    ├── territory.py       # territory → country signal             (ported)
    ├── matcher.py         # multi-pass crosscheck + dedupe          (ported)
    ├── schema.py          # dataclasses (+ Product)                 (ported)
    ├── load_hubspot.py    # HubSpot candidate loaders               (ported)
    ├── load_pricebook.py  # pricebook.csv → PricebookIndex          (ported)
    ├── classify_product.py# product family classification          (ported)
    ├── product_resolver.py# free-text Product cell → pricebook      (ported)
    ├── loaders.py         # alias / manual-match loaders
    ├── candidates.py      # gather HubSpot ∪ SF candidate pool
    ├── resolve_reps.py    # rep name → SF User Id
    ├── emit.py            # account / contact / opportunity / crosscheck
    ├── emit_opp_product.py# opp_product (OLI) — ARR → Sales Price
    └── sources/           # the lookup layer
        ├── base.py           # LookupSource protocol + header mapping + stub
        ├── csv_source.py     # local CSV backend
        └── sf_cli_source.py  # live `sf` CLI backend
```

## Tests

```bash
python3 -m pytest tests/ -q      # or: python3 tests/test_smoke.py
```

## Relationship to the engine

The matcher / normalize / territory / schema / load_forecast / load_hubspot /
load_pricebook / classify_product / product_resolver modules are **verbatim
copies** of the engine (`engine/spiideo_dataload/...`) so matching and product
resolution behave identically. The emit layer differs intentionally: the
combined Account/Contact sheet is split into `account.csv` + `contact.csv`, the
Opportunity matches the 23-col template (no `Deal Qualification ARR` /
`Primary Contact`), and league ARR therefore rides on the Opp Product
`Sales Price`. Still out of scope (live in the engine): Word-doc/Sheet
consolidation, GTM-DB push, the deal-dedup punch-lists (`deals_to_review` /
`deals_in_forecast_periods`), and the `gtm_summary` rollups.
