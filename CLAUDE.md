# League Import Tool (v2) — project memory

Turns a league's **Main Camera Sheet** (one row per club/team) into Salesforce records for
**big league deals**. **v2 pushes to Salesforce directly via the `sf` CLI** — only after a
dry-run preview and a typed `yes`. (v1 emitted CSVs for manual dataload; superseded.)

**Start here:** `RUNBOOK.md` (how to run, step by step) · `MAPPING.md` + `league_dataload/v2/mapping.py`
(every sheet column → SF field; single source of truth) · shared-brain ticket:
https://app.notion.com/p/3817ee4294a8816fb6e1d492fdd14a23

## ⛔ Guardrails (do NOT violate)
- **Never create the master league opportunity** — it's always an INPUT (Config tab / Step 1).
- **Live writes are gated:** `import` is **dry-run by default**; writing needs `--live` AND a typed `yes`.
  Show the full change list (field-level diffs) before any push. Per the dataload-approval rule,
  that confirm IS the approval.
- **No sandbox** — only prod org `spiideo`. Test field changes on ONE record at a non-Closed
  stage, then delete it. For a real batch, **canary one club live, verify, then the rest.**
- **Re-running is unsafe:** accounts upsert by Id and contacts dedup by email, but **Opps + OLIs
  ALWAYS create** → never run the same sheet `--live` twice (you'll double the opps/OLIs). If a
  run half-fails, trim the sheet to the unfinished clubs. The importer now does this check for
  you: a **preflight cross-check** (read-only, runs in dry-run too) warns when a target opp
  ALREADY has line items (double-load), a pinned Id is missing, the currency mismatches, the opp
  is already closed, **a contact's email is on a different account** (cf. Skara/Ida), or **a pinned
  account name is shared by a duplicate** (cf. the two "Skara FC"); and a **post-push verification**
  reads every touched opp back (stage/currency/Amount/OLIs vs plan). These checks are a FLOOR —
  still eyeball the data yourself (Shayan: don't just rely on the encoded rules).
- **Closed Won child opps fire the Younium sync = real orders.** Intended for real imports only.

## Two commands
```bash
# Phase A — generate the fillable order sheet (needs: pip install openpyxl)
python3 -m league_dataload.v2 gen --out outputs/WHL.xlsx --league "WHL"
#   then set the Config tab: Deal Structure (1/2/3), Master Opportunity Id, Currency,
#   Team Gender, pricing modes (list/discount/free per product type); rep fills green cols.

# Phase B — preview (dry-run) then import
python3 -m league_dataload.v2 import --sheet outputs/WHL.xlsx           # dry-run, writes nothing
python3 -m league_dataload.v2 import --sheet outputs/WHL.xlsx --live    # writes after typed 'yes'
```
Needs the `sf` CLI authed to `spiideo` (`sf org list` shows it). Live reads/writes need network →
run with the sandbox disabled.

## The 3 deal structures (Config tab picks one)
1. **Cameras + subscriptions per team** (common) → club account + child opp per team (Closed Won,
   `Master_Opportunity__c`=master), ship to each club.
2. **Cameras only per team** → club account + child opp per team, no subscription OLI.
3. **Cameras to the league** → no club accounts/opps; camera OLIs hang off the existing master opp.

**Attach mode** (struct 1/2; added 2026-06-23 for SvFF): if a sheet row carries an
**Opportunity SF ID**, the tool UPDATES that existing opp (→ Closed Won + Pricebook2Id +
Younium fields + currency forced to the run currency + Team Gender from the row) and attaches
OLIs, instead of creating a new opp. A **Customer SF ID** pins the account (beats the fuzzy matcher). A master opp is
optional, but a Config **Master Opportunity ID** (SvFF = `0067Q00000GIylWQAT` "Swedish Football
Federation") links each placeholder via `Master_Opportunity__c` — and in attach mode it is NOT
propagated onto the club account's `Level__c`. For the ~80 SvFF placeholder opps; note they
arrive named `<club> – SvFF Rollout 2026` / `… Expansion 2026` and SvFF wants them as
`<club> – SvFF Camera Order 2026` (renamed manually/live, not yet auto). See memory `svff-rollout-*`.
Config keys **Opportunity Owner / Opportunity Stage / Forecast Category** override the opp
owner + stage (SvFF = Amir Jakirlic, stage "Decision & Signature"/Commit so the rep closes it
himself — NOT Closed Won; the stage must be valid for the opp's RecordType sales process).
The loader accepts the opp Id as a bare Id **or** a Lightning URL (the sheet's `SF OPP LINK`
column) — it extracts the Id either way. A sheet **SvFF League Exchange** = yes writes
`Order_Notes__c` = "add to SvFF LE" on the opp.

## What it writes (mapping SIGNED OFF by Shayan + Egil; details in RUNBOOK/MAPPING)
Push order **Account → Contact → Opportunity → OpportunityLineItem** (staged create-and-capture).
- **Accounts:** new clubs created, matched updated (changed fields only). Address via
  `*CountryCode`/`*StateCode` (picklists on; map country→ISO); Tax → `Younium__Y_Tax_reg_Nr__c` (EU)
  / `Younium__Y_Org_Nr__c` (non-EU); `Org_Type_for_Calc__c`=CB; `Level__c` from the league;
  Invoice Delivery="Email". Contact-role lookups point at the created contact. **NEW accounts
  always need `Website` + `Domain__c`** — but the importer does NOT guess them from the email
  domain (often wrong: gmail, a municipality, a personal domain). It FLAGS every new account in
  the warnings ("flag only, no guessing", Shayan 2026-06-24); the domain is then web-searched
  (spin up agents) and set. Matched/existing accounts keep their own Website/Domain.
- **Contacts:** whole-CRM email dedup (same acct reuse / other acct prompt-to-reparent / else create).
- **Opps:** child per club, `Master_Opportunity__c`=master, **Closed Won / Forecast Closed**,
  CloseDate+Owner inherited from master, **RecordType Transactional**, `Pricebook2Id`=Younium-Spiideo AB.
- **OLIs:** subscription (struct 1) + one per camera; price from the pricing gate; `Sport__c` set
  (drives the dependent `Position_of_Field__c`); Scene + Position per camera. **Subscriptions bill
  Annually by default** (`Younium__Y_Billing_period__c` + `Younium__Y_Price_period__c` = Annual;
  bulk import else lands Monthly) — override per sheet via Config **Subscription billing period**;
  one-off hardware carries no billing period. Always create.

## Architecture
- `league_dataload/v2/` — the v2 tool: `gen_sheet`, `load_mcs`, `build_records`, `pricing`,
  `mapping`, `importer`, `picklist_deps`, `__main__` (CLI).
- `league_dataload/v2/gsheet_source.py` — read a filled Main Camera Sheet straight from a
  **Google Sheet (`import --gsheet <url|id>`)**. ⛔ **READ-ONLY, always**: authenticates with
  only `spreadsheets.readonly` + `drive.readonly` scopes (token can't write) and calls only read
  methods — the tool must NEVER edit a sheet (Shayan, 2026-06-24). Needs a service-account key
  (`--gsheet-creds` / `$GOOGLE_SHEETS_CREDENTIALS`) with Viewer access; Config comes from a local
  `--config <xlsx>` (a Google Sheet usually has no Config tab).
- `league_dataload/clubmatch/` — vendored clubsports club matcher (exact name beats
  exact-domain-to-a-different-name; proven on WHL 20/23 auto-matched, 3 new).
- `data/pricebook.csv` — **live multi-currency** (EUR/USD/GBP/SEK, Younium-Spiideo AB; refresh
  command in RUNBOOK). `pricebook_USD_snapshot.bak.csv` = old backup.
- `data/sport_position_deps.json` — **live SF field dependency** `Sport__c` →
  `Position_of_Field__c` (decoded from the describe's `validFor` bitmaps; refresh via
  `python3 -m league_dataload.v2 picklist-deps`). Source of truth for which Position values are
  valid per Sport. NOTE: the gen_sheet `POSITIONS` dropdown is still a flat hand-maintained list
  (no Sport→Position dependency yet) — this file is staged to drive dependent dropdowns later.
- `tests/` — `pytest -q` → 48/48 (offline).
- Older `league_dataload/` modules (matcher/normalize/emit/…) + `README.md` are the **legacy v1**
  CSV flow — superseded by v2; left for reference.

## After substantive changes
Mirror them to the Notion ticket (Current state / Next Step / Decisions log) — it's the
cross-person shared brain — and keep this file in sync.
