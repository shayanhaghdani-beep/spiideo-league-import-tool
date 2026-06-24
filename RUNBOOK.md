# League Import Tool — Runbook (v2)

Turn a league's **Main Camera Sheet** into Salesforce records (Accounts, Contacts,
child Opportunities, camera/subscription line items) with one preview + one confirm.
Another Claude with this repo + `sf` CLI authed to org `spiideo` can run this cold.

## Prereqs
- Python 3.10+, `pip install openpyxl` (Phase A only).
- Salesforce CLI (`sf`) authenticated to org `spiideo` (`sf org list` shows it Connected).
- `data/pricebook.csv` is a live multi-currency export (refresh if stale — see bottom).

## Phase A — generate the order sheet
```
python3 -m league_dataload.v2 gen --out outputs/<LEAGUE>.xlsx --league "<LEAGUE>"
```
Then open the file and set the **Config** tab (this drives the import):
- **Deal Structure** — 1 cameras+subs / 2 cameras only / 3 cameras to league (no club opps)
- **Master Opportunity ID** — the existing league master opp (NEVER created by the tool)
- **Currency** — EUR / USD / GBP / SEK
- **Team Gender (competition)** — Mens / Womens / Mens and Womens
- **Subscription pricing** / **Cameras pricing** — list / discount / free
  - if `discount`, enter the price(s) in the Config tab

Send the file to the rep/CSM to fill the **green** columns (one row per club). Dropdowns
cover subscription, camera type, position, sport, gender. Billing address = shipping.

## Phase B — preview, then import
**Always dry-run first** (writes nothing):
```
python3 -m league_dataload.v2 import --sheet outputs/<LEAGUE>.xlsx
```
This fetches SF accounts live, matches clubs (exact name / email-domain; exact name wins
over a parent-domain match), and prints the full change list: creates + existing-account
updates (field-level diffs) + any ⚠️ review items.

When the change list looks right, run live:
```
python3 -m league_dataload.v2 import --sheet outputs/<LEAGUE>.xlsx --live
```
You'll see the change list, then a `type 'yes' to proceed` gate. On confirm it pushes in
order **Account → Contact → Opportunity → OpportunityLineItem**, threading new Ids into
children, then a second pass sets the account contact-role lookups.

### What it writes (signed off by Shayan + Egil 2026-06-16)
- **Accounts:** new clubs created; matched clubs updated (changed fields only). Address via
  `*CountryCode`/`*StateCode` (picklists on); Tax → `Younium__Y_Tax_reg_Nr__c` (EU) or
  `Younium__Y_Org_Nr__c` (non-EU); `Org_Type_for_Calc__c=CB`; `Level__c` from the league.
- **Contacts:** whole-CRM email dedup — same account reuse / other account prompt-to-reparent
  / else create. Account contact-role lookups point at the contact.
- **Opportunities:** child per club, `Master_Opportunity__c` = the master; **Closed Won /
  Forecast Closed**; Close Date + Owner inherited from the master; **RecordType Transactional**;
  `Pricebook2Id` = Younium-Spiideo AB.
- **OLIs:** subscription (structure 1) + one per camera; price from the pricing gate;
  `Sport__c` set (controls the dependent `Position_of_Field__c`); Scene + Position per camera.

## Safety rails
- Dry-run is the default; live needs `--live` AND a typed `yes`.
- The master league opp is an INPUT, never created.
- Installation/shipping status is NOT tracked here — that lives in the CSM installation
  tracker (decoupled 2026-06-16). The order sheet is order-generation only.

## Known gotchas
- **Salesforce duplicate rules** can block a Contact/Account create on fuzzy name/email.
  The email-dedup handles exact-email; a fuzzy block surfaces as an error to resolve.
- **Closed Won opps trigger the Younium sync** (real orders). That is intended for real
  imports; never use a real run as a throwaway test. There is no sandbox — test field
  changes on a single record at a non-Closed stage and delete it.

## Refresh the pricebook (multi-currency)
```
sf data query --query "SELECT Pricebook2.Name, Product2.Name, Product2.Family, Product2Id, \
  Id, CurrencyIsoCode, UnitPrice FROM PricebookEntry \
  WHERE Pricebook2Id='01sQD000005FASPYA4' AND IsActive=true" --target-org spiideo -r csv
```
Map columns → `data/pricebook.csv` headers: Price Book Name, Product: Product Name,
Product ID, Price Book Entry ID, List Price Currency, List Price, Family.

## Refresh the Sport → Position dependency map
```
python3 -m league_dataload.v2 picklist-deps   # → data/sport_position_deps.json
```
Decodes the SF field dependency (`OpportunityLineItem.Sport__c` → `Position_of_Field__c`)
from the live describe's `validFor` bitmaps. The source of truth for which Position values
are valid for each Sport — re-run when the org's picklists change. (Default pair shown;
override with `--object/--controller/--dependent` for any other dependent picklist.)

## Map / field reference
`MAPPING.md` (every sheet column → SF field) and `league_dataload/v2/mapping.py` (the single
source of truth for API names — edit there if SF changes).
