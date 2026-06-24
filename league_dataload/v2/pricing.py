"""Currency-aware product lookup + the per-product-type pricing gate.

The MCS dropdowns emit EXACT Salesforce product names, so an exact-match lookup
filtered to the run currency is correct (and avoids the name-only index colliding
across currencies). Pricing gate per product type: free / discounted / list.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

PRICING_FREE = "free"
PRICING_DISCOUNT = "discount"
PRICING_LIST = "list"
PRICING_MODES = (PRICING_FREE, PRICING_DISCOUNT, PRICING_LIST)


@dataclass(frozen=True)
class PriceEntry:
    product_name: str
    product2_id: str
    pricebook_entry_id: str
    list_price: float
    currency: str


class CurrencyPricebook:
    """Exact-name product lookup for a single currency."""

    def __init__(self, entries: list[PriceEntry], currency: str):
        self.currency = currency
        self._by_name = {e.product_name: e for e in entries}
        # Case-insensitive fallback: manually-filled sheets drift in case from the
        # SF product name (e.g. "Spiideo Stream Encoder" vs "Spiideo stream encoder").
        self._by_name_ci = {e.product_name.strip().lower(): e for e in entries}

    @classmethod
    def load(cls, path: str | Path, currency: str) -> "CurrencyPricebook":
        entries: list[PriceEntry] = []
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if (row.get("List Price Currency") or "").strip() != currency:
                    continue
                try:
                    lp = float((row.get("List Price") or "0").strip() or 0)
                except ValueError:
                    lp = 0.0
                entries.append(PriceEntry(
                    product_name=(row.get("Product: Product Name") or "").strip(),
                    product2_id=(row.get("Product ID") or "").strip(),
                    pricebook_entry_id=(row.get("Price Book Entry ID") or "").strip(),
                    list_price=lp,
                    currency=currency,
                ))
        if not entries:
            raise ValueError(f"No pricebook entries for currency {currency!r}")
        return cls(entries, currency)

    def get(self, product_name: str) -> PriceEntry | None:
        key = product_name.strip()
        return self._by_name.get(key) or self._by_name_ci.get(key.lower())


@dataclass(frozen=True)
class PricingChoice:
    """One product type's pricing decision from the Step-1 gate."""
    mode: str                                  # free | discount | list
    # discount prices keyed by exact product name (per distinct model)
    discount_prices: dict[str, float] = None   # type: ignore[assignment]

    def unit_price(self, entry: PriceEntry) -> float:
        if self.mode == PRICING_FREE:
            return 0.0
        if self.mode == PRICING_LIST:
            return entry.list_price
        if self.mode == PRICING_DISCOUNT:
            dp = (self.discount_prices or {}).get(entry.product_name)
            if dp is None:
                raise ValueError(
                    f"No discount price provided for {entry.product_name!r}")
            return float(dp)
        raise ValueError(f"Unknown pricing mode {self.mode!r}")


@dataclass(frozen=True)
class PricingConfig:
    currency: str
    subscription: PricingChoice
    cameras: PricingChoice
