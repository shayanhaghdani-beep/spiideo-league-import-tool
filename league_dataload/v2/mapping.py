"""Centralised Sheet -> Salesforce field-mapping contract (v2 / MCS flow).

THIS IS THE ONE PLACE TO EDIT once Shayan confirms the property calls.
Every value flagged ``# >>SHAYAN`` is an assumption from the draft MAPPING.md
(2026-06-16) and may change after his review. Nothing else in the v2 code
hard-codes an API name -- it all reads from here.

Import dependency order: ACCOUNT -> CONTACT -> OPPORTUNITY -> OPP_LINE_ITEM.
"""
from __future__ import annotations

import re

# --- Object API names ------------------------------------------------------
SOBJECT = {
    "account": "Account",
    "contact": "Contact",
    "opportunity": "Opportunity",
    "oli": "OpportunityLineItem",
}

# Order in which records must be created so child lookups resolve.
IMPORT_ORDER = ["account", "contact", "opportunity", "oli"]

# The active multi-currency price book OLIs + opps must reference (Younium-Spiideo AB).
YOUNIUM_PRICEBOOK2_ID = "01sQD000005FASPYA4"
# Known Opportunity RecordType Ids (resolve live by DeveloperName where possible).
OPP_RECORD_TYPE_IDS = {"Transactional": "012QD000003gtftYAA", "Enterprise": "012QD000003gthVYAQ"}


# --- ACCOUNT ---------------------------------------------------------------
# logical key -> SF API field name. Address fields are standard.
ACCOUNT_FIELDS = {
    "name": "Name",
    "id": "Id",                                  # upsert key when matched
    # NEW accounts always NEED a Website + Domain, but the tool does NOT guess them from
    # the email domain (often wrong: gmail / municipality / personal). build_plan flags
    # every new account instead; the domain is web-searched (agents) and set into these
    # two URL fields (Shayan, 2026-06-24, "flag only, no guessing").
    "website": "Website",
    "domain": "Domain__c",
    # State/Country picklists are ENABLED in the org -> use the *Code fields.
    "billing_country_code": "BillingCountryCode",
    "billing_state_code": "BillingStateCode",
    "billing_street": "BillingStreet",
    "billing_postal": "BillingPostalCode",
    "billing_city": "BillingCity",
    "shipping_country_code": "ShippingCountryCode",
    "shipping_state_code": "ShippingStateCode",
    "shipping_street": "ShippingStreet",
    "shipping_postal": "ShippingPostalCode",
    "shipping_city": "ShippingCity",
    "currency": "CurrencyIsoCode",
    # Tax ID is conditional on EU vs non-EU billing country (Shayan, confirmed):
    "tax_id_eu": "Younium__Y_Tax_reg_Nr__c",
    "tax_id_noneu": "Younium__Y_Org_Nr__c",
    "sport": "Sport__c",                         # confirmed
    "org_type": "Org_Type_for_Calc__c",          # confirmed writeable
    "level": "Level__c",                         # confirmed; value sourced from the league
    "invoice_delivery_method": "Younium__Invoice_Delivery_Method__c",
    "payment_terms": "Younium__Payment_terms__c",
    "league_lookup": "Competition__c",           # confirmed
    # contact-role lookups -- confirmed: populate all with the created contact:
    "invoice_contact": "Invoice_Contact__c",
    "shipping_contact": "Shipping_Contact__c",
    "installation_responsible": "Installation_Responsible__c",
    "it_responsible": "IT_Responsible__c",
    # Invoice/Shipping PHONE live on the Account (the Opportunity's phone fields are
    # read-only formulas). = the invoice/shipping contact's phone (Shayan, 2026-06-22).
    "invoice_phone": "Invoice_Phone__c",
    "shipping_phone": "Shipping_Phone__c",
}

CONTACT_ROLE_LOOKUPS = (
    "invoice_contact", "shipping_contact",
    "installation_responsible", "it_responsible",
)

# EU member-state billing countries (drives the conditional Tax ID field).
EU_COUNTRIES = {
    "austria", "belgium", "bulgaria", "croatia", "cyprus", "czechia",
    "czech republic", "denmark", "estonia", "finland", "france", "germany",
    "greece", "hungary", "ireland", "italy", "latvia", "lithuania",
    "luxembourg", "malta", "netherlands", "poland", "portugal", "romania",
    "slovakia", "slovenia", "spain", "sweden",
}

ACCOUNT_DEFAULTS = {
    "org_type_value": "CB",                       # club picklist value
    "invoice_delivery_method": "Email",           # Egil, confirmed
    "payment_terms": "30",                        # SF auto-default; set for clarity
    "copy_billing_to_shipping": True,             # sheet says billing == shipping
    "populate_contact_role_lookups": True,        # Shayan, confirmed
}


# Country name -> ISO-3166 alpha-2 (for the *CountryCode picklist fields).
COUNTRY_TO_CODE = {
    "canada": "CA", "usa": "US", "united states": "US",
    "united states of america": "US", "us": "US", "ca": "CA",
    "united kingdom": "GB", "uk": "GB", "great britain": "GB",
    "sweden": "SE", "germany": "DE", "france": "FR", "spain": "ES",
    "italy": "IT", "netherlands": "NL", "belgium": "BE", "ireland": "IE",
    "norway": "NO", "denmark": "DK", "finland": "FI", "austria": "AT",
    "switzerland": "CH", "australia": "AU", "mexico": "MX",
}


def country_code(country: str) -> str:
    c = (country or "").strip()
    return COUNTRY_TO_CODE.get(c.lower(), c)


def is_eu_country(country: str) -> bool:
    return (country or "").strip().lower() in EU_COUNTRIES


def tax_field_for(country: str) -> str:
    return ACCOUNT_FIELDS["tax_id_eu"] if is_eu_country(country) else ACCOUNT_FIELDS["tax_id_noneu"]


# Team_Gender_c__c picklist values (live SF): Mens / Womens / Mens and Womens.
TEAM_GENDER_VALUES = ("Mens", "Womens", "Mens and Womens")


def team_gender_value(raw: str) -> str:
    """Normalise a free-text sheet gender to the SF Team_Gender_c__c picklist.
    Handles "Men's", "Women's", "Men's/Women's", "Men's Women's", "M/W", etc.
    Returns '' for blank/unrecognised so the caller can fall back to the Config
    value. Mixed (both men and women mentioned) -> "Mens and Womens". A sheet with
    one gender per team/sport row (e.g. college conferences) needs this per row."""
    s = (raw or "").lower()
    for ch in ("'", "/", "&", "-", "+", ",", "."):
        s = s.replace(ch, " ")
    s = " ".join(s.split())
    if not s:
        return ""
    has_women = ("women" in s) or ("womens" in s) or ("girls" in s) or ("female" in s) \
        or (" w " in f" {s} ")
    s_men = s.replace("womens", "").replace("women", "")  # "women" contains "men"
    has_men = ("men" in s_men) or ("boys" in s_men) or ("male" in s_men) \
        or (" m " in f" {s_men} ")
    if has_men and has_women:
        return "Mens and Womens"
    if has_women:
        return "Womens"
    if has_men:
        return "Mens"
    return ""


# --- SHIPPING (master-opp shipping lines; OPT-IN feature, Shayan 2026-06-22) ----
# The league MASTER opp can carry one shipping line per (camera type x destination
# region), qty = how many of that camera ship to that region. Two product names tell
# the regions apart (the rest is in Younium__Y_Younium_Charge_plan_name__c):
#   "Shipping"                   -> plan "...to <region> of <camera>"  (no tariff)
#   "Shipping including tariffs"  -> plan "...with Tariffs to US for <camera>"  (US)
# Region comes from each club's billing country. Sales Price = $0 (not billed); the
# cost auto-derives from Product2.Cost_USD__c into GP (charge type One-off).
SHIPPING_PRODUCT_NAMES = ("Shipping", "Shipping including tariffs")
_EUROPE_NON_SE = {"united kingdom", "uk", "great britain", "norway", "switzerland",
                  "iceland", "liechtenstein"}


def shipping_region(country: str) -> str:
    """Billing country -> Younium shipping-region key: US (with tariffs) / Sweden /
    Europe / Americas-nonUS (Canada, rest of Americas, APAC, ME, AF)."""
    c = (country or "").strip().lower()
    if c in ("us", "usa", "united states", "united states of america"):
        return "US"
    if c == "sweden":
        return "Sweden"
    if is_eu_country(country) or c in _EUROPE_NON_SE:
        return "Europe"
    return "Americas-nonUS"


def shipping_camera_key(name: str) -> str:
    """Normalise a camera product name so the sheet's camera matches a shipping
    charge-plan's camera. Plans drop the '(with mic)' suffix and call the encoder
    'Encoder'."""
    n = (name or "").strip()
    if n.lower() == "spiideo stream encoder":
        return "encoder"
    n = re.sub(r"\s*\(with mic\)\s*$", "", n, flags=re.IGNORECASE)
    return n.strip().lower()


def shipping_region_from_plan(plan: str) -> str:
    """Parse a shipping charge-plan name -> region key ('' if not a shipping plan)."""
    p = plan or ""
    if "Tariffs to US" in p:
        return "US"
    if "to Sweden" in p:
        return "Sweden"
    if "to Europe" in p:
        return "Europe"
    if "to Americas" in p:
        return "Americas-nonUS"
    return ""


def shipping_camera_from_plan(plan: str) -> str:
    """Parse the camera out of a shipping charge-plan name. US-tariff plans read
    '... for <camera>'; the others read '... of <camera>'."""
    p = plan or ""
    for sep in (" for ", " of "):
        i = p.rfind(sep)
        if i != -1:
            return shipping_camera_key(p[i + len(sep):])
    return ""


# --- CONTACT ---------------------------------------------------------------
CONTACT_FIELDS = {
    "first_name": "FirstName",
    "last_name": "LastName",
    "email": "Email",                             # whole-CRM dedup key
    "phone": "Phone",
    "account_id": "AccountId",
    "owner_id": "OwnerId",                        # = MASTER opp owner (Shayan, 2026-06-19):
                                                  # the running user must NOT own contacts
}


# --- OPPORTUNITY -----------------------------------------------------------
OPPORTUNITY_FIELDS = {
    "name": "Name",
    "account_id": "AccountId",
    "master_opportunity": "Master_Opportunity__c",
    "primary_contact": "Primary_Contact__c",
    "owner_id": "OwnerId",                         # = the CSM from the "Opportunity Owner"
                                                   # Config prompt (Shayan, 2026-06-19);
                                                   # falls back to master opp owner if unset
    "close_date": "CloseDate",
    "stage": "StageName",
    "forecast_category": "ForecastCategoryName",
    "effective_start_date": "Younium__Y_Effective_start_date__c",
    "spiideo_account_name": "Spiideo_Account_Name__c",
    "currency": "CurrencyIsoCode",
    "pricebook2_id": "Pricebook2Id",              # required so OLIs can attach
    "record_type_id": "RecordTypeId",             # Transactional (per-club child opps)
    "opp_type": "Type",                           # New
    "order_type": "Younium__Order_type__c",       # New order
    "sport": "Sport_c__c",                        # = account Sport__c
    "team_gender": "Team_Gender_c__c",            # competition-level, Step-1 prompt
    "invoice_account": "Younium__invoice_account__c",
    "system_admin": "System_Administrator__c",    # Contact lookup on the OPP = the line's contact (Shayan, 2026-06-22)
    "event_source": "Event_Source__c",            # default "Not applicable" (Shayan, 2026-06-22)
    "order_notes": "Order_Notes__c",              # "SvFF League Exchange"=yes -> "add to SvFF LE"
    # Billing period auto-defaults to "Annual" in SF; Notice period optional/blank.
    # Camera Shipping Schedule / Shipment Status = sheet-internal or SF-auto (not written).
}

OPPORTUNITY_DEFAULTS = {
    "stage": "Closed Won",                        # confirmed: signed deals for fulfilment
    "forecast_category": "Closed",                # confirmed
    "opp_type": "New",                            # confirmed
    "order_type": "New order",                    # confirmed
    "record_type_developer_name": "Transactional",  # confirmed (per-club child opps)
    "event_source": "Not applicable",             # default unless told otherwise (Shayan, 2026-06-22)
    "svff_le_note": "add to SvFF LE",             # Order Notes when "SvFF League Exchange"=yes (Shayan, 2026-06-24)
    # close_date inherited from the master opp. owner_id = the CSM from the
    # "Opportunity Owner" prompt (Shayan, 2026-06-19); blank prompt -> master owner.
    # Contacts are owned by the master opp owner, never the running user.
}


# --- OPPORTUNITY LINE ITEM (OLI) ------------------------------------------
OLI_FIELDS = {
    "opportunity_id": "OpportunityId",
    "product2_id": "Product2Id",
    "pricebook_entry_id": "PricebookEntryId",
    "quantity": "Quantity",
    "unit_price": "UnitPrice",                    # Sales Price (pricing gate lands here)
    "sport": "Sport__c",                          # controls the dependent Position picklist
    "camera_scene": "Camera_Scene__c",
    "position_of_field": "Position_of_Field__c",
    "voucher": "Voucher__c",                      # voucher setup: sub at qty -1 + this checked
    "camera_shipping_address": "Camera_Shipping_Address__c",  # "Camera Alternate Shipping Address";
                                                  # set MANUALLY for off-site shipping (e.g. Calgary's
                                                  # cameras -> WHL league office). NOT auto-stamped.
    "camera_order_type": "Camera_order_type__c",  # default "Shipment - No additional cost" (league pays)
    # Younium fields -- bulk-imported opps BYPASS Younium, so these arrive wrong/blank
    # and must be set explicitly (Shayan, 2026-06-22):
    "younium_charge_type": "Younium__Y_Charge_type__c",          # One-off (hardware) / Recurring (subscription)
    "younium_charge_name": "Younium__Y_Younium_Charge_name__c",  # = product name (drives Subscription Type)
    "younium_list_price": "Younium__List_price__c",              # Younium catalog list (see note in build_records)
    "billing_period": "Younium__Y_Billing_period__c",            # subscriptions = Annual; bulk import else
                                                  # defaults Monthly (Shayan, 2026-06-24). Blank on one-off hardware.
    "price_period": "Younium__Y_Price_period__c",                # subscriptions = Annual (the list price is annual)
    "id_unique_oli_product": "Younium__Y_ID_unique_OLI_product__c",  # per-ORDER sequence 1,2,3…
                                                  # (string) unique WITHIN each opp; restarts per opp
    # Discount NOT written -- confirmed sheet-internal / not used.
}

OLI_DEFAULTS = {
    "camera_quantity": 1,
    # League pays for the cameras -> ship at no extra cost (Shayan, 2026-06-22).
    "camera_order_type": "Shipment - No additional cost",
    # Charge type drives Gross Profit: the one-off cost roll-up only counts One-off lines,
    # so HARDWARE (cameras + encoder) MUST be One-off or camera cost never reaches GP.
    "charge_type_hardware": "One-off",
    "charge_type_subscription": "Recurring",
    # Subscriptions bill annually AND their list price is an annual price; bulk import bypasses
    # Younium so both land Monthly unless set (Shayan, 2026-06-24). One-off hardware: no billing
    # period (price period left at the SF default — irrelevant for a one-time charge).
    "subscription_billing_period": "Annual",
    "subscription_price_period": "Annual",
}


# Mapping fully signed off by Shayan + Egil 2026-06-16. Remaining wiring details
# (not property questions): Level value sourced from the master opp's account;
# Team Gender collected as a Step-1 prompt (competition-level).
