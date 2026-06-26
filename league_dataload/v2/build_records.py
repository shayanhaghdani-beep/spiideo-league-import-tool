"""Turn parsed club records into the four SF object payloads, per structure.

Parents are referenced by temp ref-keys (e.g. ``ACC:Calgary Hitmen``) that the
importer resolves to real Ids during the staged create-and-capture push. Existing
accounts carry their matched SF Id directly.

Deal structures:
  1  cameras + subscriptions per team  -> account + contact + opp + (sub + camera OLIs)
  2  cameras only per team             -> account + contact + opp + (camera OLIs)
  3  cameras to the league, no club opps -> camera OLIs on the master opp only
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from . import mapping as M
from .load_mcs import ClubRecord
from .pricing import CurrencyPricebook, PricingConfig

STRUCT_CAMERAS_AND_SUBS = 1
STRUCT_CAMERAS_ONLY = 2
STRUCT_CAMERAS_TO_LEAGUE = 3


@dataclass
class MasterOpp:
    """The existing master league opp the child opps inherit from (NEVER created)."""
    opp_id: str
    owner_id: str = ""        # child opps inherit this owner
    close_date: str = ""      # child opps inherit this close date
    account_level: str = ""   # club accounts take the league's Level


@dataclass
class PlannedRecord:
    sobject: str                       # SF API object name
    operation: str                     # create | upsert | reuse
    ref_key: str                       # temp key other records point at
    fields: dict                       # logical desired field values (API-named)
    sf_id: str = ""                    # known Id (existing match) if any
    parents: dict = field(default_factory=dict)  # field_api -> ref_key to resolve
    # deferred_parents are applied as a SECOND-pass update once the referenced
    # records exist (e.g. Account contact-role lookups -> the Contact created after it).
    deferred_parents: dict = field(default_factory=dict)
    label: str = ""                    # human label for the change list
    note: str = ""                     # crosscheck / review note


@dataclass
class Plan:
    structure: int
    currency: str
    master_opp_id: str
    records: list[PlannedRecord] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def by_object(self, sobject: str) -> list[PlannedRecord]:
        return [r for r in self.records if r.sobject == sobject]


def _to_iso_date(value: str) -> str:
    """Normalise a sheet date to ISO (YYYY-MM-DD) for Salesforce date fields.
    Handles 'June 1st, 2026', 'Jun 1 2026', '6/1/2026', etc. Returns '' if it
    can't be parsed (a blank is safer than a value SF rejects on insert)."""
    s = (value or "").strip()
    if not s:
        return ""
    if re.match(r"^\d{4}-\d{2}-\d{2}", s):
        return s[:10]
    s = re.sub(r"(\d{1,2})(st|nd|rd|th)\b", r"\1", s, flags=re.IGNORECASE)
    for fmt in ("%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y",
                "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def _split_name(full: str) -> tuple[str, str]:
    parts = (full or "").split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], parts[0]
    return parts[0], " ".join(parts[1:])


def _account_fields(rec: ClubRecord, currency: str, league_level: str = "",
                    is_new: bool = False) -> dict:
    F = M.ACCOUNT_FIELDS
    d = {
        F["name"]: rec.team_name,
        F["billing_country_code"]: M.country_code(rec.billing_country),
        F["billing_state_code"]: rec.billing_state,   # sheet already uses state codes
        F["billing_street"]: rec.billing_street,
        F["billing_postal"]: rec.billing_postal,
        F["billing_city"]: rec.billing_city,
        F["currency"]: currency,
        # Tax field is EU vs non-EU conditional (Shayan):
        M.tax_field_for(rec.billing_country): rec.tax_id,
        F["sport"]: rec.sport,
        F["level"]: league_level,                 # sourced from the league
        F["org_type"]: M.ACCOUNT_DEFAULTS["org_type_value"],
        F["invoice_delivery_method"]: M.ACCOUNT_DEFAULTS["invoice_delivery_method"],
        F["payment_terms"]: M.ACCOUNT_DEFAULTS["payment_terms"],
        # Invoice/Shipping phone = the club contact's phone (Shayan, 2026-06-22).
        F["invoice_phone"]: rec.contact_phone,
        F["shipping_phone"]: rec.contact_phone,
    }
    # The SHEET is the source of truth for the FULL address. On a matched/existing
    # account we OVERWRITE BOTH billing AND shipping from the sheet (Shayan, 2026-06-19;
    # the importer diff still applies changed fields only). Shipping == billing per the
    # sheet; this same address trickles down to the camera lines via _shipping_address_str.
    if M.ACCOUNT_DEFAULTS["copy_billing_to_shipping"]:
        d[F["shipping_country_code"]] = M.country_code(rec.billing_country)
        d[F["shipping_state_code"]] = rec.billing_state
        d[F["shipping_street"]] = rec.billing_street
        d[F["shipping_postal"]] = rec.billing_postal
        d[F["shipping_city"]] = rec.billing_city
    # A NEW account must always carry a Website + Domain (Shayan, 2026-06-24). Derive
    # both from the club contact's email domain; skipped (left blank -> flagged in
    # build_plan) when the email is a generic provider. Matched accounts keep their own.
    if is_new:
        wd = M.web_domain(rec.email_domain)
        if wd:
            d[F["website"]] = wd
            d[F["domain"]] = wd
    return {k: v for k, v in d.items() if v not in ("", None)}


def _contact_fields(rec: ClubRecord, owner_id: str = "") -> dict:
    F = M.CONTACT_FIELDS
    first, last = _split_name(rec.contact_name)
    d = {F["first_name"]: first, F["last_name"]: last,
         F["email"]: rec.contact_email, F["phone"]: rec.contact_phone,
         # Owner = the MASTER opp owner (never the running user). Shayan, 2026-06-19.
         F["owner_id"]: owner_id}
    return {k: v for k, v in d.items() if v not in ("", None)}


def _opp_fields(rec: ClubRecord, currency: str, master: "MasterOpp",
                team_gender: str, league: str = "", opp_owner_id: str = "",
                *, stage: str = "", forecast: str = "") -> dict:
    F = M.OPPORTUNITY_FIELDS
    D = M.OPPORTUNITY_DEFAULTS
    # Opp Name = "{Team Name} {Sport}" (Shayan, 2026-06-19), e.g.
    # "Brandon Wheat Kings Ice Hockey". Falls back to the sheet's Order Name /
    # team name if Sport is blank. (`league` is no longer used for naming.)
    name = (f"{rec.team_name} {rec.sport}".strip()
            or rec.order_name.strip() or rec.team_name)
    d = {
        F["name"]: name,
        F["master_opportunity"]: master.opp_id,
        F["currency"]: currency,
        F["pricebook2_id"]: M.YOUNIUM_PRICEBOOK2_ID,
        F["effective_start_date"]: _to_iso_date(rec.effective_start),
        F["spiideo_account_name"]: f"{rec.team_name} Perform",
        F["stage"]: stage or D["stage"],           # default Closed Won; Config can override
        F["forecast_category"]: forecast or D["forecast_category"],
        F["opp_type"]: D["opp_type"],
        F["order_type"]: D["order_type"],
        F["event_source"]: D["event_source"],       # "Not applicable" default
        F["sport"]: rec.sport,                      # = account Sport__c
        F["team_gender"]: team_gender,              # competition-level (prompt)
        # CloseDate inherited from the master opp; OwnerId = the CSM from the
        # "Opportunity Owner" prompt (falls back to master opp owner if unset).
        F["close_date"]: master.close_date,
        F["owner_id"]: opp_owner_id or master.owner_id,
    }
    if rec.wants_league_exchange:
        d[F["order_notes"]] = M.OPPORTUNITY_DEFAULTS["svff_le_note"]   # "add to SvFF LE"
    return {k: v for k, v in d.items() if k and v not in ("", None)}


def _existing_opp_update_fields(rec: ClubRecord, currency: str, *, opp_owner_id: str = "",
                                stage: str = "", forecast: str = "", master_opp_id: str = "",
                                team_gender: str = "") -> dict:
    """Minimal field set written to a PRE-EXISTING opp in attach mode (Shayan, 2026-06-23).
    Sets the stage (default Closed Won, override via Config -- SvFF uses 'Decision & Signature'
    so the rep closes it himself), the Younium-relevant fields + the pricebook (required before
    OLIs can attach), the owner IF a Config 'Opportunity Owner' was given (SvFF = Amir Jakirlic),
    and links to the master opp IF a Config 'Master Opportunity ID' was given (SvFF placeholders
    arrive with a null master -- e.g. 0067Q00000GIylWQAT 'Swedish Football Federation'). Leaves
    Name, CloseDate and RecordType untouched -- the placeholder already carries those."""
    F = M.OPPORTUNITY_FIELDS
    D = M.OPPORTUNITY_DEFAULTS
    d = {
        F["stage"]: stage or D["stage"],                    # default Closed Won; SvFF overrides
        F["forecast_category"]: forecast or D["forecast_category"],
        # The OLI pricebook entries are currency-specific -> the opp's currency MUST match
        # the run currency or SF rejects the line ("pricebook entry currency code does not
        # match opportunity currency code"). Some placeholder opps carry a stale currency
        # (SvFF: several were EUR) -> force the run currency (Shayan, 2026-06-23).
        F["currency"]: currency,
        F["pricebook2_id"]: M.YOUNIUM_PRICEBOOK2_ID,    # required so OLIs can attach
        F["opp_type"]: D["opp_type"],                   # New
        F["order_type"]: D["order_type"],               # New order
        F["event_source"]: D["event_source"],           # Not applicable
        F["spiideo_account_name"]: f"{rec.team_name} Perform",
        F["effective_start_date"]: _to_iso_date(rec.effective_start),
        F["sport"]: rec.sport,                           # = account Sport__c (parity w/ create path)
    }
    if opp_owner_id:
        d[F["owner_id"]] = opp_owner_id     # SvFF: re-own the deal to the CSM (e.g. Amir)
    if master_opp_id:
        d[F["master_opportunity"]] = master_opp_id   # link the placeholder to its master opp
    if team_gender:
        d[F["team_gender"]] = team_gender   # from the sheet's per-row Team Gender (Mens/Womens)
    if rec.wants_league_exchange:
        d[F["order_notes"]] = M.OPPORTUNITY_DEFAULTS["svff_le_note"]   # "add to SvFF LE"
    return {k: v for k, v in d.items() if k and v not in ("", None)}


def _oli_fields(price_entry, unit_price: float, *, camera=None, sport: str = "",
                sub_period: str = "") -> dict:
    F = M.OLI_FIELDS
    d = {
        F["product2_id"]: price_entry.product2_id,
        F["pricebook_entry_id"]: price_entry.pricebook_entry_id,
        F["quantity"]: M.OLI_DEFAULTS["camera_quantity"] if camera else 1,
        F["unit_price"]: unit_price,
        # Younium fields (bulk import bypasses Younium -> set explicitly, Shayan 2026-06-22):
        #  - charge type: HARDWARE (camera/encoder) = One-off so its cost reaches GP;
        #    SUBSCRIPTION = Recurring.
        #  - charge name = product name, which drives the Perform/Play/Replay Subscription Type.
        #  - list price = the pricebook list price (Younium's catalog list). NOTE: in our
        #    pricebook this equals the standard SF ListPrice; if Younium's true catalog list
        #    differs it must be sourced from Younium, not here.
        F["younium_charge_type"]: (M.OLI_DEFAULTS["charge_type_hardware"] if camera is not None
                                   else M.OLI_DEFAULTS["charge_type_subscription"]),
        F["younium_charge_name"]: price_entry.product_name,
        F["younium_list_price"]: price_entry.list_price,
    }
    if camera is not None:
        # Sport__c is the controller for the dependent Position_of_Field__c picklist.
        d[F["sport"]] = sport
        d[F["camera_scene"]] = camera.scene
        d[F["position_of_field"]] = camera.position
        # League pays -> every camera ships at no additional cost (Shayan, 2026-06-22).
        d[F["camera_order_type"]] = M.OLI_DEFAULTS["camera_order_type"]
    else:
        # Subscriptions bill ANNUALLY (and their list price is an annual price) by DEFAULT --
        # bulk import bypasses Younium so both land Monthly otherwise (Shayan, 2026-06-24).
        # Always Annual UNLESS the Config "Subscription billing period" says otherwise
        # (`sub_period`). One-off hardware carries no billing period.
        d[F["billing_period"]] = sub_period or M.OLI_DEFAULTS["subscription_billing_period"]
        d[F["price_period"]] = sub_period or M.OLI_DEFAULTS["subscription_price_period"]
    return {k: v for k, v in d.items() if k and v not in ("", None)}


def build_plan(records: list[ClubRecord], *, structure: int, currency: str,
               master: MasterOpp, pricing: PricingConfig,
               pricebook: CurrencyPricebook, team_gender: str = "", league: str = "",
               voucher: bool = False, record_type_id: str = "",
               opp_owner_id: str = "", master_shipping: bool = False,
               shipping_map: dict | None = None,
               match_ids: dict[str, str] | None = None,
               opp_stage: str = "", opp_forecast: str = "",
               sub_billing_period: str = "") -> Plan:
    """match_ids: team_name -> matched SF Account Id ('' / missing = new club).
    record_type_id: resolved Id for OPPORTUNITY_DEFAULTS['record_type_developer_name']
    (Transactional); team_gender: competition-level value from the Step-1 prompt.
    opp_owner_id: resolved SF User Id for the child opps (the CSM, from the
    "Opportunity Owner" prompt). Contacts are always owned by master.owner_id.
    Camera Alternate Shipping is NOT auto-stamped -- it's set manually for the rare
    off-site case (e.g. Calgary's cameras shipping to the WHL league office)."""
    match_ids = match_ids or {}
    master_opp_id = master.opp_id
    plan = Plan(structure=structure, currency=currency, master_opp_id=master_opp_id)

    _new_acct_seen: set[str] = set()   # warn at most once per new club (multi-team rows)
    for ridx, rec in enumerate(records):
        # A club can appear on several rows (one per team/sport), e.g. a college with
        # Basketball + Football + Volleyball. The ACCOUNT and CONTACT are shared per club
        # (keyed by team_name -> upsert/dedup consolidates them), but the OPPORTUNITY and
        # its OLIs are PER ROW, so their ref-keys must be unique per row -- otherwise every
        # team's OLIs collide onto the last opp created for that club (Shayan, 2026-06-23).
        rowkey = f"{rec.team_name}#{ridx}"
        attach_opp_id = (rec.opportunity_sf_id or "").strip()
        # ---- camera OLIs (every structure) ----
        camera_olis: list[dict] = []
        for cam in rec.active_cameras:
            pe = pricebook.get(cam.type)
            if pe is None:
                plan.warnings.append(f"{rec.team_name}: camera product not in pricebook: {cam.type!r}")
                continue
            up = pricing.cameras.unit_price(pe)
            camera_olis.append(_oli_fields(pe, up, camera=cam, sport=rec.sport))

        if structure == STRUCT_CAMERAS_TO_LEAGUE:
            # No club account/contact/opp. Cameras hang off the master opp.
            for i, oli in enumerate(camera_olis):
                plan.records.append(PlannedRecord(
                    sobject=M.SOBJECT["oli"], operation="create",
                    ref_key=f"OLI:{rowkey}:{i}", fields=oli,
                    parents={M.OLI_FIELDS["opportunity_id"]: master_opp_id},
                    label=f"OLI (league) {rec.team_name} cam{i+1}"))
            continue

        # ---- account (new vs existing) ----
        acc_ref = f"ACC:{rec.team_name}"
        con_ref = f"CON:{rec.team_name}"
        has_contact = bool(rec.contact_email or rec.contact_name)
        # An explicit Customer SF ID on the sheet WINS over the fuzzy name matcher
        # (pins the exact account; avoids picking a same-named duplicate). Shayan, 2026-06-23.
        matched_id = (rec.customer_sf_id or "").strip() or match_ids.get(rec.team_name, "")
        # Contact-role lookups point at the Contact, created AFTER the Account,
        # so they are deferred to a second-pass Account update.
        deferred: dict = {}
        if has_contact and M.ACCOUNT_DEFAULTS["populate_contact_role_lookups"]:
            for role in M.CONTACT_ROLE_LOOKUPS:
                deferred[M.ACCOUNT_FIELDS[role]] = con_ref
        plan.records.append(PlannedRecord(
            sobject=M.SOBJECT["account"],
            operation="upsert" if matched_id else "create",
            ref_key=acc_ref,
            # In attach mode DON'T propagate the master's Level onto an existing club account
            # (it would overwrite the club's own Level__c with the league/federation's). Only a
            # create-mode (new) club takes the league Level (Shayan, 2026-06-24).
            fields=_account_fields(rec, currency,
                                   league_level=("" if attach_opp_id else master.account_level),
                                   is_new=not matched_id),
            sf_id=matched_id, deferred_parents=deferred,
            label=f"Account {rec.team_name}" + (" (existing)" if matched_id else " (NEW)")))
        # A NEW account must carry a Website + Domain. We derive both from the contact's
        # email domain; if it's blank or a generic provider we can't -> flag it ONCE so the
        # operator (or an agent) looks the club's domain up and sets it (Shayan, 2026-06-24).
        if not matched_id and rec.team_name not in _new_acct_seen:
            _new_acct_seen.add(rec.team_name)
            if not M.web_domain(rec.email_domain):
                plan.warnings.append(
                    f"{rec.team_name}: NEW account, no Website/Domain derivable from contact "
                    f"email ({rec.email_domain or 'none'}) -> look up the club domain and set "
                    "Website + Domain__c manually")

        # ---- contact ----
        if has_contact:
            # Item-1 check: a contact with no phone means the Account invoice/shipping
            # phone ends up blank too -- surface it rather than silently shipping blank.
            if not rec.contact_phone:
                plan.warnings.append(
                    f"{rec.team_name}: contact has NO phone -> Contact.Phone + Account "
                    "invoice/shipping phone will be blank")
            plan.records.append(PlannedRecord(
                sobject=M.SOBJECT["contact"], operation="create",
                ref_key=con_ref, fields=_contact_fields(rec, owner_id=master.owner_id),
                parents={M.CONTACT_FIELDS["account_id"]: acc_ref},
                label=f"Contact {rec.contact_name} <{rec.contact_email}>"))

        # ---- opportunity ----
        opp_ref = f"OPP:{rowkey}"
        # Gender is per row when the sheet specifies it (college conferences carry one gender
        # per team/sport), falling back to the Config value. Set on BOTH paths (Shayan, 2026-06-24
        # -- attach mode was previously dropping it).
        row_gender = M.team_gender_value(rec.gender) or team_gender
        # Contact lookups apply to either path (the Contact is created before the Opp).
        opp_parents: dict = {}
        if has_contact:
            opp_parents[M.OPPORTUNITY_FIELDS["primary_contact"]] = con_ref
            # System Administrator on the opp = the same line contact (Shayan, 2026-06-22).
            opp_parents[M.OPPORTUNITY_FIELDS["system_admin"]] = con_ref
        if attach_opp_id:
            # ATTACH MODE (Shayan, 2026-06-23): the club already has a pre-created opp
            # (e.g. the SvFF "<club> – Rollout 2026" placeholders). Don't create a second
            # one -- UPDATE the existing opp (close it + set pricebook/Younium fields) and
            # hang the OLIs off it. Name/Owner/CloseDate/RecordType are left as-is.
            plan.records.append(PlannedRecord(
                sobject=M.SOBJECT["opportunity"], operation="upsert",
                ref_key=opp_ref, sf_id=attach_opp_id,
                fields=_existing_opp_update_fields(rec, currency, opp_owner_id=opp_owner_id,
                                                   stage=opp_stage, forecast=opp_forecast,
                                                   master_opp_id=master.opp_id,
                                                   team_gender=row_gender),
                parents=opp_parents,
                label=f"Opportunity (existing) {rec.team_name} [{attach_opp_id}]"))
        else:
            opp_fields = _opp_fields(rec, currency, master, row_gender, league, opp_owner_id,
                                     stage=opp_stage, forecast=opp_forecast)
            if record_type_id:
                opp_fields[M.OPPORTUNITY_FIELDS["record_type_id"]] = record_type_id
            opp_parents[M.OPPORTUNITY_FIELDS["account_id"]] = acc_ref
            plan.records.append(PlannedRecord(
                sobject=M.SOBJECT["opportunity"], operation="create",
                ref_key=opp_ref, fields=opp_fields, parents=opp_parents,
                label=f"Opportunity {opp_fields.get(M.OPPORTUNITY_FIELDS['name'])}"))

        # ---- subscription OLI (structure 1 only) ----
        if structure == STRUCT_CAMERAS_AND_SUBS and rec.subscription:
            pe = pricebook.get(rec.subscription)
            if pe is None:
                plan.warnings.append(f"{rec.team_name}: subscription not in pricebook: {rec.subscription!r}")
            else:
                # Voucher deals: both the +1 line and the -1 voucher line carry the
                # LIST price so the real value shows and nets to $0 (Shayan, 2026-06-22).
                # Non-voucher deals honor the configured subscription pricing mode.
                up = pe.list_price if voucher else pricing.subscription.unit_price(pe)
                # Always the normal subscription line (qty +1).
                plan.records.append(PlannedRecord(
                    sobject=M.SOBJECT["oli"], operation="create",
                    ref_key=f"OLI:{rowkey}:sub", fields=_oli_fields(pe, up, sub_period=sub_billing_period),
                    parents={M.OLI_FIELDS["opportunity_id"]: opp_ref},
                    label=f"OLI sub {rec.team_name} {rec.subscription}"))
                if voucher:
                    # Voucher setup (league pre-paid): add a SECOND line -- the SAME
                    # subscription at qty -1 with Voucher__c checked. The +1 and -1
                    # net out so the team isn't double-charged.
                    vfields = _oli_fields(pe, up, sub_period=sub_billing_period)
                    vfields[M.OLI_FIELDS["quantity"]] = -1
                    vfields[M.OLI_FIELDS["voucher"]] = "true"
                    plan.records.append(PlannedRecord(
                        sobject=M.SOBJECT["oli"], operation="create",
                        ref_key=f"OLI:{rowkey}:sub_voucher", fields=vfields,
                        parents={M.OLI_FIELDS["opportunity_id"]: opp_ref},
                        label=f"OLI sub {rec.team_name} {rec.subscription} (VOUCHER qty -1)"))

        # ---- camera OLIs ----
        for i, oli in enumerate(camera_olis):
            plan.records.append(PlannedRecord(
                sobject=M.SOBJECT["oli"], operation="create",
                ref_key=f"OLI:{rowkey}:{i}", fields=oli,
                parents={M.OLI_FIELDS["opportunity_id"]: opp_ref},
                label=f"OLI {rec.team_name} cam{i+1}"))

    # "ID unique OLI product" -- a per-ORDER sequence (1, 2, 3, …) unique WITHIN each
    # opp, restarting per opp (Shayan, 2026-06-22). Younium keys each product in an order
    # by it. Grouped by parent opp, so it's correct for every structure (incl. structure
    # 3, where all cameras share the master opp).
    _oli_seq: dict[str, int] = {}
    for r in plan.records:
        if r.sobject == M.SOBJECT["oli"]:
            opp_ref = r.parents.get(M.OLI_FIELDS["opportunity_id"], "")
            n = _oli_seq.get(opp_ref, 0) + 1
            _oli_seq[opp_ref] = n
            r.fields[M.OLI_FIELDS["id_unique_oli_product"]] = str(n)

    # ---- master-opp shipping lines (OPT-IN; Shayan 2026-06-22) ----
    # One line per (camera type x destination region) across all clubs, on the MASTER
    # opp: qty = # of that camera shipping to that region; region = each club's billing
    # country (US ships WITH TARIFFS). Sales Price $0 (not billed); cost auto-derives
    # from Product2.Cost_USD__c into GP (charge type One-off). Sales reps normally add
    # shipping themselves, so this is OFF by default. Added AFTER the idu post-pass so
    # these stay blank (the master is Younium-managed -> Younium assigns the idu).
    if master_shipping and master_opp_id and shipping_map:
        counts: dict[tuple[str, str], int] = {}
        for rec in records:
            region = M.shipping_region(rec.billing_country)
            for cam in rec.active_cameras:
                key = (M.shipping_camera_key(cam.type), region)
                counts[key] = counts.get(key, 0) + 1
        for (ckey, region), qty in sorted(counts.items()):
            sp = shipping_map.get((ckey, region))
            if not sp:
                plan.warnings.append(
                    f"master shipping: no shipping product for camera {ckey!r} / region {region!r}")
                continue
            fields = {
                M.OLI_FIELDS["product2_id"]: sp["product2_id"],
                M.OLI_FIELDS["pricebook_entry_id"]: sp["pbe_id"],
                M.OLI_FIELDS["quantity"]: qty,
                M.OLI_FIELDS["unit_price"]: 0.0,                 # not billed to the customer
                M.OLI_FIELDS["younium_charge_type"]: M.OLI_DEFAULTS["charge_type_hardware"],  # One-off
                M.OLI_FIELDS["younium_charge_name"]: sp["charge_name"],
            }
            plan.records.append(PlannedRecord(
                sobject=M.SOBJECT["oli"], operation="create",
                ref_key=f"OLI:MASTER:ship:{ckey}:{region}", fields=fields,
                parents={M.OLI_FIELDS["opportunity_id"]: master_opp_id},
                label=f"OLI master SHIP {ckey} [{region}] x{qty}"))

    return plan
