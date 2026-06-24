"""Emit opp_product.csv (OpportunityLineItem insert payload) for league deals.

Column order is byte-identical to the template `(OPS) Opp Product Import Sheet`.
Ported from the engine's league OLI emitters:

  - emit_opp_product_per_league (default): one OLI row per RESOLVED product per
    league. Tokenizes the rep's free-text Product cell, resolves to canonical
    pricebook entries, and splits the league's total ARR across them in
    proportion to list prices. This is where league ARR lands (Sales Price).
  - emit_opp_product_per_row: one OLI row per forecast row (assumes one
    resolvable product per row), used with --per-row.
"""
from __future__ import annotations

import csv
from pathlib import Path

from .classify_product import classify
from .load_pricebook import PricebookIndex
from .product_resolver import resolve_product_cell, split_arr_proportional
from .schema import LeagueAccount


OPP_PRODUCT_COLUMNS = [
    "Account ID",
    "Opportunity ID",
    "Opportunity Name",
    "Number of Product Lines",
    "Products",
    "Product ID",
    "Price Book Entry ID",
    "Sales Price",
    "List",
    "Opp Currency",
    "Quantity",
    "Unit of Measure",
    "Camera order type",
    "Charge type",
    "Price Period",
    "Billing Period",
    "Younium Charge Name",
    "Voucher",
    "Shipping status",
    "Position of Field",
    "Camera Scene",
    "Height",
    "Distance from sideline (m)",
    "ID unique OLI product",
]


def _fmt(p: float) -> str:
    if p == int(p):
        return f"{int(p)}"
    return f"{p:.2f}"


def emit_opp_product_per_league(
    leagues: list[LeagueAccount],
    pricebook: PricebookIndex,
    out_path: Path,
    warnings_out: list[str] | None = None,
    opp_currency: str = "EUR",
) -> int:
    """One OLI row per RESOLVED product per league; ARR split by list price."""
    if warnings_out is None:
        warnings_out = []
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with out_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=OPP_PRODUCT_COLUMNS)
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
            period = primary_fr.period or "H2 2026"
            league_clean = " ".join(la.display_name.split())
            opp_name = f"{league_clean} - {period}".strip(" -")

            all_product_cells = [fr.product for fr in la.forecast_rows if fr.product]
            combined_cell = ", ".join(all_product_cells)
            resolution = resolve_product_cell(combined_cell, pricebook,
                                              sport=la.primary_sport)

            if not resolution.products:
                from .product_resolver import _highest_tier_in_family
                fallback = _highest_tier_in_family(pricebook, "Spiideo Perform")
                if fallback is None or la.total_arr_eur <= 0:
                    warnings_out.append(
                        f"League {la.display_name!r}: no resolvable products in "
                        f"{combined_cell!r}; emitting 0 OLI rows"
                    )
                    continue
                warnings_out.append(
                    f"League {la.display_name!r}: no resolvable products in "
                    f"{combined_cell!r}; defaulted to {fallback.product_name!r}"
                )
                resolution = type(resolution)(
                    products=[fallback],
                    notes=[f"(fallback) {fallback.product_name}"],
                    unresolved=resolution.unresolved,
                    skipped_features=resolution.skipped_features,
                )

            if resolution.unresolved:
                warnings_out.append(
                    f"League {la.display_name!r}: unresolved tokens "
                    f"{resolution.unresolved} (skipped)"
                )
            if resolution.skipped_features:
                warnings_out.append(
                    f"League {la.display_name!r}: feature-only tokens skipped: "
                    f"{resolution.skipped_features}"
                )

            sales_prices = split_arr_proportional(resolution.products, la.total_arr_eur)

            for idx, (product, sales_price) in enumerate(
                zip(resolution.products, sales_prices), start=1
            ):
                w.writerow({
                    "Account ID": account_id,
                    "Opportunity ID": "",
                    "Opportunity Name": opp_name,
                    "Number of Product Lines": len(resolution.products),
                    "Products": product.product_name,
                    "Product ID": product.product_id,
                    "Price Book Entry ID": product.pricebook_entry_id,
                    "Sales Price": _fmt(sales_price),
                    "List": _fmt(product.list_price),
                    "Opp Currency": opp_currency,
                    "Quantity": 1,
                    "Unit of Measure": "account" if product.family == "subscription" else "camera system/s",
                    "Camera order type": "",
                    "Charge type": "Recurring" if product.family == "subscription" else "One-off",
                    "Price Period": "Annual",
                    "Billing Period": "Annual" if product.family == "subscription" else "",
                    "Younium Charge Name": product.product_name,
                    "Voucher": "",
                    "Shipping status": "",
                    "Position of Field": "",
                    "Camera Scene": "",
                    "Height": "",
                    "Distance from sideline (m)": "",
                    "ID unique OLI product": idx,
                })
                n += 1
    return n


def emit_opp_product_per_row(
    leagues: list[LeagueAccount],
    pricebook: PricebookIndex,
    out_path: Path,
    warnings_out: list[str] | None = None,
    opp_currency: str = "EUR",
) -> int:
    """One OLI row per forecast row (assumes one resolvable product per row).

    Falls back to the per-cell resolver when the rep's Product cell isn't an
    exact pricebook name, so free-text rows still resolve."""
    if warnings_out is None:
        warnings_out = []
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with out_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=OPP_PRODUCT_COLUMNS)
        w.writeheader()
        for la in leagues:
            account_id = (
                la.matched_sf_ids[0]
                if la.match_status == "matched" and la.matched_sf_ids
                else ""
            )
            for fr in la.forecast_rows:
                product = pricebook.get(fr.product)
                if product is None:
                    res = resolve_product_cell(fr.product, pricebook, sport=fr.sport)
                    if not res.products:
                        warnings_out.append(
                            f"League {la.display_name!r} row: unresolved product "
                            f"{fr.product!r}; skipped"
                        )
                        continue
                    product = res.products[0]
                suffix = f" {fr.period}" if fr.period else ""
                opp_name = f"{la.display_name} {fr.product}{suffix}".strip()
                w.writerow({
                    "Account ID": account_id,
                    "Opportunity ID": "",
                    "Opportunity Name": opp_name,
                    "Number of Product Lines": 1,
                    "Products": product.product_name,
                    "Product ID": product.product_id,
                    "Price Book Entry ID": product.pricebook_entry_id,
                    "Sales Price": _fmt(fr.arr_eur),
                    "List": _fmt(product.list_price),
                    "Opp Currency": opp_currency,
                    "Quantity": 1,
                    "Unit of Measure": "account" if product.family == "subscription" else "camera system/s",
                    "Camera order type": "",
                    "Charge type": "Recurring" if product.family == "subscription" else "One-off",
                    "Price Period": "Annual",
                    "Billing Period": "Annual" if product.family == "subscription" else "",
                    "Younium Charge Name": product.product_name,
                    "Voucher": "",
                    "Shipping status": "",
                    "Position of Field": "",
                    "Camera Scene": "",
                    "Height": "",
                    "Distance from sideline (m)": "",
                    "ID unique OLI product": 1,
                })
                n += 1
    return n
