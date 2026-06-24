"""Build an in-memory pricebook index from the local pricebook.csv.

Ported from the engine (the LookupSource/Sheet variant is dropped — this app
reads the pricebook from a local CSV reference file in data/). The pricebook
supplies Product ID / Price Book Entry ID / List price / Family for the Opp
Product (OLI) emitter.
"""
from __future__ import annotations

import csv
from pathlib import Path

from .schema import Product


class PricebookIndex:
    """Lookup pricebook by exact product name.

    Duplicate product names (multiple PricebookEntry rows in SF) are tolerated:
    the first occurrence wins and duplicates are recorded in ``self.duplicates``.
    """

    def __init__(self, products: list[Product], strict: bool = False):
        self._by_name: dict[str, Product] = {}
        self.duplicates: list[str] = []
        for p in products:
            if p.product_name in self._by_name:
                if p.product_name not in self.duplicates:
                    self.duplicates.append(p.product_name)
                continue   # keep the first
            self._by_name[p.product_name] = p
        if strict and self.duplicates:
            raise ValueError(f"Duplicate product names in pricebook: {self.duplicates}")

    def __len__(self) -> int:
        return len(self._by_name)

    def __contains__(self, name: str) -> bool:
        return name in self._by_name

    def get(self, name: str) -> Product | None:
        return self._by_name.get(name)

    def require(self, name: str) -> Product:
        p = self._by_name.get(name)
        if p is None:
            raise KeyError(f"Product {name!r} not found in pricebook")
        return p

    def all_subscriptions(self) -> list[Product]:
        return [p for p in self._by_name.values() if p.family == "subscription"]

    def all_cameras(self) -> list[Product]:
        return [p for p in self._by_name.values() if p.family == "camera"]

    def currencies(self) -> set[str]:
        return {p.currency for p in self._by_name.values() if p.currency}


def load_pricebook(path: Path) -> PricebookIndex:
    """Read pricebook.csv -> PricebookIndex.

    Expected columns:
      Price Book Name, Product: Product Name, Product ID, Price Book Entry ID,
      List Price Currency, List Price, Family
    """
    if not path.exists():
        raise FileNotFoundError(
            f"Pricebook not found at {path}. Copy a pricebook export into data/ "
            f"(see README) or set PRICEBOOK_CSV."
        )

    products: list[Product] = []
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        required = {
            "Price Book Name", "Product: Product Name", "Product ID",
            "Price Book Entry ID", "List Price Currency", "List Price", "Family",
        }
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"pricebook.csv missing columns: {missing}")

        for row in reader:
            name = (row["Product: Product Name"] or "").strip()
            if not name:
                continue
            # The live multi-currency export carries richer Family labels
            # (e.g. "Spiideo Perform (Subscription)", "Spiideo Camera Systems
            # (Purchase)", "AutoData", "Add-ons"). Normalise leniently to the
            # two v1 buckets; anything else is kept as-is (v2 ignores Family).
            raw_family = (row["Family"] or "").strip().lower()
            if "subscription" in raw_family:
                family = "subscription"
            elif "camera" in raw_family or "purchase" in raw_family:
                family = "camera"
            else:
                family = raw_family
            try:
                price = float((row["List Price"] or "0").replace(",", "."))
            except ValueError as e:
                raise ValueError(f"Bad List Price for {name!r}: {row['List Price']!r}") from e
            products.append(Product(
                pricebook_name=(row["Price Book Name"] or "").strip(),
                product_name=name,
                product_id=(row["Product ID"] or "").strip(),
                pricebook_entry_id=(row["Price Book Entry ID"] or "").strip(),
                currency=(row["List Price Currency"] or "").strip(),
                list_price=price,
                family=family,
            ))

    return PricebookIndex(products)
