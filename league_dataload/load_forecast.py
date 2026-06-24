"""Parse the GTM League DB CSV into ForecastRow dataclasses.

The CSV has a 3-line preamble (title + description + blank), then row 4 is the
real header. Empty cells in the header come through as numeric column indices —
we sniff the header by looking for 'Rep Name' anywhere in the first 10 rows.
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

from .schema import ForecastRow


# Map header text → ForecastRow field name. Whitespace and case are normalized
# before lookup so minor variation in the source spreadsheet doesn't break us.
HEADER_MAP = {
    "rep name": "rep_name",
    "territory": "territory",
    "submitted date": "submitted_date",
    "period": "period",
    "priority rank": "priority_rank",
    "league": "league",
    "sport": "sport",
    "tier": "tier",
    "gtm motion": "gtm_motion",
    "product": "product",
    "deal type": "deal_type",
    "arr (€)": "arr_eur",
    "arr": "arr_eur",
    "target close": "target_close",
    "decision maker / title": "decision_maker",
    "entry point / goal": "entry_point",
    "competition / risk": "competition_risk",
    "what marketing can do": "marketing_ask",
    "why / strategic rationale": "strategic_rationale",
    "new product to add": "new_product_to_add",
    "clubs/teams to activate": "clubs_to_activate",
    "prerequisites": "prerequisites",
    "notes": "notes",
    "scope": "scope",
    "sf league / account id": "sf_league_account_id",
    "competitor + renewal year": "competitor_renewal_year",
    "product / feature ask": "product_feature_ask",
    # Master/Mother Opportunity this deal rolls up to (child opps reference it)
    "master opportunity": "master_opportunity",
    "master opportunity id": "master_opportunity",
    "master opp": "master_opportunity",
    "master opp id": "master_opportunity",
    "mother opportunity": "master_opportunity",
    "mother deal": "master_opportunity",
    "mother deal id": "master_opportunity",
}

REQUIRED = {"rep_name", "league", "sport", "product", "deal_type", "arr_eur"}


def _normalize_header(h: str) -> str:
    return re.sub(r"\s+", " ", (h or "").strip().lower())


def _find_header_row(rows: list[list[str]]) -> int:
    """Find the row index containing 'Rep Name'. Returns 0-based index."""
    for i, row in enumerate(rows[:15]):
        normalized = [_normalize_header(c) for c in row]
        if "rep name" in normalized:
            return i
    raise ValueError(
        "Couldn't find the header row containing 'Rep Name' in the first 15 lines."
    )


def _parse_arr(value: str) -> float:
    """Parse an ARR cell value like '12,500', '€12500', '€ 12,500.00', '12.500,00'."""
    if not value:
        return 0.0
    s = str(value).strip()
    # Strip currency symbols and surrounding whitespace
    s = re.sub(r"[€$£\s]", "", s)
    if not s:
        return 0.0
    # Heuristic: if there's both ',' and '.', the rightmost is the decimal separator
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            # European: 12.500,00 → 12500.00
            s = s.replace(".", "").replace(",", ".")
        else:
            # US: 12,500.00 → 12500.00
            s = s.replace(",", "")
    elif "," in s:
        # Comma-only: could be 12,500 (US thousands) or 12,5 (Euro decimal).
        # If exactly 1 comma and ≤2 digits after, treat as decimal.
        idx = s.rfind(",")
        digits_after = len(s) - idx - 1
        if digits_after <= 2 and s.count(",") == 1:
            s = s.replace(",", ".")
        else:
            s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return 0.0


def load_forecast(path: Path) -> list[ForecastRow]:
    """Read the GTM League DB CSV → list of ForecastRow."""
    if not path.exists():
        raise FileNotFoundError(f"Forecast CSV not found: {path}")

    with path.open(newline="") as f:
        all_rows = list(csv.reader(f))

    if not all_rows:
        return []

    header_idx = _find_header_row(all_rows)
    header = all_rows[header_idx]

    # Build column index → field name mapping
    col_to_field: dict[int, str] = {}
    for i, h in enumerate(header):
        key = _normalize_header(h)
        if key in HEADER_MAP:
            col_to_field[i] = HEADER_MAP[key]

    out: list[ForecastRow] = []
    for csv_row_idx, raw in enumerate(all_rows[header_idx + 1:], start=header_idx + 2):
        if not any((c or "").strip() for c in raw):
            continue  # skip blank lines
        # Pull fields by column index
        fields: dict[str, object] = {}
        for ci, fname in col_to_field.items():
            if ci < len(raw):
                fields[fname] = raw[ci]

        rep = (fields.get("rep_name") or "").strip()
        league = (fields.get("league") or "").strip()
        if not rep and not league:
            continue  # entirely empty row in the middle of the file

        if not rep or not league:
            # Partial row — skip but the caller can warn if needed
            continue

        arr = _parse_arr(str(fields.get("arr_eur") or ""))

        # Coerce strings; ARR is the only numeric field
        kwargs: dict[str, object] = {
            "source_row_number": csv_row_idx,
            "rep_name": rep,
            "league": league,
            "sport": str(fields.get("sport") or "").strip(),
            "product": str(fields.get("product") or "").strip(),
            "deal_type": str(fields.get("deal_type") or "").strip(),
            "arr_eur": arr,
        }
        # Optional string fields
        for opt in (
            "territory", "submitted_date", "period", "priority_rank", "tier",
            "gtm_motion", "target_close", "decision_maker", "entry_point",
            "competition_risk", "marketing_ask", "strategic_rationale",
            "new_product_to_add", "clubs_to_activate", "prerequisites",
            "notes", "scope", "sf_league_account_id", "competitor_renewal_year",
            "product_feature_ask", "master_opportunity",
        ):
            v = fields.get(opt)
            if v is not None:
                kwargs[opt] = str(v).strip()

        out.append(ForecastRow(**kwargs))  # type: ignore[arg-type]
    return out


def warn_on_missing_required(rows: list[ForecastRow]) -> list[str]:
    """Return warning strings for rows where Product, Sport, Deal Type, or ARR is blank."""
    warnings: list[str] = []
    for r in rows:
        missing = []
        if not r.product:
            missing.append("Product")
        if not r.sport:
            missing.append("Sport")
        if not r.deal_type:
            missing.append("Deal Type")
        if r.arr_eur <= 0:
            missing.append("ARR")
        if missing:
            warnings.append(
                f"Row {r.source_row_number} ({r.rep_name} / {r.league!r}): "
                f"missing {', '.join(missing)}"
            )
    return warnings
