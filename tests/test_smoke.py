"""Smoke tests for the league-deals importer.

Run: python3 -m pytest tests/ -q   (or: python3 tests/test_smoke.py)
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from league_dataload import config
from league_dataload.candidates import fetch_existing_leagues
from league_dataload.emit import (
    ACCOUNT_COLUMNS,
    CONTACT_COLUMNS,
    CROSSCHECK_COLUMNS,
    OPPORTUNITY_COLUMNS,
    emit_account,
    emit_contact,
    emit_crosscheck_report,
    emit_opportunity_per_league,
    _deal_type_to_opp_type,
)
from league_dataload.emit_opp_product import (
    OPP_PRODUCT_COLUMNS,
    emit_opp_product_per_league,
)
from league_dataload.load_forecast import load_forecast
from league_dataload.load_pricebook import load_pricebook
from league_dataload.matcher import (
    crosscheck_leagues,
    dedupe_leagues,
    dedupe_leagues_by_period,
    propagate_crosscheck,
)
from league_dataload.resolve_reps import resolve_reps
from league_dataload.sources import CsvLookupSource, StubLookupSource

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "sample_deals.csv"


# Exact output contracts — must never drift from the DataLoader templates.
EXP_ACCOUNT = [
    "Account ID", "Team Name", "Billing Country", "Billing Street",
    "Billing Postal Code/Zip Code", "Billing State", "Billing City", "Tax ID",
    "Invoice Delivery Method", "Payment Terms", "Account Currency",
    "Shipping Country", "Shipping Street", "Shipping Postal Code/Zip Code",
    "Shipping State", "Shipping City", "Invoice Contact ID", "Shipping Contact ID",
    "Installation Responsible ID", "IT Responsible ID", "Org type", "Sport",
    "Level", "League ID",
]
EXP_CONTACT = [
    "Account ID", "Opportunity ID", "First Name", "Last Name", "Full Name",
    "Email", "Phone Number", "Contact Name",
]
EXP_OPP = [
    "Account ID", "Team Name", "Order Name", "Master Opportunity",
    "Opportunity ID", "Name", "Owner ID", "Close Date", "Stage",
    "Forecast Category", "Effective Start Date", "Spiideo Account Name",
    "System Administrator", "Primary Contact", "Opportunity Currency",
    "Opportunity Type", "Order Type", "Sport", "Team Gender", "Invoice Account",
    "Billing Period", "Notice Period (Months)", "Camera Shipping Schedule",
    "Shipment Status",
]
EXP_OPP_PRODUCT = [
    "Account ID", "Opportunity ID", "Opportunity Name", "Number of Product Lines",
    "Products", "Product ID", "Price Book Entry ID", "Sales Price", "List",
    "Opp Currency", "Quantity", "Unit of Measure", "Camera order type",
    "Charge type", "Price Period", "Billing Period", "Younium Charge Name",
    "Voucher", "Shipping status", "Position of Field", "Camera Scene", "Height",
    "Distance from sideline (m)", "ID unique OLI product",
]


def test_column_contracts():
    assert ACCOUNT_COLUMNS == EXP_ACCOUNT
    assert CONTACT_COLUMNS == EXP_CONTACT
    assert OPPORTUNITY_COLUMNS == EXP_OPP
    assert OPP_PRODUCT_COLUMNS == EXP_OPP_PRODUCT
    assert "Deal Qualification ARR" not in OPPORTUNITY_COLUMNS  # dropped per template
    assert "Primary Contact" in OPPORTUNITY_COLUMNS             # kept per request
    assert CROSSCHECK_COLUMNS[0] == "League" and len(CROSSCHECK_COLUMNS) == 20


def test_load_forecast_parses_preamble_and_arr():
    rows = load_forecast(FIXTURE)
    assert len(rows) == 5
    assert {r.rep_name for r in rows} == {"Alice Anderson", "Bob Bergstrom", "Carla Chen"}
    premier = [r for r in rows if r.league == "Premier League" and r.rep_name == "Alice Anderson"][0]
    assert premier.arr_eur == 50000.0


def test_deal_type_mapping():
    assert _deal_type_to_opp_type("New") == "New"
    assert _deal_type_to_opp_type("Upsell") == "Upsell"
    assert _deal_type_to_opp_type("Renewal") == "Renewal"
    assert _deal_type_to_opp_type("") == "New"


def test_crosscheck_matches_via_stub_accounts():
    rows = load_forecast(FIXTURE)
    leagues = dedupe_leagues(rows)
    source = StubLookupSource(accounts=[
        {"Id": "001AAA", "Name": "Premier League", "Org_Type__c": "League",
         "Sport__c": "Football", "BillingCountry": "United Kingdom"},
    ])
    candidates = fetch_existing_leagues(source)
    crosscheck_leagues(leagues, candidates, [], [])
    by_name = {la.display_name: la for la in leagues}
    assert by_name["Premier League"].match_status == "matched"
    assert by_name["Premier League"].matched_sf_ids == ["001AAA"]
    assert by_name["J-League"].match_status == "unmatched"


def test_rep_resolution_via_stub_users():
    rows = load_forecast(FIXTURE)
    source = StubLookupSource(users=[
        {"Id": "005AAA", "Name": "Alice Anderson", "IsActive": "true"},
        {"Id": "005BBB", "Name": "Bob Bergstrom", "IsActive": "true"},
        {"Id": "005CCC", "Name": "Carla Chen", "IsActive": "true"},
    ])
    report = resolve_reps(rows, source)
    assert report.rep_map["Alice Anderson"] == "005AAA"
    assert all(r.resolved_rep_user_id for r in rows)


def test_emit_account_and_contact(tmp_path):
    rows = load_forecast(FIXTURE)
    leagues = dedupe_leagues(rows)
    crosscheck_leagues(leagues, [], [], [])     # all net-new
    acc, con = tmp_path / "account.csv", tmp_path / "contact.csv"
    n_acc = emit_account(leagues, acc)
    n_con = emit_contact(leagues, con)
    assert n_acc == 4
    assert n_con == 0   # league forecasts carry no contact data → header-only
    with acc.open() as f:
        reader = csv.reader(f)
        assert next(reader) == EXP_ACCOUNT
        first = next(reader)
        assert first[EXP_ACCOUNT.index("Account ID")] == ""             # net-new
        assert first[EXP_ACCOUNT.index("Org type")] == "League Organization"
        assert first[EXP_ACCOUNT.index("Account Currency")] == "EUR"
    with con.open() as f:
        assert next(csv.reader(f)) == EXP_CONTACT     # header present even with 0 rows


def test_emit_opportunity_contract(tmp_path):
    rows = load_forecast(FIXTURE)
    leagues = dedupe_leagues(rows)
    crosscheck_leagues(leagues, [], [], [])
    opp_leagues = dedupe_leagues_by_period(rows)
    propagate_crosscheck(leagues, opp_leagues)
    opp = tmp_path / "opportunity.csv"
    n = emit_opportunity_per_league(opp_leagues, opp)
    assert n == 4
    with opp.open() as f:
        reader = csv.reader(f)
        assert next(reader) == EXP_OPP
        row = next(reader)
        assert row[EXP_OPP.index("Stage")] == "Discover Challenges"
        assert row[EXP_OPP.index("Forecast Category")] == "Pipeline"
        assert row[EXP_OPP.index("Opportunity Currency")] == "EUR"


def test_master_opportunity_default_and_per_row_override(tmp_path):
    """Child opps carry Master Opportunity: run-wide default, overridden per row."""
    rows = load_forecast(FIXTURE)
    for r in rows:                      # per-row master opp on Premier League only
        if r.league == "Premier League":
            r.master_opportunity = "006PERROW"
    leagues = dedupe_leagues(rows)
    crosscheck_leagues(leagues, [], [], [])
    opp_leagues = dedupe_leagues_by_period(rows)
    propagate_crosscheck(leagues, opp_leagues)
    out = tmp_path / "opportunity.csv"
    emit_opportunity_per_league(opp_leagues, out, master_opp_default="006FALLBACK")
    got = {r["Name"]: r["Master Opportunity"] for r in csv.DictReader(open(out))}
    pl = [k for k in got if k.startswith("Premier League")][0]
    other = [k for k in got if not k.startswith("Premier League")][0]
    assert got[pl] == "006PERROW"       # per-row column wins
    assert got[other] == "006FALLBACK"  # run-wide default applied elsewhere


def test_opp_product_resolves_and_conserves_arr(tmp_path):
    """ARR lands on Sales Price; total across a league's OLI rows == its ARR."""
    rows = load_forecast(FIXTURE)
    leagues = dedupe_leagues(rows)
    crosscheck_leagues(leagues, [], [], [])
    opp_leagues = dedupe_leagues_by_period(rows)
    propagate_crosscheck(leagues, opp_leagues)
    pricebook = load_pricebook(config.pricebook_csv())
    out = tmp_path / "opp_product.csv"
    warnings: list[str] = []
    n = emit_opp_product_per_league(opp_leagues, pricebook, out, warnings)
    assert n >= 4
    with out.open() as f:
        reader = csv.DictReader(f)
        assert reader.fieldnames == EXP_OPP_PRODUCT
        by_opp: dict[str, float] = {}
        product_ids = []
        for r in reader:
            by_opp[r["Opportunity Name"]] = by_opp.get(r["Opportunity Name"], 0) + float(r["Sales Price"])
            product_ids.append(r["Product ID"])
    # Premier League (Alice 50k New + Bob 35k Upsell, both H2 2026) → ARR conserved
    pl = [k for k in by_opp if k.startswith("Premier League")][0]
    assert round(by_opp[pl], 2) == 85000.0
    assert all(pid for pid in product_ids)   # every OLI row resolved to a Product ID


def test_csv_source_header_mapping(tmp_path):
    p = tmp_path / "accts.csv"
    p.write_text("Account ID,Account Name,Org Type,Sport,Billing Country\n"
                 "001ZZZ,Serie A,League,Football,Italy\n")
    src = CsvLookupSource(accounts_csv=p, users_csv=None)
    accts = src.fetch_accounts()
    assert accts == [{"Id": "001ZZZ", "Name": "Serie A", "BillingCountry": "Italy",
                      "Org_Type__c": "League", "Sport__c": "Football"}]
    assert src.fetch_users() == []


if __name__ == "__main__":
    import traceback, inspect, tempfile
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            kwargs = {}
            if "tmp_path" in inspect.signature(fn).parameters:
                kwargs["tmp_path"] = Path(tempfile.mkdtemp())
            fn(**kwargs)
            print(f"  ok   {fn.__name__}")
        except Exception:
            failed += 1
            print(f"  FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    raise SystemExit(1 if failed else 0)
