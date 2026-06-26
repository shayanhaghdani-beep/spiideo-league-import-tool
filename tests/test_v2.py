"""Offline smoke tests for the v2 (MCS) flow. No network; uses the WHL sample."""
from pathlib import Path

import pytest

from league_dataload.v2.load_mcs import load_mcs
from league_dataload.v2.pricing import (CurrencyPricebook, PricingChoice,
                                        PricingConfig, PRICING_FREE, PRICING_LIST)
from league_dataload.v2.build_records import (build_plan, MasterOpp,
                                              STRUCT_CAMERAS_AND_SUBS,
                                              STRUCT_CAMERAS_ONLY,
                                              STRUCT_CAMERAS_TO_LEAGUE)
from league_dataload.v2 import mapping as M
from league_dataload.v2.importer import (opp_preflight_warnings, verify_rows,
                                         _oli_counts_by_opp_ref,
                                         contact_preflight_warnings,
                                         duplicate_account_warnings)

ROOT = Path(__file__).resolve().parent.parent
WHL = ROOT / "inputs" / "Camera Orders - WHL - MAIN CAMERA SHEET NEW.csv"
PB = ROOT / "data" / "pricebook.csv"


@pytest.fixture
def records():
    return load_mcs(WHL)


@pytest.fixture
def pricebook():
    return CurrencyPricebook.load(PB, "USD")


def _pricing():
    return PricingConfig(currency="USD",
                         subscription=PricingChoice(mode=PRICING_LIST),
                         cameras=PricingChoice(mode=PRICING_FREE))


def test_loader_parses_whl(records):
    assert len(records) == 23
    r = records[0]
    assert r.team_name == "Brandon Wheat Kings"
    assert len(r.active_cameras) == 7
    assert r.subscription == "Spiideo Perform LITE"
    assert r.email_domain == "wheatkings.com"


def test_pricing_gate(pricebook):
    pe = pricebook.get("Spiideo Perform LITE")
    assert pe is not None
    assert PricingChoice(mode=PRICING_FREE).unit_price(pe) == 0.0
    assert PricingChoice(mode=PRICING_LIST).unit_price(pe) == pe.list_price


def test_tax_field_eu_vs_noneu():
    assert M.tax_field_for("Canada") == M.ACCOUNT_FIELDS["tax_id_noneu"]
    assert M.tax_field_for("Sweden") == M.ACCOUNT_FIELDS["tax_id_eu"]


def test_country_code():
    assert M.country_code("Canada") == "CA"
    assert M.country_code("USA") == "US"


def _plan(records, pricebook, structure):
    return build_plan(
        records, structure=structure, currency="USD",
        master=MasterOpp(opp_id="006X", owner_id="005X", close_date="2026-06-01",
                         account_level="Level 1"),
        pricing=_pricing(), pricebook=pricebook, team_gender="Mens",
        record_type_id="012RT", match_ids={})


def test_structure_1_counts(records, pricebook):
    p = _plan(records, pricebook, STRUCT_CAMERAS_AND_SUBS)
    assert len(p.by_object("Account")) == 23
    assert len(p.by_object("Contact")) == 23
    assert len(p.by_object("Opportunity")) == 23
    # 23 subs + 23*7 cameras
    assert len(p.by_object("OpportunityLineItem")) == 23 + 23 * 7
    assert not p.warnings


def test_structure_2_has_no_subscription_oli(records, pricebook):
    p = _plan(records, pricebook, STRUCT_CAMERAS_ONLY)
    assert len(p.by_object("OpportunityLineItem")) == 23 * 7


def test_structure_3_olis_only_on_master(records, pricebook):
    p = _plan(records, pricebook, STRUCT_CAMERAS_TO_LEAGUE)
    assert not p.by_object("Account")
    assert not p.by_object("Opportunity")
    olis = p.by_object("OpportunityLineItem")
    assert len(olis) == 23 * 7
    assert all(o.parents[M.OLI_FIELDS["opportunity_id"]] == "006X" for o in olis)


def test_opp_is_closed_won_with_pricebook(records, pricebook):
    p = _plan(records, pricebook, STRUCT_CAMERAS_AND_SUBS)
    opp = p.by_object("Opportunity")[0]
    f = opp.fields
    assert f[M.OPPORTUNITY_FIELDS["stage"]] == "Closed Won"
    assert f[M.OPPORTUNITY_FIELDS["forecast_category"]] == "Closed"
    assert f[M.OPPORTUNITY_FIELDS["pricebook2_id"]] == M.YOUNIUM_PRICEBOOK2_ID
    assert f[M.OPPORTUNITY_FIELDS["close_date"]] == "2026-06-01"


def test_camera_oli_sets_sport_for_position_dependency(records, pricebook):
    p = _plan(records, pricebook, STRUCT_CAMERAS_AND_SUBS)
    cam = [o for o in p.by_object("OpportunityLineItem")
           if M.OLI_FIELDS["position_of_field"] in o.fields][0]
    assert cam.fields[M.OLI_FIELDS["sport"]] == "Ice Hockey"


# --- 2026-06-22 change set ------------------------------------------------

def test_opp_event_source_default(records, pricebook):
    opp = _plan(records, pricebook, STRUCT_CAMERAS_AND_SUBS).by_object("Opportunity")[0]
    assert opp.fields[M.OPPORTUNITY_FIELDS["event_source"]] == "Not applicable"


def test_opp_system_admin_is_the_line_contact(records, pricebook):
    opp = _plan(records, pricebook, STRUCT_CAMERAS_AND_SUBS).by_object("Opportunity")[0]
    sa = opp.parents.get(M.OPPORTUNITY_FIELDS["system_admin"])
    assert sa == opp.parents.get(M.OPPORTUNITY_FIELDS["primary_contact"])
    assert str(sa).startswith("CON:")


def test_account_gets_invoice_and_shipping_phone(records, pricebook):
    acct = _plan(records, pricebook, STRUCT_CAMERAS_AND_SUBS).by_object("Account")[0]
    phone = records[0].contact_phone
    assert phone  # the WHL sheet carries a phone for every club
    assert acct.fields[M.ACCOUNT_FIELDS["invoice_phone"]] == phone
    assert acct.fields[M.ACCOUNT_FIELDS["shipping_phone"]] == phone


def test_camera_order_type_default(records, pricebook):
    cams = [o for o in _plan(records, pricebook, STRUCT_CAMERAS_AND_SUBS)
            .by_object("OpportunityLineItem")
            if M.OLI_FIELDS["position_of_field"] in o.fields]
    assert cams
    assert all(o.fields.get(M.OLI_FIELDS["camera_order_type"])
               == "Shipment - No additional cost" for o in cams)


def test_no_auto_camera_alt_shipping(records, pricebook):
    olis = _plan(records, pricebook, STRUCT_CAMERAS_AND_SUBS).by_object("OpportunityLineItem")
    assert all(M.OLI_FIELDS["camera_shipping_address"] not in o.fields for o in olis)


def test_younium_charge_type_name_and_list_price(records, pricebook):
    p = _plan(records, pricebook, STRUCT_CAMERAS_AND_SUBS)
    olis = p.by_object("OpportunityLineItem")
    cams = [o for o in olis if M.OLI_FIELDS["position_of_field"] in o.fields]
    subs = [o for o in olis if M.OLI_FIELDS["position_of_field"] not in o.fields]
    ct, cn, lp = (M.OLI_FIELDS["younium_charge_type"], M.OLI_FIELDS["younium_charge_name"],
                  M.OLI_FIELDS["younium_list_price"])
    # hardware -> One-off (so cost reaches GP); subscription -> Recurring
    assert cams and all(o.fields[ct] == "One-off" for o in cams)
    assert subs and all(o.fields[ct] == "Recurring" for o in subs)
    # charge name = product name on every line; list price populated
    assert all(o.fields.get(cn) for o in olis)
    assert all(o.fields.get(lp) for o in olis)
    sub = subs[0]
    assert sub.fields[cn] == "Spiideo Perform LITE"  # = the sheet's product name


def test_unique_oli_product_id_per_order(records, pricebook):
    from collections import defaultdict
    p = _plan(records, pricebook, STRUCT_CAMERAS_AND_SUBS)
    F, OPP = M.OLI_FIELDS["id_unique_oli_product"], M.OLI_FIELDS["opportunity_id"]
    byopp = defaultdict(list)
    for o in p.by_object("OpportunityLineItem"):
        byopp[o.parents[OPP]].append(o.fields[F])
    assert byopp
    for opp, seqs in byopp.items():
        assert len(seqs) == len(set(seqs)), f"duplicate seq within {opp}"   # unique within order
        assert sorted(int(s) for s in seqs) == list(range(1, len(seqs) + 1))  # contiguous 1..N strings


def test_shipping_region_and_camera_keys():
    assert M.shipping_region("USA") == "US"
    assert M.shipping_region("United States") == "US"
    assert M.shipping_region("Canada") == "Americas-nonUS"
    assert M.shipping_region("Sweden") == "Sweden"
    assert M.shipping_region("Germany") == "Europe"
    assert M.shipping_camera_key("S-Line WIDE MK III (with mic)") == "s-line wide mk iii"
    assert M.shipping_camera_key("Spiideo stream encoder") == "encoder"
    # sheet camera key must round-trip to the charge-plan camera key (so the map matches)
    us = "Shipping & handling with Tariffs to US for S-Line POINT MK III"
    ca = "Shipping & handling to Americas (Not including US), APAC, ME, AF of Encoder"
    assert M.shipping_region_from_plan(us) == "US"
    assert M.shipping_camera_from_plan(us) == "s-line point mk iii"
    assert M.shipping_region_from_plan(ca) == "Americas-nonUS"
    assert M.shipping_camera_from_plan(ca) == "encoder"


def test_master_shipping_lines_optin(records, pricebook):
    SHIP, F = "OLI:MASTER:ship", M.OLI_FIELDS
    # default OFF -> no master shipping lines
    off = _plan(records, pricebook, STRUCT_CAMERAS_AND_SUBS)
    assert not [r for r in off.by_object("OpportunityLineItem") if r.ref_key.startswith(SHIP)]
    # map covering every (camera, region) actually present in the sheet
    cam_keys = {M.shipping_camera_key(c.type) for rr in records for c in rr.active_cameras}
    regions = {M.shipping_region(rr.billing_country) for rr in records}
    smap = {(ck, rg): {"product2_id": "01tSHIP", "pbe_id": "01uSHIP", "charge_name": "Shipping"}
            for ck in cam_keys for rg in regions}
    on = build_plan(records, structure=STRUCT_CAMERAS_AND_SUBS, currency="USD",
                    master=MasterOpp(opp_id="006MASTER", owner_id="005X", close_date="2026-06-01"),
                    pricing=_pricing(), pricebook=pricebook, team_gender="Mens",
                    record_type_id="012RT", master_shipping=True, shipping_map=smap, match_ids={})
    ship = [r for r in on.by_object("OpportunityLineItem") if r.ref_key.startswith(SHIP)]
    assert ship
    for r in ship:
        assert r.parents[F["opportunity_id"]] == "006MASTER"   # on the master opp
        assert r.fields[F["unit_price"]] == 0.0                # not billed to the customer
        assert r.fields[F["younium_charge_type"]] == "One-off"  # cost rolls into GP
        assert F["id_unique_oli_product"] not in r.fields      # blank -> Younium assigns on master
    # total shipped units == total cameras across all clubs
    assert (sum(r.fields[F["quantity"]] for r in ship)
            == sum(len(rr.active_cameras) for rr in records))


# --- 2026-06-23 attach-to-existing-opp mode (SvFF) ------------------------

def test_attach_mode_updates_existing_opp_instead_of_creating(records, pricebook):
    """A row carrying an Opportunity SF ID UPDATES that opp (Closed Won + pricebook +
    OLIs) instead of creating one; Name/CloseDate/Owner/RecordType are left untouched,
    the account is pinned by Customer SF ID, and the OLIs hang off the existing opp."""
    rec = records[0]
    rec.customer_sf_id = "001EXISTINGACC"
    rec.opportunity_sf_id = "006EXISTINGOPP"
    p = build_plan([rec], structure=STRUCT_CAMERAS_AND_SUBS, currency="USD",
                   master=MasterOpp(opp_id="", owner_id="005OWNER"),
                   pricing=_pricing(), pricebook=pricebook, team_gender="Mens",
                   voucher=True, record_type_id="012RT", match_ids={})

    opp = p.by_object("Opportunity")[0]
    assert opp.operation == "upsert" and opp.sf_id == "006EXISTINGOPP"
    f = opp.fields
    assert f[M.OPPORTUNITY_FIELDS["stage"]] == "Closed Won"
    assert f[M.OPPORTUNITY_FIELDS["forecast_category"]] == "Closed"
    assert f[M.OPPORTUNITY_FIELDS["pricebook2_id"]] == M.YOUNIUM_PRICEBOOK2_ID
    # currency forced to the run currency so the currency-specific OLI prices attach
    assert f[M.OPPORTUNITY_FIELDS["currency"]] == "USD"
    # the placeholder already carries these -> never overwritten in attach mode
    for k in ("name", "close_date", "owner_id", "record_type_id"):
        assert M.OPPORTUNITY_FIELDS[k] not in f
    # contact lookups still set (Contact is created before the Opp)
    assert opp.parents.get(M.OPPORTUNITY_FIELDS["primary_contact"]) == f"CON:{rec.team_name}"

    acc = p.by_object("Account")[0]
    assert acc.operation == "upsert" and acc.sf_id == "001EXISTINGACC"  # pinned, not created

    olis = p.by_object("OpportunityLineItem")
    assert olis and all(o.parents[M.OLI_FIELDS["opportunity_id"]] == opp.ref_key for o in olis)
    subs = [o for o in olis if M.OLI_FIELDS["position_of_field"] not in o.fields]
    cams = [o for o in olis if M.OLI_FIELDS["position_of_field"] in o.fields]
    pe = pricebook.get(rec.subscription)
    assert subs and all(o.fields[M.OLI_FIELDS["unit_price"]] == pe.list_price for o in subs)  # sub = list (both lines)
    assert cams and all(o.fields[M.OLI_FIELDS["unit_price"]] == 0.0 for o in cams)             # cameras free


def test_attach_mode_links_master_without_forcing_account_level(records, pricebook):
    """A Config master opp links the placeholder opp via Master_Opportunity__c; in attach mode
    the master's Level is NOT propagated onto the existing club account (Shayan, 2026-06-24)."""
    rec = records[0]
    rec.customer_sf_id = "001EXISTINGACC"
    rec.opportunity_sf_id = "006EXISTINGOPP"
    p = build_plan([rec], structure=STRUCT_CAMERAS_AND_SUBS, currency="USD",
                   master=MasterOpp(opp_id="006MASTER", owner_id="005X", account_level="Level 1"),
                   pricing=_pricing(), pricebook=pricebook, team_gender="Mens",
                   record_type_id="012RT", match_ids={})
    opp = p.by_object("Opportunity")[0]
    assert opp.fields[M.OPPORTUNITY_FIELDS["master_opportunity"]] == "006MASTER"
    acc = p.by_object("Account")[0]
    assert M.ACCOUNT_FIELDS["level"] not in acc.fields   # league Level not forced onto a pinned acct


def test_attach_mode_sets_team_gender_from_row(records, pricebook):
    """Attach mode writes Team Gender from the sheet's per-row value, normalised to the SF
    picklist (Shayan, 2026-06-24 — it was previously dropped on attach)."""
    rec = records[0]
    rec.opportunity_sf_id = "006EXISTINGOPP"
    rec.gender = "Women's"
    p = build_plan([rec], structure=STRUCT_CAMERAS_AND_SUBS, currency="USD",
                   master=MasterOpp(opp_id=""), pricing=_pricing(), pricebook=pricebook,
                   team_gender="Mens", record_type_id="012RT", match_ids={})
    opp = p.by_object("Opportunity")[0]
    assert opp.fields[M.OPPORTUNITY_FIELDS["team_gender"]] == "Womens"   # per-row beats Config
    assert opp.fields[M.OPPORTUNITY_FIELDS["sport"]] == rec.sport        # Sport set too (create-path parity)


def test_extract_sf_id_from_url_or_bare():
    from league_dataload.v2.load_mcs import _extract_sf_id
    url = ("https://spiideo.lightning.force.com/lightning/r/Opportunity/"
           "006QD00000qblnIYAQ/view?0.source=alohaHeader")
    assert _extract_sf_id(url) == "006QD00000qblnIYAQ"          # "SF OPP LINK" URL form
    assert _extract_sf_id("006QD00000qblnIYAQ") == "006QD00000qblnIYAQ"   # bare Id
    assert _extract_sf_id("") == ""


def test_loader_parses_league_exchange_and_opp_link():
    from league_dataload.v2.load_mcs import parse_rows
    header = ["Team Name", "SvFF League Exchange", "SF OPP LINK"]
    row = ["Test FC", "YES",
           "https://x.lightning.force.com/lightning/r/Opportunity/006QD00000qblnIYAQ/view"]
    r = parse_rows([header, row])[0]
    assert r.wants_league_exchange is True
    assert r.opportunity_sf_id == "006QD00000qblnIYAQ"          # extracted from the URL


def test_svff_league_exchange_adds_order_note(records, pricebook):
    """'SvFF League Exchange' = yes -> Order Notes = 'add to SvFF LE'; blank -> field unset
    (Shayan, 2026-06-24). Works in attach mode (and create)."""
    on = M.OPPORTUNITY_FIELDS["order_notes"]
    rec = records[0]
    rec.opportunity_sf_id = "006EXISTINGOPP"
    rec.league_exchange = "YES"
    p = build_plan([rec], structure=STRUCT_CAMERAS_AND_SUBS, currency="USD",
                   master=MasterOpp(opp_id=""), pricing=_pricing(), pricebook=pricebook,
                   team_gender="Mens", record_type_id="012RT", match_ids={})
    assert p.by_object("Opportunity")[0].fields[on] == "add to SvFF LE"

    rec2 = records[1]
    rec2.opportunity_sf_id = "006OTHEROPP"
    rec2.league_exchange = ""        # blank -> no note
    p2 = build_plan([rec2], structure=STRUCT_CAMERAS_AND_SUBS, currency="USD",
                    master=MasterOpp(opp_id=""), pricing=_pricing(), pricebook=pricebook,
                    team_gender="Mens", record_type_id="012RT", match_ids={})
    assert on not in p2.by_object("Opportunity")[0].fields


def test_attach_mode_owner_and_stage_override(records, pricebook):
    """SvFF: a Config owner + stage/forecast override re-owns the existing opp and leaves it
    at the latest OPEN stage (rep closes it himself) instead of Closed Won (Shayan, 2026-06-23)."""
    rec = records[0]
    rec.opportunity_sf_id = "006EXISTINGOPP"
    p = build_plan([rec], structure=STRUCT_CAMERAS_AND_SUBS, currency="USD",
                   master=MasterOpp(opp_id=""), pricing=_pricing(), pricebook=pricebook,
                   team_gender="Mens", voucher=True, record_type_id="012RT", match_ids={},
                   opp_owner_id="005AMIR", opp_stage="Decision & Signature", opp_forecast="Commit")
    f = p.by_object("Opportunity")[0].fields
    assert f[M.OPPORTUNITY_FIELDS["stage"]] == "Decision & Signature"
    assert f[M.OPPORTUNITY_FIELDS["forecast_category"]] == "Commit"
    assert f[M.OPPORTUNITY_FIELDS["owner_id"]] == "005AMIR"


# --- 2026-06-23 preflight cross-check + post-push verification (baked in) ---------

def test_preflight_flags_existing_olis_currency_missing_and_closed():
    attach = [{"id": "006A", "label": "Bodens", "planned_olis": 3},
              {"id": "006B", "label": "Skara", "planned_olis": 3},
              {"id": "006C", "label": "Ghost", "planned_olis": 3}]
    existing = {
        "006A": {"stage": "Discover Challenges", "isclosed": False, "currency": "SEK", "oli_count": 0},
        "006B": {"stage": "Closed Won", "isclosed": True, "currency": "EUR", "oli_count": 3},
        # 006C absent -> not found
    }
    blob = " ".join(opp_preflight_warnings(attach, existing, "SEK"))
    assert "NOT FOUND" in blob                       # 006C bad Id
    assert "ALREADY has 3 line item" in blob         # 006B double-load guard
    assert "EUR" in blob and "SEK" in blob           # 006B currency switch
    assert "already CLOSED" in blob                  # 006B closed
    assert "Bodens" not in blob                       # 006A clean -> silent


def test_verify_rows_flags_short_olis_and_currency():
    targets = [{"id": "006A", "label": "A", "expected_olis": 3},
               {"id": "006B", "label": "B", "expected_olis": 3},
               {"id": "006C", "label": "C", "expected_olis": 3}]
    state = {
        "006A": {"name": "A", "stage": "Decision & Signature", "currency": "SEK", "amount": 0, "oli_count": 3},
        "006B": {"name": "B", "stage": "Decision & Signature", "currency": "EUR", "amount": 0, "oli_count": 3},
        "006C": {"name": "C", "stage": "Decision & Signature", "currency": "SEK", "amount": 0, "oli_count": 0},
    }
    rows, problems = verify_rows(targets, state, "SEK")
    assert len(rows) == 3
    blob = " ".join(problems)
    assert "currency EUR" in blob          # B wrong currency
    assert "0/3 line items" in blob        # C short on OLIs
    assert not any(p.startswith("A:") for p in problems)   # A clean


def test_oli_counts_by_opp_ref_matches_total(records, pricebook):
    rec = records[0]
    rec.opportunity_sf_id = "006EXISTINGOPP"
    p = build_plan([rec], structure=STRUCT_CAMERAS_AND_SUBS, currency="USD",
                   master=MasterOpp(opp_id=""), pricing=_pricing(), pricebook=pricebook,
                   team_gender="Mens", voucher=True, record_type_id="012RT", match_ids={})
    counts = _oli_counts_by_opp_ref(p)
    # Opp ref-keys are per ROW (team_name#rowindex) so multi-team clubs don't collide.
    assert counts[f"OPP:{rec.team_name}#0"] == len(p.by_object("OpportunityLineItem"))


def test_multi_team_same_club_opps_do_not_collide(pricebook):
    """Regression (Shayan, 2026-06-23): a college club with several teams shares ONE
    account but gets one opp PER team/sport, and each opp must keep its OWN OLIs. Before
    the per-row ref-key fix, all teams' OLIs piled onto the last opp created for the club."""
    from league_dataload.v2.load_mcs import ClubRecord, Camera
    def _team(sport):
        return ClubRecord(team_name="Test University", order_name=f"Test University {sport}",
                          gender="Mens", sport=sport, customer_sf_id="001PINNED",
                          contact_name="Jane Doe", contact_email="jane@test.edu",
                          contact_phone="555", subscription="Spiideo Replay PRO",
                          cameras=[Camera(index=1, scene="Gym", type="X-Line POINT MK II",
                                          position="Center")])
    recs = [_team("Basketball"), _team("Volleyball")]
    p = build_plan(recs, structure=STRUCT_CAMERAS_AND_SUBS, currency="USD",
                   master=MasterOpp(opp_id="006MASTER"), pricing=_pricing(), pricebook=pricebook,
                   team_gender="Mens", voucher=True, record_type_id="012RT", match_ids={})
    opps = p.by_object("Opportunity")
    assert len(opps) == 2                               # one opp per team
    opp_refs = {o.ref_key for o in opps}
    assert len(opp_refs) == 2                           # distinct ref-keys (no collision)
    counts = _oli_counts_by_opp_ref(p)                  # every OLI parented to an opp ref
    assert set(counts) == opp_refs
    assert all(counts[r] > 0 for r in opp_refs)


def test_contact_preflight_flags_email_on_a_different_account():
    # email-on-a-different-account case: the contact's email lives on an empty DUP
    # account, not the pinned one (synthetic data).
    contacts = [{"email": "office@dupclub.test", "label": "Dup FC", "pinned_acct": "001REAL"},
                {"email": "office@cleanclub.test", "label": "Clean FC", "pinned_acct": "001CLEAN"}]
    existing = {
        "office@dupclub.test": [{"id": "003IDA", "account_id": "001DUP"}],    # on a different acct
        "office@cleanclub.test": [{"id": "003OK", "account_id": "001CLEAN"}],  # already on the pinned acct
    }
    out = contact_preflight_warnings(contacts, existing)
    assert len(out) == 1 and "Dup FC" in out[0] and "001DUP" in out[0]


def test_duplicate_account_by_name_warning():
    pinned = [{"id": "001REAL", "name": "Dup FC", "label": "Dup FC"},
              {"id": "001UNIQ", "name": "Unique IF", "label": "Unique IF"}]
    name_ids = {"Dup FC": ["001REAL", "001DUP"], "Unique IF": ["001UNIQ"]}
    out = duplicate_account_warnings(pinned, name_ids)
    assert len(out) == 1 and "Dup FC" in out[0] and "001DUP" in out[0]


def test_subscription_olis_bill_annually_cameras_have_no_period(records, pricebook):
    """Subscription lines (incl. the voucher -1 line) bill Annually; one-off camera lines
    carry no billing period (Shayan, 2026-06-24 — bulk import else defaults Monthly)."""
    p = build_plan(records, structure=STRUCT_CAMERAS_AND_SUBS, currency="USD",
                   master=MasterOpp(opp_id="006X", owner_id="005X", close_date="2026-06-01"),
                   pricing=_pricing(), pricebook=pricebook, team_gender="Mens",
                   voucher=True, record_type_id="012RT", match_ids={})
    bp, pp = M.OLI_FIELDS["billing_period"], M.OLI_FIELDS["price_period"]
    olis = p.by_object("OpportunityLineItem")
    subs = [o for o in olis if M.OLI_FIELDS["position_of_field"] not in o.fields]
    cams = [o for o in olis if M.OLI_FIELDS["position_of_field"] in o.fields]
    # subscriptions (incl. the voucher -1 line) bill annually + carry an annual price period
    assert subs and all(o.fields.get(bp) == "Annual" for o in subs)
    assert subs and all(o.fields.get(pp) == "Annual" for o in subs)
    assert cams and all(bp not in o.fields and pp not in o.fields for o in cams)  # one-off hardware


def test_subscription_billing_period_override(records, pricebook):
    """Always Annual UNLESS the Config 'Subscription billing period' overrides it (Shayan, 2026-06-24)."""
    bp, pp = M.OLI_FIELDS["billing_period"], M.OLI_FIELDS["price_period"]
    p = build_plan(records, structure=STRUCT_CAMERAS_AND_SUBS, currency="USD",
                   master=MasterOpp(opp_id="006X", owner_id="005X", close_date="2026-06-01"),
                   pricing=_pricing(), pricebook=pricebook, team_gender="Mens",
                   voucher=True, record_type_id="012RT", match_ids={}, sub_billing_period="Monthly")
    subs = [o for o in p.by_object("OpportunityLineItem")
            if M.OLI_FIELDS["position_of_field"] not in o.fields]
    assert subs and all(o.fields.get(bp) == "Monthly" and o.fields.get(pp) == "Monthly" for o in subs)


def test_voucher_forces_subscription_list_price_on_both_lines(records, pricebook):
    # subscription pricing = FREE, but a voucher deal must still put the LIST price on
    # BOTH the +1 and the -1 line (they net to $0 but show the real value).
    free_sub = PricingConfig(currency="USD",
                             subscription=PricingChoice(mode=PRICING_FREE),
                             cameras=PricingChoice(mode=PRICING_FREE))
    p = build_plan(records, structure=STRUCT_CAMERAS_AND_SUBS, currency="USD",
                   master=MasterOpp(opp_id="006X", owner_id="005X", close_date="2026-06-01"),
                   pricing=free_sub, pricebook=pricebook, team_gender="Mens",
                   voucher=True, record_type_id="012RT", match_ids={})
    subs = [o for o in p.by_object("OpportunityLineItem")
            if M.OLI_FIELDS["position_of_field"] not in o.fields]
    assert len(subs) == 23 * 2  # a +1 and a -1 voucher line per club
    pe = pricebook.get(records[0].subscription)
    assert pe.list_price > 0
    assert all(o.fields[M.OLI_FIELDS["unit_price"]] == pe.list_price for o in subs)
    vouchers = [o for o in subs if o.fields.get(M.OLI_FIELDS["voucher"]) == "true"]
    assert len(vouchers) == 23
    assert all(o.fields[M.OLI_FIELDS["quantity"]] == -1 for o in vouchers)
