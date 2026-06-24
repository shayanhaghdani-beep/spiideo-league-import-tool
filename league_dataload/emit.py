"""Emit the Salesforce DataLoader-ready CSVs for league deals.

Column orders match the canonical import templates exactly (the combined
Account/Contact template is split into a dedicated Account file and Contact
file, per request):

  - account.csv         — Account upsert, one row per UNIQUE league (24 cols)
  - contact.csv         — Contact upsert (league forecasts carry no contact
                          data, so this is header-only for the league flow) (7 cols)
  - opportunity.csv     — Opportunity upsert, one row per (league, period) (23 cols)
  - league_crosscheck.csv — match report (analysis/QA; not a DataLoader file)

ARR lands on the Opp Product Sales Price (see emit_opp_product.py), since the
Opportunity template carries no ARR column.
"""
from __future__ import annotations

import csv
from pathlib import Path

from .config import AccountDefaults, LeagueOppDefaults
from .schema import ForecastRow, LeagueAccount


# ---------------------------------------------------------------------------
# account.csv  (Account object fields from the template, in template order)

ACCOUNT_COLUMNS = [
    "Account ID", "Team Name", "Billing Country", "Billing Street",
    "Billing Postal Code/Zip Code", "Billing State", "Billing City", "Tax ID",
    "Invoice Delivery Method", "Payment Terms", "Account Currency",
    "Shipping Country", "Shipping Street", "Shipping Postal Code/Zip Code",
    "Shipping State", "Shipping City", "Invoice Contact ID", "Shipping Contact ID",
    "Installation Responsible ID", "IT Responsible ID", "Org type", "Sport",
    "Level", "League ID",
]


def emit_account(leagues: list[LeagueAccount], out_path: Path,
                 defaults: AccountDefaults | None = None) -> int:
    """One row per UNIQUE league. Address/contact-role fields stay blank (the
    forecast doesn't carry them; sales fills them later via the MCS flow)."""
    defaults = defaults or AccountDefaults()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=ACCOUNT_COLUMNS)
        w.writeheader()
        for la in leagues:
            row = {col: "" for col in ACCOUNT_COLUMNS}
            row["Account ID"] = (
                la.matched_sf_ids[0]
                if la.match_status == "matched" and la.matched_sf_ids
                else ""
            )
            row["Team Name"] = la.display_name
            row["Account Currency"] = "EUR"   # league forecasts are €
            row["Payment Terms"] = defaults.payment_terms
            row["Invoice Delivery Method"] = defaults.invoice_delivery_method
            row["Org type"] = "League Organization"
            row["Sport"] = la.primary_sport
            w.writerow(row)
    return len(leagues)


# ---------------------------------------------------------------------------
# contact.csv  (Contact object fields from the template)

CONTACT_COLUMNS = [
    "Account ID", "Opportunity ID", "First Name", "Last Name", "Full Name",
    "Email", "Phone Number", "Contact Name",
]


def _split_name(full: str) -> tuple[str, str]:
    full = (full or "").strip()
    if not full:
        return "", ""
    parts = full.split()
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def emit_contact(leagues: list[LeagueAccount], out_path: Path) -> int:
    """Write contact.csv. League forecasts carry no contact data, so this emits
    a row ONLY for a league that has a contact name attached (currently never —
    header-only output). Kept so the import structure exists; contacts are added
    later via the MCS team/club flow."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with out_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CONTACT_COLUMNS)
        w.writeheader()
        for la in leagues:
            contact_name = getattr(la, "contact_name", "") or ""
            if not contact_name.strip():
                continue   # no contact data → don't emit a blank contact
            first, last = _split_name(contact_name)
            account_id = la.matched_sf_ids[0] if la.matched_sf_ids else ""
            w.writerow({
                "Account ID": account_id,
                "Opportunity ID": "",
                "First Name": first,
                "Last Name": last,
                "Full Name": contact_name,
                "Email": getattr(la, "contact_email", "") or "",
                "Phone Number": getattr(la, "contact_phone", "") or "",
                "Contact Name": contact_name,
            })
            n += 1
    return n


# ---------------------------------------------------------------------------
# opportunity.csv  (exact template — no 'Primary Contact', no ARR column)

OPPORTUNITY_COLUMNS = [
    "Account ID", "Team Name", "Order Name", "Master Opportunity",
    "Opportunity ID", "Name", "Owner ID", "Close Date", "Stage",
    "Forecast Category", "Effective Start Date", "Spiideo Account Name",
    "System Administrator", "Primary Contact", "Opportunity Currency",
    "Opportunity Type", "Order Type", "Sport", "Team Gender", "Invoice Account",
    "Billing Period", "Notice Period (Months)", "Camera Shipping Schedule",
    "Shipment Status",
]


def _first_nonblank(rows, attr: str) -> str:
    for r in rows:
        v = getattr(r, attr, "")
        if v and str(v).strip():
            return str(v).strip()
    return ""


def _deal_type_to_opp_type(deal_type: str) -> str:
    """Map the forecast's Deal Type to a Salesforce Opportunity Type picklist value."""
    dt = (deal_type or "").strip().lower()
    if not dt:
        return "New"
    if "renew" in dt:
        return "Renewal"
    if "upsell" in dt or "expansion" in dt or "expand" in dt:
        return "Upsell"
    return "New"


def emit_opportunity_per_league(leagues: list[LeagueAccount], out_path: Path,
                                defaults: LeagueOppDefaults | None = None,
                                master_opp_default: str = "") -> int:
    """One row per (league, period). Each is a CHILD opp under the master/mother
    deal (Master Opportunity = per-row value, else the run-wide default). ARR is
    carried on the Opp Product, not here."""
    defaults = defaults or LeagueOppDefaults()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with out_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=OPPORTUNITY_COLUMNS)
        w.writeheader()
        for la in leagues:
            account_id = (
                la.matched_sf_ids[0]
                if la.match_status == "matched" and la.matched_sf_ids
                else ""
            )
            primary_fr = la.forecast_rows[0] if la.forecast_rows else None
            if primary_fr is None:
                continue
            w.writerow(_build_consolidated_row(la, primary_fr, account_id, defaults,
                                               master_opp_default))
            n += 1
    return n


def _build_consolidated_row(la: LeagueAccount, primary_fr: ForecastRow,
                            account_id: str, d: LeagueOppDefaults,
                            master_opp_default: str = "") -> dict[str, str]:
    period = primary_fr.period or "H2 2026"
    league_clean = " ".join(la.display_name.split())
    opp_name = f"{league_clean} - {period}".strip(" -")
    deal_type = _first_nonblank(la.forecast_rows, "deal_type")
    close_date = _first_nonblank(la.forecast_rows, "target_close")
    owner_id = primary_fr.resolved_rep_user_id or ""
    master_opp = _first_nonblank(la.forecast_rows, "master_opportunity") or master_opp_default
    return {
        "Account ID": account_id,
        "Team Name": league_clean,
        "Order Name": f"{period} Forecast",
        "Master Opportunity": master_opp,
        "Opportunity ID": "",
        "Name": opp_name,
        "Owner ID": owner_id,
        "Close Date": close_date,
        "Stage": d.stage,
        "Forecast Category": d.forecast_category,
        "Effective Start Date": "",
        "Spiideo Account Name": f"{league_clean} Perform" if league_clean else "",
        "System Administrator": "",
        "Primary Contact": "",
        "Opportunity Currency": d.opp_currency,
        "Opportunity Type": _deal_type_to_opp_type(deal_type),
        "Order Type": d.order_type,
        "Sport": la.primary_sport,
        "Team Gender": "",
        "Invoice Account": account_id,
        "Billing Period": d.billing_period,
        "Notice Period (Months)": d.notice_period_months,
        "Camera Shipping Schedule": d.camera_shipping_schedule,
        "Shipment Status": "",
    }


def emit_opportunity_per_row(leagues: list[LeagueAccount], out_path: Path,
                             defaults: LeagueOppDefaults | None = None,
                             master_opp_default: str = "") -> int:
    """Alternative grain: one Opp per forecast row (per rep/product)."""
    defaults = defaults or LeagueOppDefaults()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with out_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=OPPORTUNITY_COLUMNS)
        w.writeheader()
        for la in leagues:
            account_id = (
                la.matched_sf_ids[0]
                if la.match_status == "matched" and la.matched_sf_ids
                else ""
            )
            for fr in la.forecast_rows:
                suffix = f" {fr.period}" if fr.period else ""
                opp_name = f"{la.display_name} {fr.product}{suffix}".strip()
                w.writerow({
                    "Account ID": account_id,
                    "Team Name": la.display_name,
                    "Order Name": fr.product,
                    "Master Opportunity": fr.master_opportunity or master_opp_default,
                    "Opportunity ID": "",
                    "Name": opp_name,
                    "Owner ID": fr.resolved_rep_user_id,
                    "Close Date": fr.target_close,
                    "Stage": defaults.stage,
                    "Forecast Category": defaults.forecast_category,
                    "Effective Start Date": "",
                    "Spiideo Account Name": f"{la.display_name} Perform" if la.display_name else "",
                    "System Administrator": "",
                    "Primary Contact": "",
                    "Opportunity Currency": defaults.opp_currency,
                    "Opportunity Type": _deal_type_to_opp_type(fr.deal_type),
                    "Order Type": defaults.order_type,
                    "Sport": fr.sport,
                    "Team Gender": "",
                    "Invoice Account": account_id,
                    "Billing Period": defaults.billing_period,
                    "Notice Period (Months)": defaults.notice_period_months,
                    "Camera Shipping Schedule": defaults.camera_shipping_schedule,
                    "Shipment Status": "",
                })
                n += 1
    return n


# ---------------------------------------------------------------------------
# league_crosscheck.csv  (analysis / QA — review before importing)

CROSSCHECK_COLUMNS = [
    "League", "Normalized Name", "Primary Sport", "All Sports", "Rep Count",
    "Reps", "Forecast Rows", "Total ARR (EUR)", "Match Status", "Match Source",
    "Match Type", "Confidence", "Confidence Label", "Matched SF Account IDs",
    "Matched HS Record IDs", "Matched SF Account Names", "Existing Deals",
    "Deal Review Needed?", "Existing Active ARR", "Note",
]


def emit_crosscheck_report(leagues: list[LeagueAccount], out_path: Path) -> int:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with out_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CROSSCHECK_COLUMNS)
        w.writeheader()
        for la in leagues:
            w.writerow({
                "League": la.display_name,
                "Normalized Name": la.normalized_name,
                "Primary Sport": la.primary_sport,
                "All Sports": "; ".join(la.sports),
                "Rep Count": len(la.rep_names),
                "Reps": "; ".join(la.rep_names),
                "Forecast Rows": len(la.forecast_rows),
                "Total ARR (EUR)": f"{la.total_arr_eur:.2f}",
                "Match Status": la.match_status,
                "Match Source": la.match_source,
                "Match Type": la.match_type,
                "Confidence": la.match_confidence,
                "Confidence Label": la.match_confidence_label,
                "Matched SF Account IDs": "; ".join(la.matched_sf_ids),
                "Matched HS Record IDs": "; ".join(la.matched_hs_record_ids),
                "Matched SF Account Names": "; ".join(la.matched_sf_names),
                "Existing Deals": (
                    la.matched_associated_deals if la.match_status == "matched" else ""
                ),
                "Deal Review Needed?": (
                    "YES — check CRM for duplicate deals"
                    if la.match_status == "matched" and la.matched_associated_deals > 0
                    else ""
                ),
                "Existing Active ARR": (
                    la.matched_active_arr if la.match_status == "matched" else ""
                ),
                "Note": la.crosscheck_note,
            })
            n += 1
    return n
