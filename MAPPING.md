# League Import Tool — Sheet → Salesforce field mapping

Draft generated 2026-06-16 from a live `sf sobject describe` against org `spiideo`.
**Shayan: please review every ⚠️ row — these are the property calls you own.**

Import runs in dependency order: **Account → Contact → Opportunity → OpportunityLineItem**.
Staged create-and-capture: each parent's new Id is threaded into its children.

---

## 0. Config tab (baked into the sheet at generation time — Phase A prompts)
| Config field | Used for | Notes |
|---|---|---|
| Deal Structure | 1 = cameras+subs / 2 = cameras-only / 3 = cameras-to-league-no-opps | drives which objects get created |
| Master Opportunity ID | `Opportunity.Master_Opportunity__c` on every child opp | ALWAYS an input, never created |
| Pricing mode (per product type) | subscription: free/discount/list · cameras: free/discount/list | discount → editable price column in sheet |
| Currency | `CurrencyIsoCode` on Account + Opp; picks the per-currency PricebookEntry | EUR/USD/GBP/SEK |

---

## 1. ACCOUNT  (created if new club, upserted if matched on Id)
Upsert key = matched SF Account Id (from crosscheck / `Customer SF ID`). Match → update changed fields only.

| Sheet column | SF Account field | API name | Notes |
|---|---|---|---|
| Team Name | Account Name | `Name` | |
| (matched Id) | Account ID | `Id` | upsert key; blank = create |
| Billing Country | Billing Country | `BillingCountry` | |
| Billing Street | Billing Street | `BillingStreet` | |
| Billing Postal Code/Zip Code | Billing Zip/Postal | `BillingPostalCode` | |
| Billing Province/State | Billing State | `BillingState` | |
| Billing City | Billing City | `BillingCity` | |
| (= billing, per sheet note) | Shipping * | `ShippingCountry/Street/PostalCode/State/City` | copy billing → shipping |
| Tax ID | Tax reg Nr | `Younium__Y_Tax_reg_Nr__c` | ⚠️ confirm this vs another tax field |
| Sport | Sport | `Sport__c` | ⚠️ confirm vs `Targeted_Sport_s__c` (multipicklist) |
| Org type | Org Type | `Org_Type_for_Calc__c` | ⚠️ writeable? value for a club = `CB`? confirm write target |
| Level | Level | `Level__c` | ⚠️ needed for clubs? value source? |
| (config) | Account Currency | `CurrencyIsoCode` | |
| (default) | Invoice Delivery Method | `Younium__Invoice_Delivery_Method__c` | ⚠️ default value? |
| (default) | Payment terms | `Younium__Payment_terms__c` | ⚠️ default value? |
| (league, struct-dependent) | League/Conference | `Competition__c` | ⚠️ attach club → league here? or `Mens_League__c`? |

Contact-role lookups (`Invoice_Contact__c`, `Shipping_Contact__c`, `Installation_Responsible__c`, `IT_Responsible__c`) — ⚠️ set to the created contact, or leave blank?

---

## 2. CONTACT  (whole-CRM email dedup: reuse if on same acct / prompt-reparent if elsewhere / else create)
| Sheet column | SF Contact field | API name | Notes |
|---|---|---|---|
| Contact Name | First / Last Name | `FirstName` / `LastName` | split on first space |
| Contact Email | Email | `Email` | dedup key (whole CRM) |
| Contact Number | Business Phone | `Phone` | |
| (parent) | Account ID | `AccountId` | staged from Account create |

---

## 3. OPPORTUNITY  (child opp per club — always CREATE; skipped entirely for Structure 3)
| Sheet/derived | SF Opportunity field | API name | Notes |
|---|---|---|---|
| derived (club + period) | Name | `Name` | |
| (parent) | Account ID | `AccountId` | staged club Account Id |
| Master Opportunity ID (config) | Master Opportunity | `Master_Opportunity__c` | from config tab |
| (created contact) | Primary Contact | `Primary_Contact__c` | |
| rep | Owner ID | `OwnerId` | ⚠️ owner source — inherit from master opp? |
| (default/derived) | Close Date | `CloseDate` | ⚠️ default? master opp's? |
| (default) | Stage | `StageName` | ⚠️ default stage? |
| (default) | Forecast Category | `ForecastCategoryName` | ⚠️ default? |
| Effective Start Date | Effective start date | `Younium__Y_Effective_start_date__c` | |
| derived | Spiideo Account Name | `Spiideo_Account_Name__c` | |
| (config) | Opportunity Currency | `CurrencyIsoCode` | |
| (motion logic) | Record Type ID | `RecordTypeId` | ⚠️ ENT vs Transactional (€50k split)? |
| (default) | Opportunity Type | `Type` | ⚠️ New? |
| (default) | Order type | `Younium__Order_type__c` | ⚠️ default? |
| Sport | Sport | `Sport_c__c` | ⚠️ confirm (note: `Sports__c` is "Sport_Delete") |
| Team Gender | Team Gender | `Team_Gender_c__c` | |
| (parent acct) | Invoice Account | `Younium__invoice_account__c` | |
| (default) | Billing period | `Younium__Y_Billing_period__c` | ⚠️ default? |
| (default) | Notice Period (Months) | `Younium__Notice_Period_Months__c` | ⚠️ default? |
| Order Name | ? | ? | ⚠️ where does sheet "Order Name" land? |
| Camera Shipping Schedule | only `Camera_Shipping_ScheduleDEL__c` (OLD) | — | ⚠️ current field? |
| Shipment Status | (no field found) | — | ⚠️ which field? |

---

## 4. OPPORTUNITYLINEITEM  (always CREATE: 1 subscription OLI [struct 1] + 1 per camera)
Camera placement lives on the OLI, not the Opp.

| Sheet column | SF OLI field | API name | Notes |
|---|---|---|---|
| (parent) | Opportunity | `OpportunityId` | staged opp Id (Structure 3 = master opp) |
| Spiideo Subscription / Camera N Type | Product ID | `Product2Id` | resolved via pricebook by product name |
| (resolved) | Price Book Entry ID | `PricebookEntryId` | currency-specific (from config currency) |
| (default 1) | Quantity | `Quantity` | 1 per camera; subscription qty |
| pricing gate | Sales Price | `UnitPrice` | free=0 / discount=entered / list=pricebook |
| (optional) | Discount | `Discount` | ⚠️ use % discount instead of UnitPrice override? |
| Camera N Scene | Camera Scene | `Camera_Scene__c` | per-camera |
| Camera N Position of Field | Position of Field | `Position_of_Field__c` | per-camera (picklist) |
| Camera N Installation/Calibration Status | ? | `Camera_order_type__c`? | ⚠️ confirm target field |
| derived (scene/position) | Line Description | `Description` | optional |

---

## Open ⚠️ items for Shayan (the property-mapping calls)
1. **Tax ID** target field (`Younium__Y_Tax_reg_Nr__c` vs other).
2. **Org Type** write target + club value (`Org_Type_for_Calc__c` may be calc-only).
3. **Account Sport / Level** — `Sport__c` vs `Targeted_Sport_s__c`; is Level needed for clubs?
4. **League attach** — does the club Account link to the league via `Competition__c` / `Mens_League__c`? Structure-dependent.
5. **Account defaults** — Invoice Delivery Method, Payment terms values.
6. **Account contact-role lookups** — populate from created contact or leave blank?
7. **Opp defaults** — Owner, Close Date, Stage, Forecast Category, Opportunity Type, Order type, Billing period, Notice Period.
8. **Opp Record Type** — ENT vs Transactional logic (ties to the €50k pipeline split).
9. **Opp Sport field** — confirm `Sport_c__c`.
10. **"Order Name"** sheet column → which Opp field?
11. **Camera Shipping Schedule + Shipment Status** — current (non-DEL) field API names.
12. **OLI** — % `Discount` vs `UnitPrice` override; **Installation/Calibration Status** target field.
