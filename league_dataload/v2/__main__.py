"""League Import Tool v2 CLI.

Two commands:

  gen     Phase A -- generate the fillable order sheet (.xlsx with dropdowns).
  import  Phase B -- read a filled sheet, crosscheck clubs, show the change list,
          and (only with --live + confirmation) push to Salesforce.

Examples:
  python3 -m league_dataload.v2 gen --out outputs/WHL.xlsx --league WHL
  python3 -m league_dataload.v2 import --sheet outputs/WHL.xlsx          # dry-run
  python3 -m league_dataload.v2 import --sheet outputs/WHL.xlsx --live   # writes to SF
"""
from __future__ import annotations

import argparse
import subprocess
import sys

from . import mapping as M
from . import picklist_deps as PD
from .build_records import (MasterOpp, build_plan, STRUCT_CAMERAS_TO_LEAGUE)
from .gen_sheet import generate
from .importer import Importer
from .load_mcs import load_config, load_mcs
from .pricing import (CurrencyPricebook, PricingChoice, PricingConfig,
                      PRICING_DISCOUNT, PRICING_MODES)

DEFAULT_PRICEBOOK = "data/pricebook.csv"
DEFAULT_ORG = "spiideo"


# ---------------------------------------------------------------------------
# SF helpers (live reads via sf CLI)

def _sf_query(soql: str, org: str) -> list[dict]:
    import json
    p = subprocess.run(["sf", "data", "query", "--query", soql,
                        "--target-org", org, "--json"],
                       capture_output=True, text=True, timeout=180)
    out = json.loads(p.stdout or "{}")
    if p.returncode != 0:
        raise SystemExit(f"SF query failed: {out.get('message') or p.stderr}")
    return out["result"]["records"]


def fetch_accounts(org: str) -> list[dict]:
    rows = _sf_query("SELECT Id, Name, Website FROM Account", org)
    return [{"Name": r["Name"], "Id": r["Id"], "domain": r.get("Website") or ""}
            for r in rows]


def fetch_master_opp(opp_id: str, org: str) -> MasterOpp:
    rows = _sf_query(
        "SELECT Id, OwnerId, CloseDate, Account.Level__c "
        f"FROM Opportunity WHERE Id = '{opp_id}'", org)
    if not rows:
        raise SystemExit(f"Master opp {opp_id} not found in org {org}")
    r = rows[0]
    acct = r.get("Account") or {}
    return MasterOpp(opp_id=opp_id, owner_id=r.get("OwnerId") or "",
                     close_date=r.get("CloseDate") or "",
                     account_level=acct.get("Level__c") or "")


def resolve_user(name_or_id: str, org: str) -> str:
    """Resolve the "Opportunity Owner" Config value to an active SF User Id.
    Accepts a 15/18-char User Id (starts 005) verbatim, or a full Name to look up.
    Returns '' if blank (caller falls back to the master opp owner)."""
    v = (name_or_id or "").strip()
    if not v:
        return ""
    if v.startswith("005") and len(v) in (15, 18) and " " not in v:
        return v
    safe = v.replace("'", r"\'")
    rows = _sf_query(
        f"SELECT Id, Name FROM User WHERE Name = '{safe}' AND IsActive = true", org)
    if not rows:
        raise SystemExit(
            f"Config 'Opportunity Owner' = {v!r} matched no active SF user. "
            "Use the exact full name or paste the User Id (005...).")
    if len(rows) > 1:
        ids = ", ".join(r["Id"] for r in rows)
        raise SystemExit(
            f"Config 'Opportunity Owner' = {v!r} matched {len(rows)} users ({ids}). "
            "Paste the specific User Id (005...) instead.")
    return rows[0]["Id"]


def resolve_record_type(dev_name: str, org: str) -> str:
    try:
        rows = _sf_query("SELECT Id FROM RecordType WHERE SobjectType='Opportunity' "
                         f"AND DeveloperName='{dev_name}'", org)
        if rows:
            return rows[0]["Id"]
    except SystemExit:
        pass
    return M.OPP_RECORD_TYPE_IDS.get(dev_name, "")


def resolve_shipping_products(org: str, currency: str) -> dict:
    """For the OPT-IN master-opp shipping feature: live-resolve the shipping products
    into {(camera_key, region): {product2_id, pbe_id, charge_name}}. Excludes
    refurbished variants. Region/camera parsing lives in mapping.py."""
    names = "', '".join(M.SHIPPING_PRODUCT_NAMES)
    rows = _sf_query(
        "SELECT Id, Name, Younium__Y_Younium_Charge_plan_name__c, "
        f"Younium__Y_Younium_Charge_name__c FROM Product2 WHERE Name IN ('{names}') "
        "AND IsActive = true", org)
    prod: dict[str, tuple] = {}
    for r in rows:
        plan = r.get("Younium__Y_Younium_Charge_plan_name__c") or ""
        if "refurb" in plan.lower():
            continue
        region = M.shipping_region_from_plan(plan)
        cam = M.shipping_camera_from_plan(plan)
        if not region or not cam:
            continue
        prod[r["Id"]] = (cam, region,
                         r.get("Younium__Y_Younium_Charge_name__c") or r.get("Name"))
    if not prod:
        return {}
    ids = "', '".join(prod)
    pbes = _sf_query(
        "SELECT Id, Product2Id FROM PricebookEntry "
        f"WHERE Pricebook2Id = '{M.YOUNIUM_PRICEBOOK2_ID}' AND CurrencyIsoCode = '{currency}' "
        f"AND Product2Id IN ('{ids}')", org)
    out: dict[tuple, dict] = {}
    for p in pbes:
        info = prod.get(p["Product2Id"])
        if info:
            cam, region, cname = info
            out[(cam, region)] = {"product2_id": p["Product2Id"],
                                  "pbe_id": p["Id"], "charge_name": cname}
    return out


def resolve_matches(records, accounts, *, normaliser):
    """team_name -> matched SF Account Id. Exact-name beats exact-domain-to-other."""
    from ..clubmatch import matching  # noqa: import here to keep gen lightweight
    truth = [{"name": r.team_name, "domain": r.email_domain} for r in records]
    matches, _, _ = matching.match(
        truth, accounts, truth_name_key="name", truth_domain_key="domain",
        crm_name_key="Name", crm_id_key="Id", crm_domain_key="domain",
        normaliser=normaliser)
    out: dict[str, str] = {}
    review: list[str] = []
    for r in records:
        ms = matches.get(r.team_name, [])
        exact = [m for m in ms if m["match_type"] == "exact"]
        if exact:
            out[r.team_name] = exact[0]["crm_row"]["Id"]
        elif len(ms) == 1:
            out[r.team_name] = ms[0]["crm_row"]["Id"]
        elif len(ms) > 1:
            review.append(f"{r.team_name}: {len(ms)} candidate matches -> REVIEW")
    return out, review


# ---------------------------------------------------------------------------
# config-tab parsing

def _cfg_get(cfg: dict, *needles: str, default: str = "") -> str:
    for k, v in cfg.items():
        if all(n.lower() in k.lower() for n in needles):
            return v
    return default


def _pricing_choice(mode: str, discount_price: str) -> PricingChoice:
    mode = (mode or "list").strip().lower()
    if mode not in PRICING_MODES:
        mode = "list"
    prices = {}
    if mode == PRICING_DISCOUNT and discount_price:
        # single blended price keyed later by product; left for the operator to
        # extend per-model in the Config tab. Stored under "*" sentinel.
        try:
            prices["*"] = float(discount_price)
        except ValueError:
            pass
    return PricingChoice(mode=mode, discount_prices=prices)


# ---------------------------------------------------------------------------
# commands

def cmd_gen(args) -> None:
    out = generate(args.pricebook, args.out, league_name=args.league, rows=args.rows)
    print(f"Generated order sheet: {out}")
    print("Set the Config tab (structure, master opp, currency, pricing, gender), "
          "then send to sales to fill the green columns.")


def cmd_import(args) -> None:
    records = load_mcs(args.sheet)
    cfg = load_config(args.sheet)
    if not records:
        raise SystemExit("No club rows found in the sheet.")

    structure = int((_cfg_get(cfg, "Deal Structure", default="1") or "1").strip()[0])
    master_id = _cfg_get(cfg, "Master Opportunity").strip()
    currency = (_cfg_get(cfg, "Currency", default="EUR") or "EUR").strip()
    gender = _cfg_get(cfg, "Team Gender").strip()
    league = _cfg_get(cfg, "League").strip()
    voucher = _cfg_get(cfg, "Voucher").strip().lower() in ("yes", "true", "1", "y")
    sub_mode = _cfg_get(cfg, "Subscription pricing", default="list")
    cam_mode = _cfg_get(cfg, "Cameras pricing", default="list")
    sub_disc = _cfg_get(cfg, "Subscription discount")
    opp_owner_cfg = _cfg_get(cfg, "Opportunity Owner").strip()
    # Opp stage/forecast override (blank = default Closed Won / Closed). SvFF sets these to
    # "Decision & Signature" / "Commit" so the rep (Amir) closes the deal himself (2026-06-23).
    opp_stage = _cfg_get(cfg, "Opportunity Stage").strip()
    opp_forecast = _cfg_get(cfg, "Forecast", "Category").strip()
    # Subscriptions bill ANNUALLY unless the sheet says otherwise (Shayan, 2026-06-24).
    sub_billing_period = _cfg_get(cfg, "Subscription", "billing").strip()
    # OPT-IN (default OFF): add per camera×region shipping lines to the MASTER opp.
    # Sales reps normally add shipping themselves, so leave off unless explicitly asked.
    master_shipping = _cfg_get(cfg, "master", "shipping").strip().lower() in ("yes", "true", "1", "y")

    # ATTACH MODE (Shayan, 2026-06-23): rows carrying an Opportunity SF ID update that
    # existing opp instead of creating one, so no master opp is needed (the opps already
    # exist; e.g. the SvFF "<club> – Rollout 2026" placeholders).
    attach_existing = any((r.opportunity_sf_id or "").strip() for r in records)
    if structure != STRUCT_CAMERAS_TO_LEAGUE and not master_id and not attach_existing:
        raise SystemExit("Config: Master Opportunity ID is required (never created).")

    print(f"Sheet: {args.sheet}")
    print(f"Structure={structure}  Currency={currency}  Master opp={master_id or '(n/a)'}  "
          f"Gender={gender or '(unset)'}  Pricing: sub={sub_mode} cam={cam_mode}  "
          f"Voucher={'YES (sub qty -1)' if voucher else 'no'}  "
          f"MasterShip={'YES' if master_shipping else 'no'}  "
          f"Stage={opp_stage or '(Closed Won)'}{('/'+opp_forecast) if opp_forecast else ''}  "
          f"SubBilling={sub_billing_period or '(Annual)'}")

    pricebook = CurrencyPricebook.load(args.pricebook, currency)
    pricing = PricingConfig(currency=currency,
                            subscription=_pricing_choice(sub_mode, sub_disc),
                            cameras=_pricing_choice(cam_mode, ""))

    master = (fetch_master_opp(master_id, args.org) if master_id
              else MasterOpp(opp_id=""))
    if not master_id and attach_existing:
        # No master in attach mode -> new contacts inherit the EXISTING opp's owner
        # (never the running user; Shayan guardrail). Use the first pinned opp's owner;
        # a single SvFF batch shares one owner.
        first_opp = next((r.opportunity_sf_id.strip() for r in records
                          if (r.opportunity_sf_id or "").strip()), "")
        if first_opp:
            master.owner_id = fetch_master_opp(first_opp, args.org).owner_id
            print(f"Attach mode: no master opp; contacts will be owned by the existing "
                  f"opp owner [{master.owner_id or '(none)'}]")
    rt_id = resolve_record_type(
        M.OPPORTUNITY_DEFAULTS["record_type_developer_name"], args.org)

    # Owner structure (always set, per Shayan 2026-06-19):
    #   - child opps  -> the CSM from the "Opportunity Owner" Config prompt
    #                    (falls back to the master opp owner if left blank)
    #   - contacts    -> ALWAYS the master opp owner (never the running user)
    opp_owner_id = resolve_user(opp_owner_cfg, args.org)
    print(f"Owner structure → opps: {opp_owner_cfg or '(inherit master owner)'}"
          f"{(' ['+opp_owner_id+']') if opp_owner_id else ''}  |  "
          f"contacts: master opp owner [{master.owner_id or '(none)'}]")

    from ..clubmatch.normalise import normalise as _norm
    accounts = fetch_accounts(args.org)
    match_ids, review = resolve_matches(records, accounts, normaliser=_norm)
    pinned_ct = sum(1 for r in records
                    if (r.customer_sf_id or "").strip() and r.team_name not in match_ids)
    resolved = {r.team_name for r in records
                if r.team_name in match_ids or (r.customer_sf_id or "").strip()}
    print(f"Resolved {len(resolved)}/{len(records)} clubs to existing SF accounts "
          f"({len(match_ids)} by name, {pinned_ct} pinned by Customer SF ID); "
          f"{len(records)-len(resolved)} new.")
    for r in review:
        print(f"  ⚠️  {r}")

    # HubSpot second pass: a club not matched in Salesforce but ALREADY in HubSpot
    # is almost always pending a HubSpot->SF sync, not genuinely new. Creating it
    # would make a duplicate SF account (and trip SF duplicate rules). Flag those
    # and EXCLUDE them from this import — sync HubSpot->SF first, then re-run.
    from .hubspot_lookup import load_hubspot_companies
    hs_flagged: dict[str, str] = {}
    hs_companies = load_hubspot_companies(args.hubspot_csv)
    if hs_companies:
        # A club pinned by an explicit Customer SF ID is already resolved -> never treat
        # it as "unmatched" / flag it for HubSpot exclusion (Shayan, 2026-06-23).
        pinned = {r.team_name for r in records if (r.customer_sf_id or "").strip()}
        unmatched = [r for r in records
                     if r.team_name not in match_ids and r.team_name not in pinned]
        hs_ids, _ = resolve_matches(unmatched, hs_companies, normaliser=_norm)
        hs_flagged = {r.team_name: hs_ids[r.team_name]
                      for r in unmatched if r.team_name in hs_ids}
    if hs_flagged:
        print(f"\n⚠️  {len(hs_flagged)} club(s) exist in HubSpot but matched NO Salesforce "
              f"account — NOT creating (sync HubSpot→SF first, then re-run):")
        for t, hid in hs_flagged.items():
            print(f"   • {t}  (HubSpot company {hid})")

    importable = [r for r in records if r.team_name not in hs_flagged]
    shipping_map = resolve_shipping_products(args.org, currency) if master_shipping else {}
    if master_shipping:
        print(f"Master shipping: ON — resolved {len(shipping_map)} (camera×region) shipping products")

    plan = build_plan(importable, structure=structure, currency=currency, master=master,
                      pricing=pricing, pricebook=pricebook, team_gender=gender, league=league,
                      voucher=voucher, record_type_id=rt_id, opp_owner_id=opp_owner_id,
                      master_shipping=master_shipping, shipping_map=shipping_map,
                      match_ids=match_ids, opp_stage=opp_stage, opp_forecast=opp_forecast,
                      sub_billing_period=sub_billing_period)

    def _confirm(cs) -> bool:
        ans = input("\nPush these changes to Salesforce? type 'yes' to proceed: ")
        return ans.strip().lower() == "yes"

    def _confirm_reparent(email, other_acct, target_acct) -> bool:
        ans = input(f"  Contact {email} is on account {other_acct}, not {target_acct}. "
                    "Move it? [y/N]: ")
        return ans.strip().lower() in ("y", "yes")

    imp = Importer(target_org=args.org, dry_run=not args.live)
    result = imp.execute(plan, confirm=_confirm, confirm_reparent=_confirm_reparent)
    print("\nResult:", result)


def cmd_picklist_deps(args) -> None:
    data = PD.refresh(org=args.org, out_path=args.out, sobject=args.object,
                      controller=args.controller, dependent=args.dependent)
    deps = data["dependencies"]
    print(f"Wrote {args.out}: {data['object']}.{data['controlling_field']} -> "
          f"{data['dependent_field']} "
          f"({len(deps)} controlling values, {len(data['dependent_values'])} dependent).")
    for c, vals in deps.items():
        print(f"  {c}: {len(vals)}")


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="league_dataload.v2", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("gen", help="Phase A: generate the order sheet (.xlsx)")
    g.add_argument("--out", required=True)
    g.add_argument("--pricebook", default=DEFAULT_PRICEBOOK)
    g.add_argument("--league", default="")
    g.add_argument("--rows", type=int, default=60)
    g.set_defaults(func=cmd_gen)

    i = sub.add_parser("import", help="Phase B: read a filled sheet and import")
    i.add_argument("--sheet", required=True)
    i.add_argument("--pricebook", default=DEFAULT_PRICEBOOK)
    i.add_argument("--org", default=DEFAULT_ORG)
    i.add_argument("--hubspot-csv", default="data/hubspot_companies.csv",
                   help="HubSpot companies export; clubs found here but not in SF "
                        "are flagged (exist in HubSpot, pending sync) and not created")
    i.add_argument("--live", action="store_true",
                   help="actually write to SF (default is dry-run)")
    i.set_defaults(func=cmd_import)

    d = sub.add_parser("picklist-deps",
                       help="Refresh the dependent-picklist map (default: Sport -> "
                            "Position of Field) to data/sport_position_deps.json")
    d.add_argument("--out", default=PD.DEFAULT_PATH)
    d.add_argument("--org", default=DEFAULT_ORG)
    d.add_argument("--object", default=PD.DEFAULT_OBJECT)
    d.add_argument("--controller", default=PD.DEFAULT_CONTROLLER)
    d.add_argument("--dependent", default=PD.DEFAULT_DEPENDENT)
    d.set_defaults(func=cmd_picklist_deps)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
