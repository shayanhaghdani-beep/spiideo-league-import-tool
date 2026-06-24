"""Resolve rep-typed product tokens → canonical pricebook product names.

The forecast 'Product' cell is free-text, comma-or-ampersand-separated, with
abbreviations and feature names mixed in. e.g.:

    'Broadcasting, multi-angle, LE, LITE Team, Betting feed, AI Highlights, Replay'

This module:
  1. Tokenizes that cell into individual product tokens.
  2. Resolves each token to a pricebook product, preferring:
       - exact match (case-insensitive)
       - known abbreviation/alias (LE → League Exchange, AD → AutoData …)
       - product family + tier inference (Perform LITE TEAM → 'Spiideo Perform LITE TEAM')
       - product family alone → highest-tier in that family (excluding bulk variants)
  3. Returns unresolved tokens as warnings so the human can review.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .load_pricebook import PricebookIndex
from .schema import Product


# ---------------------------------------------------------------------------
# Tokenization

# Drop these prefix chars / phrases when parsing a Product cell.
_PRODUCT_CELL_PREFIX_RE = re.compile(r"^\s*\+\s*")
_SPLIT_RE = re.compile(r"\s*[,/&]\s*|\s+&\s+|\s+and\s+", re.IGNORECASE)


_UNCERTAINTY_PREFIX_RE = re.compile(
    r"^\s*(?:could be|maybe|potentially|probably|likely|possibly)\s+",
    re.IGNORECASE,
)
_PUNCT_TRAIL_RE = re.compile(r"[.;:,\s]+$")


def tokenize_product_cell(cell: str) -> list[str]:
    """Split a rep-typed Product cell into individual product tokens.

    Also normalizes:
      - 'multi - angle' (spaces around dash) → 'multi-angle'
      - newlines → spaces
      - 'could be/maybe/potentially' prefixes stripped
      - trailing periods / semicolons / colons stripped
    """
    s = (cell or "").strip()
    if not s:
        return []
    # Normalize multi-line cells and weird spacing around dashes
    s = re.sub(r"[\n\r]+", " ", s)
    s = re.sub(r"\s+-\s+", "-", s)
    s = _PRODUCT_CELL_PREFIX_RE.sub("", s)
    raw_tokens = _SPLIT_RE.split(s)
    out: list[str] = []
    for t in raw_tokens:
        t = t.strip()
        if not t:
            continue
        # Strip 'foo bar:' prefix (e.g. 'full scope: S.Perform' → 'S.Perform')
        if ":" in t:
            t = t.split(":", 1)[1].strip()
        # Strip parenthetical suffix ('Automatic capture (3 cameras set up)' → 'Automatic capture')
        t = re.sub(r"\s*\([^)]*\)\s*$", "", t).strip()
        t = _UNCERTAINTY_PREFIX_RE.sub("", t).strip()
        t = _PUNCT_TRAIL_RE.sub("", t).strip()
        if t:
            out.append(t)
    return out


# ---------------------------------------------------------------------------
# Known aliases — rep shorthand → canonical pricebook substring(s) to look for.
# Lowercase keys; the resolver does case-insensitive comparison.

ALIASES: dict[str, str] = {
    # League Exchange
    "le": "League Exchange",
    "league exchange": "League Exchange",
    # Activity Data / AutoData
    "ad": "AutoData",
    "activity data": "AutoData",
    "auto data": "AutoData",
    "autodata": "AutoData",
    # Replay-only variants the resolver should expand to Replay
    "replay": "Spiideo Replay",
    # Play
    "play": "Spiideo Play",
    # Perform
    "perform": "Spiideo Perform",
    # LITE shorthand alone is ambiguous — we resolve from rep context elsewhere;
    # this alias targets the Perform line as the most common interpretation.
    "lite": "Spiideo Perform LITE",
    "lite team": "Spiideo Perform LITE TEAM",
    "lite team no rec": "Spiideo Perform LITE TEAM NO RECORDING",
    "pro": "Spiideo Perform PRO",
    "pro plus": "Spiideo Perform PRO PLUS",
    "elite": "Spiideo Perform ELITE",
    "basic team": "Spiideo Perform BASIC TEAM",
    "spiideo perform": "Spiideo Perform",
    "spiideo replay": "Spiideo Replay",
    "spiideo play": "Spiideo Play",
}


# Variants we filter out when picking 'highest tier' — these are bulk packages,
# referee-only, etc., not the standard tier ladder.
_TIER_NOISE_SUFFIX_RE = re.compile(
    r"(\b\d+\s+teams?\b|\breferees?\b|\bmedia\b|\bcustom\b|\bno\s+recording\b"
    r"|\bmatch( and training)?$|\bbig10\b)",
    re.IGNORECASE,
)

# Feature tokens → product family. The rep wrote a FEATURE name instead of a
# product line, but features are bundled into specific tiers. Mapping each
# feature to the canonical product means the OLI emitter never drops a deal
# just because the rep used informal language.

FEATURE_TO_PRODUCT: dict[str, str] = {
    # Video / production / broadcasting features → Spiideo Play
    "broadcasting": "Spiideo Play",
    "multi-angle": "Spiideo Play",
    "multiangle": "Spiideo Play",
    "production": "Spiideo Play",
    "low latency": "Spiideo Play",
    "betting feed": "Spiideo Play",
    "ai highlights": "Spiideo Play",
    # Tactical / analytics features → Spiideo Perform
    "tactical feed": "Spiideo Perform",
    "tactical view": "Spiideo Perform",
    "bench": "Spiideo Perform",
    "automatic capture": "Spiideo Perform",
    "spiideo 360-solution": "Spiideo Perform",
    "spiideo 360": "Spiideo Perform",
    "spiideo perform referees": "Spiideo Perform",
    # Specific product/variant shorthands
    "perfnorec": "Spiideo Perform LITE TEAM NO RECORDING",
    "perfnorec.": "Spiideo Perform LITE TEAM NO RECORDING",
    "s.perform": "Spiideo Perform",
    "s. perform": "Spiideo Perform",
    "s.play": "Spiideo Play",
    "s. play": "Spiideo Play",
    # League management feature → League Exchange
    "competition manager": "League Exchange",
    "competitionmanager": "League Exchange",
    # Activity Data variants — sport-resolved later in resolve_token()
    "lw activitydata": "AutoData",
    "lw activity data": "AutoData",
}


# Sport label (from forecast) → AutoData variant suffix
AUTODATA_SPORT_MAP: dict[str, str] = {
    "soccer": "Soccer",
    "football": "Soccer",
    "football/soccer": "Soccer",
    "ice hockey": "Ice Hockey",
    "basketball": "Basketball",
    "handball": "Handball",
    "field hockey": "Field Hockey",
    "fieldhockey": "Field Hockey",
}


@dataclass(frozen=True)
class ResolutionResult:
    """Outcome of resolving one product token."""
    raw_token: str
    product: Product | None
    note: str   # "exact" | "alias" | "feature-mapped" | "family-highest-tier" | "unresolved"


# ---------------------------------------------------------------------------
# Resolution


def resolve_token(token: str, pricebook: PricebookIndex, sport: str = "") -> ResolutionResult:
    """Resolve a single rep-typed token → ResolutionResult.

    ``sport`` is the league's sport (e.g. 'Football', 'Ice Hockey', 'Basketball').
    Used to pick the right AutoData variant when the token resolves to AutoData.
    """
    raw = token.strip()
    if not raw:
        return ResolutionResult(raw, None, "empty")

    lower = raw.lower()

    # 1. Exact pricebook match (case-insensitive)
    for name in pricebook._by_name.keys():  # type: ignore[attr-defined]
        if name.lower() == lower:
            return ResolutionResult(raw, pricebook.require(name), "exact")

    # 2. Feature token → product family
    feature_target = FEATURE_TO_PRODUCT.get(lower)
    if feature_target:
        product = _resolve_family(pricebook, feature_target, sport)
        if product is not None:
            return ResolutionResult(raw, product, "feature-mapped")

    # 3. Alias lookup (exact key, then prefix match)
    alias_target = ALIASES.get(lower)
    if alias_target is None:
        for alias_key, target in ALIASES.items():
            if lower == alias_key or lower.startswith(alias_key + " "):
                alias_target = target
                break

    if alias_target:
        product = _resolve_family(pricebook, alias_target, sport)
        if product is not None:
            note = "alias" if alias_target.lower() in pricebook._by_name and \
                pricebook._by_name[alias_target.lower()].product_name.lower() == alias_target.lower() \
                else "family-highest-tier"  # type: ignore[attr-defined]
            return ResolutionResult(raw, product, note)

    # 4. Direct family-prefix scan against the pricebook
    for prefix in ("Spiideo Perform", "Spiideo Replay", "Spiideo Play",
                   "AutoData", "League Exchange"):
        if lower.startswith(prefix.lower()):
            product = _resolve_family(pricebook, prefix, sport)
            if product is not None:
                return ResolutionResult(raw, product, "family-highest-tier")

    return ResolutionResult(raw, None, "unresolved")


def _resolve_family(pricebook: PricebookIndex, family_target: str, sport: str) -> Product | None:
    """Map a family target name (e.g. 'Spiideo Perform', 'AutoData', or an
    exact product like 'Spiideo Perform LITE TEAM NO RECORDING') to a Product.

    For AutoData: respects the league's Sport context, falling back to Soccer.
    For exact name targets: returns that exact entry if present.
    For family prefixes: returns the highest-tier in that family.
    """
    # Exact name target wins
    target_lower = family_target.lower()
    for name in pricebook._by_name.keys():  # type: ignore[attr-defined]
        if name.lower() == target_lower:
            return pricebook.require(name)

    # AutoData special case
    if family_target == "AutoData":
        return _autodata_for_sport(pricebook, sport)

    # Generic family-prefix → highest tier
    return _highest_tier_in_family(pricebook, family_target)


def _autodata_for_sport(pricebook: PricebookIndex, sport: str) -> Product | None:
    """Pick the 'AutoData <Sport> Credits for league wide usage' variant.

    Handles 'Basketball / Spain' → 'Basketball' (strips country suffix after
    slash, dash, or pipe).
    """
    sport_key = re.sub(r"\s*[/|\-]\s*.*$", "", (sport or "").strip()).lower()
    sport_name = AUTODATA_SPORT_MAP.get(sport_key, "Soccer")   # default fall-back
    # Prefer 'league wide' since these are league-level deals
    preferred_substr = f"AutoData {sport_name} Credits for league wide usage"
    for product in pricebook._by_name.values():    # type: ignore[attr-defined]
        if product.product_name.lower() == preferred_substr.lower():
            return product
    # Case variations: SF data has 'AutoData Basketball credits' (lowercase 'c')
    for product in pricebook._by_name.values():    # type: ignore[attr-defined]
        if (sport_name.lower() in product.product_name.lower()
                and "league wide" in product.product_name.lower()
                and product.product_name.lower().startswith("autodata")):
            return product
    # Last resort: any AutoData for this sport
    for product in pricebook._by_name.values():    # type: ignore[attr-defined]
        if (sport_name.lower() in product.product_name.lower()
                and product.product_name.lower().startswith("autodata")):
            return product
    return None


def _highest_tier_in_family(pricebook: PricebookIndex, family_prefix: str) -> Product | None:
    """Pick the highest-list-price product whose name starts with `family_prefix`,
    excluding bulk/specialized variants (X TEAMS, REFEREES, MEDIA, Custom, etc.).
    """
    candidates: list[Product] = []
    for product in pricebook._by_name.values():  # type: ignore[attr-defined]
        if not product.product_name.lower().startswith(family_prefix.lower()):
            continue
        # The 'remaining' part of the name (after the family prefix) tells us
        # if this is a noise variant
        remaining = product.product_name[len(family_prefix):].strip()
        if _TIER_NOISE_SUFFIX_RE.search(remaining):
            continue
        candidates.append(product)
    if not candidates:
        # If the strict filter eliminated everything, fall back to any product
        # starting with the prefix (better than nothing)
        candidates = [
            p for p in pricebook._by_name.values()    # type: ignore[attr-defined]
            if p.product_name.lower().startswith(family_prefix.lower())
        ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.list_price)


# ---------------------------------------------------------------------------
# Top-level: resolve a Product cell into a list of (Product, note)


@dataclass(frozen=True)
class CellResolution:
    """All resolutions for one rep-typed Product cell."""
    products: list[Product]            # successfully resolved products (deduped by name)
    notes: list[str]                   # one line per token: '"raw_token" → product_name (note)'
    unresolved: list[str]              # tokens that couldn't be mapped
    skipped_features: list[str]        # feature-only tokens (broadcasting, multi-angle, …)


def resolve_product_cell(cell: str, pricebook: PricebookIndex,
                         sport: str = "") -> CellResolution:
    tokens = tokenize_product_cell(cell)
    products: list[Product] = []
    seen_names: set[str] = set()
    notes: list[str] = []
    unresolved: list[str] = []
    skipped: list[str] = []

    for tok in tokens:
        res = resolve_token(tok, pricebook, sport=sport)
        if res.product is None:
            if res.note == "unresolved":
                unresolved.append(tok)
            continue
        if res.product.product_name in seen_names:
            notes.append(f"{tok!r} → (dup, skipped)")
            continue
        seen_names.add(res.product.product_name)
        products.append(res.product)
        notes.append(f"{tok!r} → {res.product.product_name} ({res.note})")

    return CellResolution(
        products=products,
        notes=notes,
        unresolved=unresolved,
        skipped_features=skipped,
    )


# ---------------------------------------------------------------------------
# ARR splitting


def split_arr_proportional(products: list[Product], total_arr: float) -> list[float]:
    """Split `total_arr` across `products` in proportion to their list prices.

    Edge cases:
      - If only one product, it gets the full ARR.
      - If all products have list_price = 0, ARR is split equally.
      - If total list price > 0, products with list_price = 0 get Sales Price 0
        (they're included as free bundled items) and the rest split proportionally.
    """
    if not products:
        return []
    if len(products) == 1:
        return [total_arr]
    total_list = sum(p.list_price for p in products)
    if total_list == 0:
        equal = total_arr / len(products)
        return [equal] * len(products)
    return [(p.list_price / total_list) * total_arr for p in products]
