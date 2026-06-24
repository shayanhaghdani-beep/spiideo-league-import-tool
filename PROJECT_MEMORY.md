# League Import Tool — Project Memory / Decisions Log

> Running decisions + context for `league_deals_dataload/`, mirrored from the Claude session
> memory note `project_league_deals_dataload`. **Operational docs:** `CLAUDE.md` (auto-load
> guardrails), `RUNBOOK.md` (how to run), `MAPPING.md` (Sheet→Salesforce field map). Keep this in
> sync with the session memory; if they diverge, the session memory is canonical.
>
> **Shared-brain ticket:** Notion "Operations backlog" → "League Import Tool"
> (https://app.notion.com/p/3817ee4294a8816fb6e1d492fdd14a23, owner Shayan+Egil, Status "Rolling
> out"). Mirror substantive changes ticket↔memory. Decisions log synced through **2026-06-22**.

## What it is
Stdlib-only Python CLI (its own top-level project) that imports **big league deals** into Salesforce
in two phases (`python3 -m league_dataload.v2`):
- **Phase A — `gen`:** `gen --out outputs/<L>.xlsx --league "<L>"` → a fill-in xlsx for sales
  (instruction preamble, green input cols, ~17 dropdowns, product link) + a **Config tab** (deal
  structure 1/2/3, master league opp Id, currency, team gender, pricing modes, + opt-in toggles).
- **Phase B — `import`:** `import --sheet outputs/<L>.xlsx` (DRY-RUN default; `--live` writes after a
  typed `yes`). Reads sheet → club crosscheck → build Account/Contact/Opp/OLI → change list + confirm
  gate → push **Account→Contact→Opportunity→OpportunityLineItem** (staged create-and-capture).

## Locked decisions / behaviour (mapping SIGNED OFF by Shayan+Egil)
- **Upsert keys:** Accounts = matched SF Id (update changed fields only); Contacts = whole-CRM **email
  dedup** (same acct→reuse; no acct→adopt; other acct→flag/reparent; else create); **Opps + OLIs ALWAYS
  create** (no match check) — so never run the same sheet `--live` twice.
- **Child opps = Closed Won** signed deals for fulfilment. **Younium sync is MANUAL** — Closed Won does
  NOT auto-fire a Younium order; someone triggers it later. `RecordTypeId`=Transactional (master stays
  Enterprise); CloseDate inherited from master; ForecastCategory=Closed.
- **NEVER creates the master league opp** — always a Step-1 input (`Opportunity.Master_Opportunity__c`).
- **Owner structure (always prompted):** Config "Opportunity Owner (CSM name or User Id)" → child opps
  owned by that CSM (blank = inherit master owner). **Contacts owned by the master opp owner, never the
  running user** (only on CREATE; reused/adopted contacts keep their owner).
- **Opp Name = "{Team Name} {Sport}"** (e.g. "Brandon Wheat Kings Ice Hockey").
- **Addresses:** sheet is the source of truth for the FULL address; on an existing/matched account
  overwrite BOTH billing AND shipping (diff = changed only). Shipping uses the **state CODE** (AB/WA).
  Tax ID conditional: EU billing → `Younium__Y_Tax_reg_Nr__c`; non-EU → `Younium__Y_Org_Nr__c`.
- **Account defaults:** Invoice Delivery="Email", Payment terms "30"; Sport/Org type/Level set;
  league attach `Competition__c`; contact-role lookups populated with the created contact;
  `Invoice_Phone__c`/`Shipping_Phone__c` = contact phone.
- **Club matching** = vendored clubsports matcher: exact name/domain auto-attach, **exact name beats
  exact-domain-to-a-different-name**; rest → review. (WHL: 20/23 auto, 3 new.)
- **Voucher setup:** TWO subscription lines — +1 and −1 (`Voucher__c`), both at LIST price (net $0,
  real value shows).
- **Younium OLI fields (bulk import bypasses Younium → set on every OLI):** `Younium__Y_Charge_type__c`
  = **One-off** for hardware (cameras + encoder), **Recurring** for subscriptions (drives GP — one-off
  cost roll-up only counts One-off lines); `Younium__Y_Younium_Charge_name__c` = product name (drives
  Subscription Type); `Younium__List_price__c` = pricebook list (note: our pricebook list == standard
  SF ListPrice; true Younium catalog list must be loaded into the pricebook if it differs).
- **`Younium__Y_ID_unique_OLI_product__c`** = per-ORDER sequence "1","2","3"… unique within each opp,
  restarting per opp (assigned in a post-pass grouped by parent opp; master-parented lines left blank →
  Younium assigns).
- **Camera Order Type** default "Shipment - No additional cost" (league pays). **Event Source** default
  "Not applicable". **Effective start date** from the sheet, re-applied in a post-create SECOND PASS
  (an SF automation forces effective=CloseDate on INSERT only).
- **Camera Alternate Shipping is NOT auto-stamped** — set manually only for off-site shipping (cameras
  that ship somewhere other than the club's own account; contact info goes in the textarea since OLIs
  have no contact field).
- **Master-opp shipping lines = OPT-IN** (Config "Master-opp shipping lines (per camera × region)?",
  default no; reps usually add shipping themselves). When on: one line per (camera type × destination
  region) on the master, qty = cameras going there. **US ships WITH TARIFFS** (Product "Shipping
  including tariffs"); Canada/non-US uses Product "Shipping". Sales Price $0 (not billed); **cost
  auto-derives** from `Product2.Cost_USD__c` → GP (charge type One-off).

## SF gotchas (learned live; NO sandbox — only prod `spiideo`)
- State/Country picklists ON → use `BillingStateCode`/`BillingCountryCode` + name→ISO map.
- Opp needs `Pricebook2Id` (Younium-Spiideo AB) or OLIs won't attach; camera OLI needs `Sport__c`
  (controls the dependent `Position_of_Field__c` picklist, which doesn't "take" on insert → second pass).
- Contact dup rules: bypass at create via REST `Sforce-Duplicate-Rule-Header: allowSave` (we dedup on
  email ourselves). Importer never crashes on dup rules (reuse exact / skip+flag / continue + report).
- `OLI.OpportunityId`/`Product2Id`/`PricebookEntryId` are NOT updateable → moving a line / swapping its
  product = delete + recreate (which MUST re-carry the Younium fields).
- Bulk-update empty cells do NOT null a field (Bulk API 2.0 ignores blanks) → clear via REST PATCH null.
- **Scope batch checks by MASTER OPPORTUNITY, not product family** (a club with cameras on another opp
  slips past a product-family filter).

## Status — WHL (first real batch, imported 2026-06-18, revised 2026-06-22) ✅
23 child opps under master `006QD00000eF92MYAS` (the separate "WHL League Office" opp was deleted). All
Closed Won/Transactional, Primary Contact set, $0 net. Each: 2 sub lines (LITE TEAM @ $2,783, +1/−1
voucher), cameras + encoder, order type "Shipment - No additional cost", eff start 2026-06-01, event
source "Not applicable", System Administrator = the contact, account phones set. Master opp carries 8
shipping lines ($0 sales, $20,830 cost into GP; US tariffs vs CA non-US). Calgary holds its own 7
cameras (alt-ship label → WHL League Office / Noah Rousseau) + encoder + subs. **Younium NOT yet
synced** (manual — Shayan triggers). Owners: opps → Mike Djerroud (CSM), contacts → David Cook (master
owner). Tests: 29/29.

## In progress
- **Attach mode for PRE-EXISTING opps** (e.g. SvFF, where "rollout" accounts/opps already exist):
  `_existing_opp_update_fields()` in `build_records.py` writes a minimal field set onto an existing opp
  (closes it + sets Younium fields + pricebook) instead of creating a new one. Crosscheck the system
  first to find the existing SvFF accounts/opps before importing.

## Ported foundation (don't rebuild)
League matcher/normalize/territory/schema/load_forecast/load_hubspot + pricebook/classify/
product_resolver from the clubsports engine; clubsports matcher vendored into `clubmatch/`.
