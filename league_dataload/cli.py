"""league-dataload — turn a big-league-deals sheet into Salesforce DataLoader CSVs.

Zero third-party dependencies (stdlib only). Run as a module:

    python3 -m league_dataload build --input inputs/sample_deals.csv --out outputs/run/
    python3 -m league_dataload crosscheck --input inputs/sample_deals.csv --out outputs/cc/

Lookup source for the crosscheck + rep resolution:
    --source csv   (default) read existing SF Accounts/Users from data/ CSV exports
    --source sf    query the org live via the `sf` CLI (default org alias 'spiideo')
    --source none  skip crosscheck + rep resolution (everything emits as net-new)

The curated HubSpot league pool (data/hubspot_leagues.csv) is always used as the
primary candidate source regardless of --source.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import config
from .candidates import gather_candidates
from .emit import (
    emit_account,
    emit_contact,
    emit_crosscheck_report,
    emit_opportunity_per_league,
    emit_opportunity_per_row,
)
from .emit_opp_product import (
    emit_opp_product_per_league,
    emit_opp_product_per_row,
)
from .load_pricebook import load_pricebook
from .loaders import load_deal_aliases, load_manual_account_ids
from .load_forecast import load_forecast, warn_on_missing_required
from .matcher import (
    crosscheck_leagues,
    dedupe_leagues,
    dedupe_leagues_by_period,
    propagate_crosscheck,
)
from .resolve_reps import resolve_reps
from .sources import (
    CsvLookupSource,
    SfCliError,
    SfCliLookupSource,
    StubLookupSource,
)


def _echo(msg: str = "", err: bool = False) -> None:
    print(msg, file=sys.stderr if err else sys.stdout)


def _make_source(kind: str, target_org: str):
    if kind == "none":
        _echo("Source: none — skipping crosscheck + rep resolution.")
        return StubLookupSource()
    if kind == "sf":
        _echo(f"Source: sf CLI (org={target_org!r}).")
        return SfCliLookupSource(target_org=target_org)
    _echo(f"Source: local CSV ({config.sf_accounts_csv()}, {config.users_csv()}).")
    return CsvLookupSource(config.sf_accounts_csv(), config.users_csv())


def _load_and_filter(input_path: Path, filter_rep: str | None,
                     filter_period: str | None):
    _echo(f"Loading deals: {input_path}")
    rows = load_forecast(input_path)
    _echo(f"  Parsed {len(rows)} rows ({len({r.rep_name for r in rows})} reps)")
    for w in warn_on_missing_required(rows):
        _echo(f"  WARNING: {w}", err=True)
    if filter_rep:
        rows = [r for r in rows if r.rep_name.lower() == filter_rep.lower()]
    if filter_period:
        rows = [r for r in rows if (r.period or "").lower() == filter_period.lower()]
    return rows


def _crosscheck(rows, source, do_reps: bool):
    """Dedupe → gather candidates → crosscheck. Returns (leagues, counts)."""
    leagues = dedupe_leagues(rows)
    _echo(f"Deduped into {len(leagues)} unique leagues")

    if isinstance(source, StubLookupSource):
        candidates = []
    else:
        _echo("Gathering candidates (HubSpot ∪ Salesforce)…")
        candidates, (hs_n, sf_n, merged_n) = gather_candidates(source)
        _echo(f"  {merged_n} candidates (HubSpot: {hs_n}, SF: {sf_n})")
        if do_reps:
            _echo("Resolving rep names → SF User IDs…")
            rep_report = resolve_reps(rows, source)
            _echo(rep_report.summary())
            for w in rep_report.warnings:
                _echo(f"  WARNING: {w}", err=True)

    aliases = load_deal_aliases(config.league_deal_aliases_csv())
    manual = load_manual_account_ids(config.manual_account_ids_csv())
    crosscheck_leagues(leagues, candidates, aliases, manual)

    matched = sum(1 for la in leagues if la.match_status == "matched")
    matched_id = sum(1 for la in leagues if la.matched_sf_ids and la.matched_sf_ids[0])
    ambiguous = sum(1 for la in leagues if la.match_status == "ambiguous")
    unmatched = sum(1 for la in leagues if la.match_status == "unmatched")
    _echo(f"  Leagues: {matched} matched ({matched_id} with SF Account ID), "
          f"{ambiguous} ambiguous, {unmatched} NEW")
    return leagues


def _resolve_master_opp(args, rows) -> str:
    """Run-wide Master Opportunity ID for the child opps. Precedence:
    --master-opp flag > interactive prompt (unless --no-prompt / non-tty).
    A per-row 'Master Opportunity' column always overrides this per opp."""
    if args.master_opp is not None:
        return args.master_opp.strip()
    have_col = any(getattr(r, "master_opportunity", "") for r in rows)
    if args.no_prompt or not sys.stdin.isatty():
        if not have_col:
            _echo("  No --master-opp and no 'Master Opportunity' column — "
                  "child opps will have a blank Master Opportunity.", err=True)
        return ""
    suffix = " (or Enter to use the per-row column)" if have_col else ""
    try:
        val = input(f"Master Opportunity ID to set on these child opps{suffix}: ").strip()
    except EOFError:
        val = ""
    return val


def cmd_build(args) -> int:
    config.load_env()
    rows = _load_and_filter(Path(args.input), args.filter_rep, args.filter_period)
    if not rows:
        _echo("Nothing to do — no rows after filtering.")
        return 0

    master_opp = _resolve_master_opp(args, rows)
    if master_opp:
        _echo(f"Master Opportunity (default for child opps): {master_opp}")

    source = _make_source(args.source, args.target_org)
    try:
        leagues = _crosscheck(rows, source, do_reps=True)
    except SfCliError as e:
        _echo(f"ERROR: {e}", err=True)
        return 2

    # Opportunities/products are per (league, period); accounts/crosscheck per league.
    opp_leagues = dedupe_leagues_by_period(rows)
    propagate_crosscheck(leagues, opp_leagues)

    # Pricebook for the Opp Product line items (ARR lands on Sales Price).
    try:
        pricebook = load_pricebook(config.pricebook_csv())
    except (FileNotFoundError, ValueError) as e:
        _echo(f"ERROR loading pricebook: {e}", err=True)
        return 2
    opp_ccy = config.league_opp_currency()
    ccys = pricebook.currencies()
    if ccys and opp_ccy not in ccys:
        _echo(f"  WARNING: pricebook currency {sorted(ccys)} != opp currency {opp_ccy!r}. "
              f"Product IDs are fine, but Price Book Entry IDs are currency-specific — "
              f"refresh data/pricebook.csv with a {opp_ccy} export before a real import.", err=True)

    # Split accounts: only the UNMATCHED leagues are net-new (to CREATE);
    # matched/ambiguous already exist in SF (reference only, don't re-create).
    new_leagues = [la for la in leagues if la.match_status == "unmatched"]
    existing_leagues = [la for la in leagues if la.match_status != "unmatched"]

    if args.dry_run:
        n_opp = len(rows) if args.per_row else len(opp_leagues)
        _echo("\n--dry-run: not writing files. Summary:")
        _echo(f"  account_new.csv       would have {len(new_leagues)} rows (CREATE these)")
        _echo(f"  account_existing.csv  would have {len(existing_leagues)} rows (already in SF)")
        _echo(f"  contact.csv           header-only (forecast carries no contact data)")
        _echo(f"  opportunity.csv       would have {n_opp} rows")
        _echo(f"  opp_product.csv       would have ≥{n_opp} rows (one per resolved product)")
        _echo(f"  league_crosscheck.csv would have {len(leagues)} rows")
        return 0

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []
    n_new = emit_account(new_leagues, out_dir / "account_new.csv")
    n_existing = emit_account(existing_leagues, out_dir / "account_existing.csv")
    n_contact = emit_contact(leagues, out_dir / "contact.csv")
    if args.per_row:
        n_opp = emit_opportunity_per_row(opp_leagues, out_dir / "opportunity.csv",
                                         master_opp_default=master_opp)
        n_oli = emit_opp_product_per_row(opp_leagues, pricebook,
                                         out_dir / "opp_product.csv", warnings, opp_ccy)
    else:
        n_opp = emit_opportunity_per_league(opp_leagues, out_dir / "opportunity.csv",
                                            master_opp_default=master_opp)
        n_oli = emit_opp_product_per_league(opp_leagues, pricebook,
                                            out_dir / "opp_product.csv", warnings, opp_ccy)
    n_cc = emit_crosscheck_report(leagues, out_dir / "league_crosscheck.csv")

    _echo("\nWrote (import in this order):")
    _echo(f"  1. {out_dir/'account_new.csv'}       ({n_new} rows) ← CREATE these accounts first")
    _echo(f"     {out_dir/'account_existing.csv'}  ({n_existing} rows) ← already in SF (reference)")
    _echo(f"  2. {out_dir/'contact.csv'}           ({n_contact} rows)")
    _echo(f"  3. {out_dir/'opportunity.csv'}       ({n_opp} rows)")
    _echo(f"  4. {out_dir/'opp_product.csv'}       ({n_oli} rows) ← ARR in 'Sales Price'")
    _echo(f"  •  {out_dir/'league_crosscheck.csv'} ({n_cc} rows) ← review BEFORE importing")
    if warnings:
        _echo(f"\n{len(warnings)} product-resolution note(s):", err=True)
        for w in warnings:
            _echo(f"  {w}", err=True)
    return 0


def cmd_crosscheck(args) -> int:
    config.load_env()
    rows = _load_and_filter(Path(args.input), args.filter_rep, args.filter_period)
    if not rows:
        _echo("Nothing to do — no rows after filtering.")
        return 0
    source = _make_source(args.source, args.target_org)
    try:
        leagues = _crosscheck(rows, source, do_reps=False)
    except SfCliError as e:
        _echo(f"ERROR: {e}", err=True)
        return 2
    if args.dry_run:
        _echo(f"\n--dry-run: league_crosscheck.csv would have {len(leagues)} rows")
        return 0
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    n_cc = emit_crosscheck_report(leagues, out_dir / "league_crosscheck.csv")
    _echo(f"\nWrote: {out_dir/'league_crosscheck.csv'}  ({n_cc} rows)")
    return 0


def _add_common(p: argparse.ArgumentParser, need_out: bool = True) -> None:
    p.add_argument("--input", required=True, help="Path to the big-league-deals CSV (GTM League DB shape)")
    if need_out:
        p.add_argument("--out", required=True, help="Output directory for the CSVs")
    p.add_argument("--source", choices=["csv", "sf", "none"], default="csv",
                   help="Where to read existing SF Accounts/Users from (default: csv)")
    p.add_argument("--target-org", default=config.sf_target_org(),
                   help="sf CLI target org alias (only used with --source sf; default: spiideo)")
    p.add_argument("--filter-rep", default=None, help="Process only this rep")
    p.add_argument("--filter-period", default=None, help="Process only this period (e.g. 'H2 2026')")
    p.add_argument("--dry-run", action="store_true", help="Print a summary; don't write files")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="league-dataload",
        description="Turn a big-league-deals sheet into Salesforce DataLoader CSVs.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    b = sub.add_parser("build", help="Emit account_new/existing + contact + opportunity + opp_product + crosscheck")
    _add_common(b)
    b.add_argument("--per-row", action="store_true",
                   help="One Opp per forecast row instead of per (league, period)")
    b.add_argument("--master-opp", default=None,
                   help="Master/Mother Opportunity ID to set on every child opp "
                        "(a per-row 'Master Opportunity' column overrides it). "
                        "If omitted, you're prompted interactively.")
    b.add_argument("--no-prompt", action="store_true",
                   help="Don't prompt for a Master Opportunity ID; use per-row column values only.")
    b.set_defaults(func=cmd_build)

    c = sub.add_parser("crosscheck", help="Emit league_crosscheck.csv only")
    _add_common(c)
    c.set_defaults(func=cmd_crosscheck)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
